"""Inspect finalized rosbag2 sessions and stream their recorded files."""

import errno
import gzip
import json
import os
import shutil
import stat
import tarfile
import threading
from collections import Counter
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
_SIDECARS = ("metadata.yaml", "session_meta.json", "rosout.log", "rosout.jsonl")


def _component(name):
    if (not isinstance(name, str) or not name or name.startswith(".")
            or "/" in name or "\\" in name or not name.isprintable()):
        raise ValueError("Invalid recording path component")
    return name


def _parts(relative_name):
    if not isinstance(relative_name, str):
        raise ValueError("Invalid recording file path")
    return [_component(part) for part in relative_name.split("/")]


@contextmanager
def _directory(path, *, dir_fd=None):
    try:
        fd = os.open(path, _DIR_FLAGS, dir_fd=dir_fd)
    except NotADirectoryError as exc:
        raise ValueError("Expected a recording directory") from exc
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _regular_file(directory, relative_name):
    """Pin every component and open the final regular file without following links."""
    parts = _parts(relative_name)
    parent = os.dup(directory)
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, _DIR_FLAGS, dir_fd=parent)
            os.close(parent)
            parent = next_fd
        try:
            fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError("Recording files must not be symlinks") from exc
            raise
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("Expected a regular recording file")
        with os.fdopen(fd, "rb") as stream:
            yield stream
    finally:
        os.close(parent)


def _nonnegative_int(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Invalid count or duration in rosbag metadata")
    return value


def _session_info(directory):
    with _regular_file(directory, "metadata.yaml") as stream:
        try:
            raw = yaml.safe_load(stream)
        except (UnicodeError, yaml.YAMLError) as exc:
            raise ValueError("Invalid rosbag metadata.yaml") from exc
    info = raw.get("rosbag2_bagfile_information") if isinstance(raw, dict) else None
    if not isinstance(info, dict) or not isinstance(info.get("storage_identifier"), str):
        raise ValueError("Missing rosbag2_bagfile_information in metadata.yaml")
    return info


def _session_meta(directory):
    try:
        with _regular_file(directory, "session_meta.json") as stream:
            meta = json.load(stream)
            return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        # A standard rosbag remains readable if the app sidecar is missing or damaged.
        return {}


def _relative_files(info):
    paths = info.get("relative_file_paths")
    if paths is None:
        records = info.get("files", [])
        if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
            raise ValueError("Invalid list of rosbag files")
        paths = [item.get("path") for item in records]
    if not isinstance(paths, list):
        raise ValueError("Invalid list of rosbag files")
    for path in paths:
        _parts(path)
    return list(dict.fromkeys([*paths, *_SIDECARS]))


def _file_sizes(directory, names):
    files = {}
    for name in names:
        try:
            with _regular_file(directory, name) as stream:
                files[name] = os.fstat(stream.fileno()).st_size
        except (OSError, ValueError):
            # Do not expose links, device nodes, or missing shards as download targets.
            continue
    return files


class _Download:
    def __init__(self, filename, directory, files, stream=None):
        self.filename = filename
        self.content_type = "application/octet-stream" if stream else "application/gzip"
        self.size = os.fstat(stream.fileno()).st_size if stream else None
        self._directory = directory
        self._files = files
        self._stream = stream

    def write_to(self, output):
        if self._stream is not None:
            shutil.copyfileobj(self._stream, output, length=256 * 1024)
            return
        prefix = self.filename[:-len(".tar.gz")]
        with gzip.GzipFile(fileobj=output, mode="wb", compresslevel=1) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as archive:
                for name in self._files:
                    with _regular_file(self._directory, name) as stream:
                        status = os.fstat(stream.fileno())
                        header = tarfile.TarInfo(f"{prefix}/{name}")
                        header.size = status.st_size
                        header.mtime = int(status.st_mtime)
                        header.mode = 0o644
                        archive.addfile(header, stream)


class BagManager:
    def __init__(self, output_dir):
        self.root = Path(output_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._protected = set()
        self._downloads = Counter()

    def protect(self, name):
        """Reserve the writer's session name before starting subscriptions."""
        _component(name)
        with self._lock:
            if name in self._protected or self._downloads[name]:
                raise RuntimeError("Recording is in use")
            self._protected.add(name)

    def unprotect(self, name):
        with self._lock:
            self._protected.discard(name)

    def _closed_session(self, name):
        _component(name)
        if name in self._protected:
            raise RuntimeError("Recording is still active")
        return _directory(self.root / name)

    def detail(self, name):
        with self._lock, self._closed_session(name) as directory:
            try:
                info = _session_info(directory)
                meta = _session_meta(directory)
                files = _file_sizes(directory, _relative_files(info))
                topics = []
                for entry in info.get("topics_with_message_count", []):
                    topic = entry["topic_metadata"]
                    topics.append({"name": topic["name"], "type": topic["type"],
                                   "count": _nonnegative_int(entry["message_count"])})
                count = _nonnegative_int(info.get("message_count", sum(t["count"] for t in topics)))
                duration = _nonnegative_int(info.get("duration", {}).get("nanoseconds", 0)) / 1e9
                start_time = meta.get("started_at")
                if not isinstance(start_time, str) or not start_time:
                    stamp = info.get("starting_time", {}).get("nanoseconds_since_epoch")
                    seconds = os.fstat(directory).st_mtime if stamp is None else int(stamp) / 1e9
                    start_time = datetime.fromtimestamp(seconds, timezone.utc).isoformat()
            except (KeyError, TypeError, AttributeError, OverflowError) as exc:
                raise ValueError("Invalid rosbag metadata fields") from exc

            return {
                "name": name, "path": str(self.root / name), "start_time": start_time,
                "duration_sec": duration, "size_bytes": sum(files.values()),
                "topic_count": len(topics), "message_count": count,
                "note": str(meta.get("notes", meta.get("note", ""))),
                "storage_id": info["storage_identifier"], "topics": topics,
                "files": list(files),
            }

    def list(self):
        result = []
        for entry in self.root.iterdir():
            if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                continue
            try:
                item = self.detail(entry.name)
            except (OSError, ValueError, RuntimeError):
                # Skip other directories, open sessions and incomplete metadata.
                continue
            item.pop("topics")
            item.pop("files")
            result.append(item)
        return sorted(result, key=lambda item: item["start_time"], reverse=True)

    @contextmanager
    def download(self, name, filename=None):
        if filename is not None:
            _parts(filename)
        with ExitStack() as stack:
            with self._lock:
                directory = stack.enter_context(self._closed_session(name))
                self._downloads[name] += 1
            stack.callback(self._finish_download, name)
            info = _session_info(directory)
            files = _file_sizes(directory, _relative_files(info))
            if filename is None:
                yield _Download(name + ".tar.gz", directory, files)
            else:
                if filename not in files:
                    raise FileNotFoundError("Recording file is unavailable")
                with _regular_file(directory, filename) as stream:
                    yield _Download(filename.rsplit("/", 1)[-1], directory, files, stream)

    def _finish_download(self, name):
        with self._lock:
            self._downloads[name] -= 1
            if not self._downloads[name]:
                del self._downloads[name]

    def delete(self, name):
        with self._lock, self._closed_session(name) as directory:
            if self._downloads[name]:
                raise RuntimeError("Recording has an active download")
            _session_info(directory)
            target = self.root / name
            if not os.path.samestat(os.fstat(directory), target.lstat()):
                raise RuntimeError("Recording directory changed")
            # CPython's fd-based rmtree on Linux does not traverse symlinks.
            shutil.rmtree(target)
