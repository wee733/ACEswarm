from copy import copy
import math
from types import SimpleNamespace as Msg

import pytest

from uxrce_basic_control.controller import ARM_DISARM, NAV_LAND, SET_MODE, SET_NAV_STATE, Controller, Request


class Port:
    def __init__(self):
        self.sent, self.points, self.errors = [], [], []
        self.command_stamps, self.hold_stamps = [], []
        self.mode_id = None

    def registered_id(self):
        return self.mode_id

    def check_offboard(self):
        pass

    def send(self, command, params, status, stamp):
        self.sent.append((command, params))
        self.command_stamps.append(stamp)
        return status.system_id, 193

    def publish_hold(self, hold, stamp):
        self.points.append((hold.north, hold.east, hold.down))
        self.hold_stamps.append(stamp)

    def report(self, reason):
        self.errors.append(reason)


class Sim:
    def __init__(self, **settings):
        self.port = Port()
        self.ctrl = Controller(self.port, .5, 10, **settings)
        self.status = Msg(arming_state=2, pre_flight_checks_pass=True, nav_state=4,
                          system_id=7, failsafe=False, can_set_nav_states_mask=1 << 25,
                          valid_nav_states_mask=1 << 25)
        self.pose = Msg(x=2., y=-3., z=.3, xy_valid=True, z_valid=True, heading=0.,
                        xy_reset_counter=1, z_reset_counter=2, heading_reset_counter=0)
        self.landed = Msg(landed=True)
        self.now = 10.
        self.boot_at = self.now - 2300.
        self.ack_seq = 0
        self.refresh()

    def refresh(self):
        self.status = copy(self.status)
        self.status.timestamp = int((self.now - self.boot_at) * 1_000_000)
        for k, v in (('vehicle_status', self.status), ('vehicle_local_position', self.pose),
                     ('vehicle_land_detected', self.landed)):
            self.ctrl.receive(k, v, self.now)

    def tick(self, count=1):
        for _ in range(count):
            self.now += .05
            self.refresh()
            self.ctrl.tick(self.now)

    def request(self, action, **payload):
        out = []
        self.ctrl.request(Request(action, payload, out.append), self.now)
        return out

    def ack(self, result=0, **overrides):
        self.ack_seq += 1
        fields = dict(command=self.port.sent[-1][0], timestamp=self.port.command_stamps[-1] + self.ack_seq,
                      target_system=7, target_component=193,
                      result=result, result_param1=0, result_param2=1)
        fields.update(overrides)
        self.ctrl.acknowledge(Msg(**fields), self.now)


def test_real_ack_only_and_dedicated_routing():
    s = Sim()
    out = s.request('disarm')
    assert s.port.sent == [(ARM_DISARM, (0, 0))]
    for override in (dict(command=176), dict(target_component=1), dict(target_system=1), dict(timestamp=99)):
        s.ack(**override)
        assert not out
    s.ack(5)
    assert not out
    s.ack(2)
    assert out[0]['result_code'] == 2 and not out[0]['ok'] and 'DENIED' in out[0]['message']


def test_timeout_does_not_look_like_ack_and_late_ack_does_not_succeed():
    s = Sim()
    out = s.request('disarm')
    s.tick(41)
    assert out[0]['result_code'] is None and not out[0]['ok']
    s.ack()
    assert not s.request('disarm')[0]['ok']
    assert len(s.port.sent) == 1


def test_arm_preflight_and_explicit_confirmation():
    s = Sim()
    for payload in ({}, {'value': True}, {'value': 1, 'confirm': True}, {'value': True, 'confirm': 'true'}):
        assert not s.request('arm', **payload)[0]['ok']
    s.status.pre_flight_checks_pass = False
    assert not s.request('arm', value=True, confirm=True)[0]['ok']
    assert not s.port.sent
    result = s.request('arm', value=False)
    s.ack()
    assert result[0]['ok'] and s.port.sent[-1] == (ARM_DISARM, (0, 0))


