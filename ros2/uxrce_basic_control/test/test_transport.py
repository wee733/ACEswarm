"""Loopback-only GET -> DDS -> ACK -> GET through the production node."""
import json
import socket
from concurrent.futures import ThreadPoolExecutor
import time

from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import qos_profile_sensor_data
import rclpy
from px4_msgs.msg import VehicleCommand, VehicleCommandAck, VehicleStatus
import zenoh

from uxrce_basic_control.node import BasicControl, topic_for


def test_ack_roundtrip_and_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv('ROS_DOMAIN_ID', '214')
    monkeypatch.setenv('ROS_AUTOMATIC_DISCOVERY_RANGE', 'LOCALHOST')
    monkeypatch.setenv('ROS_STATIC_PEERS', '')
    monkeypatch.setenv('ROS_HOME', str(tmp_path))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        endpoint = f'tcp/127.0.0.1:{sock.getsockname()[1]}'
    cfg = dict(mode='peer', listen={'endpoints': [endpoint]},
               scouting={'multicast': {'enabled': False}, 'gossip': {'enabled': False}})
    with zenoh.open(zenoh.Config.from_json5(json.dumps(cfg))) as session:
        cfg.update(mode='client', listen={'endpoints': []}, connect={'endpoints': [endpoint]})
        path = tmp_path / 'zenoh.json'
        path.write_text(json.dumps(cfg))
        rclpy.init(args=['--ros-args', '-p', 'agent_id:=control_fixture',
                         '-p', 'goto_fence_shape:=rectangle', '-p', 'field_east_size_m:=50.0',
                         '-p', 'field_north_size_m:=30.0',
                         '-p', 'px4_namespace:=control_fixture', '-p', f'zenoh_config:={path}'])
        ctrl, source = BasicControl(), rclpy.create_node('fixture_autopilot')
        executor = SingleThreadedExecutor()
        executor.add_node(ctrl)
        executor.add_node(source)
        try:
            def topic(direction, name, cls):
                return topic_for('control_fixture', direction, name, cls)

            ack_pub = source.create_publisher(VehicleCommandAck, topic('out', 'vehicle_command_ack', VehicleCommandAck),
                                              qos_profile_sensor_data)
            statuses = source.create_publisher(VehicleStatus, topic('out', 'vehicle_status', VehicleStatus),
                                               qos_profile_sensor_data)
            seen = []
            def receive(command):
                seen.append(command)
                if command.command == 176:
                    ack_pub.publish(VehicleCommandAck(timestamp=command.timestamp + 10, command=176, result=2,
                                                      target_system=command.source_system,
                                                      target_component=command.source_component))

            source.create_subscription(VehicleCommand, topic('in', 'vehicle_command', VehicleCommand), receive,
                                       qos_profile_sensor_data)
            boot_origin = time.monotonic() - 60.
            def publish_status():
                # Simulate UXRCE_DDS_SYNCT=0 on a Jetson whose ROS clock uses epoch time.
                stamp = int((time.monotonic() - boot_origin) * 1_000_000)
                statuses.publish(VehicleStatus(timestamp=stamp, system_id=42, component_id=1))

            source.create_timer(.05, publish_status)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                executor.spin_once(timeout_sec=.01)
                if (ctrl.cmd.get_subscription_count() and ctrl.ack.get_publisher_count()
                        and 'vehicle_status' in ctrl.ctrl.samples):
                    break
            assert 'vehicle_status' in ctrl.ctrl.samples

            def get(key, payload):
                replies = list(session.get('control_fixture/control/' + key, payload=payload, timeout=4))
                assert len(replies) == 1
                return json.loads(replies[0].ok.payload.to_string())

            with ThreadPoolExecutor(max_workers=1) as worker:
                for action, payload, expected in [
                        ('goto', '{"target":"invalid"}', None), ('goto', '{"target":"-12.5 0 1.5"}', None),
                        ('goto', '{"target":"0 15.1 1.5"}', None),
                        ('arm', '{}', None), ('set_mode', '{"mode":"AUTO.LOITER"}', 2), ('disarm', '{}', None)]:
                    future = worker.submit(get, action, payload)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and not future.done():
                        executor.spin_once(timeout_sec=.005)
                    response = future.result(timeout=.2)
                    assert response['ok'] is False and response['result_code'] == expected
                    assert response['command'] == action
                    if payload == '{"target":"-12.5 0 1.5"}':
                        assert 'armed vehicle' in response['message']
                    if payload == '{"target":"0 15.1 1.5"}':
                        assert 'outside field' in response['message']
            assert [(m.command, m.target_system, m.target_component) for m in seen] == [(176, 42, 1), (400, 42, 1)]
            assert all(60_000_000 <= m.timestamp < 70_000_000 for m in seen)
            assert seen[0].param1 == 1 and seen[0].param2 == 4 and seen[0].param3 == 3
            assert seen[1].param1 == seen[1].param2 == 0
            assert 'ACK' in response['message']
        finally:
            executor.shutdown()
            ctrl.close()
            ctrl.destroy_node()
            source.destroy_node()
            rclpy.try_shutdown()
