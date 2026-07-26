"""
routes/logs.py — Log listing, search, stats, and clearing.
"""

from fastapi import APIRouter, Query

import database as db

router = APIRouter(prefix="/fapi/logs", tags=["logs"])


@router.get("")
async def list_logs(
    limit: int = Query(200, ge=1, le=1000),
    status: str = Query(""),
    search: str = Query(""),
):
    return {"logs": await db.list_logs(limit=limit, status=status, search=search)}


@router.get("/stats")
async def stats():
    return await db.log_stats()


@router.delete("")
async def clear_logs():
    deleted = await db.clear_logs()
    return {"deleted": deleted}


@router.get("/cas")
async def list_cas():
    return {"cas": await db.list_cas()}


@router.delete("/cas")
async def clear_cas():
    deleted = await db.clear_cas()
    return {"deleted": deleted}
