"""One serialized PX4 command transaction and one local NED hold trajectory."""
from dataclasses import dataclass
import math
import re
from typing import Optional, Tuple


ARM_DISARM, SET_MODE, NAV_LAND, SET_NAV_STATE = 400, 176, 21, 100001
OFFBOARD, AUTO_LOITER = 14, 4
ARMED = 2
# PX4 custom main/sub modes, as consumed by Commander::handle_command.
MODES = {'OFFBOARD': (6, 0), 'POSCTL': (3, 0), 'ALTCTL': (2, 0), 'STABILIZED': (7, 0),
         'AUTO.LOITER': (4, 3), 'AUTO.LAND': (4, 6), 'AUTO.RTL': (4, 5)}
ACTIONS = ('arm', 'disarm', 'set_mode', 'takeoff', 'goto', 'land', 'cancel', 'register_extra')
RESULT = {0: 'ACCEPTED / PX4 已接受', 1: 'TEMPORARILY_REJECTED / PX4 暂时拒绝',
          2: 'DENIED / PX4 拒绝', 3: 'UNSUPPORTED / 不支持该指令', 4: 'FAILED / 指令执行失败',
          5: 'IN_PROGRESS / PX4 正在处理', 6: 'CANCELLED / PX4 已取消',
          7: 'COMMAND_LONG_ONLY', 8: 'COMMAND_INT_ONLY', 9: 'UNSUPPORTED_MAV_FRAME'}
ACK_TIMEOUT = 2.0
WARMUP = 1.1
# DDS samples arrive with some scheduling/transport jitter. Bracket the send
# using recent evidence from PX4 rather than assuming a link with zero delay.
ACK_JITTER_US = 50_000
CLOCK_JUMP_US = 2_000_000
YAW_RATE = 0.6


def reply(command, message, code=None, ok=False, **extra):
    return dict(ok=ok, command=command, result_code=code, message=message, **extra)


@dataclass
class Request:
    name: str
    payload: dict
    respond: object
    deadline: float = 0.
    phase: str = ''
    command_id: int = 0
    ack_floor_us: int = 0
    source_system: int = 0
    source_component: int = 0
    ack: object = None
    height: float = 0.
    target: Optional[Tuple[float, float, float, Optional[float]]] = None

    @property
    def needs_offboard(self):
        return self.name in ('takeoff', 'goto') or (self.name == 'set_mode' and self.payload.get('mode') == 'OFFBOARD')


@dataclass
class Hold:
    north: float
    east: float
    down: float
    yaw: float
    xy_reset: int
    z_reset: int
    yaw_reset: int
    target_north: float
    target_east: float
    target_down: float
    target_yaw: float
    last_tick: float
    first_sample: float = 0.
    samples: int = 0
    active: bool = False
    armed: bool = False
    entry_grace_until: float = 0.

    def follow_pose(self, p):
        self.north = self.target_north = p.x
        self.east = self.target_east = p.y
        self.down = self.target_down = p.z
        self.yaw = self.target_yaw = p.heading

    def set_target(self, north, east, down, yaw):
        self.target_north, self.target_east, self.target_down = north, east, down
        self.target_yaw = self.yaw if yaw is None else yaw

    def check_frame(self, p):
        if (p.xy_reset_counter, p.z_reset_counter, p.heading_reset_counter) != (
                self.xy_reset, self.z_reset, self.yaw_reset):
            raise ValueError('Local estimate reset / 定位基准发生重置，已停止 OFFBOARD 输出')


