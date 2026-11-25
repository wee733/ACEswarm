"""Convert incoming PX4 and ROS samples into JSON-compatible telemetry."""

from collections import deque
from dataclasses import dataclass, field
import math


def finite(value):
    return float(value) if value is not None and math.isfinite(value) else None


def xyz(values):
    return dict(zip(('x', 'y', 'z'), map(finite, values), strict=True))


def ned_to_enu(values):
    north, east, down = values
    return xyz((east, north, -down if down is not None else None))


def quaternion(values):
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm < 1e-8:
        return None
    # Keep a stable sign for the usual level pose.
    if values[0] < 0:
        norm = -norm
    w, x, y, z = (float(v) / norm for v in values)
    return {'x': x, 'y': y, 'z': z, 'w': w}


def enu_flu_attitude(q):
    w, x, y, z = q
    # Hamilton product q_ENU<-NED * q_NED<-FRD * q_FRD<-FLU.
    # The common 1/sqrt(2) factor is removed by quaternion normalization.
    return quaternion((w + z, x + y, x - y, w - z))


def rotate_vector(vector, attitude, *, inverse=False):
    """Apply the ENU-from-FLU unit quaternion, or its inverse for map-to-body."""
    vx, vy, vz = (vector[axis] for axis in 'xyz')
    x, y, z, w = (attitude[axis] for axis in 'xyzw')
    if inverse:
        x, y, z = -x, -y, -z
    tx, ty, tz = 2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx)
    return xyz((vx + w * tx + y * tz - z * ty,
                vy + w * ty + z * tx - x * tz,
                vz + w * tz + x * ty - y * tx))


def imu(msg):
    gx, gy, gz = msg.gyro_rad
    ax, ay, az = msg.accelerometer_m_s2
    accel_valid = msg.accelerometer_timestamp_relative != msg.RELATIVE_TIMESTAMP_INVALID
    return {'angular_velocity': xyz((gx, -gy, -gz)),
            'linear_acceleration': xyz((ax, -ay, -az)) if accel_valid else None}


def state(msg):
    mode = next((label[17:] for label in dir(type(msg))
                 if label.startswith('NAVIGATION_STATE_') and label != 'NAVIGATION_STATE_MAX'
                 and getattr(type(msg), label) == msg.nav_state), f'UNKNOWN({msg.nav_state})')
    system_status = 0
    if msg.arming_state == msg.ARMING_STATE_ARMED:
        system_status = 5 if msg.failsafe else 4
    elif msg.calibration_enabled or msg.rc_calibration_in_progress:
        system_status = 2
    elif msg.pre_flight_checks_pass:
        system_status = 3
    vtol_state = 0
    if msg.is_vtol:
        if msg.in_transition_mode:
            vtol_state = 1 if msg.in_transition_to_fw else 2
        else:
            vtol_state = 3 if msg.vehicle_type == msg.VEHICLE_TYPE_ROTARY_WING else 4
    return {'connected': True, 'mode': 'STABILIZED' if mode == 'STAB' else mode.replace('AUTO_', 'AUTO.', 1),
            'armed': msg.arming_state == msg.ARMING_STATE_ARMED if msg.arming_state in (
                msg.ARMING_STATE_DISARMED, msg.ARMING_STATE_ARMED) else None,
            'pre_flight_checks_pass': msg.pre_flight_checks_pass, 'failsafe': msg.failsafe,
            'system_status': system_status, 'vtol_state': vtol_state}


def local_position(msg):
    return {'position': ned_to_enu((msg.x if msg.xy_valid else None, msg.y if msg.xy_valid else None,
                                    msg.z if msg.z_valid else None)),
            'linear_velocity': ned_to_enu((msg.vx if msg.v_xy_valid else None, msg.vy if msg.v_xy_valid else None,
                                           msg.vz if msg.v_z_valid else None)), 'frame_id': 'enu',
            'groundspeed': finite(math.hypot(msg.vx, msg.vy)) if msg.v_xy_valid else None,
            'heading': finite(math.degrees(msg.heading) % 360) if msg.heading_good_for_control else None,
            'altitude': finite(msg.ref_alt - msg.z) if msg.z_valid and msg.z_global else None,
            'climb': finite(-msg.vz) if msg.v_z_valid else None}


