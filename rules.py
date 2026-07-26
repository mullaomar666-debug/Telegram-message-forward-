"""
routes/rules.py — Forwarding rule CRUD (multi-source / multi-destination).
"""

import logging
from fastapi import APIRouter, HTTPException

import database as db
from models import RulePayload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/fapi/rules", tags=["rules"])


@router.get("")
async def list_rules():
    return {"rules": await db.list_rules()}


@router.get("/{rule_id}")
async def get_rule(rule_id: int):
    rule = await db.get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    return rule


@router.post("")
async def create_rule(payload: RulePayload):
    if not payload.sources:
        raise HTTPException(status_code=400, detail="Select at least one source chat")
    if not payload.destinations:
        raise HTTPException(status_code=400, detail="Select at least one destination chat")
    data = payload.model_dump()
    sources = data.pop("sources")
    destinations = data.pop("destinations")
    rule_id = await db.create_rule(data, sources, destinations)
    return {"id": rule_id, "ok": True}


@router.put("/{rule_id}")
async def update_rule(rule_id: int, payload: RulePayload):
    if not await db.get_rule(rule_id):
        raise HTTPException(status_code=404, detail="Rule not found")
    if not payload.sources:
        raise HTTPException(status_code=400, detail="Select at least one source chat")
    if not payload.destinations:
        raise HTTPException(status_code=400, detail="Select at least one destination chat")
    data = payload.model_dump()
    sources = data.pop("sources")
    destinations = data.pop("destinations")
    await db.update_rule(rule_id, data, sources, destinations)
    return {"id": rule_id, "ok": True}


@router.post("/{rule_id}/toggle")
async def toggle_rule(rule_id: int):
    result = await db.toggle_rule(rule_id)
    if not result:
        raise HTTPException(status_code=404, detail="Rule not found")
    return result


@router.delete("/{rule_id}")
async def delete_rule(rule_id: int):
    if not await db.get_rule(rule_id):
        raise HTTPException(status_code=404, detail="Rule not found")
    await db.delete_rule(rule_id)
    return {"ok": True}
