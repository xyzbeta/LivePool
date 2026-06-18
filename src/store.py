"""SQLite-based persistent storage engine.

Same API as the old JsonStore, but backed by SQLite via aiosqlite.
Auto-migrates JSON files on first run.
"""

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import aiosqlite

from .config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DB_PATH = PROJECT_ROOT / "data" / "livepool.db"


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------


async def _get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    return db


async def _ensure_tables(db: aiosqlite.Connection, key: str):
    if key == "users":
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'user',
                is_active INTEGER DEFAULT 1,
                subscription_token TEXT DEFAULT '',
                subscription_enabled INTEGER DEFAULT 0,
                subscribed_groups TEXT DEFAULT '*',
                favorites TEXT DEFAULT '[]',
                created_at TEXT,
                updated_at TEXT
            )
        """)
        # Migration: add favorites column for DBs created before v1.3
        try:
            await db.execute("ALTER TABLE users ADD COLUMN favorites TEXT DEFAULT '[]'")
        except Exception:
            pass
        # Migration: add TOTP 2FA columns for DBs created before v1.4
        for col in [
            ("totp_secret", "TEXT DEFAULT ''"),
            ("totp_enabled", "INTEGER DEFAULT 0"),
            ("totp_backup_codes", "TEXT DEFAULT '[]'"),
            ("force_2fa", "INTEGER DEFAULT 0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col[0]} {col[1]}")
            except Exception:
                pass
        # Migration: add subscription pull tracking
        for col in [
            ("pull_count", "INTEGER DEFAULT 0"),
            ("last_pull_at", "TEXT DEFAULT ''"),
        ]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col[0]} {col[1]}")
            except Exception:
                pass
    elif key == "invite_codes":
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invite_codes (
                id TEXT PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                created_by TEXT NOT NULL,
                max_uses INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0,
                used_by TEXT DEFAULT '[]',
                is_active INTEGER DEFAULT 1,
                created_at TEXT,
                updated_at TEXT,
                expires_at TEXT DEFAULT ''
            )
        """)
        try:
            await db.execute("ALTER TABLE invite_codes ADD COLUMN updated_at TEXT")
        except Exception:
            pass
    elif key == "sources":
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT DEFAULT 'raw_m3u',
                urls TEXT DEFAULT '[]',
                enabled INTEGER DEFAULT 1,
                last_fetch_at TEXT DEFAULT '',
                fetch_count INTEGER DEFAULT 0,
                fetch_error TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
            )
        """)
    elif key == "channels":
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                "group" TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                latency_ms REAL DEFAULT 0,
                resolution TEXT DEFAULT '',
                last_check TEXT DEFAULT '',
                last_alive TEXT DEFAULT '',
                source TEXT DEFAULT '',
                tvg_id TEXT DEFAULT '',
                tvg_logo TEXT DEFAULT '',
                has_cors INTEGER DEFAULT 0,
                has_video INTEGER DEFAULT 0,
                created_at TEXT DEFAULT ""
            )
        """)
        # Migration: add tvg_logo column for DBs created before v1.2
        try:
            await db.execute("ALTER TABLE channels ADD COLUMN tvg_logo TEXT DEFAULT ''")
        except Exception:
            pass  # column already exists
        # Migration: add score column for quality display
        try:
            await db.execute("ALTER TABLE channels ADD COLUMN score REAL DEFAULT 0.0")
        except Exception:
            pass
    elif key == "stats":
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stats_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                total INTEGER, alive INTEGER, dead INTEGER,
                timeout INTEGER, error INTEGER, audio INTEGER,
                duration_sec REAL
            )
        """)
    elif key == "local_seeds":
        await db.execute("""
            CREATE TABLE IF NOT EXISTS local_seeds (
                filename TEXT PRIMARY KEY,
                enabled INTEGER DEFAULT 1
            )
        """)
    await db.commit()


# ---------------------------------------------------------------------------
# JSON → SQLite migration
# ---------------------------------------------------------------------------

_JSON_FILES = {
    "users": PROJECT_ROOT / "data" / "users.json",
    "sources": PROJECT_ROOT / "data" / "sources.json",
    "channels": PROJECT_ROOT / "data" / "channels_state.json",
}


async def _migrate_json(key: str, db: aiosqlite.Connection):
    """Migrate data from JSON file to SQLite if JSON exists and table is empty."""
    json_path = _JSON_FILES.get(key)
    if not json_path or not json_path.exists():
        return

    # Check if table already has data
    cursor = await db.execute(f"SELECT COUNT(*) FROM {key}")
    count = (await cursor.fetchone())[0]
    if count > 0:
        return  # already migrated

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        items = data.get(key, []) if isinstance(data, dict) and key in data else (data if isinstance(data, list) else [])

        if not items:
            return

        if key == "users":
            for item in items:
                await db.execute(
                    "INSERT OR IGNORE INTO users (id,username,password_hash,role,is_active,subscription_token,subscription_enabled,subscribed_groups,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (item.get("id", uuid.uuid4().hex[:12]), item.get("username", ""), item.get("password_hash", ""),
                     item.get("role", "user"), int(item.get("is_active", 1)), item.get("subscription_token", ""),
                     int(item.get("subscription_enabled", 0)), item.get("subscribed_groups", "*"), item.get("created_at", "")),
                )
        elif key == "sources":
            for item in items:
                await db.execute(
                    "INSERT OR IGNORE INTO sources (id,name,type,urls,enabled,last_fetch_at,fetch_count,fetch_error,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (item.get("id", uuid.uuid4().hex[:12]), item.get("name", ""), item.get("type", "raw_m3u"),
                     json.dumps(item.get("urls", [])), int(item.get("enabled", 1)),
                     item.get("last_fetch_at", ""), item.get("fetch_count", 0), item.get("fetch_error", ""),
                     item.get("created_at", "")),
                )
        elif key == "channels":
            for item in items:
                await db.execute(
                    """INSERT OR IGNORE INTO channels (id,name,url,"group",status,latency_ms,resolution,last_check,last_alive,source,tvg_id,has_cors,has_video)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item.get("id", uuid.uuid4().hex[:12]), item.get("name", ""), item.get("url", ""),
                     item.get("group", ""), item.get("status", "pending"), item.get("latency_ms", 0),
                     item.get("resolution", ""), item.get("last_check", ""), item.get("last_alive", ""),
                     item.get("source", ""), item.get("tvg_id", ""), int(item.get("has_cors", 0)),
                     int(item.get("has_video", 0))),
                )

        await db.commit()

        # Rename JSON as backup
        backup = json_path.with_suffix(json_path.suffix + ".bak")
        os.rename(json_path, backup)
        logger.info(f"Migrated {len(items)} {key} from {json_path.name}")
    except Exception as e:
        logger.warning(f"Migration {key} failed: {e}")


