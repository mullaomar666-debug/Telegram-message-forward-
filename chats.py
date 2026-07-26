"""
routes/chats.py — Chat picker endpoints (dialog listing + avatars).
"""

import logging
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

import telegram_client as tg

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/fapi/chats", tags=["chats"])


@router.get("")
async def list_chats(search: str = Query(""), refresh: bool = Query(False)):
    try:
        dialogs = await tg.list_dialogs(search=search, force=refresh)
        return {"chats": dialogs}
    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except Exception as exc:
        logger.error("list_chats failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# 1×1 transparent PNG for chats without a photo
_BLANK_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63fcffff3f030005fe02fea72d994400000000494e44ae426082"
)


@router.get("/{chat_id}/avatar")
async def chat_avatar(chat_id: str):
    try:
        path = await tg.get_avatar_path(chat_id)
    except Exception:
        path = None
    if path:
        return FileResponse(str(path), media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})
    return Response(content=_BLANK_PNG, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})
