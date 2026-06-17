"""M3U8 file parser for IPTV channel lists.

Parses standard IPTV m3u8 format:
    #EXTM3U
    #EXTINF:-1 group-title="News" tvg-id="cctv1" tvg-logo="...",CCTV-1 HD
    http://example.com/stream.m3u8

Not to be confused with HLS m3u8 playlists (segments). This is the IPTV
channel list format used by most IPTV sources.
"""

import logging
import re
from pathlib import Path
from typing import List

from . import SourceType, StreamEntry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns for EXTINF attribute extraction
# ---------------------------------------------------------------------------

_RE_EXTINF = re.compile(r"^#EXTINF:\s*-?\d+\s*(.*?),(.*)$")
_RE_ATTR = re.compile(r'(\w[\w-]*)\s*=\s*"([^"]*)"')
_RE_RESOLUTION = re.compile(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", re.IGNORECASE)


def parse_m3u8_content(content: str, source: str = "", source_type: SourceType = SourceType.RAW_M3U) -> List[StreamEntry]:
    """Parse IPTV m3u8 text content into StreamEntry list.

    Args:
        content: Raw m3u8 text.
        source: Identifier for the source (e.g. URL).
        source_type: Type of the source.

    Returns:
        List of parsed StreamEntry objects.
    """
    entries: List[StreamEntry] = []
    lines = content.splitlines()
    current_attrs: dict = {}
    current_name = ""

    for line in lines:
        line = line.strip()
        if not line or line == "#EXTM3U" or line.startswith("#EXTVLCOPT"):
            continue

        if line.startswith("#EXTINF:"):
            # Parse EXTINF line
            m = _RE_EXTINF.match(line)
            if m:
                attr_str = m.group(1).strip()
                current_name = m.group(2).strip()

                # Extract attributes
                attrs = {}
                for am in _RE_ATTR.finditer(attr_str):
                    attrs[am.group(1)] = am.group(2)
                current_attrs = attrs
            else:
                # Fallback: treat entire rest after EXTINF:-1 as name
                current_name = line.split(",", 1)[-1].strip() if "," in line else line.split(":", 1)[-1].strip()

        elif line.startswith("#"):
            # Other comments/tags — skip
            continue

        elif current_name:
            # URL line
            url = line.strip()
            if url.startswith("http://") or url.startswith("https://"):
                resolution = current_attrs.get("tvg-resolution", "") or _extract_resolution(current_name)
                entries.append(
                    StreamEntry(
                        name=current_name,
                        url=url,
                        source=source,
                        source_type=source_type,
                        group=current_attrs.get("group-title", ""),
                        tvg_id=current_attrs.get("tvg-id", ""),
                        tvg_logo=current_attrs.get("tvg-logo", ""),
                        resolution=resolution,
                    )
                )
            else:
                logger.debug(f"Skipping non-http URL: {url}")
            current_name = ""
            current_attrs = {}

    logger.debug(f"Parsed {len(entries)} entries from m3u8 content (source: {source})")
    return entries


def _extract_resolution(text: str) -> str:
    """Try to extract resolution marker from channel name, e.g. 'CCTV-1 1080P' → '1080P'."""
    m = _RE_RESOLUTION.search(text)
    if m:
        return f"{m.group(1)}x{m.group(2)}"
    # Also check for simple "720p", "1080p", "4K" markers
    upper = text.upper()
    if "4K" in upper:
        return "3840x2160"
    if "1080P" in upper:
        return "1920x1080"
    if "720P" in upper:
        return "1280x720"
    if "576P" in upper:
        return "720x576"
    if "480P" in upper:
        return "640x480"
    return ""


def parse_m3u8_file(filepath: Path, source_type: SourceType = SourceType.LOCAL_FILE) -> List[StreamEntry]:
    """Parse an m3u8 file from disk."""
    content = filepath.read_text(encoding="utf-8", errors="replace")
    return parse_m3u8_content(content, source=str(filepath), source_type=source_type)


def parse_txt_urls(filepath: Path, source_type: SourceType = SourceType.LOCAL_FILE) -> List[StreamEntry]:
    """Parse a plain-text URL list (one per line) into StreamEntry list."""
    entries: List[StreamEntry] = []
    content = filepath.read_text(encoding="utf-8", errors="replace")
    for line in content.splitlines():
        url = line.strip()
        if url and (url.startswith("http://") or url.startswith("https://")):
            # Extract a name from the URL path
            name = url.rsplit("/", 1)[-1].rsplit(".", 1)[0] or url
            entries.append(
                StreamEntry(name=name, url=url, source=str(filepath), source_type=source_type)
            )
    return entries
