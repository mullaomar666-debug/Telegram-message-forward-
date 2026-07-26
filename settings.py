"""
routes/settings.py — Settings, export/import, backup, engine control, stats.
"""

import io
import json
import logging
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse

import database as db
import telegram_client as tg
import forwarder_engine as engine
from config import DATABASE_PATH, APP_VERSION
from models import SettingsPatch

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/fapi/settings", tags=["settings"])


# ── Settings ──────────────────────────────────────────────────────────────────

@router.get("")
async def get_settings():
    settings = await db.get_all_settings()
    settings.pop("api_hash", None)  # never expose secret hash
    return settings


@router.patch("")
async def patch_settings(payload: SettingsPatch):
    if payload.dark_mode is not None:
        await db.set_setting("dark_mode", "1" if payload.dark_mode else "0")
    if payload.auto_reconnect is not None:
        await db.set_setting("auto_reconnect", "1" if payload.auto_reconnect else "0")
    return {"ok": True}


# ── Engine control ────────────────────────────────────────────────────────────

@router.post("/engine/start")
async def engine_start():
    if not await tg.is_authorized():
        raise HTTPException(status_code=400, detail="Not connected to Telegram")
    await engine.start()
    return {"running": True}


@router.post("/engine/stop")
async def engine_stop():
    await engine.stop()
    return {"running": False}


@router.post("/engine/toggle")
async def engine_toggle():
    if engine.is_running():
        await engine.stop()
        return {"running": False}
    if not await tg.is_authorized():
        raise HTTPException(status_code=400, detail="Not connected to Telegram")
    await engine.start()
    return {"running": True}


# ── Live stats (dashboard) ────────────────────────────────────────────────────

@router.get("/stats")
async def live_stats():
    rules = await db.list_rules()
    src_ids, dst_ids = set(), set()
    for r in rules:
        for s in r["sources"]:
            src_ids.add(s["chat_id"])
        for d in r["destinations"]:
            dst_ids.add(d["chat_id"])
    log_stats = await db.log_stats()
    today = log_stats["today"]
    uptime = engine.uptime_seconds()
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    return {
        "running": engine.is_running(),
        "connected": await tg.is_authorized(),
        "sources": len(src_ids),
        "destinations": len(dst_ids),
        "rules": len(rules),
        "queue": engine.queue_size(),
        "forwarded_today": today.get("ok", 0),
        "duplicates_skipped": today.get("duplicate", 0) + log_stats["all_time"].get("duplicate", 0) - today.get("duplicate", 0),
        "duplicates_today": today.get("duplicate", 0),
        "failed": today.get("error", 0),
        "uptime": f"{h:02d}:{m:02d}:{s:02d}",
        "uptime_seconds": uptime,
    }


# ── Export / Import / Backup ──────────────────────────────────────────────────

@router.get("/export")
async def export_config():
    rules = await db.list_rules()
    settings = await db.get_all_settings()
    settings.pop("api_hash", None)
    settings.pop("api_id", None)
    settings.pop("phone", None)
    payload = {
        "app": "telegram-auto-forwarder",
        "version": APP_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "settings": settings,
        "rules": rules,
    }
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": "attachment; filename=forwarder-config.json"},
    )


@router.post("/import")
async def import_config(file: UploadFile = File(...)):
    try:
        raw = await file.read()
        data = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON file")
    if data.get("app") != "telegram-auto-forwarder":
        raise HTTPException(status_code=400, detail="Not a forwarder config file")

    imported = 0
    for rule in data.get("rules", []):
        sources = rule.pop("sources", [])
        destinations = rule.pop("destinations", [])
        rule.pop("id", None)
        rule.pop("created_at", None)
        await db.create_rule(rule, sources, destinations)
        imported += 1
    for key, value in (data.get("settings") or {}).items():
        if key in ("dark_mode", "auto_reconnect"):
            await db.set_setting(key, str(value))
    return {"imported_rules": imported}


@router.get("/backup")
async def backup_db():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if DATABASE_PATH.exists():
            zf.write(DATABASE_PATH, arcname="forwarder.db")
    buf.seek(0)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=forwarder-backup-{ts}.zip"},
    )


@router.post("/reset-duplicates")
async def reset_duplicates():
    deleted = await db.clear_cas()
    return {"deleted": deleted}
