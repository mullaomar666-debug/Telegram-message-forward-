"""
forwarder_engine.py — The message forwarding engine.

Listens for new messages via Telethon, matches them against enabled rules
(multi-source, multi-destination), applies per-rule filters and options,
and forwards through an asyncio queue with retry + FloodWait handling.
"""

import asyncio
import logging
import time
from typing import Optional

from telethon import errors

import database as db
import telegram_client as tg
import filter_engine

logger = logging.getLogger(__name__)

# ── Engine state ──────────────────────────────────────────────────────────────

_queue: Optional[asyncio.Queue] = None
_worker_task: Optional[asyncio.Task] = None
_running: bool = False
_started_at: Optional[float] = None

RETRY_DELAY_SECONDS = 5


def is_running() -> bool:
    return _running


def queue_size() -> int:
    return _queue.qsize() if _queue else 0


def uptime_seconds() -> int:
    if not _running or _started_at is None:
        return 0
    return int(time.monotonic() - _started_at)


async def start():
    """Start the forwarding engine (idempotent)."""
    global _queue, _worker_task, _running, _started_at
    if _running:
        return
    _queue = asyncio.Queue()
    _worker_task = asyncio.create_task(_worker())
    _running = True
    _started_at = time.monotonic()
    tg.register_message_handler(_on_new_message)
    await db.set_setting("engine_enabled", "1")
    logger.info("Forwarding engine started.")


async def stop(persist_intent: bool = True):
    """Stop the forwarding engine.

    persist_intent=True records "paused by user" (engine_enabled=0) so the
    engine stays off across restarts. Mechanical stops — app shutdown, logout,
    dead-session cleanup — must pass False so the user's auto-start preference
    survives the stop.
    """
    global _worker_task, _running, _started_at
    _running = False
    _started_at = None
    if _worker_task:
        _worker_task.cancel()
        _worker_task = None
    if persist_intent:
        await db.set_setting("engine_enabled", "0")
    logger.info("Forwarding engine %s.", "paused" if persist_intent else "stopped")


# ── Event handler ─────────────────────────────────────────────────────────────

async def _on_new_message(event):
    """Telethon NewMessage handler: enqueue if any rule matches the chat."""
    if not _running or _queue is None:
        return
    try:
        chat = await event.get_chat()
        chat_id = str(abs(getattr(chat, "id", 0)))
        rules = await db.rules_for_source_chat(chat_id)
        if not rules:
            return
        await _queue.put((event.message, chat_id, rules))
    except Exception as exc:
        logger.error("Error in message handler: %s", exc)


# ── Worker ────────────────────────────────────────────────────────────────────

async def _worker():
    while True:
        try:
            message, chat_id, rules = await _queue.get()
            for rule in rules:
                try:
                    await _process_rule(rule, message, chat_id)
                except Exception as exc:
                    logger.error("Rule %s processing error: %s", rule.get("id"), exc)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("Worker error: %s", exc)
            await asyncio.sleep(1)


def _source_title(rule: dict, chat_id: str) -> str:
    for s in rule.get("sources", []):
        if s["chat_id"] == chat_id:
            return s["title"] or chat_id
    return chat_id


async def _process_rule(rule: dict, message, chat_id: str):
    src_title = _source_title(rule, chat_id)
    text = message.raw_text or ""

    ok, reason, new_cas = await filter_engine.evaluate(rule, message)
    if not ok:
        status = "duplicate" if "duplicate" in reason else "skipped"
        await db.add_log(src_title, _dest_names(rule), text, status, reason,
                         message_id=message.id)
        return

    delay_ms = int(rule.get("delay_ms") or 0)
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)

    retry_count = max(1, int(rule.get("retry_count") or 3))
    forwarded_any = False

    for dest in rule.get("destinations", []):
        dest_title = dest["title"] or dest["chat_id"]
        success = await _forward_to(rule, message, dest, retry_count,
                                    src_title, dest_title, text)
        forwarded_any = forwarded_any or success

    if forwarded_any and new_cas:
        await filter_engine.record_cas(new_cas, rule)


def _dest_names(rule: dict) -> str:
    return ", ".join(d["title"] or d["chat_id"] for d in rule.get("destinations", [])) or "—"


async def _forward_to(rule, message, dest, retry_count, src_title, dest_title, text) -> bool:
    last_err = ""
    for attempt in range(1, retry_count + 1):
        try:
            entity = await tg.client.get_entity(int(dest["chat_id"]))
            silent = bool(rule.get("silent"))
            prefix = rule.get("prefix") or ""
            suffix = rule.get("suffix") or ""

            if rule.get("forward_as_copy") or prefix or suffix:
                body = f"{prefix}\n{text}" if prefix else text
                if suffix:
                    body = f"{body}\n{suffix}"
                if message.media and not message.web_preview:
                    await tg.client.send_file(
                        entity, message.media, caption=body[:1024], silent=silent
                    )
                else:
                    await tg.client.send_message(entity, body or "(empty)", silent=silent)
            else:
                await tg.client.forward_messages(entity, message, silent=silent)

            await db.add_log(src_title, dest_title, text, "ok", message_id=message.id)
            return True

        except errors.FloodWaitError as exc:
            wait = min(exc.seconds, 300)
            logger.warning("FloodWait %ss on rule %s", wait, rule.get("id"))
            await asyncio.sleep(wait)
            last_err = f"FloodWait {exc.seconds}s"
        except Exception as exc:
            last_err = str(exc)
            logger.error("Forward attempt %d/%d failed: %s", attempt, retry_count, exc)
            if attempt < retry_count:
                await asyncio.sleep(RETRY_DELAY_SECONDS)

    await db.add_log(src_title, dest_title, text, "error", last_err,
                     message_id=message.id)
    return False