@pytest.mark.parametrize('mode,params', [
    ('POSCTL', (1, 3, 0)), ('ALTCTL', (1, 2, 0)), ('STABILIZED', (1, 7, 0)),
    ('AUTO.LOITER', (1, 4, 3)), ('AUTO.RTL', (1, 4, 5)), ('AUTO.LAND', (1, 4, 6))])
def test_px4_modes(mode, params):
    s = Sim()
    out = s.request('set_mode', mode=mode)
    assert not out and s.port.sent == [(SET_MODE, params)]
    s.ack()
    assert out[0]['ok'] and out[0]['command'] == 'set_mode'


def test_external_mode_registered_selectable_only():
    s = Sim()
    assert not s.request('set_mode', mode='EXTRA')[0]['ok']
    assert not s.request('set_mode', mode='EXTERNAL1')[0]['ok']
    s.port.mode_id = 25
    result = s.request('set_mode', mode='EXTRA')
    assert s.port.sent == [(SET_NAV_STATE, (25,))]
    s.ack(1)
    assert not result[0]['ok']
    result = s.request('set_mode', mode='EXTERNAL3')
    s.ack()
    assert result[0]['ok']


def test_takeoff_requires_good_position_and_live_armed_ground_status():
    s = Sim()
    for value in (-1, False, float('nan'), float('inf'), 11, '1.5'):
        assert not s.request('takeoff', target_altitude_m=value)[0]['ok']
    for obj, field, bad in ((s.status, 'arming_state', 1), (s.pose, 'xy_valid', False),
                             (s.landed, 'landed', False), (s.status, 'pre_flight_checks_pass', False)):
        old = getattr(obj, field)
        setattr(obj, field, bad)
        assert not s.request('takeoff', target_altitude_m=1.)[0]['ok']
        setattr(obj, field, old)
    assert not s.port.sent and not s.port.points


def test_takeoff_waits_for_warmup_ack_and_mode_then_climbs_without_overshoot():
    s = Sim()
    out = s.request('takeoff', target_altitude_m=1.5)
    s.tick(21)
    assert not s.port.sent
    s.tick(4)
    assert len(s.port.points) == 25 and s.port.sent == [(SET_MODE, (1, 6, 0))]
    assert set(s.port.points) == {(2., -3., .3)}
    s.ack()
    s.tick(3)
    assert not out and set(s.port.points) == {(2., -3., .3)}
    s.status.nav_state = 14
    s.tick()
    assert out[0]['ok'] and 'climb started' in out[0]['message']
    s.landed.landed = False
    s.tick(100)
    assert s.port.points[-1] == (2., -3., -1.2)
    assert all(0 <= a[2]-b[2] <= .025001 for a, b in zip(s.port.points, s.port.points[1:]))
    s.status.nav_state = 2
    s.tick()
    assert s.ctrl.hold is None


def test_cancel_during_ack_stops_warmup_and_cannot_consume_old_ack():
    s = Sim()
    out = s.request('takeoff', target_altitude_m=1.5)
    s.tick(25)
    cancel = s.request('cancel')
    assert out and not out[0]['ok'] and s.ctrl.hold is None
    assert s.port.sent[-1] == (SET_NAV_STATE, (4,))
    s.ack(command=SET_MODE)
    assert not cancel
    s.ack()
    assert cancel[0]['ok']


def test_cancel_during_climb_holds_measured_pose_until_loiter_ack():
    s = Sim()
    s.request('takeoff', target_altitude_m=1.5)
    s.tick(25)
    s.ack()
    s.status.nav_state = 14
    s.tick(10)
    s.pose.z = .2
    out = s.request('cancel')
    s.tick(3)
    assert s.port.points[-1] == (2., -3., .2)
    s.ack(1)
    assert not out[0]['ok'] and s.ctrl.hold.target_down == .2


