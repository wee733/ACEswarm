"""ROS control plane. Serialized messages, throttling and the writer stay in C++."""
import importlib.machinery
import importlib.util
import json
import logging
import os
import platform
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from recording_options import normalize, session_name


def native_library():
    target = f"{os.environ.get('ROS_DISTRO', '')}-{platform.machine()}"
    directory = Path(__file__).parent / "native" / target
    for suffix in importlib.machinery.EXTENSION_SUFFIXES:
        library = directory / ("_recorder_native" + suffix)
        if library.is_file():
            return library
    return None


def create_engine(output_dir, use_sim_time=False, buffer_mib=64):
    library = native_library()
    if library is None:
        # Older distro/architecture pairs can still use the original portable service.
        logging.warning("No native module for this ROS/Python/CPU combination. Using Python recorder.")
        from legacy_engine import RecorderEngine as LegacyRecorder
        return LegacyRecorder(output_dir, use_sim_time)
    return RecorderEngine(output_dir, use_sim_time, buffer_mib)


class RecorderEngine:
    def __init__(self, output_dir, use_sim_time=False, buffer_mib=64):
        if type(buffer_mib) is not int or not 1 <= buffer_mib <= 512:
            raise ValueError("写入队列须为 1 至 512 MiB")
        self.root = Path(output_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        library = native_library()
        if not library:
            raise RuntimeError("此环境缺少 C++ 录制模块，请运行 build_native.sh")
        spec = importlib.util.spec_from_file_location("_recorder_native", library)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.native = module.NativeRecorder(use_sim_time)
        self.buffer_mib = buffer_mib
        self.lock = threading.RLock()
        self.session = None
        self.last = {"engine": "cpp", "recording": False, "name": "", "notes": "", "topics": {},
                     "elapsed_seconds": 0, "size_bytes": 0, "rate_bps": 0, "error": "",
                     "queue_bytes": 0, "peak_queue_bytes": 0, "buffer_bytes": buffer_mib * 1024**2}
        self.closed = False

    def topics(self):
        return self.native.topics()

    def logs(self, since=0):
        return self.native.logs(since)

    def start(self, payload):
        with self.lock:
            if self.closed:
                raise RuntimeError("ROS 录制器已关闭")
            if self.session:
                raise RuntimeError("已有录制正在进行")
            settings = normalize(payload, self.buffer_mib)
            folder = self.root / settings["name"]
            if os.path.lexists(folder):
                raise ValueError("同名录包已存在")
            started = datetime.now(timezone.utc).isoformat()
            self.native.start(str(folder), settings)
            self.started = self.sample_time = time.monotonic()
            self.size = self.rate = 0
            self.session = {k: v for k, v in settings.items() if k != "topics"}
            self.session.update(started_at=started, engine="cpp")
            return self.status()

    def _snapshot(self, stats, final=False):
        now = time.monotonic()
        delta = now - self.sample_time
        if final or delta >= .5:
            size = 0
            for entry in (self.root / self.session["name"]).iterdir():
                try:
                    if entry.is_file() and not entry.is_symlink():
                        size += entry.stat().st_size
                except FileNotFoundError:
                    continue  # The compression plugin may rename a completed shard.
            self.rate = round(max(0, size - self.size) / max(.001, delta))
            self.sample_time, self.size = now, size
        return {**self.session, **stats, "recording": not final, "elapsed_seconds": now - self.started,
                "size_bytes": self.size, "rate_bps": 0 if final else self.rate}

    def status(self):
        with self.lock:
            if self.session:
                stats = self.native.stats()
                if stats.get("error") or not stats["recording"]:
                    self._finish(stats.get("error", ""))
                    state = self.last.copy()
                else:
                    state = self._snapshot(stats)
            else:
                state = self.last.copy()
            state["free_bytes"] = shutil.disk_usage(self.root).free
            return state

    def _finish(self, prior_error=""):
        folder = self.root / self.session["name"]
        stats = self.native.stop()
        state = self._snapshot(stats, final=True)
        self.last, self.session = state, None
        state["error"] = stats.get("error") or prior_error
        if not (folder / "metadata.yaml").is_file():
            state["error"] = (state["error"] + "；未生成 metadata.yaml").lstrip("；")
        try:
            temp = folder / ".session_meta.tmp"
            with temp.open("x", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
            os.replace(temp, folder / "session_meta.json")
        except OSError as exc:
            state["error"] = (state["error"] + "；" + str(exc)).lstrip("；")

    def stop(self):
        with self.lock:
            if self.session:
                self._finish()
                if self.last["error"]:
                    raise RuntimeError(self.last["error"])
            return self.status()

    def close(self):
        with self.lock:
            if self.closed:
                return
            try:
                self.stop()
            finally:
                self.closed = True
                self.native.close()
