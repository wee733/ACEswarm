#!/usr/bin/env python3
"""Run an isolated synthetic recording on Linux with a sourced ROS 2 environment.

Example: python3 bench_mixed.py --backend /path/to/backend --work-dir /path/to/nvme
--jpeg and --h264 optionally add identical encoded fixture bytes at every image timestamp.
No camera driver, flight topic, or running recorder service is used.
"""

import argparse
from array import array
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import uuid


def record_worker(backend, directory, payload, connection):
    os.setsid()
    sys.path.insert(0, backend)
    engine = None
    try:
        from recorder_engine import RecorderEngine
        engine = RecorderEngine(directory)
        expected = {topic["name"] for topic in payload["topics"]}
        deadline = time.monotonic() + 15
        while not expected <= {row["name"] for row in engine.topics() if row["publishers"]}:
            if time.monotonic() > deadline:
                raise TimeoutError("Recorder did not discover all synthetic publishers")
            time.sleep(0.1)
        status = engine.start(payload)
        if not status["recording"] or status.get("error"):
            raise RuntimeError(status.get("error") or "Recorder did not start")
        connection.send(("ready", status.get("engine", "python")))
        if connection.recv() != "stop":
            raise RuntimeError("Unexpected benchmark control message")
        status = engine.stop()
        if status.get("error"):
            raise RuntimeError(status["error"])
        engine.close()
        engine = None
        connection.send(("stopped", status))
    except BaseException:
        connection.send(("error", traceback.format_exc()))
    finally:
        if engine is not None:
            engine.close()
        connection.close()


def receive(connection, expected, timeout):
    if not connection.poll(timeout):
        raise TimeoutError(f"Timed out waiting for {expected}")
    event, value = connection.recv()
    if event != expected:
        raise RuntimeError(value)
    return value


def process_tree(pid):
    """Linux process CPU ticks and summed RSS, including a recorder worker subprocess."""
    ticks = {}
    todo = [pid]
    rss_pages = 0
    while todo:
        current = todo.pop()
        if current in ticks:
            continue
        directory = Path("/proc") / str(current)
        try:
            fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
            ticks[current] = int(fields[11]) + int(fields[12])
            rss_pages += int(fields[21])
            for children in (directory / "task").glob("*/children"):
                todo.extend(map(int, children.read_text().split()))
        except FileNotFoundError:
            continue
    return ticks, rss_pages * os.sysconf("SC_PAGE_SIZE")


def udp_errors():
    lines = [line.split()[1:] for line in Path("/proc/net/snmp").read_text().splitlines()
             if line.startswith("Udp:")]
    if len(lines) != 2:
        return {}
    counters = dict(zip(lines[0], map(int, lines[1])))
    return {name: counters[name] for name in ("InErrors", "RcvbufErrors", "SndbufErrors")
            if name in counters}


def loopback_drops():
    directory = Path("/sys/class/net/lo/statistics")
    return {name: int((directory / name).read_text()) for name in ("rx_dropped", "tx_dropped")}


