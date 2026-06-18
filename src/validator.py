"""Async stream validator: concurrent HTTP probing + content validation."""

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import aiohttp

from . import CheckResult, StreamEntry, StreamStatus
from .config import PROJECT_ROOT, get_validator_config

logger = logging.getLogger(__name__)

# Number of bytes to read from GET response for content validation
BODY_SAMPLE_SIZE = 2048


async def validate(
    entries: List[StreamEntry],
    concurrency: Optional[int] = None,
    connect_timeout: Optional[int] = None,
    read_timeout: Optional[int] = None,
    total_timeout: Optional[int] = None,
    user_agent: Optional[str] = None,
    retry: Optional[int] = None,
    proxy: Optional[str] = None,
    progress_callback=None,
) -> List[CheckResult]:
    cfg = get_validator_config()
    concurrency = concurrency or cfg.get("concurrency", 50)
    connect_timeout = connect_timeout or cfg.get("connect_timeout", 5)
    read_timeout = read_timeout or cfg.get("read_timeout", 10)
    total_timeout = total_timeout or cfg.get("total_timeout", 15)
    user_agent = user_agent or cfg.get("user_agent", "Mozilla/5.0")
    retry = retry if retry is not None else cfg.get("retry", 0)
    proxy = proxy or cfg.get("proxy")
    deep_check = cfg.get("deep_check", True)
    content_min = cfg.get("content_min_bytes", 256)

    semaphore = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=10)
    timeout = aiohttp.ClientTimeout(total=total_timeout, connect=connect_timeout, sock_read=read_timeout)
    headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    results: List[CheckResult] = []
    if entries:
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=headers,
            proxy=proxy,
        ) as session:
            total = len(entries)
            done_count = 0

            async def _check_with_progress(entry: StreamEntry) -> CheckResult:
                nonlocal done_count
                result = await _check_one(session, entry, semaphore, retry, deep_check, content_min)
                done_count += 1
                if progress_callback:
                    progress_callback("validate", f"校验中: {done_count}/{total}")
                return result

            tasks = [_check_with_progress(entry) for entry in entries]
            results = await asyncio.gather(*tasks)

    alive = sum(1 for r in results if r.is_alive)
    dead = len(results) - alive
    logger.info(f"Validation complete: {alive} alive, {dead} dead out of {len(results)}")
    return results


async def _check_one(
    session: aiohttp.ClientSession,
    entry: StreamEntry,
    semaphore: asyncio.Semaphore,
    retry: int = 0,
    deep_check: bool = True,
    content_min: int = 256,
) -> CheckResult:
    """Validate a single stream URL.

    Phase 1 (inside semaphore): GET + status + content validation.
    Phase 2 (outside semaphore): segment HEAD + NAL scan.
    """
    result = CheckResult(url=entry.url, name=entry.name)
    max_attempts = retry + 1

    for attempt in range(max_attempts):
        seg_url: Optional[str] = None
        retry_flag = False

        # ── Phase 1: GET + content validation (inside semaphore) ─────────
        async with semaphore:
            start = time.monotonic()

            try:
                parsed = urlparse(entry.url)
                referer = f"{parsed.scheme}://{parsed.netloc}/"

                async with session.get(
                    entry.url,
                    allow_redirects=True,
                    timeout=session.timeout,
                    headers={"Referer": referer},
                ) as resp:
                    result.latency_ms = round((time.monotonic() - start) * 1000, 1)
                    result.http_code = resp.status
                    result.content_type = resp.content_type or ""
                    result.has_cors = (
                        resp.headers.get("Access-Control-Allow-Origin") is not None
                    )

                    if resp.status in (401, 403):
                        result.status = StreamStatus.DEAD
                        result.error_msg = (
                            f"Stream requires auth (HTTP {resp.status}) — unplayable"
                        )
                        result.checked_at = datetime.now().isoformat()
                        return result

                    if not (200 <= resp.status < 400):
                        result.status = StreamStatus.DEAD
                        result.error_msg = f"HTTP {resp.status}"
                        result.checked_at = datetime.now().isoformat()
                        return result

                    if not deep_check:
                        result.status = StreamStatus.ALIVE
                        result.checked_at = datetime.now().isoformat()
                        return result

                    body = await resp.content.read(BODY_SAMPLE_SIZE)
                    body_text = body.decode("utf-8", errors="replace")
                    result.body_sample = body_text[:128]
                    result.latency_ms = round((time.monotonic() - start) * 1000, 1)

                    _extract_resolution_from_content(body_text, entry)

                    if len(body) < content_min:
                        result.status = StreamStatus.DEAD
                        result.error_msg = f"Response too small ({len(body)} bytes)"
                        result.checked_at = datetime.now().isoformat()
                        return result
                    if body_text.lstrip().startswith(("<", "<!")):
                        result.status = StreamStatus.DEAD
                        result.error_msg = "Response is HTML, not m3u8"
                        result.checked_at = datetime.now().isoformat()
                        return result
                    if (
                        not _has_m3u_signature(body_text)
                    ):
                        result.status = StreamStatus.DEAD
                        result.error_msg = (
                            "Response missing #EXTM3U / #EXTINF header"
                        )
                        result.checked_at = datetime.now().isoformat()
                        return result

                    cfg = get_validator_config()
                    if cfg.get("segment_check", True):
                        seg_url = _extract_first_segment(body_text, entry.url)

            except asyncio.TimeoutError:
                result.latency_ms = round((time.monotonic() - start) * 1000, 1)
                result.status = StreamStatus.TIMEOUT
                result.error_msg = "GET timeout"
                retry_flag = True
            except aiohttp.ClientConnectorError as e:
                result.latency_ms = round((time.monotonic() - start) * 1000, 1)
                result.status = StreamStatus.DEAD
                result.error_msg = f"Connection refused: {e}"
                result.checked_at = datetime.now().isoformat()
                return result
            except aiohttp.ClientError as e:
                result.latency_ms = round((time.monotonic() - start) * 1000, 1)
                if attempt < max_attempts - 1:
                    retry_flag = True
                else:
                    result.status = StreamStatus.ERROR
                    result.error_msg = str(e)[:200]
                    result.checked_at = datetime.now().isoformat()
                    return result
            except Exception as e:
                result.latency_ms = round((time.monotonic() - start) * 1000, 1)
                if attempt < max_attempts - 1:
                    retry_flag = True
                else:
                    result.status = StreamStatus.ERROR
                    result.error_msg = f"Unknown: {e}"
                    result.checked_at = datetime.now().isoformat()
                    return result

        # ── Retry? ──────────────────────────────────────────────────────
        if retry_flag:
            if attempt < max_attempts - 1:
                await asyncio.sleep(1)
            continue

        # ── Phase 2: segment probe (OUTSIDE semaphore) ──────────────────
        if seg_url:
            seg_ok = await _check_segment(session, seg_url)
            if not seg_ok:
                result.status = StreamStatus.DEAD
                result.error_msg = "Media segment unreachable"
                result.checked_at = datetime.now().isoformat()
                return result

            has_vid, res_est = await _check_video(session, seg_url)
            result.has_video = has_vid
            if res_est and not entry.resolution:
                entry.resolution = res_est
            if not has_vid:
                result.status = StreamStatus.AUDIO
                result.error_msg = "Audio-only (no video track)"
                result.checked_at = datetime.now().isoformat()
                return result

        # All checks passed
        result.status = StreamStatus.ALIVE
        result.has_video = True
        result.checked_at = datetime.now().isoformat()
        return result

    result.checked_at = datetime.now().isoformat()
    return result


