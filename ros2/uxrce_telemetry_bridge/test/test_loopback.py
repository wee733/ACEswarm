import json
import socket
import time

import rclpy
import pytest
from nav_msgs.msg import Odometry
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import qos_profile_sensor_data
from px4_msgs.msg import VehicleAttitude, VehicleLocalPosition, VehicleStatus
import zenoh

from uxrce_telemetry_bridge.node import TelemetryBridge, topic_for


def test_ros_dds_to_zenoh_and_disconnect(tmp_path, monkeypatch):
    monkeypatch.setenv('ROS_DOMAIN_ID', '216')
    monkeypatch.setenv('AIVUDA_OS_AVAHI_HOSTNAME', 'fixture-mdns')
    monkeypatch.setenv('ROS_AUTOMATIC_DISCOVERY_RANGE', 'LOCALHOST')
    monkeypatch.setenv('ROS_STATIC_PEERS', '')
    monkeypatch.setenv('ROS_HOME', str(tmp_path))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        endpoint = 'tcp/127.0.0.1:' + str(sock.getsockname()[1])
    cfg = {'mode': 'peer', 'scouting': {'multicast': {'enabled': False}, 'gossip': {'enabled': False}},
           'listen': {'endpoints': [endpoint]}}
    latest = {}
    with zenoh.open(zenoh.Config.from_json5(json.dumps(cfg))) as sink:
        def capture(sample):
            latest[str(sample.key_expr)] = json.loads(sample.payload.to_string())
        subscriber = sink.declare_subscriber('fixture/**', capture)
        cfg.update(mode='client', listen={'endpoints': []}, connect={'endpoints': [endpoint]})
        path = tmp_path / 'zenoh.json'
        path.write_text(json.dumps(cfg))
        rclpy.init(args=['--ros-args', '-p', 'uav_id:=fixture', '-p', 'px4_namespace:=fixture',
                         '-p', f'zenoh_config:={path}', '-p', 'state_timeout_s:=0.3'])
        try:
            bridge = TelemetryBridge()
            source = rclpy.create_node('fixture_telemetry_source')
            executor = SingleThreadedExecutor()
            executor.add_node(bridge)
            executor.add_node(source)
            try:
                status_key = topic_for('fixture', 'out', 'vehicle_status', VehicleStatus)
                assert status_key == '/fixture/fmu/out/vehicle_status_v5'
                statuses = source.create_publisher(VehicleStatus, status_key, qos_profile_sensor_data)
                pose_key = topic_for('fixture', 'out', 'vehicle_local_position', VehicleLocalPosition)
                poses = source.create_publisher(VehicleLocalPosition, pose_key, qos_profile_sensor_data)
                att_key = topic_for('fixture', 'out', 'vehicle_attitude', VehicleAttitude)
                attitudes = source.create_publisher(VehicleAttitude, att_key, qos_profile_sensor_data)
                odometry = []
                source.create_subscription(Odometry, '/swarm/odom', odometry.append, 10)
                start = time.monotonic()
                while time.monotonic() - start < 5:
                    statuses.publish(VehicleStatus(nav_state=2, arming_state=1))
                    poses.publish(VehicleLocalPosition(xy_valid=True, z_valid=True, x=1., y=2., z=-3.,
                                                       v_xy_valid=True, v_z_valid=True, vx=4., vy=1., vz=-2.))
                    attitudes.publish(VehicleAttitude(q=[1., 0., 0., 0.]))
                    for _ in range(8):
                        executor.spin_once(timeout_sec=.002)
                    state = latest.get('fixture/telemetry/state', {}).get('data', {})
                    pose = latest.get('fixture/telemetry/local_pos', {}).get('data', {})
                    if state.get('connected') and pose.get('position') and 'fixture/heartbeat' in latest and odometry:
                        break
                    time.sleep(.002)
                assert latest['fixture/heartbeat']['robot_type'] == 'uxrce_mocap'
                assert latest['fixture/heartbeat']['avahi_hostname'] == 'fixture-mdns'
                assert state['mode'] == 'POSCTL' and state['armed'] is False
                assert pose['position'] == {'x': 2., 'y': 1., 'z': 3.}
                assert pose['linear_velocity'] == {'x': 1., 'y': 4., 'z': 2.}
                for stream in ('state', 'local_pos', 'imu', 'battery', 'gps', 'setpoint', 'vision_pose', 'status_text'):
                    envelope = latest['fixture/telemetry/' + stream]
                    assert envelope['schema_version'] == 1 and envelope['stream'] == stream
                    assert envelope['ts_ms'] == envelope['timestamp_unix_ms'] > 0
                    assert envelope['robot_type'] == 'uxrce_mocap' and envelope['source_variant'] == 'uxrce_vehicle'
                    assert {'max_rate_hz', 'measured_hz'} == set(envelope['publish'])
                    for info in envelope['sources'].values():
                        assert info['topic'].startswith('/fixture/fmu/')
                        assert {'received', 'stale', 'age_ms', 'measured_hz'} <= info.keys()
                assert latest['fixture/telemetry/vision_pose']['sources']['vision_pose']['age_ms'] == -1
                odom = odometry[-1]
                assert (odom.header.frame_id, odom.child_frame_id) == ('map', 'fixture/base_link')
                assert odom.header.stamp.sec > 0
                assert [getattr(odom.pose.pose.position, axis) for axis in 'xyz'] == [2., 1., 3.]
                assert [getattr(odom.pose.pose.orientation, axis) for axis in 'xyzw'] == pytest.approx(
                    [0., 0., .5 ** .5, .5 ** .5])
                assert [getattr(odom.twist.twist.linear, axis) for axis in 'xyz'] == pytest.approx([4., -1., 2.])
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and latest['fixture/telemetry/state']['data']['connected']:
                    executor.spin_once(timeout_sec=.02)
                assert latest['fixture/telemetry/state']['data']['armed'] is None
                assert latest['fixture/telemetry/state']['data']['connected'] is False
            finally:
                executor.shutdown()
                source.destroy_node()
                bridge.session.close()
                bridge.destroy_node()
        finally:
            rclpy.try_shutdown()
            subscriber.undeclare()
