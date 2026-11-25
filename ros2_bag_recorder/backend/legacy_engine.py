"""A passive ROS 2 recorder. One executor owns subscriptions and the bag writer."""

import json
import os
import queue
import shutil
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from recording_options import recordable_topic_name, selected_topics, session_name, _split, validate_video_rate

import rosbag2_py as bag
import yaml
from rcl_interfaces.msg import Log
from rclpy.context import Context
from rclpy.exceptions import InvalidTopicNameException
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import QoSDurabilityPolicy as Durability
from rclpy.qos import QoSProfile
from rclpy.qos import QoSReliabilityPolicy as Reliability
from rclpy.serialization import deserialize_message
from rclpy.type_support import check_for_type_support
from rosidl_runtime_py.utilities import get_message


SECOND = 1_000_000_000
LEVELS = {10: "DEBUG", 20: "INFO", 30: "WARN", 40: "ERROR", 50: "FATAL"}


def subscriber_qos(profiles):
    # Without a publisher, keep the subscription compatible with future sensors.
    reliable = profiles and all(p.reliability == Reliability.RELIABLE for p in profiles)
    latched = profiles and all(p.durability == Durability.TRANSIENT_LOCAL for p in profiles)
    return QoSProfile(depth=10,
                      reliability=Reliability.RELIABLE if reliable else Reliability.BEST_EFFORT,
                      durability=Durability.TRANSIENT_LOCAL if latched else Durability.VOLATILE)


def _topic_metadata(name, type_name, profiles):
    # Humble stores YAML in TopicMetadata; Jazzy+ stores a vector of rclcpp QoS.
    encoded = []
    for q in profiles:
        item = {k: int(getattr(q, k)) for k in
                ("history", "depth", "reliability", "durability", "liveliness")}
        for key in ("deadline", "lifespan", "liveliness_lease_duration"):
            sec, nsec = divmod(getattr(q, key).nanoseconds, SECOND)
            item[key] = {"sec": sec, "nsec": nsec}
        item["avoid_ros_namespace_conventions"] = q.avoid_ros_namespace_conventions
        if item not in encoded:
            encoded.append(item)
    qos_yaml = yaml.safe_dump(encoded)
    options = dict(name=name, type=type_name, serialization_format="cdr")
    if hasattr(bag.TopicMetadata, "id"):
        options.update(id=0, offered_qos_profiles=bag.to_rclcpp_qos_vector(qos_yaml, 5))
    else:
        options["offered_qos_profiles"] = qos_yaml
    return bag.TopicMetadata(**options)


@dataclass
class TopicCounter:
    max_hz: float
    last_ns: int = None
    written_count: int = 0
    dropped_count: int = 0
    written_bytes: int = 0
    buckets: deque = field(default_factory=lambda: deque(maxlen=16))

    def accepts(self, now):
        if self.max_hz > 0 and self.last_ns is not None and (now - self.last_ns) * self.max_hz < SECOND:
            self.dropped_count += 1
            return False
        return True

    def written(self, now, size):
        self.last_ns = now
        self.written_count += 1
        self.written_bytes += size
        bucket = now // 250_000_000
        if self.buckets and self.buckets[-1][0] == bucket:
            self.buckets[-1][1] += 1
        else:
            self.buckets.append([bucket, 1, now])

    def snapshot(self, now):
        cutoff = now // 250_000_000 - 15
        while self.buckets and self.buckets[0][0] < cutoff:
            self.buckets.popleft()
        actual_hz = 0.0
        if self.buckets:
            count = sum(bucket[1] for bucket in self.buckets)
            elapsed = now - self.buckets[0][2]
            if elapsed > 0:
                actual_hz = (count - 1) * SECOND / elapsed
        return {"max_hz": self.max_hz, "written_count": self.written_count,
                "dropped_count": self.dropped_count, "written_bytes": self.written_bytes,
                "actual_hz": round(actual_hz, 2)}


