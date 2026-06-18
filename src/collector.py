"""Collector: orchestrates crawlers and local file imports to gather raw stream entries."""

import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Set

import aiohttp

from . import SourceType, StreamEntry
from .config import (
    PROJECT_ROOT,
    get_collector_config,
    get_local_seeds,
    get_validator_config,
)
from .parser import parse_m3u8_file, parse_txt_urls
from .sources import GitHubM3UCrawler, RawM3UCrawler
from .store import get_sources_store

logger = logging.getLogger(__name__)

# Crawler type → class mapping
CRAWLER_REGISTRY: Dict[str, type] = {
    "github_m3u": GitHubM3UCrawler,
    "raw_m3u": RawM3UCrawler,
}


def _migrate_sources_from_config():
    """On first run, seed sources.json from config.yaml crawlers."""
    store = get_sources_store()
    if store.count() > 0:
        return  # Already has sources

    from .config import get_enabled_crawlers

    config_crawlers = get_enabled_crawlers()
    migrated = 0
    for cfg in config_crawlers:
        store.add({
            "name": cfg["name"],
            "type": cfg.get("type", "raw_m3u"),
            "urls": cfg.get("urls", []),
            "enabled": cfg.get("enabled", True),
        })
        migrated += 1
    if migrated > 0:
        logger.info(f"Migrated {migrated} crawlers from config.yaml to sources.json")


def _build_crawlers() -> List:
    """Instantiate crawlers from sources.json (with config.yaml fallback)."""
    _migrate_sources_from_config()

    store = get_sources_store()
    config = get_collector_config()
    timeout = config.get("timeout", 30)
    crawlers = []

    for cfg in store.all():
        if not cfg.get("enabled", True):
            continue
        ctype = cfg.get("type", "")
        cls = CRAWLER_REGISTRY.get(ctype)
        if cls is None:
            logger.warning(f"Unknown crawler type '{ctype}' for '{cfg.get('name')}', skipping")
            continue
        crawlers.append(cls(name=cfg["name"], urls=cfg.get("urls", []), timeout=timeout))

    # Fallback: if sources.json is empty, use config.yaml directly
    if not crawlers:
        from .config import get_enabled_crawlers
        for cfg in get_enabled_crawlers():
            ctype = cfg.get("type", "")
            cls = CRAWLER_REGISTRY.get(ctype)
            if cls:
                crawlers.append(cls(name=cfg["name"], urls=cfg.get("urls", []), timeout=timeout))

    return crawlers


def _import_local_seeds() -> List[StreamEntry]:
    """Import stream entries from local seed files in sources/ directory."""
    entries: List[StreamEntry] = []
    sources_dir = PROJECT_ROOT / "data" / "sources"

    local_seeds = get_local_seeds()
    if not local_seeds and sources_dir.exists():
        # Auto-discover: all .m3u8, .m3u and .txt files
        local_seeds = sorted(
            [str(p.relative_to(PROJECT_ROOT)) for p in sources_dir.glob("*.m3u8")]
            + [str(p.relative_to(PROJECT_ROOT)) for p in sources_dir.glob("*.m3u")]
            + [str(p.relative_to(PROJECT_ROOT)) for p in sources_dir.glob("*.txt")]
        )
    if not local_seeds:
        # Fallback: project-root sources/ (dev/repo default)
        legacy_dir = PROJECT_ROOT / "sources"
        if legacy_dir.exists():
            local_seeds = sorted(
                [str(p.relative_to(PROJECT_ROOT)) for p in legacy_dir.glob("*.m3u8")]
                + [str(p.relative_to(PROJECT_ROOT)) for p in legacy_dir.glob("*.m3u")]
                + [str(p.relative_to(PROJECT_ROOT)) for p in legacy_dir.glob("*.txt")]
            )

    # Filter disabled seeds (state stored in SQLite)
    _seed_enabled = {}
    try:
        import sqlite3 as _sqlite3
        from .store import DB_PATH
        _conn = _sqlite3.connect(str(DB_PATH))
        _conn.row_factory = _sqlite3.Row
        _conn.execute("CREATE TABLE IF NOT EXISTS local_seeds (filename TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1)")
        for _row in _conn.execute("SELECT filename, enabled FROM local_seeds").fetchall():
            _seed_enabled[_row["filename"]] = bool(_row["enabled"])
        _conn.close()
    except Exception:
        pass
    local_seeds = [
        s for s in local_seeds
        if _seed_enabled.get(Path(s).name, True)
    ]

    for seed_path_str in local_seeds:
        seed_path = PROJECT_ROOT / seed_path_str
        if not seed_path.exists():
            logger.warning(f"Local seed not found: {seed_path}")
            continue

        try:
            suffix = seed_path.suffix.lower()
            if suffix == ".m3u8" or suffix == ".m3u":
                parsed = parse_m3u8_file(seed_path)
            elif suffix == ".txt":
                parsed = parse_txt_urls(seed_path)
            else:
                logger.warning(f"Unsupported seed format: {seed_path}")
                continue

            logger.info(f"Imported {len(parsed)} entries from {seed_path}")
            entries.extend(parsed)
        except Exception as e:
            logger.error(f"Error importing {seed_path}: {e}")

    return entries


async def collect() -> List[StreamEntry]:
    """Run all enabled crawlers + import local seeds.

    Returns deduplicated list of raw StreamEntry objects.
    """
    all_entries: List[StreamEntry] = []

    # 1. Import local seeds (synchronous, fast)
    local_entries = _import_local_seeds()
    all_entries.extend(local_entries)
    logger.info(f"Local seeds: {len(local_entries)} entries")

    # 2. Run remote crawlers concurrently
    crawlers = _build_crawlers()
    if not crawlers:
        logger.info("No remote crawlers enabled")
        return _dedup(all_entries)

    timeout = get_collector_config().get("timeout", 30)
    proxy = get_collector_config().get("proxy") or get_validator_config().get("proxy")
    connector_kwargs = {"limit": 10, "limit_per_host": 5}
    headers = {"User-Agent": get_validator_config().get("user_agent", "LivePool/1.0")}
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(**connector_kwargs),
        timeout=aiohttp.ClientTimeout(total=timeout),
        headers=headers,
    ) as session:
        tasks = [crawler.fetch(session) for crawler in crawlers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for crawler, result in zip(crawlers, results):
        if isinstance(result, Exception):
            logger.error(f"Crawler '{crawler.name}' failed: {result}")
        else:
            logger.info(f"Crawler '{crawler.name}': {len(result)} entries")
            all_entries.extend(result)

    # 3. Deduplicate by URL
    unique = _dedup(all_entries)
    logger.info(f"Total collected: {len(all_entries)}, unique: {len(unique)}")
    return unique


def _dedup(entries: List[StreamEntry]) -> List[StreamEntry]:
    """Deduplicate by URL, keeping first occurrence."""
    seen: Set[str] = set()
    result: List[StreamEntry] = []
    for e in entries:
        if e.url not in seen:
            seen.add(e.url)
            result.append(e)
    return result