def test_disarmed_offboard_then_arm_transition():
    s = Sim()
    s.status.arming_state = 1
    out = s.request('set_mode', mode='OFFBOARD')
    s.tick(25)
    s.ack()
    s.status.nav_state = 14
    s.tick()
    assert out[0]['ok']
    s.status.arming_state = 2
    s.tick()
    s.status.arming_state = 1
    s.tick()
    assert s.ctrl.hold is None


def test_missing_pose_and_frame_reset_stop_output():
    s = Sim()
    out = s.request('takeoff', target_altitude_m=1.5)
    s.tick(3)
    s.pose.z_reset_counter += 1
    s.tick()
    assert not out[0]['ok'] and s.ctrl.hold is None and not s.port.sent
    s.now += 4
    assert not s.request('set_mode', mode='OFFBOARD')[0]['ok']


def test_land_always_returns_px4_decision():
    s = Sim()
    s.pose.xy_valid = False
    s.status.pre_flight_checks_pass = False
    result = s.request('land')
    assert s.port.sent[0][0] == NAV_LAND
    s.ack(4)
    assert not result[0]['ok'] and result[0]['result_code'] == 4


def test_incidental_mode_field_cannot_change_ack_handling():
    s = Sim()
    for action in ('land', 'disarm', 'arm'):
        out = s.request(action, mode='OFFBOARD', value=False)
        s.ack()
        assert out[0]['ok'] and s.ctrl.pending is None and s.ctrl.hold is None


def test_active_hold_survives_rejected_offboard_and_takeoff_requests():
    s = Sim()
    s.request('set_mode', mode='OFFBOARD')
    s.tick(25)
    s.ack()
    s.status.nav_state = 14
    s.tick()
    hold = s.ctrl.hold
    for action, payload in [('set_mode', {'mode': 'OFFBOARD'}), ('takeoff', {'target_altitude_m': 1.5})]:
        out = s.request(action, **payload)
        s.ack(1)
        assert not out[0]['ok'] and s.ctrl.hold is hold and hold.active
        assert hold.target_down == hold.down


def test_cancel_between_mode_ack_and_status_holds_during_handover():
    s = Sim()
    s.request('takeoff', target_altitude_m=1.5)
    s.tick(24)
    s.ack()
    cancel = s.request('cancel')
    s.tick(2)
    assert s.ctrl.hold.active and s.ctrl.hold.target_down == s.pose.z
    s.status.nav_state = 14
    s.tick()
    s.ack()
    assert cancel[0]['ok'] and s.ctrl.hold is not None
    s.status.nav_state = 4
    s.tick()
    assert s.ctrl.hold is None


@pytest.mark.parametrize('clock_offset', [0, 1_790_000_000])
def test_command_and_setpoint_share_px4_clock_with_or_without_epoch(clock_offset):
    s = Sim()
    # Establish the fixture's PX4 clock before the first transaction.
    s.ctrl.samples.clear()
    s.boot_at -= clock_offset
    s.refresh()
    stamp = s.status.timestamp
    out = s.request('set_mode', mode='OFFBOARD')
    s.tick(25)
    assert stamp < s.port.command_stamps[-1] <= stamp + 1_300_000
    assert stamp < min(s.port.hold_stamps) <= max(s.port.hold_stamps) <= stamp + 1_300_000
    s.ack()
    s.status.nav_state = 14
    s.tick()
    assert out[0]['ok']


def test_ack_jitter_replay_and_wrong_epoch():
    s = Sim()
    s.now += .100  # send between two status samples
    out = s.request('disarm')
    sent = s.port.command_stamps[-1]
    assert abs((sent - s.status.timestamp) - 100_000) <= 1
    s.ack(timestamp=1_790_000_000_000_000)
    s.ack(timestamp=s.status.timestamp - 1)
    assert not out
    # Slight difference between predicted and real PX4 time must not discard the ACK.
    s.ack(timestamp=sent - 5000)
    assert out[0]['ok']
    second = s.request('disarm')
    s.ack(timestamp=sent - 5000)
    assert not second
    s.ack(timestamp=sent + 100)
    assert second[0]['ok']


