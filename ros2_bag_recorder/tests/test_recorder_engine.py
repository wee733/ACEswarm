"""Run with system Python after sourcing a ROS 2 installation."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

try:
    import rclpy
    import rosbag2_py as bag
except ImportError:
    rclpy = None
else:
    import yaml
    from rcl_interfaces.msg import Log
    from rclpy.context import Context
    from rclpy.exceptions import InvalidTopicNameException
    from rclpy.node import Node
    from rclpy.serialization import deserialize_message
    from std_msgs.msg import String
    from recorder_engine import RecorderEngine
    from legacy_engine import RecorderEngine as PythonRecorderEngine
    from legacy_engine import (Durability, QoSProfile, Reliability,
                                 TopicCounter, session_name, subscriber_qos)


@unittest.skipUnless(rclpy, "source ROS 2 setup.bash")
class PolicyTests(unittest.TestCase):
    def test_per_topic_limits_and_rolling_rate(self):
        fast, slow = TopicCounter(0), TopicCounter(2)
        for now in (0, 100_000_000, 499_999_999, 500_000_000, 999_999_999, 1_000_000_000):
            for counter in (fast, slow):
                if counter.accepts(now):
                    counter.written(now, 10)
        self.assertEqual((slow.written_count, slow.dropped_count, fast.written_count), (3, 3, 6))
        self.assertEqual(slow.written_bytes, 30)
        self.assertEqual(slow.snapshot(1_000_000_000)["actual_hz"], 2)
        self.assertEqual(slow.snapshot(2_000_000_000)["actual_hz"], 1)
        self.assertEqual(slow.snapshot(6_000_000_000)["actual_hz"], 0)

    def test_compatible_qos(self):
        reliable = QoSProfile(depth=1, reliability=Reliability.RELIABLE,
                              durability=Durability.TRANSIENT_LOCAL)
        sensor = QoSProfile(depth=1, reliability=Reliability.BEST_EFFORT,
                            durability=Durability.VOLATILE)
        for offered, expected in (([], sensor), ([reliable], reliable),
                                  ([sensor], sensor), ([reliable, sensor], sensor)):
            actual = subscriber_qos(offered)
            self.assertEqual(actual.reliability, expected.reliability)
            self.assertEqual(actual.durability, expected.durability)

    def test_python_graph_skips_only_bad_entries(self):
        engine = PythonRecorderEngine.__new__(PythonRecorderEngine)
        engine._types = {}
        engine._node = Mock(spec=Node)
        good = ["/camera/image_raw", "/camera/_buf_cpu"]
        reserved = "/camera/_buf/" + "ab" * 16
        names = [*good, "/7_invalid", reserved, "/bad_publisher_query", "/bad_subscriber_query"]
        engine._node.get_topic_names_and_types.return_value = [(n, ["std_msgs/msg/String"]) for n in names]

        def publishers(name):
            self.assertIn(name, [*good, "/bad_publisher_query", "/bad_subscriber_query"])
            if name == "/bad_publisher_query":
                raise InvalidTopicNameException(name, "invalid in endpoint query", 0)
            return []

        def subscribers(name):
            if name == "/bad_subscriber_query":
                raise InvalidTopicNameException(name, "invalid in count query", 0)
            return 2

        engine._node.get_publishers_info_by_topic.side_effect = publishers
        engine._node.count_subscribers.side_effect = subscribers
        rows = engine._topics()
        self.assertEqual({r["name"] for r in rows}, set(good))
        self.assertTrue(all(r["type_available"] and r["subscribers"] == 2 for r in rows))
        engine._node.get_topic_names_and_types.side_effect = RuntimeError("graph unavailable")
        with self.assertRaisesRegex(RuntimeError, "graph unavailable"):
            engine._topics()

    def test_python_start_rejects_invalid_name_before_graph_lookup(self):
        engine = PythonRecorderEngine.__new__(PythonRecorderEngine)
        engine._session = None
        engine._node = Mock(spec=Node)
        with tempfile.TemporaryDirectory() as tmp:
            engine.root = Path(tmp)
            for name in ("/camera/_buf/" + "abc1" * 8, "/cam/9bad"):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "话题名无效"):
                    engine._start({"topics": [{"name": name}], "save_rosout": False})
            engine._node.get_topic_names_and_types.assert_not_called()
            self.assertEqual(list(engine.root.iterdir()), [])

    def test_session_component(self):
        for value in (".", "../outside", "nested/bag", "a\\b", "\r", "a"*121, "雾"*41):
            with self.subTest(value=value), self.assertRaises(ValueError):
                session_name(value)
        self.assertEqual(session_name("实验 01"), "实验 01")


@unittest.skipUnless(rclpy and shutil.which("ros2"), "source ROS 2 setup.bash")
class RosRecordingTests(unittest.TestCase):
    def setUp(self):
        domain = os.environ.get("RECORDER_TEST_ROS_DOMAIN_ID", str(30 + os.getpid() % 60))
        environment = patch.dict(os.environ, {"ROS_DOMAIN_ID": str(domain),
                                              "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST",
                                              "ROS_STATIC_PEERS": ""})
        environment.start()
        self.addCleanup(environment.stop)
        if os.environ.get("ROS_DISTRO") == "humble":
            localhost = patch.dict(os.environ, {"ROS_LOCALHOST_ONLY": "1"})
            localhost.start()
            self.addCleanup(localhost.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        context = Context()
        context.init(args=[])
        self.addCleanup(context.shutdown)
        self.node = Node("recorder_fixture", enable_rosout=False, context=context)
        self.addCleanup(self.node.destroy_node)
        self.topic = "/recorder_fixture/sample"
        qos = QoSProfile(depth=8, reliability=Reliability.RELIABLE,
                         durability=Durability.TRANSIENT_LOCAL)
        self.pub = self.node.create_publisher(String, self.topic, qos)
        self.sensor_name = "/recorder_fixture/sensor"
        self.sensor = self.node.create_publisher(
            String, self.sensor_name, QoSProfile(depth=8, reliability=Reliability.BEST_EFFORT,
                                                 durability=Durability.VOLATILE))
        self.log_pub = self.node.create_publisher(
            Log, "/rosout", QoSProfile(depth=1, reliability=Reliability.RELIABLE,
                                       durability=Durability.TRANSIENT_LOCAL))
        self.engine = RecorderEngine(self.root)
        self.addCleanup(self.engine.close)

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("ROS condition timed out")

    def test_native_scan_omits_buffer_gid_and_keeps_cpu_and_user_topics(self):
        base = self.topic + "/image_raw"
        transport = base + "/_buf/" + "abcd" * 8
        visible = {base, base + "/_buf_cpu", base + "/_buf/calibration"}
        for name in (transport, *sorted(visible)):
            self.node.create_publisher(String, name, 1)
        self.wait_for(lambda: transport in dict(self.node.get_topic_names_and_types()))
        self.wait_for(lambda: visible <= {row["name"] for row in self.engine.topics()})
        self.assertNotIn(transport, {row["name"] for row in self.engine.topics()})

    def test_live_record_close_and_ros2_bag_info(self):
        self.pub.publish(String(data="retained_before_recording"))
        self.wait_for(lambda: any(t["name"] == self.topic for t in self.engine.topics()))
        row = next(t for t in self.engine.topics() if t["name"] == self.topic)
        self.assertEqual((row["reliability"], row["durability"]), ("RELIABLE", "TRANSIENT_LOCAL"))
        self.assertTrue(row["type_available"])
        payload = dict(name="mcap_fixture", topics=[{"name": self.topic, "max_hz": 10}, {"name": self.sensor_name}],
                       notes="fixture", save_rosout=True, format="mcap", compression="zstd_fast",
                       split_seconds=1)
        self.engine.start(payload)
        with self.assertRaises(RuntimeError):
            self.engine.start(payload)
        self.wait_for(lambda: self.engine.status()["topics"][self.topic]["written_count"] > 0)
        self.wait_for(lambda: self.log_pub.get_subscription_count() == 2)
        log = Log(name="recorder_fixture", msg="log for fixture", level=Log.WARN)
        log.stamp = self.node.get_clock().now().to_msg()
        self.log_pub.publish(log)
        for i in range(75):
            self.pub.publish(String(data=str(i)))
            self.sensor.publish(String(data=str(i)))
            time.sleep(0.02)
        self.wait_for(lambda: any(r["message"] == log.msg for r in self.engine.logs()))
        status = self.engine.status()
        self.assertGreater(status["topics"][self.topic]["actual_hz"], 0)
        self.assertLessEqual(status["topics"][self.topic]["actual_hz"], 10)
        # Exercise service shutdown while a real writer is active.
        self.engine.close()
        folder = self.root / "mcap_fixture"
        summary = json.loads((folder / "session_meta.json").read_text())
        stats = summary["topics"][self.topic]
        self.assertGreater(stats["dropped_count"], 0)
        self.assertGreater(stats["written_count"], 1)
        self.assertGreater(stats["written_bytes"], stats["written_count"])
        self.assertGreater(summary["topics"][self.sensor_name]["written_count"], stats["written_count"])
        self.assertEqual(summary["notes"], "fixture")
        self.assertEqual(summary["error"], "")
        self.assertIn(log.msg, (folder / "rosout.log").read_text())
        row = json.loads((folder / "rosout.jsonl").read_text().strip().splitlines()[-1])
        self.assertEqual((row["node"], row["level"], row["message"]), (log.name, "WARN", log.msg))
        info = subprocess.run(["ros2", "bag", "info", str(folder)], check=True, text=True,
                              capture_output=True, timeout=20)
        self.assertIn(self.topic, info.stdout)
        self.assertIn("mcap", info.stdout)
        metadata = yaml.safe_load((folder / "metadata.yaml").read_text())["rosbag2_bagfile_information"]
        self.assertGreaterEqual(len(metadata["relative_file_paths"]), 2)
        self.assertEqual(metadata["message_count"], sum(t["written_count"] for t in summary["topics"].values()))
        reader = bag.SequentialReader()
        reader.open(bag.StorageOptions(uri=str(folder), storage_id="mcap"), bag.ConverterOptions("", ""))
        values = []
        while reader.has_next():
            name, raw, timestamp = reader.read_next()
            if name == self.topic:
                self.assertGreater(timestamp, 0)
                values.append(deserialize_message(raw, String).data)
        self.assertEqual(len(values), stats["written_count"])
        self.assertIn("retained_before_recording", values)

    def test_rosout_retained_live_and_text_with_bag_limit(self):
        self.wait_for(lambda: self.log_pub.get_subscription_count() == 1)
        for folder, hz in (("full_logs", 0), ("limited_logs", 0.01)):
            with self.subTest(folder=folder):
                retained = Log(name="fixture", level=Log.INFO, msg="retained_" + folder)
                self.log_pub.publish(retained)
                self.wait_for(lambda: any(t["name"] == "/rosout" and t["durability"] == "TRANSIENT_LOCAL"
                                          for t in self.engine.topics()))
                self.engine.start({"name": folder, "save_rosout": True,
                                   "topics": [{"name": "/rosout", "max_hz": hz}]})
                self.wait_for(lambda: self.engine.status()["topics"]["/rosout"]["written_count"] == 1)
                live = Log(name="fixture", level=Log.WARN, msg="live_" + folder)
                self.log_pub.publish(live)

                def received_both():
                    stats = self.engine.status()["topics"]["/rosout"]
                    return stats["written_count"] + stats["dropped_count"] == 2

                self.wait_for(received_both)
                self.engine.stop()
                path = self.root / folder
                sidecar = [json.loads(line)["message"]
                           for line in (path / "rosout.jsonl").read_text().splitlines()]
                self.assertEqual(sidecar, [retained.msg, live.msg])
                text = (path / "rosout.log").read_text().splitlines()
                self.assertEqual(len(text), 2)
                self.assertTrue(text[0].endswith(retained.msg))
                self.assertTrue(text[1].endswith(live.msg))
                reader = bag.SequentialReader()
                reader.open(bag.StorageOptions(uri=str(path), storage_id="mcap"),
                            bag.ConverterOptions("", ""))
                recorded = []
                while reader.has_next():
                    recorded.append(deserialize_message(reader.read_next()[1], Log).msg)
                self.assertEqual(recorded, sidecar if hz == 0 else [retained.msg])
                del reader

    def test_queue_overflow_is_distinct_from_throttling_and_drains_on_stop(self):
        self.engine.close()
        self.engine = RecorderEngine(self.root, buffer_mib=1)
        self.addCleanup(self.engine.close)
        self.wait_for(lambda: any(t["name"] == self.topic for t in self.engine.topics()))
        self.engine.start({"name": "overflow", "topics": [{"name": self.topic}], "save_rosout": False})
        self.wait_for(lambda: self.pub.get_subscription_count() > 0)
        self.pub.publish(String(data="X" * (1024 * 1024 + 1)))
        self.wait_for(lambda: self.engine.status()["topics"][self.topic]["queue_dropped_count"] == 1)
        self.pub.publish(String(data="survived"))
        self.wait_for(lambda: self.engine.status()["topics"][self.topic]["written_count"] == 1)
        state = self.engine.stop()
        self.assertEqual(state["engine"], "cpp")
        self.assertEqual(state["queue_bytes"], 0)
        self.assertLessEqual(state["peak_queue_bytes"], state["buffer_bytes"])
        self.assertEqual(state["topics"][self.topic]["dropped_count"], 0)
        meta = json.loads((self.root / "overflow" / "session_meta.json").read_text())
        self.assertEqual(meta["topics"][self.topic]["queue_dropped_count"], 1)
        reader = bag.SequentialReader()
        reader.open(bag.StorageOptions(uri=str(self.root / "overflow"), storage_id="mcap"),
                    bag.ConverterOptions("", ""))
        self.assertEqual(deserialize_message(reader.read_next()[1], String).data, "survived")
        self.assertFalse(reader.has_next())

    def test_interframe_video_requires_all_frames(self):
        from foxglove_msgs.msg import CompressedVideo
        name = "/recorder_fixture/video/h264"
        self.node.create_publisher(CompressedVideo, name, 10)
        self.wait_for(lambda: any(t["name"] == name for t in self.engine.topics()))
        with self.assertRaisesRegex(ValueError, "全速"):
            self.engine.start({"name": "video", "topics": [{"name": name, "max_hz": 5}],
                               "save_rosout": False})
        self.assertFalse((self.root / "video").exists())
        self.engine.start({"name": "video", "topics": [{"name": name}], "save_rosout": False})
        self.assertEqual(self.engine.stop()["error"], "")

    def test_sqlite_compression_and_retries(self):
        self.wait_for(lambda: any(t["name"] == self.topic for t in self.engine.topics()))
        existing = self.root / "partial"
        existing.mkdir()
        (existing / "original").write_text("preserve")
        with self.assertRaises(ValueError):
            self.engine.start({"name": "../outside"})
        with self.assertRaises(ValueError):
            self.engine.start({"name": existing.name})
        self.assertEqual((existing / "original").read_text(), "preserve")
        for mode in ("zstd_small", "zstd_fast", "none"):
            with self.subTest(mode=mode):
                self.engine.start({"name": mode, "format": "sqlite3", "compression": mode,
                                   "topics": [{"name": self.topic}], "save_rosout": False,
                                   "split_mb": 1})
                self.wait_for(lambda: self.pub.get_subscription_count() > 0)
                self.pub.publish(String(data="sqlite"))
                self.wait_for(lambda: self.engine.status()["topics"][self.topic]["written_count"] > 0)
                status = self.engine.stop()
                self.assertEqual(status["error"], "")
                folder = self.root / mode
                self.assertFalse((folder / "rosout.log").exists())
                info = yaml.safe_load((folder / "metadata.yaml").read_text())["rosbag2_bagfile_information"]
                self.assertEqual(info["compression_format"], "" if mode == "none" else "zstd")
                for filename in info["relative_file_paths"]:
                    self.assertTrue((folder / filename).is_file())
                reader = bag.SequentialReader() if mode == "none" else bag.SequentialCompressionReader()
                reader.open(bag.StorageOptions(uri=str(folder), storage_id="sqlite3"),
                            bag.ConverterOptions("", ""))
                self.assertTrue(reader.has_next())
                self.assertEqual(deserialize_message(reader.read_next()[1], String).data, "sqlite")
                del reader
        self.assertFalse(self.engine.stop()["recording"])


if __name__ == "__main__":
    unittest.main()
