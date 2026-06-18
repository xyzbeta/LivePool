"""Filter: remove dead links, deduplicate, and select best stream per channel."""

import logging
from typing import Dict, List, Optional, Tuple

from . import ChannelRecord, CheckResult, StreamEntry, StreamStatus

logger = logging.getLogger(__name__)


def filter_alive(
    entries: List[StreamEntry],
    results: List[CheckResult],
) -> Tuple[List[ChannelRecord], List[ChannelRecord], Dict[str, float]]:
    """Filter streams by check results.

    Args:
        entries: Original stream entries.
        results: Corresponding check results (same order).

    Returns:
        Tuple of (alive_channels, dead_channels, source_health).
        source_health maps source_id → alive_ratio for downstream dedup.
    """
    # Build URL → CheckResult map
    result_map: Dict[str, CheckResult] = {r.url: r for r in results}

    # Compute per-source health for quality scoring
    src_total: Dict[str, int] = {}
    src_alive: Dict[str, int] = {}
    for entry in entries:
        key = entry.source or "__unknown__"
        src_total[key] = src_total.get(key, 0) + 1
        cr = result_map.get(entry.url)
        if cr and cr.is_alive:
            src_alive[key] = src_alive.get(key, 0) + 1
    source_health = {s: src_alive.get(s, 0) / max(src_total[s], 1) for s in src_total}

    alive: List[ChannelRecord] = []
    dead: List[ChannelRecord] = []

    for entry in entries:
        cr = result_map.get(entry.url)
        record = ChannelRecord(
            id="",
            name=entry.name,
            url=entry.url,
            group=entry.group,
            status=cr.status if cr else StreamStatus.PENDING,
            latency_ms=cr.latency_ms if cr else 0.0,
            resolution=entry.resolution,
            last_check=cr.checked_at if cr else "",
            last_alive=cr.checked_at if (cr and cr.is_alive) else "",
            source=entry.source,
            tvg_id=entry.tvg_id,
            tvg_logo=entry.tvg_logo,
            has_cors=cr.has_cors if cr else False,
            has_video=cr.has_video if cr else False,
        )
        # Generate a stable id from name+url
        if cr and cr.status == StreamStatus.AUDIO:
            record.status = StreamStatus.AUDIO
        record.id = _make_id(record.name, record.url)

        # Compute quality score for display
        record.score = _channel_score(record, source_health.get(record.source or "__unknown__", 0.5))

        if cr and cr.is_alive:
            alive.append(record)
        else:
            dead.append(record)

    logger.info(f"Filter: {len(alive)} alive, {len(dead)} dead")
    return alive, dead, source_health


def dedup_by_url(records: List[ChannelRecord]) -> List[ChannelRecord]:
    """Remove duplicate URLs, keeping the first occurrence."""
    seen: set = set()
    result: List[ChannelRecord] = []
    for r in records:
        if r.url not in seen:
            seen.add(r.url)
            result.append(r)
    return result


