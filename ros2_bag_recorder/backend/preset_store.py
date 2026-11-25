"""Atomic topic preset persistence for the recorder."""

import fcntl
import json
import math
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from recording_options import recordable_topic_name, require_topic_name


def _name(value):
    if (not isinstance(value, str) or not value.strip() or not value.isprintable()
            or "/" in value or "\\" in value or value in (".", "..")):
        raise ValueError("Invalid preset name")
    return value.strip()


def _topics(items, *, existing=False):
    if not isinstance(items, list):
        raise ValueError("topics must be a list")
    result = []
    names = set()
    for item in items:
        topic = item.get("name") if isinstance(item, dict) else None
        # Older versions could persist DDS transport endpoints alongside ROS topics.
        if existing and isinstance(topic, str) and not recordable_topic_name(topic):
            continue
        require_topic_name(topic)
        if topic in names:
            raise ValueError(f"Duplicate topic {topic}")
        hz = item.get("max_hz", 0)
        if isinstance(hz, bool) or not isinstance(hz, (float, int)):
            raise ValueError("max_hz must be a finite number")
        try:
            valid = math.isfinite(hz)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("max_hz must be a finite number")
        names.add(topic)
        result.append({"name": topic, "max_hz": max(0, hz)})
    return result


class PresetStore:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def _locked(self, exclusive):
        # A separate lock inode stays stable across atomic replacements of the JSON file.
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            records = json.load(stream)
        if not isinstance(records, dict):
            raise ValueError("Invalid preset file")
        return records

    def _write(self, records):
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix="." + self.path.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(records, stream, ensure_ascii=False, allow_nan=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def list(self):
        with self._locked(False):
            records = self._read()
        return [{"name": name, "topics": _topics(records[name], existing=True)} for name in sorted(records)]

    def save(self, name, topics):
        name = _name(name)
        topics = _topics(topics)
        with self._locked(True):
            records = self._read()
            records[name] = topics
            self._write(records)
        return {"name": name, "topics": topics}

    def delete(self, name):
        name = _name(name)
        with self._locked(True):
            records = self._read()
            if name not in records:
                raise FileNotFoundError("Preset does not exist")
            del records[name]
            self._write(records)