def battery(msg):
    fraction = finite(msg.remaining) if msg.connected and 0 <= msg.remaining <= 1 else None
    capacity = msg.capacity / 1000 if msg.connected and msg.capacity > 0 else None
    return {'connected': msg.connected, 'present': msg.connected, 'capacity': capacity,
            'charge': capacity * fraction if capacity is not None and fraction is not None else None,
            'voltage': finite(msg.voltage_v) if msg.connected and msg.voltage_v > 0 else None,
            'current': finite(-msg.current_a) if msg.connected and msg.current_a != -1 else None,
            'percentage': fraction}


def global_position(msg):
    return {'lat': finite(msg.lat) if msg.lat_lon_valid else None,
            'lon': finite(msg.lon) if msg.lat_lon_valid else None,
            'alt': finite(msg.alt_ellipsoid) if msg.alt_valid else None,
            'status': None, 'service': None}


def gps_raw(msg):
    gps = msg.receiver
    return {'fix_type': int(gps.fix_type), 'satellites_visible': int(gps.satellites_used),
            'lat': finite(gps.latitude), 'lon': finite(gps.longitude), 'alt': finite(gps.altitude_msl),
            'alt_ellipsoid': finite(gps.altitude_ellipsoid), 'eph': finite(gps.hdop * 100),
            'epv': finite(gps.vdop * 100), 'vel': finite(gps.ground_speed * 100) if gps.vel_ned_valid else None,
            'cog': finite((math.degrees(gps.course) % 360) * 100) if gps.vel_ned_valid else None, 'yaw': None}


def trajectory_setpoint(msg):
    yaw = finite(msg.yaw)
    data = {'coordinate_frame': 1, 'position': ned_to_enu(msg.position), 'velocity': ned_to_enu(msg.velocity),
            'acceleration_or_force': ned_to_enu(msg.acceleration),
            'yaw': math.remainder(math.pi / 2 - yaw, 2 * math.pi) if yaw is not None else None,
            'yaw_rate': finite(-msg.yawspeed)}
    # MAVLink mask bits address the axes of the local NED frame.
    channels = (*msg.position, *msg.velocity, *msg.acceleration)
    mask = sum(1 << bit for bit, value in enumerate(channels) if not math.isfinite(value))
    data['type_mask'] = mask | (1024 if yaw is None else 0) | (2048 if data['yaw_rate'] is None else 0)
    return data


def attitude_setpoint(msg):
    q = enu_flu_attitude(msg.q_d)
    thrust = finite(-msg.thrust_body[2])
    # The attitude target has no roll/pitch/yaw body-rate setpoints.
    return {'type_mask': 7 | (128 if q is None else 0) | (64 if thrust is None else 0),
            'orientation': q, 'body_rate': dict.fromkeys('xyz'), 'thrust': thrust}


def px4_vision(msg):
    if msg.pose_frame == msg.POSE_FRAME_NED:
        return {'position': ned_to_enu(msg.position), 'orientation': enu_flu_attitude(msg.q), 'frame_id': 'enu'}
    if msg.pose_frame == msg.POSE_FRAME_FRD:
        # The horizontal reference has an unknown alignment to north. Preserve the
        # aligned local forward axis, and convert both the world and body to FLU.
        x, y, z = msg.position
        w, qx, qy, qz = msg.q
        return {'position': xyz((x, -y, -z)), 'orientation': quaternion((w, qx, -qy, -qz)), 'frame_id': 'local_flu'}
    return {'position': None, 'orientation': None, 'frame_id': 'unknown'}


