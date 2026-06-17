"""Task scheduler for periodic collection + validation + generation."""

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import Stats
from .classifier import classify
from .collector import collect
from .config import get_scheduler_config
from .filter import dedup_by_name, dedup_by_url, filter_alive
from .generator import cache_logos, generate, save_state
from .store import get_sources_store
from .validator import validate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------


async def run_pipeline(progress_callback=None) -> Stats:
    """Execute the full pipeline. Optionally call progress_callback(step, detail) between stages."""

    def _progress(step: str, detail: str = ""):
        logger.info(f"Pipeline: {step} - {detail}")
        if progress_callback:
            progress_callback(step, detail)

    started = time.monotonic()
    stats = Stats()

    # 1. Collect
    _progress("collect", "正在从采集源获取频道列表...")
    entries = await collect()
    stats.sources_count = len(set(e.source for e in entries))
    _progress("collect", f"获取到 {len(entries)} 个频道")

    # 2. Validate
    _progress("validate", f"正在校验 {len(entries)} 个链接...")
    results = await validate(entries, progress_callback=progress_callback)
    alive_count = sum(1 for r in results if r.is_alive)
    _progress("validate", f"校验完成: {alive_count} 响应正常, {len(entries) - alive_count} 异常")

    # 3. Filter — build ChannelRecord for every entry (preserve full counts)
    _progress("filter", "正在整理校验结果...")
    alive_raw, dead_raw = filter_alive(entries, results)

    # 4. Classify all records (alive + dead) for complete state
    _progress("classify", "正在分类...")
    alive_raw = classify(alive_raw)
    dead_raw = classify(dead_raw)

    # Save FULL state (all records) BEFORE dedup
    save_state(alive_raw + dead_raw)

    # 5. Dedup + Generate (alive only, deduped for cleaner m3u8)
    _progress("generate", "正在去重并生成 m3u8...")
    alive_dedup = dedup_by_url(alive_raw)
    alive_dedup = dedup_by_name(alive_dedup)

    # Cache logos to local storage for faster / more reliable serving
    _progress("cache_logos", "正在缓存频道图标到本地...")
    logo_cache = await cache_logos(alive_dedup)
    # Rewrite local paths to include anti-hotlinking token
    if logo_cache:
        from .auth import get_logo_token
        logo_token = get_logo_token()
        logo_cache = {
            url: f"/api/logo/{logo_token}/{Path(local).name}"
            for url, local in logo_cache.items()
        }
    cached_count = sum(1 for v in logo_cache.values() if "/api/logo/" in v)
    _progress("cache_logos", f"图标缓存完成: {cached_count} 个本地, {len(logo_cache) - cached_count} 个远程")
    output_path = generate(alive_dedup, logo_cache=logo_cache)
    _progress("generate", f"已生成: {output_path} ({len(alive_dedup)} 个唯一频道)")

    # 6. EPG refresh (if configured)
    from .epg import refresh_epg, get_epg_source_url
    if get_epg_source_url():
        _progress("epg", "正在刷新 EPG 节目数据...")
        epg_ok = await refresh_epg()
        _progress("epg", "EPG 刷新" + ("完成" if epg_ok else "失败（源不可用）"))
    else:
        logger.debug("EPG not configured, skipping refresh")

    # Build stats
    elapsed = time.monotonic() - started
    stats.total = len(entries)
    stats.alive = len(alive_dedup)  # deduped = actual m3u8 channel count
    stats.dead = sum(1 for r in results if r.status.value == "dead")
    stats.timeout = sum(1 for r in results if r.status.value == "timeout")
    stats.error = sum(1 for r in results if r.status.value == "error")
    stats.audio = sum(1 for r in results if r.status.value == "audio")

    # Update source fetch history
    src_store = get_sources_store()
    src_entry_counts: dict = {}
    for e in entries:
        src_entry_counts[e.source] = src_entry_counts.get(e.source, 0) + 1
    for s in src_store.all():
        total_entries = 0
        for url in s.get("urls", []):
            total_entries += src_entry_counts.get(url, 0)
        src_store.update(s["id"], {
            "last_fetch_at": datetime.now().isoformat(),
            "fetch_count": total_entries,
            "fetch_error": "",
        })

    for ch in alive_dedup:
        stats.groups[ch.group] = stats.groups.get(ch.group, 0) + 1
    stats.last_check = datetime.now().isoformat()
    stats.check_duration_sec = round(elapsed, 1)

    # Save snapshot for trend comparison
    _save_stats_snapshot(stats)

    logger.info("=" * 60)
    logger.info(
        f"Pipeline complete: {stats.alive}/{stats.total} alive, "
        f"{stats.dead} dead, {elapsed:.1f}s"
    )
    logger.info(f"Output: {output_path}")

    return stats


def _save_stats_snapshot(stats: Stats):
    """Save current stats as JSON for trend comparison."""
    import json
    from .config import PROJECT_ROOT
    snap = {
        "total": stats.total, "alive": stats.alive, "dead": stats.dead,
        "timeout": stats.timeout, "error": stats.error, "audio": stats.audio,
        "duration_sec": stats.check_duration_sec,
        "last_check": stats.last_check,
    }
    path = PROJECT_ROOT / "data" / "stats_snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# APScheduler integration
# ---------------------------------------------------------------------------


def create_scheduler() -> AsyncIOScheduler:
    """Create and configure the APScheduler instance."""
    cfg = get_scheduler_config()
    cron_expr = cfg.get("cron", "0 */6 * * *")
    timezone = cfg.get("timezone", "Asia/Shanghai")

    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(
        _scheduled_run,
        trigger=CronTrigger.from_crontab(cron_expr, timezone=timezone),
        id="pipeline_run",
        name="LivePool pipeline",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    logger.info(f"APScheduler configured: cron='{cron_expr}', tz='{timezone}'")
    return scheduler


async def _scheduled_run():
    """Wrapper for scheduled runs with error handling."""
    try:
        await run_pipeline()
    except Exception as e:
        logger.exception(f"Scheduled pipeline run failed: {e}")


def start_scheduler():
    """Blocking entry point: start the scheduler and keep alive."""
    logger.info("Starting LivePool scheduler...")

    # Create event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    scheduler = create_scheduler()
    scheduler.start()

    # Run once immediately on startup
    loop.run_until_complete(run_pipeline())

    # Register signal handlers for graceful shutdown
    def _shutdown(signame):
        logger.info(f"Received {signame}, shutting down...")
        loop.call_soon_threadsafe(loop.stop)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown, sig.name)

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("Scheduler shutting down...")
    finally:
        scheduler.shutdown()
        loop.close()


def print_cron_expression():
    """Print the cron expression for use with system crontab."""
    cfg = get_scheduler_config()
    cron_expr = cfg.get("cron", "0 */6 * * *")
    project_root = __file__.rsplit("/src/", 1)[0]
    cmd = f"{cron_expr} cd {project_root} && {sys.executable} src/main.py run >> data/cron.log 2>&1"
    print(f"# Add this line to crontab (crontab -e):")
    print(cmd)