def dedup_by_name(records: List[ChannelRecord],
                  source_health: Optional[Dict[str, float]] = None) -> List[ChannelRecord]:
    """Deduplicate by tvg-id and channel name similarity.

    Priority: tvg-id group > normalized name group.
    Picks the best candidate by weighted score:
      stability (source health) > playability (CORS + video) > quality > latency.

    Args:
        records: Channel records to deduplicate.
        source_health: Pre-computed per-source health ratios. If None, computed
                       from records (slower, duplicates filter_alive work).
    """
    from collections import defaultdict

    # ── per‑source health for stability weighting ──────────────────────
    if source_health is None:
        src_total: Dict[str, int] = {}
        src_alive: Dict[str, int] = {}
        for r in records:
            key = r.source or "__unknown__"
            src_total[key] = src_total.get(key, 0) + 1
            if r.status == StreamStatus.ALIVE:
                src_alive[key] = src_alive.get(key, 0) + 1
        source_health = {
            s: src_alive.get(s, 0) / max(src_total[s], 1) for s in src_total
        }

    def _score(r: ChannelRecord) -> float:
        return _channel_score(r, source_health.get(r.source or "__unknown__", 0.5))

    # Phase 1: group by tvg-id if available
    tvg_groups: Dict[str, List[ChannelRecord]] = defaultdict(list)
    no_tvg: List[ChannelRecord] = []
    for r in records:
        if r.tvg_id:
            tvg_groups[r.tvg_id].append(r)
        else:
            no_tvg.append(r)

    # Phase 2: within each tvg-id group, pick best by weighted score
    result: List[ChannelRecord] = []
    for tvg_id, group in tvg_groups.items():
        best = max(group, key=_score)
        if len(group) > 1:
            logger.debug(
                "Dedup tvg-id '%s': kept '%s' (score=%.1f), dropped %d",
                tvg_id, best.name, _score(best), len(group) - 1,
            )
        result.append(best)

    # Phase 3: group remaining (no tvg-id) by normalized name
    name_groups: Dict[str, List[ChannelRecord]] = defaultdict(list)
    for r in no_tvg:
        name_groups[_normalize_name(r.name)].append(r)

    for key, group in name_groups.items():
        best = max(group, key=_score)
        if len(group) > 1:
            logger.debug(
                "Dedup name '%s': kept '%s' (score=%.1f), dropped %d",
                key, best.name, _score(best), len(group) - 1,
            )
        result.append(best)

    logger.info(f"Dedup: {len(records)} → {len(result)}")
    return result


def _channel_score(r: ChannelRecord, source_alive_ratio: float = 0.5) -> float:
    """Weighted score for picking the best stream among duplicates.

    Weights (tuned so latency and stability are balanced):
      stability  — source alive ratio × 50     (0 … 50)
      playability — has_video × 8              (0 … 8)
      quality     — resolution / 500 000       (~0 … 4 for 1080p)
      latency     — (3000 − min(ms, 3000)) / 30  (0 … 100)
    """
    stability = max(source_alive_ratio, 0.0)
    return (
        stability * 50
        + (1 if r.has_video else 0) * 8
        + resolution_score(r.resolution) / 500_000
        + (3000 - min(r.latency_ms, 3000)) / 30
    )


def _make_id(name: str, url: str) -> str:
    """Generate a short stable id from name and URL."""
    import hashlib
    h = hashlib.md5(f"{name}|{url}".encode(), usedforsecurity=False).hexdigest()
    return h[:12]


def _normalize_name(name: str) -> str:
    """Normalize channel name for dedup comparison.

    Removes resolution markers, source suffixes, and extra whitespace.
    """
    import re
    n = name.strip()
    # Remove common suffixes like " 1080P", " HD", " FHD", " 4K", " HEVC"
    n = re.sub(r'\s+\d{3,4}[Pp]\b', '', n)
    n = re.sub(r'\s+(HD|FHD|UHD|SD|HEVC|H\.?265|H\.?264|AVC)\b', '', n, flags=re.IGNORECASE)
    n = re.sub(r'\s+\(.*?\)', '', n)
    n = re.sub(r'\s+\[.*?\]', '', n)
    n = re.sub(r'\s+', '', n)
    return n.lower()


def resolution_score(resolution: str) -> int:
    """Convert resolution string to a score for comparison (higher = better)."""
    # e.g. "1920x1080" → 1920*1080 ≈ 2M
    if "x" in resolution.lower():
        try:
            w, h = resolution.lower().replace("x", " ").split()[:2]
            return int(w) * int(h)
        except (ValueError, IndexError):
            pass
    # 4K → ~8M
    if "4k" in resolution.lower() or "2160" in resolution:
        return 3840 * 2160
    if "1080" in resolution:
        return 1920 * 1080
    if "720" in resolution:
        return 1280 * 720
    return 0