def ros_vision(msg):
    pose = msg.pose.pose if hasattr(msg.pose, 'pose') else msg.pose
    p, q = pose.position, pose.orientation
    return {'position': xyz((p.x, p.y, p.z)), 'orientation': quaternion((q.w, q.x, q.y, q.z)),
            'frame_id': msg.header.frame_id}


def px4_log(msg):
    text = bytes(msg.text).split(b'\0', 1)[0].decode('utf-8', 'replace')
    return {'text': text, 'severity': int(msg.severity)}


@dataclass
class Source:
    samples: deque = field(default_factory=deque)
    last_time: float | None = None
    value: object = None
    topic: str = ''

    def receive(self, value, now):
        self.value = value
        self.last_time = now
        self.samples.append(now)
        self.trim(now)

    def trim(self, now):
        while self.samples and self.samples[0] < now - 2.0:
            self.samples.popleft()

    def describe(self, now, timeout):
        self.trim(now)
        age = None if self.last_time is None else max(0.0, now - self.last_time)
        fresh = age is not None and age <= timeout
        return {'topic': self.topic, 'received': age is not None, 'stale': not fresh, 'fresh': fresh,
                'measured_hz': len(self.samples) / 2.0, 'age_ms': int(age * 1000) if age is not None else -1}


# Public source names follow robot-telemetry-ros2 schema v1. Several MAVROS
# streams use data that PX4 supplies together in VehicleLocalPosition.
STREAM_SOURCES = {
    'state': {'state': 'state', 'extended_state': 'land', 'control_mode': 'control_mode'},
    'local_pos': {'pose': 'pose', 'velocity': 'pose', 'vfr_hud': 'pose', 'attitude': 'attitude'},
    'battery': {'battery': 'battery'}, 'imu': {'imu': 'imu'},
    'gps': {'global': 'global', 'gps_raw': 'gps_raw'},
    'vision_pose': {'vision_pose': 'vision_pose'},
    'setpoint': {'local': 'setpoint_in', 'attitude': 'attitude_setpoint'},
    'status_text': {'status_text': 'status_text', 'state': 'state'},
}
STREAM_RATES = {'state': 10, 'local_pos': 25, 'battery': 3, 'imu': 10, 'gps': 5,
                'vision_pose': 20, 'setpoint': 10, 'status_text': 5}
STALE_TIMEOUT_SEC = {'state': 5., 'local_pos': 2., 'battery': 10., 'imu': 2., 'gps': 10.,
                     'vision_pose': 2., 'setpoint': 5., 'status_text': 8.}


def empty_vector():
    return dict.fromkeys('xyz')


def empty_attitude():
    return dict.fromkeys('xyzw')


def empty_local_setpoint():
    return {'coordinate_frame': None, 'type_mask': None, 'position': empty_vector(), 'velocity': empty_vector(),
            'acceleration_or_force': empty_vector(), 'yaw': None, 'yaw_rate': None}