def test_clock_reset_aborts_transaction_until_next_progressing_sample():
    s = Sim()
    out = s.request('disarm')
    s.boot_at = s.now - 0.1
    s.tick()
    assert not out[0]['ok'] and not s.request('set_mode', mode='POSCTL')[0]['ok']
    assert s.port.errors
    s.tick()
    recovered = s.request('set_mode', mode='POSCTL')
    assert not recovered and s.port.command_stamps[-1] < 1_000_000
    s.ack(timestamp=2_300_000_000)
    assert not recovered
    s.ack()
    assert recovered[0]['ok']


def test_zero_or_stuck_px4_stamp_never_keeps_commands_live():
    s = Sim()
    s.ctrl.samples.clear()
    s.status.timestamp = 0
    s.ctrl.receive('vehicle_status', s.status, s.now)
    assert not s.request('disarm')[0]['ok'] and not s.port.sent
    s.refresh()
    s.now += 2.1
    s.ctrl.receive('vehicle_status', s.status, s.now)
    assert not s.request('disarm')[0]['ok'] and not s.port.sent


def airborne(offboard=False, **settings):
    s = Sim(**settings)
    s.landed.landed = False
    s.pose.x, s.pose.y, s.pose.z, s.pose.heading = 0., 0., -1., math.radians(179)
    if offboard:
        result = s.request('set_mode', mode='OFFBOARD')
        s.tick(25)
        s.ack()
        s.status.nav_state = 14
        s.tick()
        assert result[0]['ok']
    return s


def target(hold):
    return hold.target_north, hold.target_east, hold.target_down, hold.target_yaw


@pytest.mark.parametrize('frame', ['enu', 'ned'])
@pytest.mark.parametrize('east,north,inside', [
    (-12.5, 0, True), (-25, -15, True), (-25, 15, True), (25, -15, True), (25, 15, True),
    (-25.01, 0, False), (25.01, 0, False), (0, -15.01, False), (0, 15.01, False),
])
def test_goto_rectangular_field(frame, east, north, inside):
    s = airborne(True, goto_fence_shape='rectangle', field_east_size_m=50., field_north_size_m=30.)
    old = target(s.ctrl.hold)
    xyz = f'{east}, {north}, 1.5' if frame == 'enu' else f'{north}, {east}, -1.5'
    result, = s.request('goto', frame=frame, target=xyz)
    assert result['ok'] is inside
    if inside:
        assert target(s.ctrl.hold)[:3] == (north, east, -1.5)
    else:
        assert '[-25, 25]' in result['message'] and '[-15, 15]' in result['message']
        assert target(s.ctrl.hold) == old


def test_goto_field_uses_configured_full_lengths_and_existing_height_limit():
    s = airborne(True, goto_fence_shape='rectangle', field_east_size_m=8., field_north_size_m=4.)
    assert s.request('goto', target='4 2 1.5')[0]['ok']
    assert not s.request('goto', target='0 2.01 1.5')[0]['ok']
    assert not s.request('goto', target='4.01 0 1.5')[0]['ok']
    assert 'height' in s.request('goto', target='0 0 11')[0]['message']


@pytest.mark.parametrize('payload,expected', [
    ({'target': ' 1,  0.5 , 1.2, 90 '}, (.5, 1., -1.2, 0.)),
    ({'x': 1, 'y': .5, 'z': 1.2, 'yaw_deg': 360}, (.5, 1., -1.2, math.pi / 2)),
    ({'target': '-3 0 0.3'}, (0., -3., -.3, math.radians(179))),
    ({'x': 0, 'y': 0, 'z': 10}, (0., 0., -10., math.radians(179))),
    ({'frame': 'ned', 'target': '1 -.5 -1.2 -450'}, (1., -.5, -1.2, -math.pi / 2)),
])
def test_goto_active_offboard_updates_target_without_command(payload, expected):
    s = airborne(True)
    commands = list(s.port.sent)
    assert s.request('goto', **payload)[0]['ok']
    assert target(s.ctrl.hold) == pytest.approx(expected)
    assert s.port.sent == commands and s.ctrl.pending is None
    assert s.port.points[-1] == (0., 0., -1.)


