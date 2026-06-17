"""Classifier: auto-categorize channel names into groups."""

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from . import ChannelRecord
from .config import PROJECT_ROOT, get_classifier_config

logger = logging.getLogger(__name__)

DEFAULT_MAPPING_FILE = PROJECT_ROOT / "data" / "channels.json"

# Built-in fallback rules if mapping file is missing
BUILTIN_RULES: Dict[str, str] = {
    "cctv": "央视频道",
    "cgtn": "央视频道",
    "cetv": "央视频道",
    "china天气": "央视频道",
    "卫视": "卫视频道",
    "凤凰": "海外频道",
    "tvb": "海外频道",
    "bb:": "海外频道",
    "cnn": "海外频道",
    "nhk": "海外频道",
    "hbo": "海外频道",
    "discovery": "海外频道",
    "espn": "海外频道",
    "mtv": "海外频道",
    "national geographic": "海外频道",
}

# Province/region keywords for local channels
PROVINCE_KEYWORDS = [
    "北京", "上海", "天津", "重庆",
    "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东",
    "河南", "湖北", "湖南", "广东", "海南",
    "四川", "贵州", "云南", "陕西", "甘肃", "青海",
    "广西", "内蒙古", "西藏", "宁夏", "新疆",
    "深圳", "厦门", "大连", "青岛", "宁波",
]


def classify(
    records: List[ChannelRecord],
    mapping_file: Optional[Path] = None,
    default_group: Optional[str] = None,
) -> List[ChannelRecord]:
    """Assign group labels to all records in-place.

    Classification priority:
      1. Existing group tag from m3u8 EXTINF (if non-empty)
      2. Manual mapping from channels.json (exact or partial match)
      3. Keyword rules (CCTV→央视频道, 卫视→卫视频道, etc.)
      4. Province name match → 地方频道
      5. Default group

    Args:
        records: ChannelRecord list to classify.
        mapping_file: Path to JSON mapping file.
        default_group: Fallback group label.

    Returns:
        The same list (modified in-place).
    """
    cfg = get_classifier_config()
    mapping_file = mapping_file or Path(cfg.get("mapping_file", DEFAULT_MAPPING_FILE))
    if not mapping_file.is_absolute():
        mapping_file = PROJECT_ROOT / mapping_file
    default_group = default_group or cfg.get("default_group", "其他")

    mapping = _load_mapping(mapping_file)

    for record in records:
        if record.group:
            # Already has a group tag from source
            record.group = _normalize_group(record.group, default_group)
            continue

        # Try manual mapping
        matched = _match_mapping(record.name, mapping)
        if matched:
            record.group = matched
            continue

        # Try keyword rules
        matched = _match_rules(record.name)
        if matched:
            record.group = matched
            continue

        # Try province match
        if _is_local(record.name):
            record.group = "地方频道"
            continue

        # Fallback
        record.group = default_group

    # Log stats
    groups: Dict[str, int] = {}
    for r in records:
        groups[r.group] = groups.get(r.group, 0) + 1
    logger.info(f"Classification: {dict(sorted(groups.items()))}")

    return records


def _load_mapping(path: Path) -> Dict[str, str]:
    """Load channel→group mapping from JSON file."""
    if not path.exists():
        logger.warning(f"Mapping file not found: {path}, using built-in rules")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Remove metadata keys starting with _
        return {k: v for k, v in data.items() if not k.startswith("_") and v}
    except Exception as e:
        logger.error(f"Failed to load mapping file: {e}")
        return {}


def _match_mapping(name: str, mapping: Dict[str, str]) -> Optional[str]:
    """Find the best mapping match for a channel name.

    - Exact match first
    - Then substring match (longest key first to avoid short matches)
    """
    if not mapping:
        return None

    # Exact match (case-insensitive)
    name_lower = name.strip().lower()
    for key, group in mapping.items():
        if key.lower() == name_lower:
            return group

    # Substring match: sort by key length descending so "CCTV-5+" matches
    # before "CCTV"
    for key, group in sorted(mapping.items(), key=lambda x: -len(x[0])):
        if key.lower() in name_lower:
            return group

    return None


def _match_rules(name: str) -> Optional[str]:
    """Match channel name against built-in keyword rules."""
    name_lower = name.strip().lower()
    for keyword, group in BUILTIN_RULES.items():
        if keyword.lower() in name_lower:
            return group
    return None


def _is_local(name: str) -> bool:
    """Check if the channel name indicates a provincial/local station."""
    for kw in PROVINCE_KEYWORDS:
        if kw in name:
            return True
    return False


def _normalize_group(raw_group: str, fallback: str = "其他") -> str:
    """Normalize raw group titles from m3u8 sources into standard categories."""
    import re as _re
    # Strip leading emoji / symbols (e.g. "🌊港·澳·台" → "港·澳·台")
    cleaned = _re.sub(r'^[^\w一-鿿]+', '', raw_group).strip()

    mapping = [
        ("cctv", "央视频道"),
        ("央视", "央视频道"),
        ("卫视", "卫视频道"),
        ("satellite", "卫视频道"),
        ("地方", "地方频道"),
        ("local", "地方频道"),
        ("海外", "海外频道"),
        ("港澳台", "海外频道"),
        ("港澳", "海外频道"),
        ("港·澳", "海外频道"),
        ("凤凰", "海外频道"),
        ("tvb", "海外频道"),
        ("overseas", "海外频道"),
        ("international", "海外频道"),
        ("体育", "体育频道"),
        ("sport", "体育频道"),
        ("电影", "电影频道"),
        ("movie", "电影频道"),
        ("纪录", "纪录频道"),
        ("documentary", "纪录频道"),
        ("儿童", "儿童频道"),
        ("动画", "儿童频道"),
        ("kids", "儿童频道"),
        ("音乐", "音乐频道"),
        ("music", "音乐频道"),
    ]
    gl = cleaned.lower()
    for key, val in mapping:
        if key in gl:
            return val
    return cleaned or fallback


def get_group_order() -> List[str]:
    """Return the configured group display order."""
    cfg = get_classifier_config()
    return cfg.get("group_order", ["央视频道", "卫视频道", "地方频道", "海外频道", "其他"])