def _has_m3u_signature(text: str) -> bool:
    """Return True if *text* contains #EXTM3U / #EXTINF, tolerant of BOM
    and leading comment / blank lines that may appear before the header."""
    cleaned = text.lstrip("﻿").lstrip()
    if "#EXTM3U" in cleaned[:512] or "#EXTINF" in cleaned[:512]:
        return True
    for line in cleaned.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#EXTM3U") or stripped.startswith("#EXTINF"):
            return True
        if not stripped.startswith("#"):
            break
    return False


def _extract_resolution_from_content(body_text: str, entry) -> None:
    """Parse m3u8 playlist for RESOLUTION= in #EXT-X-STREAM-INF tags."""
    match = re.search(r'#EXT-X-STREAM-INF:.*RESOLUTION=(\d+x\d+)', body_text, re.IGNORECASE)
    if match:
        entry.resolution = match.group(1)
    else:
        match = re.search(r'tvg-resolution="(\d+x\d+)"', body_text)
        if match:
            entry.resolution = match.group(1)


def _extract_first_segment(body_text: str, base_url: str, max_depth: int = 2) -> Optional[str]:
    """Parse m3u8 content and return the absolute URL of the first media segment."""
    lines = body_text.splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("http://") or line.startswith("https://"):
            url = line
        else:
            url = urljoin(base_url, line)

        if url.lower().endswith((".m3u8", ".m3u")):
            continue

        return url
    return None


async def _check_segment(
    session: aiohttp.ClientSession,
    seg_url: str,
) -> bool:
    """Quick HEAD check on a single media segment. Returns True if reachable."""
    try:
        async with session.head(
            seg_url,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=3, connect=1.5),
        ) as resp:
            return 200 <= resp.status < 400
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


async def _check_video(
    session: aiohttp.ClientSession,
    seg_url: str,
) -> tuple:
    """Download first 8KB of a TS segment, scan for video NAL and estimate resolution."""
    seg_size = 0
    try:
        headers = {"Range": "bytes=0-8191"}
        async with session.get(
            seg_url,
            headers=headers,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=4, connect=2),
        ) as resp:
            if resp.status not in (200, 206):
                return (False, "")
            cr = resp.headers.get("Content-Range", "")
            if cr and "/" in cr:
                try:
                    seg_size = int(cr.rsplit("/", 1)[-1])
                except ValueError:
                    pass
            data = await resp.content.read(8192)
    except asyncio.CancelledError:
        raise
    except Exception:
        return (False, "")

    if len(data) < 188:
        return (False, "")

    has_video = False
    for i in range(len(data) - 5):
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 0 and data[i + 3] == 1:
            nal_type = data[i + 4] & 0x1F
            if nal_type in (1, 2, 3, 4, 5, 19):
                has_video = True
                break
            hevc_type = (data[i + 4] >> 1) & 0x3F
            if hevc_type <= 31:
                has_video = True
                break
        if i < len(data) - 4 and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            nal_type = data[i + 3] & 0x1F
            if nal_type in (1, 2, 3, 4, 5, 19):
                has_video = True
                break
            hevc_type = (data[i + 3] >> 1) & 0x3F
            if hevc_type <= 31:
                has_video = True
                break

    resolution = ""
    if seg_size > 0:
        if seg_size > 1_500_000:
            resolution = "1920x1080"
        elif seg_size > 600_000:
            resolution = "1280x720"
        elif seg_size > 150_000:
            resolution = "720x576"

    return (has_video, resolution)
