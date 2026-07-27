"""
routes/chats.py — Chat picker endpoints (dialog listing + avatars).
"""

import logging
from fastapi import APIRouter, Request, Query
from fastapi.responses import FileResponse, Response, JSONResponse

import telegram_client as tg

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chats"])


@router.get("/fapi/chats")
@router.get("/chats")
async def list_chats(request: Request, search: str = Query(""), refresh: bool = Query(False)):
    """
    Fetches the Telegram dialogs safely without hanging the UI on errors.
    """
    try:
        dialogs = await tg.list_dialogs(search=search, force=refresh)
        if dialogs is None:
            dialogs = []
        return JSONResponse(content={"chats": dialogs})
    except RuntimeError as exc:
        logger.warning("Authentication runtime error in list_chats: %s", exc)
        return JSONResponse(
            status_code=401,
            content={"chats": [], "error": "Not authenticated with Telegram"}
        )
    except Exception as exc:
        logger.error("list_chats failed: %s", exc)
        return JSONResponse(
            status_code=200,
            content={"chats": [], "error": str(exc)}
        )


_BLANK_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63fcffff3f030005fe02fea72d994400000000494e44ae426082"
)


@router.get("/fapi/chats/{chat_id}/avatar")
@router.get("/chats/{chat_id}/avatar")
async def chat_avatar(chat_id: str):
    """
    Returns the avatar image for a chat or a blank PNG fallback.
    """
    try:
        path = await tg.get_avatar_path(chat_id)
    except Exception as exc:
        logger.debug("Failed to get avatar for %s: %s", chat_id, exc)
        path = None

    if path:
        return FileResponse(
            str(path),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"}
        )
    
    return Response(
        content=_BLANK_PNG,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"}
    )