class Telemetry:
    def __init__(self, state_timeout=5., *, stale_timeout_sec=None):
        self.sources = {key: Source() for names in STREAM_SOURCES.values() for key in names.values()}
        self.sources['setpoint_out'] = Source()
        self.stale_timeout_sec = {**STALE_TIMEOUT_SEC, 'state': state_timeout}
        self.stale_timeout_sec.update(stale_timeout_sec or {})
        for stream, seconds in self.stale_timeout_sec.items():
            if stream not in STREAM_SOURCES or not math.isfinite(seconds) or seconds <= 0:
                raise ValueError(f'Invalid stale_timeout_sec.{stream}: {seconds!r}')

    def receive(self, name, value, now):
        self.sources[name].receive(value, now)

    def _local_setpoint(self, now):
        incoming, outgoing = self.sources['setpoint_in'], self.sources['setpoint_out']
        if outgoing.last_time is None:
            return incoming
        if incoming.last_time is None:
            return outgoing
        if now - incoming.last_time <= .5 or outgoing.last_time < incoming.last_time:
            return incoming
        return outgoing

    def fresh_value(self, name, now, stream):
        source = self.sources[name]
        return source.value if source.describe(now, self.stale_timeout_sec[stream])['fresh'] else None

    def payload(self, stream, now):
        info, values = {}, {}
        for name, key in STREAM_SOURCES[stream].items():
            source = self._local_setpoint(now) if name == 'local' else self.sources[key]
            # A status-text fallback is valid only while its VehicleStatus is fresh.
            timeout_stream = 'state' if stream == 'status_text' and key == 'state' else stream
            info[name] = source.describe(now, self.stale_timeout_sec[timeout_stream])
            values[name] = source.value if info[name]['fresh'] else None

        if stream == 'state':
            data = dict.fromkeys(('armed', 'mode', 'pre_flight_checks_pass', 'failsafe', 'system_status', 'vtol_state',
                                  'guided', 'manual_input', 'raw'))
            data.update(connected=False, landed_state=values['extended_state'])
            data.update(values['state'] or {})
            data.update(values['control_mode'] or {})
        elif stream == 'local_pos':
            data = dict.fromkeys(('airspeed', 'groundspeed', 'heading', 'throttle', 'altitude', 'climb'))
            data.update(frame_id='enu', position=empty_vector(), linear_velocity=empty_vector())
            data.update(values['pose'] or {})
            data['orientation'] = values['attitude'] or empty_attitude()
            gyro = (self.fresh_value('imu', now, stream) or {}).get('angular_velocity')
            data['angular_velocity'] = (rotate_vector(gyro, values['attitude'])
                                        if values['attitude'] and gyro and None not in gyro.values() else empty_vector())
        elif stream == 'imu':
            data = dict(angular_velocity=empty_vector(), linear_acceleration=empty_vector())
            data.update(values['imu'] or {})
            data['orientation'] = self.fresh_value('attitude', now, stream) or empty_attitude()
        elif stream == 'gps':
            raw = values['gps_raw'] or dict.fromkeys(('fix_type', 'satellites_visible', 'yaw', 'lat', 'lon', 'alt',
                                                       'eph', 'epv', 'vel', 'cog', 'alt_ellipsoid'))
            position = values['global'] or dict.fromkeys(('status', 'service', 'lat', 'lon', 'alt'))
            data = {'global': position, 'gps_raw': raw, 'fix_type': raw['fix_type']}
        elif stream == 'setpoint':
            target_attitude = values['attitude'] or dict(type_mask=None, orientation=empty_attitude(),
                                                         body_rate=empty_vector(), thrust=None)
            data = {'local': values['local'] or empty_local_setpoint(), 'attitude': target_attitude,
                    'derived_command': None, 'frame_id': 'enu'}
        elif stream == 'status_text':
            health = values['state']
            if values['status_text'] and values['status_text']['text']:
                data = values['status_text']
            elif health is None:
                data = {'text': '等待 PX4 VehicleStatus', 'severity': 4}
            elif health['failsafe']:
                data = {'text': 'PX4 failsafe', 'severity': 3}
            elif not health['pre_flight_checks_pass']:
                data = {'text': 'PX4 起飞前检查未通过', 'severity': 4}
            else:
                data = {'text': '', 'severity': None}
        elif stream == 'battery':
            data = values['battery'] or dict.fromkeys(('present', 'connected', 'voltage', 'current', 'charge',
                                                       'capacity', 'percentage'))
        elif stream == 'vision_pose':
            data = values['vision_pose'] or dict(position=empty_vector(), orientation=empty_attitude(), frame_id=None)

        # The optional PX4 control-mode source supplements state, and state
        # supplies a status-text fallback between PX4 log events.
        required = [source for name, source in info.items() if name != 'control_mode']
        stale = any(source['stale'] for source in required)
        if stream == 'status_text':
            stale = all(source['stale'] for source in required)
        return {'schema_version': 1, 'stream': stream, 'source_variant': 'uxrce_vehicle',
                'stale': stale, 'data': data, 'sources': info}
