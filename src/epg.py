"""EPG (Electronic Program Guide) proxy: fetch, cache, and serve XMLTV data."""

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

import aiohttp

from .config import PROJECT_ROOT, load_config

logger = logging.getLogger(__name__)

EPG_CACHE_FILE = PROJECT_ROOT / "data" / "epg_cache.xml"
_epg_cache: Optional[str] = None
_epg_cache_time: float = 0
_EPG_REFRESH_INTERVAL = 21600  # 6 hours


def get_epg_config() -> dict:
    """Return the EPG section from config."""
    return load_config().get("epg", {})


def get_epg_source_url() -> str:
    """Return the configured EPG source URL, or empty string."""
    return get_epg_config().get("source", "")


async def fetch_epg() -> Optional[str]:
    """Fetch XMLTV data from the configured source URL.

    Returns:
        XMLTV content as string, or None on failure.
    """
    url = get_epg_source_url()
    if not url:
        return None

    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    headers = {
        "User-Agent": "LivePool/1.0",
        "Accept": "application/xml, text/xml, */*",
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    logger.warning(f"EPG fetch returned HTTP {resp.status} from {url}")
                    return None
                content = await resp.text(encoding="utf-8", errors="replace")
                if len(content) < 200:
                    logger.warning(f"EPG content too small ({len(content)} bytes)")
                    return None
                logger.info(f"EPG fetched: {len(content)} bytes from {url}")
                return content
    except asyncio.TimeoutError:
        logger.warning(f"EPG fetch timeout from {url}")
    except Exception as e:
        logger.warning(f"EPG fetch failed: {e}")

    return None


async def get_epg_async() -> Optional[str]:
    """Async version: fetch EPG if needed. Call from API endpoints."""
    global _epg_cache, _epg_cache_time

    now = time.monotonic()
    if _epg_cache is not None and (now - _epg_cache_time) < _EPG_REFRESH_INTERVAL:
        return _epg_cache

    # Try file cache
    if EPG_CACHE_FILE.exists():
        file_age = now - EPG_CACHE_FILE.stat().st_mtime
        if file_age < _EPG_REFRESH_INTERVAL:
            try:
                content = EPG_CACHE_FILE.read_text(encoding="utf-8")
                if content and len(content) > 200:
                    _epg_cache = content
                    _epg_cache_time = now
                    return content
            except Exception:
                pass

    # Fetch fresh
    content = await fetch_epg()
    if content:
        _epg_cache = content
        _epg_cache_time = now
        try:
            EPG_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            EPG_CACHE_FILE.write_text(content, encoding="utf-8")
        except Exception:
            pass
        return content

    # Stale cache fallback
    if EPG_CACHE_FILE.exists():
        try:
            content = EPG_CACHE_FILE.read_text(encoding="utf-8")
            if content and len(content) > 200:
                return content
        except Exception:
            pass

    return None


def get_epg() -> Optional[str]:
    """Sync version: for use in non-async contexts (e.g. generator)."""
    global _epg_cache, _epg_cache_time

    now = time.monotonic()
    if _epg_cache is not None and (now - _epg_cache_time) < _EPG_REFRESH_INTERVAL:
        return _epg_cache

    # File cache
    if EPG_CACHE_FILE.exists():
        file_age = now - EPG_CACHE_FILE.stat().st_mtime
        if file_age < _EPG_REFRESH_INTERVAL:
            try:
                content = EPG_CACHE_FILE.read_text(encoding="utf-8")
                if content and len(content) > 200:
                    _epg_cache = content
                    _epg_cache_time = now
                    return content
            except Exception:
                pass

    # Try async run (may fail if loop is running, catches gracefully)
    try:
        content = asyncio.run(fetch_epg())
        if content:
            _epg_cache = content
            _epg_cache_time = now
            try:
                EPG_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                EPG_CACHE_FILE.write_text(content, encoding="utf-8")
            except Exception:
                pass
            return content
    except RuntimeError:
        pass  # Running in async context, caller should use get_epg_async()

    # Stale cache fallback
    if EPG_CACHE_FILE.exists():
        try:
            content = EPG_CACHE_FILE.read_text(encoding="utf-8")
            if content and len(content) > 200:
                return content
        except Exception:
            pass

    return None


async def refresh_epg() -> bool:
    """Force refresh EPG cache. Returns True on success."""
    content = await fetch_epg()
    if content:
        global _epg_cache, _epg_cache_time
        _epg_cache = content
        _epg_cache_time = time.monotonic()
        try:
            EPG_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            EPG_CACHE_FILE.write_text(content, encoding="utf-8")
        except Exception:
            pass
        return True
    return False