# ---------------------------------------------------------------------------
# DbStore — same API as old JsonStore
# ---------------------------------------------------------------------------


class _TableDef:
    """Column definitions for each table type."""
    def __init__(self, table: str, cols: dict, default: dict):
        self.table = table
        self.cols = cols  # json_key → db_column
        self.default = default


_TABLES = {
    "users": _TableDef("users",
        {"id": "id", "username": "username", "password_hash": "password_hash", "role": "role",
         "is_active": "is_active", "subscription_token": "subscription_token",
         "subscription_enabled": "subscription_enabled", "subscribed_groups": "subscribed_groups",
         "favorites": "favorites", "totp_secret": "totp_secret",
         "totp_enabled": "totp_enabled", "totp_backup_codes": "totp_backup_codes",
         "force_2fa": "force_2fa",
         "created_at": "created_at", "updated_at": "updated_at"},
        {}),
    "sources": _TableDef("sources",
        {"id": "id", "name": "name", "type": "type", "urls": "urls", "enabled": "enabled",
         "last_fetch_at": "last_fetch_at", "fetch_count": "fetch_count", "fetch_error": "fetch_error",
         "created_at": "created_at", "updated_at": "updated_at"},
        {}),
    "channels": _TableDef("channels",
        {"id": "id", "name": "name", "url": "url", "group": "group", "status": "status",
         "latency_ms": "latency_ms", "resolution": "resolution", "last_check": "last_check",
         "last_alive": "last_alive", "source": "source", "tvg_id": "tvg_id",
         "tvg_logo": "tvg_logo",
         "has_cors": "has_cors", "has_video": "has_video",
         "score": "score"},
        {}),
    "invite_codes": _TableDef("invite_codes",
        {"id": "id", "code": "code", "created_by": "created_by",
         "max_uses": "max_uses", "used_count": "used_count", "used_by": "used_by",
         "is_active": "is_active", "created_at": "created_at", "expires_at": "expires_at"},
        {}),
}