def image_reference(size):
    block = bytes((i * 31 + i // 13) % 256 for i in range(8192))
    return (block * ((size + len(block) - 1) // len(block)))[:size]


def encoded_fixture(path, start_codes):
    if path is None:
        return None
    if path.stat().st_size > 4 * 1024 ** 2:
        raise ValueError("Encoded fixture exceeds 4 MiB")
    data = path.read_bytes()
    if not data.startswith(start_codes):
        raise ValueError(f"Invalid encoded fixture {path}")
    return data


class Workload:
    def __init__(self, args, node):
        from geometry_msgs.msg import Vector3Stamped
        from sensor_msgs.msg import CameraInfo, CompressedImage, Image
        from rclpy.qos import QoSProfile, ReliabilityPolicy

        self.args = args
        self.prefix = "/recorder_bench/run_" + uuid.uuid4().hex
        self.width, self.height = 1280, 720
        self.bpp = 1 if args.encoding == "mono8" else 3
        self.image_data = image_reference(self.width * self.height * self.bpp) if not args.encoded_only else None
        self.jpeg = encoded_fixture(args.jpeg, (b"\xff\xd8\xff",))
        self.h264 = encoded_fixture(args.h264, (b"\x00\x00\x00\x01", b"\x00\x00\x01"))
        self.messages = {}
        for i in range(args.telemetry_topics):
            name = f"telemetry_{i}"
            message = Vector3Stamped()
            message.header.frame_id = name
            message.vector.y = float(i)
            self.messages[name] = message
        if self.image_data is not None:
            image = Image(width=self.width, height=self.height, encoding=args.encoding, step=self.width * self.bpp)
            image.header.frame_id = "bench_camera"
            image.data = array("B", self.image_data)
            self.messages["image"] = image
        info = CameraInfo(width=self.width, height=self.height)
        info.header.frame_id = "bench_camera"
        info.k = [480., 0., self.width / 2, 0., 480., self.height / 2, 0., 0., 1.]
        self.messages["camera_info"] = info
        if self.jpeg is not None:
            compressed = CompressedImage(format="jpeg")
            compressed.data = array("B", self.jpeg)
            compressed.header.frame_id = "bench_camera"
            self.messages["compressed"] = compressed
        if self.h264 is not None:
            from foxglove_msgs.msg import CompressedVideo
            video = CompressedVideo(frame_id="bench_camera", format="h264")
            video.data = array("B", self.h264)
            self.messages["video"] = video
        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy[args.qos.upper()])
        self.publishers = {
            name: node.create_publisher(type(msg), self.topic(name), qos)
            for name, msg in self.messages.items()
        }
        self.unselected_publisher = None
        if args.publish_unselected_rgb:
            self.background_rgb = Image(width=self.width, height=self.height, encoding="rgb8", step=self.width * 3)
            self.background_rgb.header.frame_id = "bench_camera"
            self.background_rgb.data = array("B", image_reference(self.width * self.height * 3))
            self.unselected_publisher = node.create_publisher(Image, self.topic("unselected_rgb"), qos)
        self.stamps = {name: [] for name in self.messages}

    def topic(self, short):
        return self.prefix + "/" + short

    def run(self, recorder_pid):
        a = self.args
        schedule = [(i / a.telemetry_hz, True, i) for i in range(round(a.seconds * a.telemetry_hz))]
        schedule.extend((i / a.camera_hz, False, i) for i in range(round(a.seconds * a.camera_hz)))
        schedule.sort()
        telemetry = [name for name in self.messages if name.startswith("telemetry_")]
        camera = [name for name in self.messages if name not in telemetry]
        initial, peak = process_tree(recorder_pid)
        udp = udp_errors()
        loopback = loopback_drops()
        start = time.monotonic()
        next_sample = start + 0.20
        max_lateness = 0.
        for seconds, is_telemetry, sequence in schedule:
            target = start + seconds
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            now = time.monotonic()
            max_lateness = max(max_lateness, now - target)
            stamp = time.time_ns()
            if not is_telemetry and self.unselected_publisher is not None:
                field = self.background_rgb.header.stamp
                field.sec, field.nanosec = divmod(stamp, 10**9)
                struct.pack_into("<Q", self.background_rgb.data, 0, sequence)
                self.unselected_publisher.publish(self.background_rgb)
            for name in (telemetry if is_telemetry else camera):
                message = self.messages[name]
                field = message.timestamp if name == "video" else message.header.stamp
                field.sec, field.nanosec = divmod(stamp, 10**9)
                if is_telemetry:
                    message.vector.x = float(sequence)
                    message.vector.z = float(sequence) + message.vector.y
                if name == "image":
                    struct.pack_into("<Q", message.data, 0, sequence)
                self.publishers[name].publish(message)
                self.stamps[name].append(stamp)
            if now >= next_sample:
                peak = max(peak, process_tree(recorder_pid)[1])
                next_sample = now + 0.20
        elapsed = time.monotonic() - start
        final, rss = process_tree(recorder_pid)
        ticks = sum(value - initial.get(pid, 0) for pid, value in final.items())
        after_udp = udp_errors()
        after_loopback = loopback_drops()
        return {
            "publish_window_s": round(elapsed, 4), "max_publisher_late_ms": round(max_lateness * 1e3, 2),
            "recorder_cpu_pct_one_core": round(100 * ticks / os.sysconf("SC_CLK_TCK") / elapsed, 2),
            "recorder_tree_pids": sorted(final),
            "peak_observed_recorder_tree_rss_mib": round(max(peak, rss) / 2**20, 2),
            "host_udp_error_deltas": {k: after_udp[k] - v for k, v in udp.items()},
            "host_loopback_drop_deltas": {k: after_loopback[k] - v for k, v in loopback.items()},
        }

    def verify(self, directory, recorder_status):
        import rosbag2_py
        from rclpy.serialization import deserialize_message

        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=str(directory), storage_id="mcap"),
                    rosbag2_py.ConverterOptions("", ""))
        seen = {name: set() for name in self.messages}
        indices = {name: {stamp: i for i, stamp in enumerate(stamps)} for name, stamps in self.stamps.items()}
        previous = {name: -1 for name in seen}
        latencies = {name: [] for name in seen}
        while reader.has_next():
            topic, data, bag_stamp = reader.read_next()
            name = topic.rsplit("/", 1)[-1]
            assert topic == self.topic(name) and name in self.messages, f"Unexpected topic {topic}"
            msg = deserialize_message(data, type(self.messages[name]))
            field = msg.timestamp if name == "video" else msg.header.stamp
            stamp = field.sec * 10**9 + field.nanosec
            i = indices[name].get(stamp)
            assert i is not None and i > previous[name], f"{topic}: unexpected stamp, order, or duplicate"
            previous[name] = i
            seen[name].add(i)
            latencies[name].append((bag_stamp - stamp) / 1e6)
            if name.startswith("telemetry_"):
                channel = int(name.split("_")[1])
                assert msg.header.frame_id == name
                assert (msg.vector.x, msg.vector.y, msg.vector.z) == (i, channel, i + channel)
            else:
                frame = msg.frame_id if name == "video" else msg.header.frame_id
                assert frame == "bench_camera"
                if name == "image":
                    assert (msg.width, msg.height, msg.step, msg.encoding) == (
                        self.width, self.height, self.width * self.bpp, self.args.encoding)
                    assert struct.unpack_from("<Q", msg.data)[0] == i and msg.data[8:].tobytes() == self.image_data[8:]
                elif name == "camera_info":
                    assert (msg.width, msg.height) == (self.width, self.height)
                    assert tuple(msg.k) == tuple(self.messages[name].k)
                elif name == "compressed":
                    assert msg.format == "jpeg" and msg.data.tobytes() == self.jpeg
                else:
                    assert msg.format == "h264" and msg.data.tobytes() == self.h264
        rows = {}
        for name, kept in seen.items():
            sent = len(self.stamps[name])
            counters = recorder_status["topics"][self.topic(name)]
            assert counters["written_count"] == len(kept), f"{name}: counter disagrees with MCAP"
            missing = sorted(set(range(sent)) - kept)
            delays = sorted(latencies[name])
            rows[name] = {"sent": sent, "read_back": len(kept), "missing": len(missing),
                          "first_missing_sequences": missing[:10], "recorder": counters}
            if delays:
                rows[name]["bag_minus_header_ms"] = {
                    "p50": round(delays[(len(delays) - 1) // 2], 3),
                    "p95": round(delays[int((len(delays) - 1) * 0.95)], 3),
                    "max": round(delays[-1], 3),
                }
        return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True, help="Directory containing recorder_engine.py")
    parser.add_argument("--work-dir", type=Path, required=True, help="Existing directory on the storage under test")
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--telemetry-hz", type=int, default=100)
    parser.add_argument("--telemetry-topics", type=int, default=8)
    parser.add_argument("--camera-hz", type=int, default=25)
    parser.add_argument("--encoding", choices=("mono8", "rgb8"), default="mono8")
    parser.add_argument("--qos", choices=("best_effort", "reliable"), default="best_effort")
    parser.add_argument("--jpeg", type=Path, help="Optional real JPEG fixture")
    parser.add_argument("--h264", type=Path, help="Optional real Annex-B H.264 keyframe fixture, with SPS/PPS")
    parser.add_argument("--encoded-only", action="store_true", help="Publish only the selected encoded media and CameraInfo alongside telemetry")
    parser.add_argument("--publish-unselected-rgb", action="store_true", help="Also publish an unselected RGB8 input on the camera clock")
    parser.add_argument("--domain", type=int, default=84, help="Dedicated nonproduction DDS domain (1-99)")
    parser.add_argument("--json", type=Path, help="Save benchmark result")
    args = parser.parse_args()
    if not 1 <= args.telemetry_topics <= 32:
        parser.error("Choose 1–32 telemetry topics")
    if args.publish_unselected_rgb and not args.encoded_only:
        parser.error("--publish-unselected-rgb requires --encoded-only")
    if (not 1 <= args.domain <= 99 or not 1 <= args.seconds <= 15 or
            not 1 <= args.telemetry_hz <= 1000 or not 1 <= args.camera_hz <= 60):
        parser.error("Choose a dedicated domain 1–99, duration 1–15 s, telemetry 1–1000 Hz and camera 1–60 Hz")
    if not (args.backend / "recorder_engine.py").is_file() or not args.work_dir.is_dir():
        parser.error("Backend and scratch paths must exist")
    if args.encoded_only and not (args.jpeg or args.h264):
        parser.error("--encoded-only requires --jpeg or --h264")
    raw_image_size = 0 if args.encoded_only else 1280 * 720 * (3 if args.encoding == "rgb8" else 1)
    estimated = (raw_image_size
                 + sum(p.stat().st_size for p in (args.jpeg, args.h264) if p)) * round(args.seconds * args.camera_hz)
    if estimated > 600 * 2**20 or shutil.disk_usage(args.work_dir).free < estimated + 256 * 2**20:
        parser.error("Generated images must fit within 600 MiB and leave 256 MiB free")
    os.environ.update(ROS_DOMAIN_ID=str(args.domain), ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST",
                      ROS_STATIC_PEERS="", PYTHONDONTWRITEBYTECODE="1")
    for name in ("ROS_DISCOVERY_SERVER", "ROS_LOCALHOST_ONLY"):
        os.environ.pop(name, None)

    import rclpy
    with tempfile.TemporaryDirectory(prefix="mixed-recorder-", dir=args.work_dir) as scratch:
        os.environ["ROS_LOG_DIR"] = str(Path(scratch) / "logs")
        rclpy.init(args=[])
        node = rclpy.create_node("mixed_benchmark_publisher", enable_rosout=False)
        process = None
        try:
            fixture = Workload(args, node)
            payload = {
                "name": "mixed", "notes": "synthetic benchmark", "format": "mcap",
                "compression": "none", "save_rosout": False,
                "topics": [{"name": fixture.topic(n), "max_hz": 0} for n in fixture.messages],
            }
            context = multiprocessing.get_context("spawn")
            control, worker = context.Pipe()
            process = context.Process(target=record_worker, args=(str(args.backend.resolve()), scratch, payload, worker))
            process.start()
            worker.close()
            backend = receive(control, "ready", 25)
            deadline = time.monotonic() + 15
            while not all(p.get_subscription_count() for p in fixture.publishers.values()):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Not all synthetic publishers matched a recording subscription")
                time.sleep(0.05)
            # Discovery has matched every publisher. Let all DDS data paths settle
            # before the first measured sample, particularly large best-effort images.
            time.sleep(0.35)
            if fixture.unselected_publisher is not None:
                assert fixture.unselected_publisher.get_subscription_count() == 0
            stats = fixture.run(process.pid)
            if fixture.unselected_publisher is not None:
                assert fixture.unselected_publisher.get_subscription_count() == 0
                stats["unselected_rgb_frames_sent"] = len(fixture.stamps["camera_info"])
                stats["unselected_rgb_matched_subscribers_at_start_and_end"] = 0
            time.sleep(1)
            control.send("stop")
            stopped = receive(control, "stopped", 25)
            process.join(5)
            directory = Path(scratch) / "mixed"
            subprocess.run(
                ["ros2", "bag", "info", str(directory)],
                check=True, stdout=subprocess.DEVNULL, timeout=15,
            )
            topics = fixture.verify(directory, stopped)
            report = {
                "backend_dir": str(args.backend.resolve()), "backend": backend, "domain": args.domain,
                "encoding": args.encoding, "qos": args.qos, "seconds": args.seconds, "telemetry_hz": args.telemetry_hz,
                "telemetry_topics": args.telemetry_topics, "raw_images": not args.encoded_only,
                "camera_hz": args.camera_hz,
                "bag_bytes": sum(p.stat().st_size for p in directory.glob("*.mcap")),
                "topics": topics, **stats,
            }
            for name in ("buffer_bytes", "peak_queue_bytes"):
                if name in stopped:
                    report[name] = stopped[name]
            report["lost_total"] = sum(t["missing"] for t in topics.values())
            result = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
            if args.json:
                args.json.write_text(result + "\n")
            print(result)
        finally:
            if process and process.is_alive():
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                process.join(3)
            node.destroy_node()
            rclpy.shutdown()
    return int(report["lost_total"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
