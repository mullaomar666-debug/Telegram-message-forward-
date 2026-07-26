"""
database.py — SQLite database layer using aiosqlite.

Schema v2: rules support unlimited sources and destinations (rule_chats table),
with filters and options embedded directly on the rule row.
"""

import json
import asyncio
import aiosqlite
from typing import Any, Optional

from config import DATABASE_PATH, LOG_MAX_ROWS

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Drop legacy v1 tables (single source→destination model)
DROP TABLE IF EXISTS filters;
DROP TABLE IF EXISTS rules;
DROP TABLE IF EXISTS sources;
DROP TABLE IF EXISTS destinations;

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules_v2 (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT    NOT NULL DEFAULT '',
    enabled            INTEGER NOT NULL DEFAULT 1,
    -- Options
    delay_ms           INTEGER NOT NULL DEFAULT 0,
    retry_count        INTEGER NOT NULL DEFAULT 3,
    silent             INTEGER NOT NULL DEFAULT 0,
    forward_as_copy    INTEGER NOT NULL DEFAULT 0,
    prefix             TEXT    NOT NULL DEFAULT '',
    suffix             TEXT    NOT NULL DEFAULT '',
    -- Filters
    msg_types          TEXT    NOT NULL DEFAULT '[]',
    whitelist          TEXT    NOT NULL DEFAULT '[]',
    blacklist          TEXT    NOT NULL DEFAULT '[]',
    regex_include      TEXT    NOT NULL DEFAULT '',
    regex_exclude      TEXT    NOT NULL DEFAULT '',
    forward_media_no_text INTEGER NOT NULL DEFAULT 1,
    ca_only            INTEGER NOT NULL DEFAULT 0,
    dup_protection     INTEGER NOT NULL DEFAULT 1,
    first_ca_only      INTEGER NOT NULL DEFAULT 0,
    ignore_dup_forever INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now','utc'))
);

CREATE TABLE IF NOT EXISTS rule_chats (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id   INTEGER NOT NULL REFERENCES rules_v2(id) ON DELETE CASCADE,
    role      TEXT    NOT NULL,
    chat_id   TEXT    NOT NULL,
    title     TEXT    NOT NULL DEFAULT '',
    username  TEXT    NOT NULL DEFAULT '',
    chat_type TEXT    NOT NULL DEFAULT 'channel'
);

CREATE INDEX IF NOT EXISTS idx_rule_chats_rule ON rule_chats(rule_id, role);
CREATE INDEX IF NOT EXISTS idx_rule_chats_chat ON rule_chats(chat_id, role);

CREATE TABLE IF NOT EXISTS forwarded_cas (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_address TEXT NOT NULL,
    rule_id          INTEGER,
    created_at       TEXT NOT NULL DEFAULT (datetime('now','utc')),
    UNIQUE (contract_address)
);

CREATE TABLE IF NOT EXISTS logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    INTEGER,
    dest_id      INTEGER,
    source_title TEXT    NOT NULL DEFAULT '',
    dest_title   TEXT    NOT NULL DEFAULT '',
    message_id   INTEGER,
    message_text TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'ok',
    error_msg    TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now','utc'))
);

CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_logs_status  ON logs(status);
"""

_db: Optional[aiosqlite.Connection] = None
_lock = asyncio.Lock()


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(str(DATABASE_PATH))
        _db.row_factory = aiosqlite.Row
        await _db.executescript(SCHEMA_SQL)
        await _migrate(_db)
        await _db.commit()
    return _db


async def _migrate(db: aiosqlite.Connection):
    """Additive migrations for databases created before new columns existed."""
    async with db.execute("PRAGMA table_info(rules_v2)") as cur:
        cols = {row["name"] for row in await cur.fetchall()}
    if "forward_media_no_text" not in cols:
        await db.execute(
            "ALTER TABLE rules_v2 ADD COLUMN forward_media_no_text INTEGER NOT NULL DEFAULT 1"
        )


async def close_db():
    global _db
    if _db is not None:
        await _db.close()
        _db = None


# ── Generic helpers ───────────────────────────────────────────────────────────

async def fetchone(sql: str, params: tuple = ()) -> Optional[dict]:
    db = await get_db()
    async with db.execute(sql, params) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def fetchall(sql: str, params: tuple = ()) -> list[dict]:
    db = await get_db()
    async with db.execute(sql, params) as cursor:
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def execute(sql: str, params: tuple = ()) -> int:
    db = await get_db()
    async with _lock:
        cursor = await db.execute(sql, params)
        await db.commit()
        return cursor.lastrowid or 0


# ── Settings ──────────────────────────────────────────────────────────────────

async def get_setting(key: str, default: str = "") -> str:
    row = await fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


async def set_setting(key: str, value: str):
    await execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


async def get_all_settings() -> dict:
    rows = await fetchall("SELECT key, value FROM settings")
    return {r["key"]: r["value"] for r in rows}


# ── Rules ─────────────────────────────────────────────────────────────────────

RULE_FIELDS = [
    "name", "enabled", "delay_ms", "retry_count", "silent", "forward_as_copy",
    "prefix", "suffix", "msg_types", "whitelist", "blacklist",
    "regex_include", "regex_exclude", "forward_media_no_text", "ca_only",
    "dup_protection", "first_ca_only", "ignore_dup_forever",
]


def _hydrate_rule(row: dict) -> dict:
    """Parse JSON columns into Python lists."""
    rule = dict(row)
    for col in ("msg_types", "whitelist", "blacklist"):
        try:
            rule[col] = json.loads(rule.get(col) or "[]")
        except (json.JSONDecodeError, TypeError):
            rule[col] = []
    return rule


async def list_rules() -> list[dict]:
    rows = await fetchall("SELECT * FROM rules_v2 ORDER BY id DESC")
    rules = [_hydrate_rule(r) for r in rows]
    for rule in rules:
        chats = await fetchall(
            "SELECT * FROM rule_chats WHERE rule_id = ? ORDER BY id", (rule["id"],)
        )
        rule["sources"] = [c for c in chats if c["role"] == "source"]
        rule["destinations"] = [c for c in chats if c["role"] == "destination"]
    return rules


async def get_rule(rule_id: int) -> Optional[dict]:
    row = await fetchone("SELECT * FROM rules_v2 WHERE id = ?", (rule_id,))
    if not row:
        return None
    rule = _hydrate_rule(row)
    chats = await fetchall(
        "SELECT * FROM rule_chats WHERE rule_id = ? ORDER BY id", (rule_id,)
    )
    rule["sources"] = [c for c in chats if c["role"] == "source"]
    rule["destinations"] = [c for c in chats if c["role"] == "destination"]
    return rule


async def create_rule(data: dict, sources: list[dict], destinations: list[dict]) -> int:
    values = _rule_values(data)
    cols = ", ".join(RULE_FIELDS)
    marks = ", ".join("?" for _ in RULE_FIELDS)
    rule_id = await execute(
        f"INSERT INTO rules_v2 ({cols}) VALUES ({marks})", tuple(values)
    )
    await _replace_rule_chats(rule_id, sources, destinations)
    return rule_id


async def update_rule(rule_id: int, data: dict, sources: list[dict], destinations: list[dict]):
    values = _rule_values(data)
    sets = ", ".join(f"{f} = ?" for f in RULE_FIELDS)
    await execute(
        f"UPDATE rules_v2 SET {sets} WHERE id = ?", tuple(values) + (rule_id,)
    )
    await _replace_rule_chats(rule_id, sources, destinations)


def _rule_values(data: dict) -> list:
    # Fields that default ON when absent (e.g. imports of older backups)
    data = {"forward_media_no_text": 1, **data}
    values = []
    for f in RULE_FIELDS:
        v = data.get(f)
        if f in ("msg_types", "whitelist", "blacklist"):
            values.append(json.dumps(v or []))
        elif f in ("enabled", "silent", "forward_as_copy", "ca_only",
                   "dup_protection", "first_ca_only", "ignore_dup_forever",
                   "forward_media_no_text"):
            values.append(1 if v else 0)
        elif f in ("delay_ms", "retry_count"):
            values.append(int(v or 0))
        else:
            values.append(str(v or ""))
    return values


async def _replace_rule_chats(rule_id: int, sources: list[dict], destinations: list[dict]):
    await execute("DELETE FROM rule_chats WHERE rule_id = ?", (rule_id,))
    db = await get_db()
    async with _lock:
        for role, chats in (("source", sources), ("destination", destinations)):
            for c in chats:
                await db.execute(
                    "INSERT INTO rule_chats (rule_id, role, chat_id, title, username, chat_type) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (rule_id, role, str(c.get("chat_id", "")), c.get("title", ""),
                     c.get("username", ""), c.get("chat_type", "channel")),
                )
        await db.commit()


async def delete_rule(rule_id: int):
    await execute("DELETE FROM rule_chats WHERE rule_id = ?", (rule_id,))
    await execute("DELETE FROM rules_v2 WHERE id = ?", (rule_id,))


async def toggle_rule(rule_id: int) -> Optional[dict]:
    rule = await fetchone("SELECT id, enabled FROM rules_v2 WHERE id = ?", (rule_id,))
    if not rule:
        return None
    new_val = 0 if rule["enabled"] else 1
    await execute("UPDATE rules_v2 SET enabled = ? WHERE id = ?", (new_val, rule_id))
    return {"id": rule_id, "enabled": bool(new_val)}


async def rules_for_source_chat(chat_id: str) -> list[dict]:
    """All enabled rules that have chat_id as a source."""
    rows = await fetchall(
        "SELECT DISTINCT r.* FROM rules_v2 r "
        "JOIN rule_chats rc ON rc.rule_id = r.id "
        "WHERE rc.role = 'source' AND rc.chat_id = ? AND r.enabled = 1",
        (str(chat_id),),
    )
    rules = [_hydrate_rule(r) for r in rows]
    for rule in rules:
        chats = await fetchall(
            "SELECT * FROM rule_chats WHERE rule_id = ? ORDER BY id", (rule["id"],)
        )
        rule["sources"] = [c for c in chats if c["role"] == "source"]
        rule["destinations"] = [c for c in chats if c["role"] == "destination"]
    return rules


# ── Forwarded CAs ─────────────────────────────────────────────────────────────

async def ca_already_forwarded(ca: str) -> bool:
    row = await fetchone(
        "SELECT id FROM forwarded_cas WHERE contract_address = ?", (ca.lower(),)
    )
    return row is not None


async def record_ca(ca: str, rule_id: Optional[int] = None):
    await execute(
        "INSERT OR IGNORE INTO forwarded_cas (contract_address, rule_id) VALUES (?, ?)",
        (ca.lower(), rule_id),
    )


async def list_cas(limit: int = 200) -> list[dict]:
    return await fetchall(
        "SELECT * FROM forwarded_cas ORDER BY id DESC LIMIT ?", (limit,)
    )


async def clear_cas() -> int:
    db = await get_db()
    async with _lock:
        cursor = await db.execute("DELETE FROM forwarded_cas")
        await db.commit()
        return cursor.rowcount


# ── Logs ──────────────────────────────────────────────────────────────────────

async def add_log(source_title: str, dest_title: str, message_text: str,
                  status: str, error_msg: str = "", message_id: Optional[int] = None):
    await execute(
        "INSERT INTO logs (source_title, dest_title, message_id, message_text, status, error_msg) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source_title, dest_title, message_id, message_text[:500], status, error_msg[:500]),
    )
    # Trim old rows occasionally
    row = await fetchone("SELECT COUNT(*) AS c FROM logs")
    if row and row["c"] > LOG_MAX_ROWS:
        await execute(
            "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
            (LOG_MAX_ROWS,),
        )


async def list_logs(limit: int = 200, status: str = "", search: str = "") -> list[dict]:
    sql = "SELECT * FROM logs WHERE 1=1"
    params: list[Any] = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if search:
        sql += " AND (message_text LIKE ? OR source_title LIKE ? OR dest_title LIKE ? OR error_msg LIKE ?)"
        like = f"%{search}%"
        params += [like, like, like, like]
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return await fetchall(sql, tuple(params))


async def clear_logs() -> int:
    db = await get_db()
    async with _lock:
        cursor = await db.execute("DELETE FROM logs")
        await db.commit()
        return cursor.rowcount


async def log_stats() -> dict:
    total = await fetchone("SELECT COUNT(*) AS c FROM logs") or {"c": 0}
    today = await fetchall(
        "SELECT status, COUNT(*) AS c FROM logs "
        "WHERE created_at >= date('now','utc') GROUP BY status"
    )
    by_status_today = {r["status"]: r["c"] for r in today}
    all_time = await fetchall("SELECT status, COUNT(*) AS c FROM logs GROUP BY status")
    by_status = {r["status"]: r["c"] for r in all_time}
    return {
        "total": total["c"],
        "today": by_status_today,
        "all_time": by_status,
    }
