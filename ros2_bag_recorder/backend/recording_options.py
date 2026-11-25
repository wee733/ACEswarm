"""Session options shared by native and compatibility engines."""

import math
import re
from datetime import datetime


# RMW_TOPIC_MAX_NAME_LENGTH reserves 8 bytes of the 255-byte DDS limit.
_FULL_TOPIC = re.compile(r"(?:/[A-Za-z_][A-Za-z0-9_]*)+")
_BUFFER_ENDPOINT = re.compile(r"/_buf/[0-9a-fA-F]{32}\Z")


def recordable_topic_name(name):
    return (isinstance(name, str) and len(name) <= 247
            and _FULL_TOPIC.fullmatch(name) is not None
            and _BUFFER_ENDPOINT.search(name) is None)


def require_topic_name(name):
    if not recordable_topic_name(name):
        raise ValueError(f"话题名无效或属于 DDS 内部缓冲端点：{name!r}")
    return name


def session_name(value):
    if value is None or value == "":
        return datetime.now().strftime("bag_%Y%m%d_%H%M%S")
    if (not isinstance(value, str) or not value.strip() or value.startswith(".")
            or "/" in value or "\\" in value or not value.isprintable()
            or len(value.encode("utf-8")) > 120):
        raise ValueError("录包名称须为 1 至 120 字节的单个可见目录名")
    return value


def _limit(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("话题频率必须为有限数值")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError("话题频率超出范围") from exc
    if not math.isfinite(value):
        raise ValueError("话题频率必须为有限数值")
    return max(0.0, value)


def _split(value, unit):
    if type(value) is not int or not 0 <= value <= (2**63 - 1) // unit:
        raise ValueError("分片阈值必须为非负整数")
    return value * unit


def validate_video_rate(name, type_name, hz):
    interframe = type_name in ("foxglove_msgs/msg/CompressedVideo", "ffmpeg_image_transport_msgs/msg/FFMPEGPacket")
    if type_name == "sensor_msgs/msg/CompressedImage":
        interframe = any(codec in name.lower() for codec in ("h264", "h265", "hevc"))
    if interframe and hz > 0:
        raise ValueError("编码视频须全速录制以保留帧间依赖，请在相机端设置帧率：" + name)


def selected_topics(topics):
    if not isinstance(topics, list):
        raise ValueError("话题须为列表")
    selected = {}
    for item in topics:
        if not isinstance(item, dict):
            raise ValueError("话题名称无效")
        name = require_topic_name(item.get("name"))
        if name in selected:
            raise ValueError("话题重复：" + name)
        selected[name] = _limit(item.get("max_hz", 0))
    return selected


def normalize(payload, buffer_mib):
    if not isinstance(payload, dict):
        raise ValueError("录制配置必须为对象")
    fmt, mode = payload.get("format", "mcap"), payload.get("compression", "none")
    if fmt not in ("mcap", "sqlite3") or mode not in ("none", "zstd_fast", "zstd_small"):
        raise ValueError("不支持该存储或压缩格式")
    mb = payload.get("split_mb", 0)
    _split(mb, 1024 * 1024)
    seconds = _split(payload.get("split_seconds", 0), 1)
    notes, save = payload.get("notes", ""), payload.get("save_rosout", True)
    if not isinstance(notes, str) or type(save) is not bool:
        raise ValueError("备注须为文本，日志开关须为布尔值")
    selected = selected_topics(payload.get("topics", []))
    requested = list(selected)
    if save:
        selected.setdefault("/rosout", 0.)
    if not selected:
        raise ValueError("请至少选择一个话题或开启日志记录")
    return {"name": session_name(payload.get("name")), "notes": notes, "format": fmt,
            "compression": mode, "split_mb": mb, "split_seconds": seconds, "save_rosout": save,
            "requested_topics": requested, "buffer_mib": buffer_mib,
            "topics": [{"name": n, "max_hz": hz} for n, hz in selected.items()]}
