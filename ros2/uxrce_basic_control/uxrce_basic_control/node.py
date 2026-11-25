"""PX4 uXRCE-DDS control endpoint. Incoming Zenoh GETs enter the ROS executor."""
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import json
import math
import os
from queue import Empty, Full, Queue
import re
import socket
import threading
import time

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from px4_msgs import msg as px4
import zenoh

from .controller import ACTIONS, Controller, Request, reply
from .extra_mode import ExtraMode


def topic_for(namespace, direction, name, msg_type):
    version = getattr(msg_type, 'MESSAGE_VERSION', 0)
    parts = (namespace.strip('/'), 'fmu', direction, name + (f'_v{version}' if version else ''))
    return '/' + '/'.join(filter(None, parts))


class BasicControl(Node):
    def __init__(self):
        super().__init__('uxrce_basic_control')

        def param(name, default):
            return self.declare_parameter(name, default).value

        self.agent = re.sub(r'\.local$', '', param('agent_id', '') or os.getenv('AIVUDA_OS_AVAHI_HOSTNAME')
                            or socket.gethostname())
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+', self.agent):
            raise ValueError('agent_id must be one concrete Zenoh path segment')
        namespace = param('px4_namespace', '')
        climb = param('climb_rate_m_s', 0.5)
        height = param('max_takeoff_height_m', 10.0)
        if not math.isfinite(climb) or not 0 < climb <= 2. or not math.isfinite(height) or height <= 0:
            raise ValueError('climb_rate_m_s must be in (0, 2]; max_takeoff_height_m must be finite and positive')
        names = ('VehicleStatus', 'VehicleCommand', 'VehicleCommandAck')
        versions = tuple(getattr(getattr(px4, name), 'MESSAGE_VERSION', 0) for name in names)
        if versions not in ((5, 0, 0), (5, 1, 2)):
            raise ValueError(f'Unsupported px4_msgs {dict(zip(names, versions))}; expected (5,0,0) or (5,1,2)')
        self.get_logger().info('PX4 v1.17/main • ' + ', '.join(f'{n} v{v}' for n, v in zip(names, versions)))
        self.ctrl = Controller(self, climb, height, param('xy_speed_m_s', 0.5),
                               param('max_xy_radius_m', 3.0), param('min_goto_height_m', 0.3),
                               goto_fence_shape=param('goto_fence_shape', 'circle'),
                               field_east_size_m=param('field_east_size_m', 50.0),
                               field_north_size_m=param('field_north_size_m', 30.0))

        def publisher(name, kind):
            return self.create_publisher(kind, topic_for(namespace, 'in', name, kind), qos_profile_sensor_data)

        def subscribe(name, kind, callback=None):
            if callback is None:
                def callback(m):
                    self.ctrl.receive(name, m, time.monotonic())
            return self.create_subscription(kind, topic_for(namespace, 'out', name, kind),
                                            callback, qos_profile_sensor_data)

        self.cmd = publisher('vehicle_command', px4.VehicleCommand)
        self.offboard = publisher('offboard_control_mode', px4.OffboardControlMode)
        self.setpoint = publisher('trajectory_setpoint', px4.TrajectorySetpoint)
        self.ack = subscribe('vehicle_command_ack', px4.VehicleCommandAck,
                             lambda m: self.ctrl.acknowledge(m, time.monotonic()))
        # ROS retains these subscriptions for the lifetime of the node.
        subscribe('vehicle_status', px4.VehicleStatus)
        subscribe('vehicle_local_position', px4.VehicleLocalPosition)
        subscribe('vehicle_land_detected', px4.VehicleLandDetected)
        self.extra = ExtraMode(namespace)
        self.incoming = Queue(maxsize=8)
        self.register_lock = threading.Lock()
        self.register_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='px4-registration')
        path = param('zenoh_config', '')
        self.session = zenoh.open(zenoh.Config.from_file(path) if path else zenoh.Config())
        self.queryables = []
        for action in ACTIONS:
            key = f'{self.agent}/control/{action}'
            self.queryables.append(self.session.declare_queryable(key, partial(self.on_query, action, key),
                                                                  complete=True))
        self.create_timer(.05, self.tick)
        self.get_logger().info(f'Serving {self.agent}/control/* • {self.cmd.topic_name}, {self.ack.topic_name}')

    def on_query(self, action, key, query):
        def respond(body):
            try:
                query.reply(key, json.dumps(body, ensure_ascii=False, allow_nan=False),
                            encoding=zenoh.Encoding.APPLICATION_JSON)
            except zenoh.ZError as exc:
                self.get_logger().warning(f'{action} reply delivery failed: {exc}')
            finally:
                query.drop()

        if str(query.key_expr) != key:
            respond(reply(action, 'A concrete agent and control key are required'))
            return
        try:
            payload = json.loads(query.payload.to_bytes()) if query.payload is not None else {}
            if not isinstance(payload, dict):
                raise ValueError('Payload must be a JSON object')
            self.incoming.put_nowait(Request(action, payload, respond))
        except (ValueError, UnicodeError, RecursionError) as exc:
            respond(reply(action, str(exc)))
        except Full:
            respond(reply(action, 'Busy / 指令队列已满'))

    def registered_id(self):
        # Registration may wait on PX4 for several seconds. Never wait on its lock
        # in the ROS executor that publishes the OFFBOARD heartbeat and receives ACKs.
        return None if self.register_lock.locked() else self.extra.registered_id()

    def register(self, req):
        try:
            req.respond(self.extra.register())
        except Exception as exc:
            req.respond(reply(req.name, f'External mode registration failed: {exc}'))
        finally:
            self.register_lock.release()

    def tick(self):
        for _ in range(self.incoming.maxsize):
            try:
                req = self.incoming.get_nowait()
            except Empty:
                break
            if req.name == 'register_extra':
                if self.register_lock.acquire(blocking=False):
                    self.register_worker.submit(self.register, req)
                else:
                    req.respond(reply(req.name, 'External mode registration is in progress'))
            else:
                self.ctrl.request(req, time.monotonic())
        self.ctrl.tick(time.monotonic())

    def check_offboard(self):
        if self.offboard.get_subscription_count() == 0 or self.setpoint.get_subscription_count() == 0:
            raise ValueError('PX4 OFFBOARD input unavailable / 请检查 DDS 定位设定值和控制模式端点')

    def send(self, command, params, status, stamp):
        if self.cmd.get_subscription_count() == 0 or self.ack.get_publisher_count() == 0:
            candidates = sorted(t for t, _ in self.get_topic_names_and_types()
                                if re.search(r'/fmu/(?:in/vehicle_command|out/vehicle_command_ack)(?:_v\d+)?$', t))
            raise ValueError(f'PX4 command/ACK endpoints unavailable. Expected {self.cmd.topic_name}, '
                             f'{self.ack.topic_name}; discovered {candidates}')
        if not status.system_id or not status.component_id:
            raise ValueError('PX4 has not advertised a concrete system/component ID')
        message = px4.VehicleCommand()
        message.timestamp = stamp
        message.command = command
        message.target_system, message.target_component = status.system_id, status.component_id
        # MAV_COMP_ID_ONBOARD_COMPUTER3 distinguishes these ACKs from GCS/other controllers.
        message.source_system, message.source_component = status.system_id, 193
        message.from_external = True
        for i, value in enumerate(params, start=1):
            setattr(message, f'param{i}', float(value))
        self.cmd.publish(message)
        return message.source_system, message.source_component

    def publish_hold(self, hold, stamp):
        heartbeat = px4.OffboardControlMode(timestamp=stamp, position=True)
        point = px4.TrajectorySetpoint(timestamp=stamp, position=[hold.north, hold.east, hold.down],
                                       velocity=[math.nan]*3, acceleration=[math.nan]*3, jerk=[math.nan]*3,
                                       yaw=hold.yaw, yawspeed=math.nan)
        self.offboard.publish(heartbeat)
        self.setpoint.publish(point)

    def report(self, message):
        self.get_logger().warning(message)

    def close(self):
        for q in self.queryables:
            q.undeclare()
        self.register_worker.shutdown(wait=True)
        self.extra.close()
        if self.ctrl.pending:
            req = self.ctrl.pending
            req.respond(reply(req.name, 'Controller stopped / 控制服务已停止'))
        while not self.incoming.empty():
            req = self.incoming.get_nowait()
            req.respond(reply(req.name, 'Controller stopped / 控制服务已停止'))
        self.session.close()


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = BasicControl()
        rclpy.spin(node)
    except (ExternalShutdownException, KeyboardInterrupt):
        pass
    finally:
        if node:
            node.close()
            node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
