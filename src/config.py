"""
Configuration loader. Reads config.yaml and provides typed access.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_config_cache: Optional[Dict[str, Any]] = None


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load config.yaml and return as dict. Cached on first call."""
    global _config_cache
    if _config_cache is not None and path is None:
        return _config_cache

    target = path or CONFIG_PATH
    if not target.exists():
        raise FileNotFoundError(f"Config file not found: {target}")

    with open(target, "r", encoding="utf-8") as f:
        _config_cache = yaml.safe_load(f)
    return _config_cache


def reload_config() -> Dict[str, Any]:
    """Force reload from disk."""
    global _config_cache
    _config_cache = None
    return load_config()


# ---------------------------------------------------------------------------
# Typed accessors
# ---------------------------------------------------------------------------


def get_collector_config() -> Dict[str, Any]:
    return load_config().get("collector", {})


def get_enabled_crawlers() -> List[Dict[str, Any]]:
    crawlers = get_collector_config().get("crawlers", [])
    return [c for c in crawlers if c.get("enabled", True)]


def get_local_seeds() -> List[str]:
    return get_collector_config().get("local_seeds", [])


def get_validator_config() -> Dict[str, Any]:
    return load_config().get("validator", {})


def get_classifier_config() -> Dict[str, Any]:
    return load_config().get("classifier", {})


def get_generator_config() -> Dict[str, Any]:
    return load_config().get("generator", {})


def get_scheduler_config() -> Dict[str, Any]:
    return load_config().get("scheduler", {})


def get_web_config() -> Dict[str, Any]:
    return load_config().get("web", {})


def get_logging_config() -> Dict[str, Any]:
    return load_config().get("logging", {})


def resolve_path(key: str, default: str) -> Path:
    """Resolve a config path relative to project root."""
    path_str = load_config().get(key, default)
    p = Path(path_str)
    if p.is_absolute():
        return p
    return PROJECT_ROOT / p
