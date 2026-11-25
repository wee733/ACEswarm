"""Subscribe to uXRCE DDS and publish the measured state through a Zenoh session."""

import json
import math
import os
import re
import socket
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.executors import ExternalShutdownException
from px4_msgs import msg as px4
from geometry_msgs.msg import Point, PoseStamped, Quaternion, Vector3
from nav_msgs.msg import Odometry
from std_msgs.msg import String
import zenoh

from . import telemetry as t


EXPECTED_VERSIONS = {'VehicleStatus': 5, 'VehicleLocalPosition': 1, 'BatteryStatus': 1,
                     'VehicleAttitude': 0, 'TrajectorySetpoint': 0}


def topic_for(namespace, direction, name, msg_type):
    base = '/'.join(filter(None, (namespace.strip('/'), 'fmu', direction, name)))
    version = getattr(msg_type, 'MESSAGE_VERSION', 0)
    return '/' + base + (f'_v{version}' if version else '')


class TelemetryBridge(Node):
    def __init__(self):
        super().__init__('uxrce_telemetry_bridge')
        def param(name, default):
            return self.declare_parameter(name, default).value

        self.hostname = (param('avahi_hostname', '') or os.getenv('AIVUDA_OS_AVAHI_HOSTNAME')
                         or socket.gethostname()).removesuffix('.local')
        legacy_id = param('uav_id', '')
        self.agent = param('agent_id', '') or legacy_id or self.hostname
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+', self.agent):
            raise ValueError('agent_id must be a single concrete Zenoh key segment')
        self.robot_type = param('robot_type', 'uxrce_mocap')
        if not self.robot_type.strip():
            raise ValueError('robot_type must be non-empty')
        self.namespace = param('px4_namespace', '')
        logger = self.get_logger()
        versions = {name: getattr(getattr(px4, name, None), 'MESSAGE_VERSION', None)
                    for name in EXPECTED_VERSIONS}
        logger.info('PX4 v1.17/main | imported px4_msgs MESSAGE_VERSION: ' +
                    ', '.join(f'{name}={version}' for name, version in versions.items()))
        mismatches = [f'{name}: expected v{expected}, imported {versions[name]}'
                      for name, expected in EXPECTED_VERSIONS.items() if versions[name] != expected]
        if mismatches:
            error = 'Incompatible px4_msgs overlay for PX4 v1.17/main: ' + ', '.join(mismatches)
            logger.error(error)
            raise RuntimeError(error)
        legacy_state_timeout = param('state_timeout_s', t.STALE_TIMEOUT_SEC['state'])
        defaults = {**t.STALE_TIMEOUT_SEC, 'state': legacy_state_timeout}
        self.model = t.Telemetry(stale_timeout_sec={
            stream: param(f'stale_timeout_sec.{stream}', default) for stream, default in defaults.items()
        })
        self.dirty = set(t.STREAM_SOURCES)
        self.last_sent = {}
        self.period = {}
        self.publish_rates = {stream: t.Source() for stream in t.STREAM_RATES}
        for stream, default in t.STREAM_RATES.items():
            hz = param(stream + '_hz', float(default))
            if not math.isfinite(hz) or not 0 < hz <= 50:
                raise ValueError(f'{stream}_hz must be in (0, 50]')
            self.period[stream] = 1 / hz
        enable_heartbeat = param('publish_heartbeat', True)
        publish_odom = param('publish_standard_odom', True)
        odom_topic = param('standard_odom_topic', '/swarm/odom')
        odom_hz = param('standard_odom_hz', 20.0)
        self.pose_stamp = None
        if publish_odom:
            if not math.isfinite(odom_hz) or odom_hz <= 0:
                raise ValueError('standard_odom_hz must be finite and positive')
            self.odom = self.create_publisher(Odometry, odom_topic, 5)
            self.create_timer(1 / odom_hz, self.publish_odom)
            logger.info(f'Publishing nav_msgs/msg/Odometry on {self.odom.topic_name} at up to {odom_hz:g} Hz')

        def subscribe(topic, msg_type, source, convert, streams):
            def receive(msg):
                self.model.sources[source].topic = sub.topic_name
                self.model.receive(source, convert(msg), time.monotonic())
                if source == 'pose' and publish_odom:
                    self.pose_stamp = self.get_clock().now().to_msg()
                self.dirty.update(streams)
            sub = self.create_subscription(msg_type, topic, receive, qos_profile_sensor_data)
            self.model.sources[source].topic = sub.topic_name
            logger.info(f'Subscribed {sub.topic_name} ({msg_type.__name__}, {source})')

        def fmu(name, kind, source, convert, streams, direction='out'):
            subscribe(topic_for(self.namespace, direction, name, kind), kind, source, convert, streams)

        fmu('vehicle_status', px4.VehicleStatus, 'state', t.state, ['state', 'status_text'])
        fmu('vehicle_land_detected', px4.VehicleLandDetected, 'land', lambda m: 1 if m.landed else 2, ['state'])
        fmu('vehicle_control_mode', px4.VehicleControlMode, 'control_mode',
            lambda m: {'manual_input': m.flag_control_manual_enabled, 'guided': m.flag_control_auto_enabled}, ['state'])
        fmu('vehicle_attitude', px4.VehicleAttitude, 'attitude', lambda msg: t.enu_flu_attitude(msg.q), ['local_pos'])
        fmu('vehicle_local_position', px4.VehicleLocalPosition, 'pose', t.local_position, ['local_pos'])
        fmu('battery_status', px4.BatteryStatus, 'battery', t.battery, ['battery'])
        fmu('sensor_combined', px4.SensorCombined, 'imu', t.imu, ['imu'])
        fmu('vehicle_global_position', px4.VehicleGlobalPosition, 'global', t.global_position, ['gps'])
        fmu('vehicle_gnss', px4.VehicleGnss, 'gps_raw', t.gps_raw, ['gps'])
        for direction in ('in', 'out'):
            fmu('trajectory_setpoint', px4.TrajectorySetpoint, 'setpoint_' + direction,
                t.trajectory_setpoint, ['setpoint'], direction)
        fmu('vehicle_attitude_setpoint', px4.VehicleAttitudeSetpoint, 'attitude_setpoint',
            t.attitude_setpoint, ['setpoint'], 'in')
        vision = param('vision_source', 'px4')
        vision_topic = param('vision_topic', '')
        if vision == 'px4':
            topic = vision_topic or topic_for(self.namespace, 'in', 'vehicle_visual_odometry', px4.VehicleOdometry)
            subscribe(topic, px4.VehicleOdometry, 'vision_pose', t.px4_vision, ['vision_pose'])
        elif vision in ('odometry', 'pose_stamped'):
            msg_type = Odometry if vision == 'odometry' else PoseStamped
            topic = vision_topic or ('/Odometry' if vision == 'odometry' else '/mocap/pose')
            subscribe(topic, msg_type, 'vision_pose', t.ros_vision, ['vision_pose'])
        elif vision != 'none':
            raise ValueError('vision_source must be px4, odometry, pose_stamped or none')
        fmu('log_message', px4.LogMessage, 'status_text', t.px4_log, ['status_text'])
        custom_status = param('status_text_topic', '')
        if custom_status:
            subscribe(custom_status, String, 'status_text', lambda msg: {'text': msg.data, 'severity': None},
                      ['status_text'])

        config_file = param('zenoh_config', '')
        cfg = zenoh.Config.from_file(config_file) if config_file else zenoh.Config()
        self.session = zenoh.open(cfg)
        self.create_timer(.01, self.publish_telemetry)
        if enable_heartbeat:
            self.create_timer(.5, self.publish_heartbeat)
        logger.info(f'Reporting {self.agent} telemetry (robot_type={self.robot_type})')

    def emit(self, key, body):
        stamp = time.time_ns() // 1_000_000
        payload = {'agent_id': self.agent, 'timestamp_unix_ms': stamp, 'robot_type': self.robot_type, **body}
        if 'stream' in body:
            payload['ts_ms'] = stamp
        try:
            self.session.put(self.agent + '/' + key, json.dumps(payload, allow_nan=False),
                             encoding=zenoh.Encoding.APPLICATION_JSON)
        except zenoh.ZError as exc:
            self.get_logger().error(f'Zenoh publication failed: {exc}', throttle_duration_sec=5)

    def publish_heartbeat(self):
        self.emit('heartbeat', {'key_prefix': self.agent, 'robot_type': self.robot_type,
                                'avahi_hostname': self.hostname, 'online': True})

    def publish_odom(self):
        if self.pose_stamp is None:
            return
        now = time.monotonic()
        # Standard odometry pairs current pose and attitude within the original 500 ms window.
        if any(not self.model.sources[name].describe(now, .5)['fresh'] for name in ('pose', 'attitude')):
            return
        pose = self.model.payload('local_pos', now)['data']
        pos, q, velocity = pose['position'], pose['orientation'], pose['linear_velocity']
        if any(not value or None in value.values() for value in (pos, q, velocity)):
            return
        msg = Odometry()
        msg.header.stamp = self.pose_stamp
        msg.header.frame_id = 'map'
        msg.child_frame_id = f'{self.agent}/base_link'
        msg.pose.pose.position = Point(**pos)
        msg.pose.pose.orientation = Quaternion(**q)
        msg.twist.twist.linear = Vector3(**t.rotate_vector(velocity, q, inverse=True))
        imu = self.model.sources['imu']
        gyro = imu.value.get('angular_velocity') if imu.last_time is not None and now - imu.last_time <= .5 else None
        msg.twist.twist.angular = Vector3(**(gyro if gyro and None not in gyro.values()
                                             else dict.fromkeys('xyz', math.nan)))
        self.odom.publish(msg)
        self.pose_stamp = None

    def publish_telemetry(self):
        now = time.monotonic()
        for stream, period in self.period.items():
            # Refresh stale/absent sources at up to 2 Hz even after DDS stops.
            delay = period if stream in self.dirty else max(.5, period)
            if now - self.last_sent.get(stream, -math.inf) < delay:
                continue
            payload = self.model.payload(stream, now)
            rate = self.publish_rates[stream]
            rate.receive(None, now)
            payload['publish'] = {'max_rate_hz': 1 / period, 'measured_hz': len(rate.samples) / 2.0}
            self.emit('telemetry/' + stream, payload)
            self.last_sent[stream] = now
            self.dirty.discard(stream)


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = TelemetryBridge()
        rclpy.spin(node)
    except (ExternalShutdownException, KeyboardInterrupt):
        pass
    except RuntimeError:
        # SIGTERM may invalidate a wait set before the executor raises
        # ExternalShutdownException (observed on Lyrical).
        if rclpy.ok():
            raise
    finally:
        if node is not None:
            node.session.close()
            node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
