import math

from px4_msgs import msg as px4
import pytest

from uxrce_telemetry_bridge import telemetry as t


def test_full_attitude_basis_change():
    # Level facing north, level facing east and a body roll in the north-facing frame.
    half = math.sqrt(.5)
    assert t.enu_flu_attitude([1, 0, 0, 0]) == pytest.approx(dict(w=half, x=0, y=0, z=half))
    assert t.enu_flu_attitude([half, 0, 0, half]) == pytest.approx(dict(w=1, x=0, y=0, z=0))
    assert t.enu_flu_attitude([half, half, 0, 0]) == pytest.approx(dict(w=.5, x=.5, y=.5, z=.5))
    assert t.enu_flu_attitude([0, 0, 0, 0]) is None
    assert t.enu_flu_attitude([1, math.nan, 0, 0]) is None


def test_state_validity_and_timestamp_expiry():
    model = t.Telemetry(2.5)
    msg = px4.VehicleStatus(nav_state=18, arming_state=1, pre_flight_checks_pass=True)
    model.receive('state', t.state(msg), 10)
    state = model.payload('state', 11)['data']
    assert state == {'mode': 'AUTO.LAND', 'connected': True, 'armed': False, 'failsafe': False,
                     'pre_flight_checks_pass': True, 'raw': None, 'system_status': 3, 'vtol_state': 0,
                     'manual_input': None, 'guided': None, 'landed_state': None}
    assert model.payload('state', 12.51)['data'] == {
        'mode': None, 'connected': False, 'armed': None, 'pre_flight_checks_pass': None, 'failsafe': None,
        'raw': None, 'system_status': None, 'vtol_state': None, 'manual_input': None, 'guided': None,
        'landed_state': None}
    assert model.payload('status_text', 13)['data']['text']
    msg.nav_state = 14
    msg.arming_state = 2
    assert t.state(msg)['mode'] == 'OFFBOARD' and t.state(msg)['armed']
    assert t.state(px4.VehicleStatus())['armed'] is None
    assert t.state(px4.VehicleStatus(nav_state=px4.VehicleStatus.NAVIGATION_STATE_STAB))['mode'] == 'STABILIZED'


def test_imu_frd_to_flu_and_invalid_acceleration():
    msg = px4.SensorCombined(gyro_rad=[1., 2., 3.], accelerometer_m_s2=[4., -5., -6.])
    assert t.imu(msg) == {'angular_velocity': dict(x=1, y=-2, z=-3),
                          'linear_acceleration': dict(x=4, y=5, z=6)}
    msg.accelerometer_timestamp_relative = msg.RELATIVE_TIMESTAMP_INVALID
    assert t.imu(msg)['linear_acceleration'] is None


def test_estimate_validity_and_nonfinite_values():
    msg = px4.VehicleLocalPosition(x=1., y=2., z=-3., xy_valid=True, z_valid=False,
                                   vx=.5, vy=math.nan, vz=-4., v_xy_valid=True, v_z_valid=True)
    pose = t.local_position(msg)
    assert pose['position'] == dict(x=2, y=1, z=None)
    assert pose['linear_velocity'] == dict(x=None, y=.5, z=4)
    assert t.battery(px4.BatteryStatus(connected=True, remaining=-1., current_a=-1., voltage_v=0.)) == {
        'connected': True, 'present': True, 'charge': None, 'capacity': None,
        'percentage': None, 'current': None, 'voltage': None}
    assert t.global_position(px4.VehicleGlobalPosition(lat=44., lon=130., alt_ellipsoid=3., alt_valid=True)) == {
        'lat': None, 'lon': None, 'alt': 3, 'status': None, 'service': None}
    b = t.battery(px4.BatteryStatus(connected=True, remaining=.5, current_a=2., voltage_v=12., capacity=2000))
    assert b == dict(connected=True, present=True, voltage=12, current=-2, percentage=.5, capacity=2, charge=1)
    assert t.px4_log(px4.LogMessage(text=list(b'abc') + [0] * 124, severity=4)) == {'text': 'abc', 'severity': 4}