class DbStore:
    """SQLite-backed store with the same API as the old JsonStore."""

    def __init__(self, path_hint: str, collection_key: str, default: Optional[dict] = None):
        self._key = collection_key
        self._migrated = False

    async def _init(self):
        """Ensure tables exist and migrate if needed. Called once lazily."""
        if self._migrated:
            return
        db = await _get_db()
        try:
            await _ensure_tables(db, self._key)
            await _migrate_json(self._key, db)
            self._migrated = True
        finally:
            await db.close()

    def _row_to_dict(self, row) -> dict:
        d = dict(row)
        # Unpack JSON fields
        if self._key == "sources" and "urls" in d and isinstance(d["urls"], str):
            try:
                d["urls"] = json.loads(d["urls"])
            except (json.JSONDecodeError, TypeError):
                d["urls"] = []
        if self._key == "users":
            for json_field in ("favorites", "totp_backup_codes"):
                if json_field in d and isinstance(d[json_field], str):
                    try:
                        d[json_field] = json.loads(d[json_field])
                    except (json.JSONDecodeError, TypeError):
                        d[json_field] = []
        if self._key == "invite_codes" and "used_by" in d and isinstance(d["used_by"], str):
            try:
                d["used_by"] = json.loads(d["used_by"])
            except (json.JSONDecodeError, TypeError):
                d["used_by"] = []
        # Convert integer booleans
        for bool_field in ("is_active", "subscription_enabled", "enabled", "has_cors", "has_video", "totp_enabled", "force_2fa"):
            if bool_field in d:
                d[bool_field] = bool(d[bool_field])
        return d

    async def all(self) -> List[dict]:
        await self._init()
        db = await _get_db()
        try:
            table = self._key if self._key in _TABLES else self._key
            cursor = await db.execute(f"SELECT * FROM {table}")
            rows = await cursor.fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            await db.close()

    async def get(self, item_id: str) -> Optional[dict]:
        await self._init()
        db = await _get_db()
        try:
            table = self._key if self._key in _TABLES else self._key
            cursor = await db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,))
            row = await cursor.fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            await db.close()

    async def find(self, predicate: Callable[[dict], bool]) -> Optional[dict]:
        # Note: This is less efficient than JSON (full table scan with Python filter)
        items = await self.all()
        for item in items:
            if predicate(item):
                return item
        return None

    async def add(self, item: dict) -> str:
        await self._init()
        db = await _get_db()
        try:
            table = self._key if self._key in _TABLES else self._key
            if "id" not in item or not item["id"]:
                item["id"] = uuid.uuid4().hex[:12]
            if "created_at" not in item:
                item["created_at"] = datetime.now().isoformat()

            cols = list(item.keys())
            placeholders = ",".join(["?"] * len(cols))
            # Quote column names to handle SQL reserved words like "group"
            quoted_cols = ",".join(f'"{c}"' if c == "group" else c for c in cols)
            values = []
            for k in cols:
                v = item[k]
                if isinstance(v, list):
                    v = json.dumps(v)
                elif isinstance(v, bool):
                    v = int(v)
                values.append(v)

            await db.execute(f"INSERT OR REPLACE INTO {table} ({quoted_cols}) VALUES ({placeholders})", values)
            await db.commit()
            return item["id"]
        finally:
            await db.close()

    async def update(self, item_id: str, patch: dict) -> bool:
        await self._init()
        db = await _get_db()
        try:
            table = self._key if self._key in _TABLES else self._key
            set_clauses = []
            values = []
            for k, v in patch.items():
                if isinstance(v, list):
                    v = json.dumps(v)
                elif isinstance(v, bool):
                    v = int(v)
                col = f'"{k}"' if k == "group" else k
                set_clauses.append(f"{col}=?")
                values.append(v)
            set_clauses.append("updated_at=?")
            values.append(datetime.now().isoformat())
            # item_id MUST be last — it fills the WHERE id=? placeholder
            values.append(item_id)

            cursor = await db.execute(
                f"UPDATE {table} SET {','.join(set_clauses)} WHERE id=?",
                values,
            )
            await db.commit()
            return cursor.rowcount > 0
        finally:
            await db.close()

    async def delete(self, item_id: str) -> bool:
        await self._init()
        db = await _get_db()
        try:
            table = self._key if self._key in _TABLES else self._key
            cursor = await db.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
            await db.commit()
            return cursor.rowcount > 0
        finally:
            await db.close()

    async def count(self) -> int:
        await self._init()
        db = await _get_db()
        try:
            table = self._key if self._key in _TABLES else self._key
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
            return (await cursor.fetchone())[0]
        finally:
            await db.close()

    async def replace_all(self, items: List[dict]) -> int:
        """Atomically replace all records: DELETE all then bulk INSERT in one
        transaction on a single connection. For channels table, this turns
        ~2N+1 connections into 1.

        Returns the number of records inserted.
        """
        await self._init()
        db = await _get_db()
        try:
            table = self._key if self._key in _TABLES else self._key

            await db.execute(f"DELETE FROM {table}")

            if not items:
                await db.commit()
                return 0

            # Build column list from first item (all items share same schema)
            now = datetime.now().isoformat()
            cols: Optional[list] = None
            quoted_cols: Optional[str] = None
            placeholders: Optional[str] = None

            for item in items:
                if "id" not in item or not item["id"]:
                    item["id"] = uuid.uuid4().hex[:12]
                if "created_at" not in item:
                    item["created_at"] = now

                if cols is None:
                    cols = list(item.keys())
                    placeholders = ",".join(["?"] * len(cols))
                    quoted_cols = ",".join(
                        f'"{c}"' if c == "group" else c for c in cols
                    )

                values = []
                for k in cols:
                    v = item.get(k, "")
                    if k == "urls" and isinstance(v, list):
                        v = json.dumps(v)
                    elif k == "favorites" and isinstance(v, list):
                        v = json.dumps(v)
                    elif isinstance(v, bool):
                        v = int(v)
                    values.append(v)

                await db.execute(
                    f"INSERT OR REPLACE INTO {table} ({quoted_cols})"
                    f" VALUES ({placeholders})",
                    values,
                )

            await db.commit()
            return len(items)
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Sync wrappers — maintain backward compatibility with existing code
# ---------------------------------------------------------------------------