@dataclass
class Session:
    path: Path
    notes: str
    format: str
    compression: str
    split_mb: int
    split_seconds: int
    save_rosout: bool
    requested_topics: list
    start_ns: int = field(default_factory=time.monotonic_ns)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    counters: dict = field(default_factory=dict)
    subscriptions: list = field(default_factory=list)
    writer: object = None
    log_file: object = None
    json_file: object = None
    mcap_config: object = None
    sampled_ns: int = 0
    bytes_on_disk: int = 0
    rate_bps: float = 0


class RecorderEngine:
    def __init__(self, output_dir, use_sim_time=False):
        self.root = Path(output_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._session = None
        self._last = {"name": "", "notes": "", "elapsed_seconds": 0, "size_bytes": 0,
                      "rate_bps": 0, "topics": {}, "error": ""}
        self._logs = deque(maxlen=1000)
        self._log_id = 0
        self._types = {}
        self._calls = queue.Queue()
        self._gate = threading.Lock()
        self._accepting = True
        self._exiting = False
        self._context = Context()
        self._context.init(args=[])
        self._node = Node("ros2_bag_recorder", context=self._context, enable_rosout=False,
                          parameter_overrides=[Parameter("use_sim_time", value=use_sim_time)])
        self._executor = SingleThreadedExecutor(context=self._context)
        self._executor.add_node(self._node)
        self._guard = self._node.create_guard_condition(self._dispatch)
        self._node.create_subscription(Log, "/rosout", self._on_log, subscriber_qos([]))
        self._thread = threading.Thread(target=self._spin, name="rosbag-executor")
        self._thread.start()

    def _spin(self):
        try:
            while not self._exiting:
                self._executor.spin_once()
        finally:
            with self._gate:
                self._accepting = False
                while not self._calls.empty():
                    _, future = self._calls.get_nowait()
                    future.set_exception(RuntimeError("ROS executor stopped"))
            try:
                if self._session:
                    self._finish()
            finally:
                self._executor.shutdown()
                self._node.destroy_node()
                self._context.shutdown()

    def _dispatch(self):
        while not self._calls.empty():
            operation, future = self._calls.get_nowait()
            try:
                future.set_result(operation())
            except Exception as exc:
                future.set_exception(exc)

    def _submit(self, operation):
        future = Future()
        with self._gate:
            if not self._accepting:
                raise RuntimeError("ROS 录制器已关闭")
            self._calls.put((operation, future))
            self._guard.trigger()
        return future.result()

    def close(self):
        def shutdown():
            self._exiting = True
            if self._session:
                self._finish()

        future = None
        with self._gate:
            if self._accepting:
                self._accepting = False
                future = Future()
                self._calls.put((shutdown, future))
                self._guard.trigger()
        self._thread.join()
        if future:
            future.result()

    def _type(self, type_name):
        if type_name not in self._types:
            try:
                message = get_message(type_name)
                check_for_type_support(message)
            # Generated message packages can raise distro-specific type-support
            # exceptions if their compiled extension or one of its libraries is absent.
            except Exception:
                message = None
            self._types[type_name] = message
        return self._types[type_name]

    def topics(self):
        return self._submit(self._topics)

    def _topics(self):
        rows = []
        graph = self._node.get_topic_names_and_types()
        for name, types in sorted((name, types) for name, types in graph if recordable_topic_name(name)):
            try:
                endpoints = self._node.get_publishers_info_by_topic(name)
                subscribers = self._node.count_subscribers(name)
            except InvalidTopicNameException:
                continue
            qos = subscriber_qos([e.qos_profile for e in endpoints])
            rows.append({
                "name": name, "type": ", ".join(types), "types": list(types),
                "publishers": len(endpoints), "subscribers": subscribers,
                "reliability": qos.reliability.name, "durability": qos.durability.name,
                "publisher_qos": [{"node": e.node_name, "namespace": e.node_namespace,
                                   "reliability": e.qos_profile.reliability.name,
                                   "durability": e.qos_profile.durability.name} for e in endpoints],
                "type_available": len(types) == 1 and self._type(types[0]) is not None,
            })
        return rows

    def start(self, payload):
        return self._submit(partial(self._start, payload))

    def _start(self, payload):
        if self._session:
            raise RuntimeError("已有录制正在进行")
        if not isinstance(payload, dict):
            raise ValueError("录制配置必须为对象")
        path = self.root / session_name(payload.get("name"))
        if os.path.lexists(path):
            raise ValueError("同名录包已存在")
        fmt, mode = payload.get("format", "mcap"), payload.get("compression", "none")
        if fmt not in ("mcap", "sqlite3") or mode not in ("none", "zstd_fast", "zstd_small"):
            raise ValueError("不支持该存储或压缩格式")
        size = _split(payload.get("split_mb", 0), 1024 * 1024)
        duration = _split(payload.get("split_seconds", 0), 1)
        save_logs = payload.get("save_rosout", True)
        notes = payload.get("notes", "")
        if not isinstance(notes, str) or type(save_logs) is not bool:
            raise ValueError("备注须为文本，日志开关须为布尔值")
        selected = selected_topics(payload.get("topics", []))
        requested = list(selected)
        if save_logs:
            selected.setdefault("/rosout", 0.0)
        if not selected:
            raise ValueError("请至少选择一个话题或开启日志记录")

        graph = dict(self._node.get_topic_names_and_types())
        subscriptions = []
        for name, hz in selected.items():
            names = graph.get(name, [])
            if len(names) != 1 or self._type(names[0]) is None:
                raise ValueError("话题离线、类型冲突或缺少消息 overlay：" + name)
            validate_video_rate(name, names[0], hz)
            profiles = [e.qos_profile for e in self._node.get_publishers_info_by_topic(name)]
            subscriptions.append((name, hz, self._type(names[0]), subscriber_qos(profiles),
                                  _topic_metadata(name, names[0], profiles)))

        session = Session(path, notes, fmt, mode, size // (1024 * 1024), duration, save_logs, requested)
        self._session = session
        self._last["error"] = ""
        try:
            self._open_writer(session, size, duration)
            for name, hz, msg_type, qos, metadata in subscriptions:
                session.writer.create_topic(metadata)
                session.counters[name] = TopicCounter(hz)
                sub = self._node.create_subscription(
                    msg_type, name, self._raw_callback(session, name), qos, raw=True)
                session.subscriptions.append(sub)
            if save_logs:
                session.log_file = (path / "rosout.log").open("x", encoding="utf-8", buffering=1)
                session.json_file = (path / "rosout.jsonl").open("x", encoding="utf-8", buffering=1)
        except Exception as exc:
            self._finish(error=str(exc))
            raise
        return self._status()

    @staticmethod
    def _open_writer(session, size, duration):
        options = bag.StorageOptions(uri=str(session.path), storage_id=session.format,
                                     max_bagfile_size=size, max_bagfile_duration=duration)
        writer = bag.SequentialWriter()
        if session.format == "mcap":
            if session.compression == "none":
                # MCAP writer defaults vary; explicitly request uncompressed chunks.
                session.mcap_config = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml")
                session.mcap_config.write('compression: "None"\n')
                session.mcap_config.flush()
                options.storage_config_uri = session.mcap_config.name
            else:
                options.storage_preset_profile = session.compression
        elif session.compression != "none":
            compression_mode = (bag.CompressionMode.FILE if session.compression == "zstd_small"
                                else bag.CompressionMode.MESSAGE)
            writer = bag.SequentialCompressionWriter(bag.CompressionOptions(
                compression_format="zstd", compression_mode=compression_mode,
                compression_queue_size=0, compression_threads=1))
        writer.open(options, bag.ConverterOptions("", ""))
        session.writer = writer

    def _raw_callback(self, session, name):
        # rclpy checks the callback signature; bind the topic outside the callback.
        def receive(raw):
            if self._session is not session:
                return
            now = time.monotonic_ns()
            counter = session.counters[name]
            try:
                if counter.accepts(now):
                    session.writer.write(name, raw, self._node.get_clock().now().nanoseconds)
                    counter.written(now, len(raw))
                if name == "/rosout" and session.save_rosout:
                    # Use this session's DDS delivery, including retained samples.
                    # Only rosbag storage is throttled; text logs keep every delivery.
                    sequence = counter.written_count + counter.dropped_count
                    row = self._log_row(deserialize_message(raw, Log), sequence)
                    session.log_file.write("[{stamp}] [{level}] [{node}] {message}\n".format(**row))
                    session.json_file.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception as exc:
                self._finish(error=str(exc))
        return receive

    @staticmethod
    def _log_row(msg, sequence):
        return {"id": sequence, "stamp": f"{msg.stamp.sec}.{msg.stamp.nanosec:09d}",
                "level": LEVELS.get(msg.level, str(msg.level)), "node": msg.name,
                "message": msg.msg, "file": msg.file, "function": msg.function, "line": msg.line}

    def _on_log(self, msg):
        self._log_id += 1
        self._logs.append(self._log_row(msg, self._log_id))

    def logs(self, since=0):
        return self._submit(lambda: [row for row in self._logs if row["id"] > since])

    @staticmethod
    def _sample(session, now, force):
        if not force and now - session.sampled_ns < 500_000_000:
            return
        size = 0
        if session.path.is_dir():
            for entry in os.scandir(session.path):
                try:
                    if entry.is_file(follow_symlinks=False):
                        size += entry.stat(follow_symlinks=False).st_size
                except FileNotFoundError:
                    pass   # A compression worker can replace a finished split.
        elapsed = (now - (session.sampled_ns or session.start_ns)) / SECOND
        session.rate_bps = max(0, (size - session.bytes_on_disk) / max(0.001, elapsed))
        session.sampled_ns, session.bytes_on_disk = now, size

    def _snapshot(self, session, force=False):
        now = time.monotonic_ns()
        self._sample(session, now, force)
        return {"name": session.path.name, "notes": session.notes,
                "started_at": session.started_at, "elapsed_seconds": (now - session.start_ns) / SECOND,
                "format": session.format, "compression": session.compression,
                "split_mb": session.split_mb, "split_seconds": session.split_seconds,
                "save_rosout": session.save_rosout, "requested_topics": session.requested_topics,
                "size_bytes": session.bytes_on_disk, "rate_bps": round(session.rate_bps),
                "topics": {name: counter.snapshot(now)
                           for name, counter in session.counters.items()}, "error": ""}

    def _status(self):
        data = self._snapshot(self._session) if self._session else self._last.copy()
        data.update(engine="python", recording=self._session is not None, free_bytes=shutil.disk_usage(self.root).free)
        return data

    def status(self):
        return self._submit(self._status)

    def stop(self):
        return self._submit(self._finish)

    def _finish(self, error=""):
        session = self._session
        if session is None:
            return self._status()
        self._session = None
        errors = [error] if error else []
        for sub in session.subscriptions:
            try:
                self._node.destroy_subscription(sub)
            except Exception as exc:
                errors.append(str(exc))
        for stream in (session.log_file, session.json_file):
            if stream:
                try:
                    stream.close()
                except OSError as exc:
                    errors.append(str(exc))
        opened = session.writer is not None
        try:
            if hasattr(session.writer, "close"):
                session.writer.close()
        except Exception as exc:
            errors.append(str(exc))
        finally:
            # Some rosbag2 Python bindings (including the compression writer) only
            # finalize in the C++ destructor. Drop its sole reference before metadata.
            session.writer = None
            if session.mcap_config:
                session.mcap_config.close()
        self._last = self._snapshot(session, force=True) if opened else self._last.copy()
        self._last.update(rate_bps=0, error="；".join(errors))
        if opened:
            try:
                with (session.path / ".session_meta.tmp").open("x", encoding="utf-8") as stream:
                    json.dump(self._last, stream, ensure_ascii=False, indent=2)
                os.replace(session.path / ".session_meta.tmp", session.path / "session_meta.json")
                if not (session.path / "metadata.yaml").is_file():
                    raise OSError("未生成 metadata.yaml")
            except OSError as exc:
                self._last["error"] = "；".join([*errors, str(exc)])
        if self._last["error"] and not error:
            raise RuntimeError(self._last["error"])
        return self._status()
