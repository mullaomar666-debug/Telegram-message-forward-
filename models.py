"""
models.py — Pydantic request/response models.
"""

from typing import Optional
from pydantic import BaseModel, Field


# ── Auth ──────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    phone: str


class OTPVerify(BaseModel):
    code: str = ""
    password: Optional[str] = None


# ── Rules ─────────────────────────────────────────────────────────────────────

class RuleChat(BaseModel):
    chat_id: str
    title: str = ""
    username: str = ""
    chat_type: str = "channel"


class RulePayload(BaseModel):
    name: str = ""
    enabled: bool = True
    sources: list[RuleChat] = Field(default_factory=list)
    destinations: list[RuleChat] = Field(default_factory=list)
    # Filters
    msg_types: list[str] = Field(default_factory=list)
    whitelist: list[str] = Field(default_factory=list)
    blacklist: list[str] = Field(default_factory=list)
    regex_include: str = ""
    regex_exclude: str = ""
    forward_media_no_text: bool = True
    ca_only: bool = False
    dup_protection: bool = True
    first_ca_only: bool = False
    ignore_dup_forever: bool = True
    # Options
    delay_ms: int = Field(0, ge=0, le=600_000)
    retry_count: int = Field(3, ge=1, le=10)
    silent: bool = False
    forward_as_copy: bool = False
    prefix: str = ""
    suffix: str = ""


# ── Settings ──────────────────────────────────────────────────────────────────

class SettingsPatch(BaseModel):
    dark_mode: Optional[bool] = None
    auto_reconnect: Optional[bool] = None
