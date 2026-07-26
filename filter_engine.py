"""
filter_engine.py — Per-rule message filtering.

Each rule carries its own filter config (message types, whitelist, blacklist,
regex include/exclude, contract-address rules). All active conditions must
pass (AND logic) for a message to be forwarded.
"""

import re
import logging
from typing import Optional

import database as db
from config import CA_PATTERN_EVM, CA_PATTERN_SOLANA

logger = logging.getLogger(__name__)

_EVM_RE = re.compile(CA_PATTERN_EVM)
_SOL_RE = re.compile(r"\b" + CA_PATTERN_SOLANA + r"\b")

# Solana false-positive guard: pure hex or common words
_HEX_ONLY = re.compile(r"^[0-9a-fA-F]+$")

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)


def extract_cas(text: str) -> list[str]:
    """Extract EVM and Solana contract addresses from text, in order."""
    if not text:
        return []
    found: list[str] = []
    for m in _EVM_RE.finditer(text):
        ca = m.group(0).lower()
        if ca not in found:
            found.append(ca)
    for m in _SOL_RE.finditer(text):
        candidate = m.group(0)
        # Skip pure-hex strings (likely EVM fragments or hashes)
        if _HEX_ONLY.match(candidate):
            continue
        ca = candidate  # Solana is case-sensitive; store as-is but compare lower
        if ca.lower() not in [f.lower() for f in found]:
            found.append(ca)
    return found


def message_type_of(message) -> str:
    """Classify a Telethon message into one of the UI's type checkboxes."""
    if message.sticker:
        return "sticker"
    if message.gif:
        return "gif"
    if message.voice:
        return "voice"
    if message.video or message.video_note:
        return "video"
    if message.photo:
        return "photo"
    if message.contact:
        return "contact"
    if message.geo or message.venue:
        return "location"
    if message.document or message.audio:
        return "file"
    text = message.raw_text or ""
    if text and _EMOJI_RE.search(text) and not _EMOJI_RE.sub("", text).strip():
        return "emoji"  # message consists only of emojis
    return "text"


async def evaluate(rule: dict, message) -> tuple[bool, str, Optional[list[str]]]:
    """
    Evaluate a message against a rule's filters.
    Returns (should_forward, reason, new_cas_to_record).
    """
    text = message.raw_text or ""

    # 1. Message type
    msg_types = rule.get("msg_types") or []
    if msg_types:
        mtype = message_type_of(message)
        # 'emoji' selected also accepts text containing emojis
        if mtype not in msg_types:
            if not ("emoji" in msg_types and _EMOJI_RE.search(text)):
                return False, f"type '{mtype}' not allowed", None

    # 2. Whitelist keywords (at least one must match).
    #    "Forward media without text": captionless media (images, videos,
    #    voice, …) bypasses the keyword requirement when the rule allows it.
    whitelist = [w for w in (rule.get("whitelist") or []) if w.strip()]
    if whitelist:
        low = text.lower()
        if not any(w.lower() in low for w in whitelist):
            captionless_media = (
                not text.strip()
                and rule.get("forward_media_no_text", True)
                and message_type_of(message) != "text"
            )
            if not captionless_media:
                return False, "no whitelist keyword matched", None

    # 3. Blacklist keywords (none may match)
    blacklist = [w for w in (rule.get("blacklist") or []) if w.strip()]
    if blacklist:
        low = text.lower()
        hit = next((w for w in blacklist if w.lower() in low), None)
        if hit:
            return False, f"blacklist keyword '{hit}'", None

    # 4. Regex include
    rx_inc = (rule.get("regex_include") or "").strip()
    if rx_inc:
        try:
            if not re.search(rx_inc, text, re.IGNORECASE):
                return False, "regex include did not match", None
        except re.error as exc:
            logger.warning("Bad include regex on rule %s: %s", rule.get("id"), exc)

    # 5. Regex exclude
    rx_exc = (rule.get("regex_exclude") or "").strip()
    if rx_exc:
        try:
            if re.search(rx_exc, text, re.IGNORECASE):
                return False, "regex exclude matched", None
        except re.error as exc:
            logger.warning("Bad exclude regex on rule %s: %s", rule.get("id"), exc)

    # 6. Contract address logic
    new_cas: Optional[list[str]] = None
    if rule.get("ca_only"):
        cas = extract_cas(text)
        if not cas:
            return False, "no contract address found", None
        if rule.get("first_ca_only"):
            cas = cas[:1]
        if rule.get("dup_protection"):
            fresh = []
            for ca in cas:
                if not await db.ca_already_forwarded(ca):
                    fresh.append(ca)
            if not fresh:
                return False, "duplicate contract address", None
            new_cas = fresh
        else:
            new_cas = cas

    return True, "", new_cas


async def record_cas(cas: list[str], rule: dict):
    """Persist forwarded CAs when duplicate protection is enabled."""
    if not cas:
        return
    if rule.get("dup_protection") and rule.get("ignore_dup_forever", True):
        for ca in cas:
            await db.record_ca(ca, rule.get("id"))