@pytest.mark.parametrize('payload', [
    {}, {'target': '1, 2'}, {'target': '1,2,3,4,5'}, {'target': '1,,2,1'},
    {'target': '1 2 nan'}, {'x': True, 'y': 0, 'z': 1}, {'x': 0, 'y': 0, 'z': '1'},
    {'x': 0, 'y': 0, 'z': 1, 'yaw_deg': None}, {'target': '0 0 1 inf'},
    {'target': '0 0 1', 'x': 1}, {'frame': 'body', 'target': '1 2 1'},
    {'target': '2.2 2.2 1'}, {'target': '0 0 .299'}, {'target': '0 0 10.01'},
    {'frame': 'ned', 'target': '0 0 1'}, {'target': '0 0 1', 'yaw': 0},
])
def test_bad_goto_leaves_active_target_untouched(payload):
    s = airborne(True)
    assert s.request('goto', target='1 0 1.2')[0]['ok']
    before = target(s.ctrl.hold)
    result, = s.request('goto', **payload)
    assert not result['ok'] and result['message'] and target(s.ctrl.hold) == before
    assert len(s.port.sent) == 1


@pytest.mark.parametrize('owner,field,value,reason', [
    ('landed', 'landed', True, 'airborne'), ('status', 'arming_state', 1, 'armed'),
    ('status', 'failsafe', True, 'failsafe'), ('pose', 'xy_valid', False, 'position'),
    ('pose', 'heading', math.nan, 'position'),
])
def test_goto_requires_armed_airborne_valid_pose(owner, field, value, reason):
    s = airborne()
    setattr(getattr(s, owner), field, value)
    result, = s.request('goto', target='1 0 1')
    assert not result['ok'] and reason in result['message']
    assert s.ctrl.hold is None and not s.port.sent


def test_goto_loiter_waits_at_measured_pose_for_both_ack_and_offboard():
    s = airborne()
    result = s.request('goto', target='1 2 1.5')
    s.tick(22)
    assert not s.port.sent
    s.pose.x, s.pose.heading = .1, .4
    s.tick(3)
    assert s.port.sent == [(SET_MODE, (1, 6, 0))]
    s.ack()
    s.tick(5)
    assert not result and s.port.points[-1] == (.1, 0., -1.)
    assert target(s.ctrl.hold) == (.1, 0., -1., .4)
    s.status.nav_state = 14
    s.tick()
    assert result[0]['ok'] and result[0]['result_code'] == 0
    assert target(s.ctrl.hold) == (2., 1., -1.5, .4)
    assert s.port.points[-1] == (.1, 0., -1.)
    s.tick()
    assert s.port.points[-1][0] > .1


@pytest.mark.parametrize('outcome', ['denied', 'no_ack', 'no_mode', 'landed', 'stale_land'])
def test_failed_goto_handshake_never_moves(outcome):
    s = airborne()
    result = s.request('goto', target='1 0 1.2')
    s.tick(25)
    if outcome == 'denied':
        s.ack(2)
    elif outcome == 'no_ack':
        s.status.nav_state = 14
    else:
        s.ack()
        if outcome == 'landed':
            s.landed.landed = True
            s.status.nav_state = 14
        elif outcome == 'stale_land':
            s.ctrl.samples.pop('vehicle_land_detected')
            s.ctrl.tick(s.now)
    s.tick(42)
    assert not result[0]['ok']
    assert set(s.port.points) == {(0., 0., -1.)}