def test_source_rate_and_setpoint_preference():
    model = t.Telemetry()
    for i in range(301):
        model.receive('imu', {}, i / 100)
    info = model.payload('imu', 3)['sources']['imu']
    assert info == {'measured_hz': 100.5, 'age_ms': 0, 'fresh': True, 'received': True, 'stale': False, 'topic': ''}
    assert model.payload('imu', 3.5)['sources']['imu']['measured_hz'] < 100
    assert model.payload('imu', 5.1)['sources']['imu']['measured_hz'] == 0
    model.receive('setpoint_out', {'topic': 'out'}, 1.)
    model.receive('setpoint_in', {'topic': 'in'}, 1.)
    model.receive('setpoint_out', {'topic': 'out'}, 1.1)
    assert model.payload('setpoint', 1.1)['data']['local'] == {'topic': 'in'}
    assert model.payload('setpoint', 1.6)['data']['local'] == {'topic': 'out'}
    assert model.payload('setpoint', 6.11)['data']['local']['position'] == dict.fromkeys('xyz')


def test_external_pose_frames_and_independent_attitude_age():
    m = px4.VehicleOdometry(position=[1., 2., 3.], q=[1., 0., 0., 0.])
    assert t.px4_vision(m)['position'] is None
    m.pose_frame = m.POSE_FRAME_NED
    assert t.px4_vision(m)['position'] == dict(x=2, y=1, z=-3)
    m.pose_frame = m.POSE_FRAME_FRD
    assert t.px4_vision(m)['position'] == dict(x=1, y=-2, z=-3)
    assert t.px4_vision(m)['frame_id'] == 'local_flu'
    model = t.Telemetry()
    model.receive('attitude', dict(w=1, x=0, y=0, z=0), 1.)
    model.receive('pose', dict(position=dict(x=3, y=2, z=1)), 3.1)
    assert model.payload('local_pos', 3.1)['data']['orientation'] == dict.fromkeys('xyzw')


@pytest.mark.parametrize(('stream', 'timeout'), [
    ('battery', 10.), ('gps', 10.), ('local_pos', 2.), ('imu', 2.), ('vision_pose', 2.),
    ('state', 5.), ('setpoint', 5.),
])
def test_stream_expiry_boundary(stream, timeout):
    model = t.Telemetry()
    for source in set(t.STREAM_SOURCES[stream].values()):
        value = {'fix_type': 3} if source == 'gps_raw' else {}
        model.receive(source, value, 100.)
    assert model.payload(stream, 100. + timeout)['stale'] is False
    expired = model.payload(stream, 100.001 + timeout)
    assert expired['stale'] and all(s['stale'] for s in expired['sources'].values())


def test_configured_battery_timeout_and_recovery():
    model = t.Telemetry(stale_timeout_sec={'battery': 12.})
    value = {'percentage': .45, 'voltage': 22.1}
    model.receive('battery', value, 3.)
    assert model.payload('battery', 15.)['data'] == value
    assert all(v is None for v in model.payload('battery', 15.001)['data'].values())
    model.receive('battery', value, 20.)
    assert model.payload('battery', 20.)['data'] == value


def test_raw_gps_and_setpoint_mavros_units():
    gnss = px4.VehicleGnss()
    gnss.receiver.ground_speed = 2.
    gnss.receiver.course = -math.pi / 2
    gnss.receiver.vel_ned_valid = True
    gnss.receiver.hdop = .5
    raw = t.gps_raw(gnss)
    assert raw['vel'] == 200 and raw['eph'] == 50
    assert raw['cog'] == pytest.approx(27000)
    msg = px4.TrajectorySetpoint(position=[math.nan, 2., 3.], acceleration=[1., 2., 3.], yaw=0., yawspeed=.3)
    local = t.trajectory_setpoint(msg)
    assert local['position'] == dict(x=2, y=None, z=-3)
    assert local['acceleration_or_force'] == dict(x=2, y=1, z=-3)
    assert local['type_mask'] == 1
    assert local['yaw'] == pytest.approx(math.pi / 2) and local['yaw_rate'] == pytest.approx(-.3)
