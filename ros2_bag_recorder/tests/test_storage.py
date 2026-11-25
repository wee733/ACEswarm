import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from bag_manager import BagManager
from preset_store import PresetStore
from recording_options import normalize, recordable_topic_name


class BagHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "recordings"
        self.manager = BagManager(self.root)
        self.bag = self.root / "session"
        self.bag.mkdir()
        self.metadata = {
            "storage_identifier": "mcap", "relative_file_paths": ["session_0.mcap"],
            "starting_time": {"nanoseconds_since_epoch": 1700000000000000000},
            "duration": {"nanoseconds": 2100000000}, "message_count": 3,
            "topics_with_message_count": [
                {"topic_metadata": {"name": "/test", "type": "std_msgs/msg/String"},
                 "message_count": 3}],
        }
        self.write_metadata()
        (self.bag / "session_0.mcap").write_bytes(b"fixture payload")
        (self.bag / "session_meta.json").write_text(json.dumps({"notes": "实验 1"}))
        (self.bag / "rosout.log").write_text("fixture log\n")
        self.secret = self.base / "outside.log"
        self.secret.write_text("private data")

    def write_metadata(self):
        (self.bag / "metadata.yaml").write_text(yaml.safe_dump({
            "rosbag2_bagfile_information": self.metadata
        }))

    def test_history_reads_standard_metadata_and_text_note(self):
        detail = self.manager.detail("session")
        self.assertEqual(detail["storage_id"], "mcap")
        self.assertEqual(detail["note"], "实验 1")
        self.assertEqual(detail["topic_count"], 1)
        self.assertEqual(detail["topics"], [{"name": "/test", "type": "std_msgs/msg/String", "count": 3}])
        self.assertEqual(detail["message_count"], 3)
        self.assertEqual(detail["duration_sec"], 2.1)
        self.assertTrue(detail["start_time"].startswith("2023-11-14"))
        self.assertEqual(detail["size_bytes"], sum(p.stat().st_size for p in self.bag.iterdir()))
        self.assertEqual(detail["path"], str(self.bag))
        self.assertEqual(self.manager.list()[0]["name"], "session")
        (self.bag / "session_meta.json").unlink()
        self.assertEqual(self.manager.detail("session")["note"], "")

    def test_stream_file_and_archive(self):
        for filename in ("session_0.mcap", "rosout.log"):
            with self.manager.download("session", filename) as attachment:
                output = io.BytesIO()
                attachment.write_to(output)
                self.assertEqual(output.getvalue(), (self.bag / filename).read_bytes())
                self.assertEqual(attachment.size, len(output.getvalue()))
        self.secret_as_link("rosout.jsonl")
        with self.manager.download("session") as attachment:
            self.assertEqual(attachment.filename, "session.tar.gz")
            self.assertIsNone(attachment.size)
            output = io.BytesIO()
            attachment.write_to(output)
        output.seek(0)
        with tarfile.open(fileobj=output, mode="r:gz") as archive:
            self.assertEqual(set(archive.getnames()), {
                "session/session_0.mcap", "session/metadata.yaml",
                "session/rosout.log", "session/session_meta.json",
            })
            self.assertEqual(archive.extractfile("session/session_0.mcap").read(), b"fixture payload")

    def secret_as_link(self, name):
        (self.bag / name).symlink_to(self.secret)

    def test_path_and_link_safety(self):
        (self.root / "alias").symlink_to(self.bag, target_is_directory=True)
        self.secret_as_link("rosout.jsonl")
        os.mkfifo(self.bag / "pipe")
        self.metadata["relative_file_paths"].append("pipe")
        self.write_metadata()
        for name in ("..", ".", "../recordings/session", "/tmp", "a\\b", "alias"):
            with self.subTest(name=name), self.assertRaises((OSError, ValueError)):
                self.manager.delete(name)
        for filename in ("../outside.log", "/etc/passwd", "rosout.jsonl", "pipe"):
            with self.subTest(filename=filename), self.assertRaises((OSError, ValueError)):
                with self.manager.download("session", filename):
                    self.fail("unsafe file offered for download")
        self.metadata["relative_file_paths"] = ["../outside.log"]
        self.write_metadata()
        with self.assertRaises(ValueError):
            with self.manager.download("session"):
                self.fail("unsafe archive offered for download")
        self.assertTrue(self.secret.exists())

    def test_nested_file_and_symlink_swap(self):
        nested = self.bag / "shards"
        nested.mkdir()
        data = nested / "part.mcap"
        data.write_bytes(b"recorded")
        self.metadata["relative_file_paths"] = ["shards/part.mcap"]
        self.write_metadata()
        with self.manager.download("session", "shards/part.mcap") as download:
            data.unlink()
            data.symlink_to(self.secret)
            output = io.BytesIO()
            download.write_to(output)
            self.assertEqual(output.getvalue(), b"recorded")
        data.unlink()
        nested.rmdir()
        nested.symlink_to(self.base, target_is_directory=True)
        self.metadata["relative_file_paths"] = ["shards/outside.log"]
        self.write_metadata()
        with self.assertRaises(FileNotFoundError):
            with self.manager.download("session", "shards/outside.log"):
                self.fail("symlink directory followed")

    def test_active_and_downloading_sessions_cannot_be_deleted(self):
        self.manager.protect("session")
        self.assertEqual(self.manager.list(), [])
        with self.assertRaises(RuntimeError):
            self.manager.detail("session")
        with self.assertRaises(RuntimeError):
            self.manager.delete("session")
        with self.assertRaises(RuntimeError):
            with self.manager.download("session"):
                self.fail("active download permitted")
        self.manager.unprotect("session")
        with self.manager.download("session"):
            with self.assertRaises(RuntimeError):
                self.manager.delete("session")
            with self.assertRaises(RuntimeError):
                self.manager.protect("session")
        self.secret_as_link("rosout.jsonl")
        self.manager.delete("session")
        self.assertFalse(self.bag.exists())
        self.assertEqual(self.secret.read_text(), "private data")

    def test_unrelated_and_partial_directories_are_ignored(self):
        (self.root / "open_session").mkdir()
        corrupt = self.root / "broken_session"
        corrupt.mkdir()
        (corrupt / "metadata.yaml").write_text("another: document")
        self.assertEqual([bag["name"] for bag in self.manager.list()], ["session"])
        with self.assertRaises(FileNotFoundError):
            self.manager.delete("open_session")
        with self.assertRaises(ValueError):
            self.manager.delete("broken_session")


class PresetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / ".presets.json"
        self.store = PresetStore(self.path)

    def test_preset_save_load_update_delete(self):
        self.assertEqual(self.store.list(), [])
        topics = [{"name": "/test", "max_hz": 2.5}, {"name": "/test2", "max_hz": -1}]
        preset = self.store.save("试验 A", topics)
        self.assertEqual(preset["topics"][1]["max_hz"], 0)
        self.assertEqual(PresetStore(self.path).list(), [preset])
        replacement = self.store.save("试验 A", [{"name": "/topic"}])
        self.assertEqual(self.store.list(), [replacement])
        self.store.delete("试验 A")
        self.assertEqual(self.store.list(), [])
        with self.assertRaises(FileNotFoundError):
            self.store.delete("missing")

    def test_invalid_preset_and_failed_replace_preserve_previous_data(self):
        saved = self.store.save("working", [{"name": "/synthetic", "max_hz": 2}])
        for invalid in (float("nan"), float("inf"), True, "five"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.save("working", [{"name": "/synthetic", "max_hz": invalid}])
        with patch("preset_store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.save("working", [])
        self.assertEqual(self.store.list(), [saved])

    def test_ros_name_and_buffer_endpoint_selection(self):
        gid = "aBcD" * 8  # A letter-leading GID also passes the ROS token grammar.
        valid = ("/camera/image_raw", "/camera/_buf_cpu", "/_buf/foo", "/nested/_hidden",
                 "/cam/_buf/" + gid[:-1], "/cam/_buf/" + gid + "/user_topic", "/" + "z" * 246)
        invalid = ("/cam/_buf/" + gid, "/_buf/" + "0" * 32, "relative", "/cam/9_bad",
                   "/", "/a/", "/a//b", "/a-b", "/a\x00b", "/相机", "/" + "z" * 247)
        for name in valid:
            with self.subTest(name=name):
                self.assertTrue(recordable_topic_name(name))
                self.assertEqual(normalize({"topics": [{"name": name}]}, 64)["requested_topics"], [name])
        for name in invalid:
            with self.subTest(name=name):
                self.assertFalse(recordable_topic_name(name))
                with self.assertRaises(ValueError):
                    normalize({"topics": [{"name": name}]}, 64)
                with self.assertRaises(ValueError):
                    self.store.save("bad", [{"name": name}])

    def test_old_preset_filters_transport_names_without_rewriting_other_presets(self):
        kept = [{"name": "/camera/h264", "max_hz": 0}, {"name": "/camera/_buf_cpu", "max_hz": 2}]
        legacy = [kept[0], {"name": "/camera/_buf/" + "ab" * 16}, {"name": "/invalid/3"}, kept[1]]
        self.path.write_text(json.dumps({"legacy": legacy}))
        original = self.path.read_bytes()
        self.assertEqual(self.store.list(), [{"name": "legacy", "topics": kept}])
        self.assertEqual(self.path.read_bytes(), original)
        self.store.save("another", [{"name": "/new"}])
        self.assertEqual(json.loads(self.path.read_text())["legacy"], legacy)

    def test_multiple_writers_preserve_independent_presets(self):
        def save(index):
            PresetStore(self.path).save(str(index), [{"name": "/test", "max_hz": index}])
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(save, range(20)))
        self.assertEqual({p["name"] for p in self.store.list()}, {str(i) for i in range(20)})


if __name__ == "__main__":
    unittest.main()
