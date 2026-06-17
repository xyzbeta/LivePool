"""
LivePool: IPTV stream collector, validator, and m3u8 generator.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional

# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------


class StreamStatus(str, Enum):
    PENDING = "pending"
    ALIVE = "alive"
    DEAD = "dead"
    TIMEOUT = "timeout"
    ERROR = "error"
    AUDIO = "audio"  # stream is reachable but audio-only (no video track)


class SourceType(str, Enum):
    GITHUB_M3U = "github_m3u"
    RAW_M3U = "raw_m3u"
    WEB_SCRAPE = "web_scrape"
    LOCAL_FILE = "local_file"
    MANUAL = "manual"


@dataclass
class StreamEntry:
    """A raw stream entry collected from a source."""

    name: str
    url: str
    source: str = ""  # source identifier
    source_type: SourceType = SourceType.MANUAL
    group: str = ""
    tvg_id: str = ""
    tvg_logo: str = ""
    resolution: str = ""  # e.g. "1920x1080", "720p"
    bitrate: int = 0

    def __hash__(self):
        return hash(self.url)


@dataclass
class CheckResult:
    """Result of a stream liveness check."""

    url: str
    name: str
    status: StreamStatus = StreamStatus.PENDING
    http_code: int = 0
    latency_ms: float = 0.0
    error_msg: str = ""
    content_type: str = ""
    body_sample: str = ""  # First 128 chars of response body (for diagnosis)
    has_cors: bool = False  # Access-Control-Allow-Origin header present
    has_video: bool = False  # segment contains video NAL units
    checked_at: str = ""  # ISO timestamp

    @property
    def is_alive(self) -> bool:
        return self.status == StreamStatus.ALIVE


@dataclass
class ChannelRecord:
    """Persistent channel record with metadata."""

    id: str = ""  # unique id derived from name+url hash
    name: str = ""
    url: str = ""
    group: str = ""
    status: StreamStatus = StreamStatus.PENDING
    latency_ms: float = 0.0
    resolution: str = ""
    last_check: str = ""
    last_alive: str = ""
    source: str = ""
    tvg_id: str = ""
    tvg_logo: str = ""  # logo URL from source m3u8
    has_cors: bool = False
    has_video: bool = False
    score: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Stats:
    """System statistics snapshot."""

    total: int = 0
    alive: int = 0
    dead: int = 0
    timeout: int = 0
    error: int = 0
    audio: int = 0
    groups: dict = field(default_factory=dict)  # group_name -> count
    last_check: str = ""
    check_duration_sec: float = 0.0
    sources_count: int = 0


__all__ = [
    "StreamEntry",
    "CheckResult",
    "ChannelRecord",
    "Stats",
    "StreamStatus",
    "SourceType",
]