class Controller:
    def __init__(self, port, climb_rate, max_height, xy_speed_m_s=0.5, max_xy_radius_m=3.0, min_goto_height_m=0.3,
                 *, goto_fence_shape='circle', field_east_size_m=50.0, field_north_size_m=30.0):
        if not math.isfinite(xy_speed_m_s) or not 0 < xy_speed_m_s <= 2:
            raise ValueError('xy_speed_m_s must be in (0, 2]')
        if not math.isfinite(max_xy_radius_m) or max_xy_radius_m <= 0:
            raise ValueError('max_xy_radius_m must be finite and positive')
        if not 0 < min_goto_height_m <= max_height or not math.isfinite(min_goto_height_m):
            raise ValueError('min_goto_height_m must be positive and at most max_takeoff_height_m')
        if goto_fence_shape not in ('circle', 'rectangle'):
            raise ValueError('goto_fence_shape must be circle or rectangle')
        for name, size in (('field_east_size_m', field_east_size_m), ('field_north_size_m', field_north_size_m)):
            if not math.isfinite(size) or size <= 0:
                raise ValueError(f'{name} must be finite and positive')
        self.goto_fence_shape = goto_fence_shape
        self.east_limit = field_east_size_m / 2
        self.north_limit = field_north_size_m / 2
        self.xy_speed_m_s = xy_speed_m_s
        self.max_xy_radius_m = max_xy_radius_m
        self.min_goto_height_m = min_goto_height_m
        self.port = port
        self.climb_rate = climb_rate
        self.max_height = max_height
        self.pending = None
        self.hold = None
        self.samples = {}
        self.quarantine = {}
        self.clock_recovering = False
        self.ack_stamps = {}
        self.origin = None

    def receive(self, kind, msg, now):
        if kind == 'vehicle_status':
            stamp = msg.timestamp
            if stamp <= 0:
                return
            previous = self.samples.get(kind)
            if previous is not None:
                old, received = previous
                delta = stamp - old.timestamp
                if -ACK_JITTER_US < delta <= 0:
                    return  # Replayed/out-of-order status must not refresh freshness.
                elapsed_us = int((now - received) * 1_000_000)
                if delta < 0 or abs(delta - elapsed_us) > CLOCK_JUMP_US or msg.system_id != old.system_id:
                    # Reboot, time synchronization reset, or a changed target. An ACK or
                    # pose from the previous clock domain cannot complete this transaction.
                    self.hold = None
                    if self.pending:
                        self.abort('PX4 clock or vehicle ID changed / 时钟或目标改变，指令结果未确认', now)
                    self.samples.clear()
                    self.ack_stamps.clear()
                    self.origin = None
                    self.clock_recovering = True
                    self.port.report('PX4 clock or vehicle ID changed; awaiting a new status sample')
                else:
                    self.clock_recovering = False
        self.samples[kind] = (msg, now)

    def sample(self, kind, now, max_age):
        msg, received = self.samples.get(kind, (None, -math.inf))
        if msg is None or now - received > max_age:
            raise ValueError(f'{kind} unavailable or stale / 缺少新鲜飞控数据')
        return msg

    def pose(self, now):
        p = self.sample('vehicle_local_position', now, .5)
        if not (p.xy_valid and p.z_valid and all(map(math.isfinite, (p.x, p.y, p.z, p.heading)))):
            raise ValueError('Local position invalid / PX4 本地水平或高度定位无效')
        return p

    def status(self, now):
        if self.clock_recovering:
            raise ValueError('Waiting for PX4 clock after reset / 等待飞控时钟恢复')
        return self.sample('vehicle_status', now, 2.)

    def px4_time_us(self, now):
        status = self.status(now)
        age = now - self.samples['vehicle_status'][1]
        return status.timestamp + int(age * 1_000_000)

    def check_liftoff(self, now):
        status = self.status(now)
        if status.arming_state != ARMED:
            raise ValueError('Takeoff requires an armed vehicle / 请先解锁')
        if not status.pre_flight_checks_pass or status.failsafe:
            raise ValueError('PX4 preflight checks failed or failsafe active / 飞控起飞条件未满足')
        if not self.sample('vehicle_land_detected', now, 2.).landed:
            raise ValueError('Takeoff requires confirmed landed state / 飞控未确认已着陆')

    def parse_goto(self, payload):
        frame = payload.get('frame', 'enu')
        if frame not in ('enu', 'ned'):
            raise ValueError('frame must be enu or ned')
        fields = ('x', 'y', 'z', 'yaw_deg')
        unknown = set(payload).difference(fields, ('frame', 'target'))
        if unknown:
            raise ValueError('Unknown GoTo fields: ' + ', '.join(sorted(unknown)))
        if 'target' in payload:
            if not isinstance(payload['target'], str) or any(k in payload for k in fields):
                raise ValueError('Use either a target string or numeric x, y, z and optional yaw_deg')
            parts = re.split(r'\s*,\s*|\s+', payload['target'].strip())
            if len(parts) not in (3, 4):
                raise ValueError('target must contain x, y, z and optional yaw_deg')
            values = [float(part) for part in parts]
        else:
            values = [payload.get(k) for k in fields[:3]]
            if 'yaw_deg' in payload:
                values.append(payload['yaw_deg'])
            if any(type(v) not in (float, int) for v in values):
                raise ValueError('x, y, z and optional yaw_deg must be JSON numbers')
        if not all(map(math.isfinite, values)):
            raise ValueError('GoTo coordinates and yaw_deg must be finite')
        x, y, z = values[:3]
        north, east, down = (y, x, -z) if frame == 'enu' else (x, y, z)
        if self.goto_fence_shape == 'rectangle':
            if abs(east) > self.east_limit or abs(north) > self.north_limit:
                raise ValueError(f'Target outside field: ENU x (east) must be within '
                                 f'[-{self.east_limit:g}, {self.east_limit:g}] m and y (north) within '
                                 f'[-{self.north_limit:g}, {self.north_limit:g}] m')
        elif math.hypot(north, east) > self.max_xy_radius_m:
            raise ValueError(f'Target exceeds horizontal radius of {self.max_xy_radius_m:g} m')
        if not self.min_goto_height_m <= -down <= self.max_height:
            raise ValueError(f'Target height must be within [{self.min_goto_height_m:g}, {self.max_height:g}] m')
        yaw = None
        if len(values) == 4:
            degrees = math.remainder(values[3], 360.)
            yaw = math.remainder(math.radians(90. - degrees if frame == 'enu' else degrees), math.tau)
        return north, east, down, yaw

    def check_goto(self, now):
        status = self.status(now)
        if status.arming_state != ARMED:
            raise ValueError('GoTo requires an armed vehicle')
        if status.failsafe:
            raise ValueError('PX4 failsafe active')
        pose = self.pose(now)
        if self.hold is not None:
            self.hold.check_frame(pose)
        if self.sample('vehicle_land_detected', now, 2.).landed:
            raise ValueError('GoTo requires an airborne vehicle')

    def start_hold(self, now):
        pose = self.pose(now)
        self.hold = Hold(pose.x, pose.y, pose.z, pose.heading, pose.xy_reset_counter,
                         pose.z_reset_counter, pose.heading_reset_counter, pose.x, pose.y, pose.z, pose.heading, now)

    def freeze(self, now):
        if self.hold is not None:
            try:
                self.hold.follow_pose(self.pose(now))
            except ValueError:
                self.hold = None

    def finish(self, body):
        req, self.pending = self.pending, None
        req.respond(body)

    def abort(self, reason, now):
        req = self.pending
        if req.phase == 'ack':
            # ACK lacks a transaction ID. Never reuse an ambiguous in-flight ID on retry.
            self.quarantine[req.command_id] = now + ACK_TIMEOUT
        if self.hold is not None and not self.hold.active:
            status = self.samples['vehicle_status'][0]
            if (status.nav_state == OFFBOARD or req.phase == 'mode_confirmation'
                    or (req.name == 'goto' and req.phase == 'ack')):
                # PX4 may already be in OFFBOARD before its status sample arrives. Keep
                # the measured hold briefly so an interrupted transition never starts a climb.
                self.hold.active = True
                self.hold.armed = status.arming_state == ARMED
                self.hold.entry_grace_until = now + 1.5
                self.freeze(now)
            else:
                self.hold = None
        result = req.ack.result if req.ack is not None else None
        self.finish(reply(req.name, reason, code=result))

    def send(self, command, params, now):
        req = self.pending
        if now < self.quarantine.get(command, 0):
            raise ValueError('Previous ACK is unresolved / 上次同类型指令未确认，请稍后重试')
        status = self.status(now)
        stamp = self.px4_time_us(now)
        req.command_id = command
        req.ack_floor_us = max(status.timestamp, stamp - ACK_JITTER_US)
        req.source_system, req.source_component = self.port.send(command, params, status, stamp)
        self.origin = (req.source_system, req.source_component)
        req.deadline = now + ACK_TIMEOUT
        req.phase = 'ack'

    def set_mode(self, mode, payload, now):
        if mode == 'OFFBOARD':
            if self.status(now).failsafe:
                raise ValueError('PX4 failsafe active / 飞控处于失效保护')
            self.port.check_offboard()
            if self.hold is not None and self.hold.active and self.status(now).nav_state == OFFBOARD:
                # Our heartbeat is already running. Revalidate the mode with PX4 while
                # continuing the existing trajectory, including when PX4 rejects the command.
                self.pose(now)
                self.send(SET_MODE, (1, *MODES['OFFBOARD']), now)
                return
            self.start_hold(now)
            self.pending.phase = 'warmup'
            self.pending.deadline = now + WARMUP + 1.
            return
        if mode in MODES:
            self.send(SET_MODE, (1, *MODES[mode]), now)
            return
        if mode == 'EXTRA':
            mode_id = payload.get('nav_state', self.port.registered_id())
        elif isinstance(mode, str) and re.fullmatch(r'EXTERNAL[1-8]', mode):
            mode_id = 22 + int(mode[-1])
        else:
            raise ValueError('Unknown mode / 未知飞行模式')
        if type(mode_id) is not int or not 23 <= mode_id <= 30:
            raise ValueError('Extra mode is unregistered or nav_state is invalid / 外部模式未注册或 ID 无效')
        status = self.status(now)
        if not (status.valid_nav_states_mask & status.can_set_nav_states_mask & (1 << mode_id)):
            raise ValueError(f'External mode {mode_id} unavailable / PX4 未开放该外部模式')
        self.send(SET_NAV_STATE, (mode_id,), now)

    def request(self, req, now):
        if self.pending is not None:
            if not ((req.name == 'cancel' and self.pending.name in ('takeoff', 'set_mode', 'goto'))
                    or (req.name == 'land' and self.pending.name == 'goto')):
                req.respond(reply(req.name, 'Busy / 正在等待上一条飞控指令'))
                return
            self.abort(f'Superseded by {req.name}', now)
        self.pending = req
        try:
            p = req.payload
            if req.name in ('disarm', 'arm'):
                arm = p.get('value') if req.name == 'arm' else False
                if type(arm) is not bool or (arm and p.get('confirm') is not True):
                    raise ValueError('Arm requires {"value":true,"confirm":true}; use {"value":false} to disarm')
                if arm and not self.status(now).pre_flight_checks_pass:
                    raise ValueError('Preflight checks failed / PX4 起飞前检查未通过')
                self.send(ARM_DISARM, (int(arm), 0), now)
            elif req.name == 'takeoff':
                h = p.get('target_altitude_m')
                if type(h) not in (float, int) or not math.isfinite(h) or not 0 < h <= self.max_height:
                    raise ValueError(f'target_altitude_m must be within (0, {self.max_height:g}] m')
                self.check_liftoff(now)
                req.height = h
                self.set_mode('OFFBOARD', p, now)
            elif req.name == 'set_mode':
                mode = p.get('mode')
                if not isinstance(mode, str):
                    raise ValueError('A mode string is required / 缺少模式名称')
                self.set_mode(mode, p, now)
            elif req.name == 'goto':
                req.target = self.parse_goto(p)
                self.check_goto(now)
                if self.hold and self.hold.active and self.status(now).nav_state == OFFBOARD:
                    self.hold.set_target(*req.target)
                    self.finish(reply('goto', 'GoTo target accepted', ok=True))
                else:
                    self.set_mode('OFFBOARD', p, now)
            elif req.name == 'land':
                self.freeze(now)
                self.send(NAV_LAND, (0, 0, 0, math.nan, math.nan, math.nan, math.nan), now)
            elif req.name == 'cancel':
                self.freeze(now)
                # Use a different command ID if a DO_SET_MODE ACK is still in flight.
                if now < self.quarantine.get(SET_MODE, 0):
                    self.send(SET_NAV_STATE, (AUTO_LOITER,), now)
                else:
                    self.set_mode('AUTO.LOITER', p, now)
            else:
                raise ValueError('Unknown command')
        except (ValueError, RuntimeError, OverflowError) as err:
            self.abort(str(err), now)

    def acknowledge(self, ack, now):
        if (self.origin != (ack.target_system, ack.target_component)
                or ack.command not in (ARM_DISARM, SET_MODE, NAV_LAND, SET_NAV_STATE)):
            return
        try:
            wire_now = self.px4_time_us(now)
        except ValueError:
            return
        # Only compare timestamps produced in the same PX4 DDS time domain.
        # A delayed ACK from the previous boot/epoch cannot advance the watermark.
        if ack.timestamp > wire_now + CLOCK_JUMP_US:
            return
        if ack.timestamp <= self.ack_stamps.get(ack.command, 0):
            return
        self.ack_stamps[ack.command] = ack.timestamp
        req = self.pending
        if req is None or req.phase != 'ack' or now > req.deadline:
            return
        if ack.command != req.command_id or ack.timestamp < req.ack_floor_us:
            return
        req.ack = ack
        if ack.result == 5:
            return  # Preserve progress, continue waiting for a terminal result.
        if req.needs_offboard and ack.result == 0:
            req.phase, req.deadline = 'mode_confirmation', now + 1.5
            return
        if req.needs_offboard and self.hold and not self.hold.active:
            self.hold = None
        if ack.result == 0 and (req.name != 'arm' or req.payload.get('value') is False):
            # Continue holding until status confirms the handover or disarm.
            self.freeze(now)
        self.finish_ack()

    def finish_ack(self, detail=''):
        req, ack = self.pending, self.pending.ack
        message = RESULT.get(ack.result, f'Unknown PX4 result {ack.result}')
        self.finish(reply(req.name, (message + ' ' + detail).strip(), code=ack.result,
                          ok=ack.result == 0, vehicle_command=ack.command,
                          result_param1=ack.result_param1, result_param2=ack.result_param2))

    def tick(self, now):
        req = self.pending
        if req and now > req.deadline:
            reason = {'ack': 'No terminal PX4 ACK within 2 s / 飞控回执超时',
                      'warmup': 'OFFBOARD stream warmup interrupted / 预热未连续完成',
                      'mode_confirmation': 'PX4 ACK accepted but OFFBOARD entry unconfirmed / 未确认进入 OFFBOARD'}
            self.abort(reason[req.phase], now)
        if self.hold is None:
            return
        try:
            self.update_hold(now)
        except (ValueError, RuntimeError) as err:
            self.hold = None
            self.port.report(str(err))
            if self.pending and self.pending.needs_offboard:
                self.abort(str(err), now)

    def update_hold(self, now):
        hold, req = self.hold, self.pending
        status, p = self.status(now), self.pose(now)
        if status.failsafe:
            raise ValueError('PX4 failsafe active / 已停止 OFFBOARD 输出')
        hold.check_frame(p)
        if status.nav_state == OFFBOARD:
            hold.entry_grace_until = 0.
        if hold.active and ((status.nav_state != OFFBOARD and now >= hold.entry_grace_until)
                            or (hold.armed and status.arming_state != ARMED)):
            self.hold = None
            if req and req.needs_offboard:
                self.abort('PX4 left OFFBOARD or disarmed / 飞控已退出 OFFBOARD 或上锁', now)
            return
        dt = now - hold.last_tick
        if dt > .15 and not hold.active:
            hold.samples = 0
        if hold.samples == 0:
            hold.first_sample = now
        hold.last_tick = now
        if not hold.active:
            hold.follow_pose(p)
        else:
            if status.arming_state == ARMED:
                hold.armed = True
            dt = max(0., min(.1, dt))
            dn, de = hold.target_north - hold.north, hold.target_east - hold.east
            distance = math.hypot(dn, de)
            step = self.xy_speed_m_s * dt
            if distance <= step:
                hold.north, hold.east = hold.target_north, hold.target_east
            else:
                hold.north += (dn / distance) * step
                hold.east += (de / distance) * step
            dz = hold.target_down - hold.down
            step = self.climb_rate * dt
            hold.down = hold.target_down if abs(dz) <= step else hold.down + math.copysign(step, dz)
            dyaw = math.remainder(hold.target_yaw - hold.yaw, math.tau)
            step = YAW_RATE * dt
            hold.yaw = (hold.target_yaw if abs(dyaw) <= step else
                        math.remainder(hold.yaw + math.copysign(step, dyaw), math.tau))
        self.port.publish_hold(hold, self.px4_time_us(now))
        hold.samples += 1
        if req is None:
            return
        if req.name in ('takeoff', 'goto'):
            try:
                if req.name == 'goto':
                    self.check_goto(now)
                else:
                    self.check_liftoff(now)
            except ValueError as err:
                self.abort(str(err), now)
                return
        if req.phase == 'warmup' and now - hold.first_sample >= WARMUP and hold.samples >= 22:
            self.send(SET_MODE, (1, *MODES['OFFBOARD']), now)
        if req.phase == 'mode_confirmation' and status.nav_state == OFFBOARD:
            hold.active = True
            hold.armed = status.arming_state == ARMED
            hold.follow_pose(p)
            if req.name == 'goto':
                hold.set_target(*req.target)
                self.finish_ack('OFFBOARD confirmed; GoTo target accepted')
                return
            hold.target_down = hold.down - req.height
            self.finish_ack('OFFBOARD confirmed; climb started / 已开始爬升' if req.height
                            else 'OFFBOARD confirmed / 已确认进入 OFFBOARD')
