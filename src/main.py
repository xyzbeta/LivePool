#!/usr/bin/env python3
"""LivePool CLI entry point."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _migrate_data_dirs():
    """Migrate old project-root paths to new consolidated data/ structure."""
    import shutil
    old_dirs = [
        (PROJECT_ROOT / "output", PROJECT_ROOT / "data" / "output"),
        (PROJECT_ROOT / "logs", PROJECT_ROOT / "data" / "logs"),
        (PROJECT_ROOT / "sources", PROJECT_ROOT / "data" / "sources"),
    ]
    for old_path, new_path in old_dirs:
        if old_path.exists() and not new_path.exists():
            try:
                if old_path.is_dir() and any(old_path.iterdir()):
                    shutil.copytree(old_path, new_path, dirs_exist_ok=True)
                    print(f"  Migrated {old_path.name}/ → data/{old_path.name}/")
                old_path.rename(old_path.with_suffix(old_path.suffix + ".migrated"))
            except Exception:
                pass


def setup_logging():
    """Configure logging based on config."""
    from src.config import PROJECT_ROOT, get_logging_config

    cfg = get_logging_config()
    level = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)
    log_dir = PROJECT_ROOT / cfg.get("dir", "data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler
    fh = logging.FileHandler(log_dir / "app.log", encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(fh)
    root.addHandler(ch)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_run():
    """Execute the full pipeline once."""
    from src.scheduler import run_pipeline

    asyncio.run(run_pipeline())


def cmd_schedule():
    """Start the built-in APScheduler daemon."""
    from src.scheduler import start_scheduler

    start_scheduler()


def cmd_cron():
    """Print crontab expression for system cron."""
    from src.scheduler import print_cron_expression

    print_cron_expression()


def cmd_web():
    """Start the FastAPI web server."""
    import uvicorn
    from src.config import get_web_config

    cfg = get_web_config()
    host = cfg.get("host", "0.0.0.0")
    port = cfg.get("port", 8000)

    logging.getLogger().info(f"Starting web server on http://{host}:{port}")
    uvicorn.run(
        "src.api:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="LivePool: IPTV stream collector, validator, and m3u8 generator",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    sub.add_parser("run", help="Run full pipeline once (collect → validate → filter → generate)")
    sub.add_parser("schedule", help="Start APScheduler daemon for periodic runs")
    sub.add_parser("cron", help="Print crontab expression for system crontab")
    sub.add_parser("web", help="Start FastAPI web server with dashboard")

    args = parser.parse_args()

    # Migrate legacy data directories to consolidated data/ structure
    _migrate_data_dirs()

    setup_logging()

    if args.command == "run":
        cmd_run()
    elif args.command == "schedule":
        cmd_schedule()
    elif args.command == "cron":
        cmd_cron()
    elif args.command == "web":
        cmd_web()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