class _SyncStore:
    """Synchronous wrapper around DbStore for backward compat."""

    def __init__(self, key: str, default: dict):
        self._store = DbStore("", key, default)
        self._key = key

    def _run(self, coro):
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        # Running inside event loop — use thread to avoid nesting
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=30)

    def all(self) -> List[dict]:
        return self._run(self._store.all())

    def get(self, item_id: str) -> Optional[dict]:
        return self._run(self._store.get(item_id))

    def find(self, predicate) -> Optional[dict]:
        return self._run(self._store.find(predicate))

    def add(self, item: dict) -> str:
        return self._run(self._store.add(item))

    def update(self, item_id: str, patch: dict) -> bool:
        return self._run(self._store.update(item_id, patch))

    def delete(self, item_id: str) -> bool:
        return self._run(self._store.delete(item_id))

    def count(self) -> int:
        return self._run(self._store.count())

    def replace_all(self, items: List[dict]) -> int:
        return self._run(self._store.replace_all(items))


def get_users_store() -> _SyncStore:
    return _SyncStore("users", {"users": []})


def get_sources_store() -> _SyncStore:
    return _SyncStore("sources", {"sources": []})


def get_channels_store() -> _SyncStore:
    return _SyncStore("channels", {"channels": []})


def get_invite_codes_store() -> _SyncStore:
    return _SyncStore("invite_codes", {"invite_codes": []})