@pytest.mark.parametrize('height', [.4, 1.6])
def test_goto_vector_vertical_and_shortest_yaw_with_delayed_tick(height):
    s = airborne(True)
    s.request('goto', frame='ned', x=.6, y=.8, z=-height, yaw_deg=-175)
    s.now += .5
    s.refresh()
    s.ctrl.tick(s.now)
    hold = s.ctrl.hold
    assert (hold.north, hold.east) == pytest.approx((.03, .04))
    assert hold.down == pytest.approx(-1. + math.copysign(.05, 1. - height))
    assert math.remainder(hold.yaw - s.pose.heading, math.tau) == pytest.approx(.06)
    s.tick(45)
    assert s.port.points[-5:] == [(.6, .8, -height)] * 5
    assert hold.yaw == pytest.approx(math.radians(-175))
    assert all(b-a == pytest.approx(50000, abs=1) for a, b in zip(s.port.hold_stamps[-5:-1], s.port.hold_stamps[-4:]))


def test_goto_retarget_without_yaw_holds_interpolated_heading():
    s = airborne(True)
    s.request('goto', target='1 0 1.5 0')
    s.tick(6)
    yaw = s.ctrl.hold.yaw
    s.request('goto', target='0 1 1')
    s.tick(5)
    assert yaw != s.pose.heading and target(s.ctrl.hold) == (1., 0., -1., yaw)
    assert s.ctrl.hold.yaw == yaw


@pytest.mark.parametrize('action,command', [('cancel', SET_MODE), ('land', NAV_LAND)])
def test_goto_interruption_freezes_all_axes_until_handover(action, command):
    s = airborne(True)
    s.request('goto', target='1 1 2 0')
    s.tick(10)
    s.pose.x, s.pose.y, s.pose.z, s.pose.heading = .1, -.1, -1.1, .3
    result = s.request(action)
    assert target(s.ctrl.hold) == (.1, -.1, -1.1, .3)
    s.tick(3)
    assert s.port.points[-3:] == [(.1, -.1, -1.1)] * 3 and s.ctrl.hold.yaw == .3
    assert s.port.sent[-1][0] == command
    s.ack()
    assert result[0]['ok']
    s.status.nav_state = 4 if action == 'cancel' else 18
    s.tick()
    assert s.ctrl.hold is None


@pytest.mark.parametrize('action', ['land', 'cancel'])
@pytest.mark.parametrize('phase', ['warmup', 'ack', 'mode_confirmation'])
def test_goto_handshake_can_be_interrupted(action, phase):
    s = airborne()
    result = s.request('goto', target='1 2 1.5')
    s.tick(3 if phase == 'warmup' else 25)
    if phase == 'mode_confirmation':
        s.ack()
    assert s.ctrl.pending.phase == phase
    interrupt = s.request(action)
    assert not result[0]['ok'] and s.ctrl.pending.name == action
    s.tick(3)
    assert set(s.port.points) == {(0., 0., -1.)}
    s.ack()
    assert interrupt[0]['ok']


@pytest.mark.parametrize('field', ['xy_reset_counter', 'z_reset_counter', 'heading_reset_counter', 'failsafe'])
def test_active_goto_stops_publication_on_reset_or_failsafe(field):
    s = airborne(True)
    s.request('goto', target='1 2 1.5')
    s.tick(2)
    if field == 'failsafe':
        s.status.failsafe = True
    else:
        setattr(s.pose, field, getattr(s.pose, field) + 1)
    assert not s.request('goto', target='0 0 1')[0]['ok']
    previous = len(s.port.points)
    s.tick(5)
    assert s.ctrl.hold is None and s.port.errors and len(s.port.points) == previous


@pytest.mark.parametrize('key,values', [
    ('xy_speed_m_s', [0, 2.1, math.nan]), ('max_xy_radius_m', [0, -1, math.inf]),
    ('min_goto_height_m', [0, 11, math.nan]),
    ('goto_fence_shape', ['box', '', None]), ('field_east_size_m', [0, -1, math.inf]),
    ('field_north_size_m', [0, -1, math.nan]),
])
def test_goto_rejects_invalid_configuration(key, values):
    for value in values:
        with pytest.raises(ValueError, match=key):
            Controller(Port(), .5, 10., **{key: value})
