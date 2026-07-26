"""
telegram_client.py — Telethon client lifecycle management.

Responsibilities:
  - Build and maintain a single Telethon client instance.
  - Handle the login flow: phone → OTP → (optional) 2FA password.
  - Auto-reconnect on disconnect with exponential back-off.
  - Expose a simple global `client` object consumed by the forwarding engine.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events, errors
from telethon.sessions import SQLiteSession

import database as db
from config import SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH

logger = logging.getLogger(__name__)

# ── Shared state ──────────────────────────────────────────────────────────────

client: Optional[TelegramClient] = None

# Serializes all client lifecycle mutations (login, logout, dead-session
# discard) so a stale reconnect can never tear down a freshly built client.
_lifecycle_lock = asyncio.Lock()

# Login flow state machine
# States: None → "waiting_code" → ("waiting_2fa") → "connected"
_login_state: Optional[str] = None
_pending_phone_code_hash: Optional[str] = None
_login_api_id: Optional[int] = None
_login_api_hash: Optional[str] = None
_login_phone: Optional[str] = None

# Forwarding engine callback registered from forwarder_engine.py
_message_handler = None


# ── Client factory ────────────────────────────────────────────────────────────

def _build_client(api_id: int, api_hash: str) -> TelegramClient:
    return TelegramClient(
        SESSION_NAME,
        api_id,
        api_hash,
        # Aggressive reconnect settings
        auto_reconnect=True,
        retry_delay=1,
        connection_retries=None,   # infinite retries
        request_retries=5,
        flood_sleep_threshold=60,
    )


def _require_api_credentials() -> tuple[int, str]:
    """Return the app-level API credentials from the environment, or raise a
    clear error if the server is not configured."""
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise RuntimeError(
            "Telegram API credentials are not configured on the server. "
            "Set the TELEGRAM_API_ID and TELEGRAM_API_HASH secrets and restart the app."
        )
    try:
        return int(TELEGRAM_API_ID), TELEGRAM_API_HASH
    except ValueError:
        raise RuntimeError("TELEGRAM_API_ID must be numeric.")


# ── Dead-session handling ─────────────────────────────────────────────────────
# Errors that mean the auth key is permanently unusable. Reconnecting with the
# same session file can never succeed (e.g. AuthKeyDuplicated: the key was used
# from two IPs at once and Telegram revoked it).
_DEAD_SESSION_ERRORS = (
    errors.AuthKeyDuplicatedError,
    errors.AuthKeyUnregisteredError,
    errors.AuthKeyInvalidError,
    errors.SessionRevokedError,
    errors.UserDeactivatedError,
)


def _delete_session_files() -> None:
    """Remove the on-disk Telethon session (+ SQLite journal) so the next
    login starts with a completely fresh authorization key."""
    for suffix in (".session", ".session-journal"):
        p = Path(SESSION_NAME + suffix)
        try:
            if p.exists():
                p.unlink()
                logger.info("Deleted session file %s", p.name)
        except OSError as exc:
            logger.warning("Could not delete %s: %s", p, exc)


async def _discard_dead_session(reason: str, failed: Optional[TelegramClient] = None) -> None:
    """The auth key can never be used again: tear the client down, delete the
    session file, and mark the app as logged out so the user lands on the
    login page instead of an endless failing reconnect loop.

    `failed` binds the cleanup to the client instance that actually died: if a
    fresh login already replaced it, only the dead instance is silenced and
    the current state is left untouched."""
    global client, _login_state
    async with _lifecycle_lock:
        if failed is not None and failed is not client:
            try:
                await failed.disconnect()
            except Exception:
                pass
            return

        logger.error(
            "Telegram session invalidated (%s). Clearing it — a fresh login is required.",
            reason,
        )
        import forwarder_engine as engine  # imported here to avoid a module cycle

        # Mechanical stop: a dead session is not the user pausing the engine.
        await engine.stop(persist_intent=False)

        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        client = None
        _login_state = None
        _delete_session_files()
        await db.set_setting("authenticated", "0")


# ── Auth flow ─────────────────────────────────────────────────────────────────

async def start_login(phone: str) -> str:
    """
    Step 1: Request the OTP from Telegram for this phone number.
    App-level API credentials come from the environment (Secrets).
    Returns the next expected state: "waiting_code".
    """
    global client, _login_state, _pending_phone_code_hash
    global _login_api_id, _login_api_hash, _login_phone

    # Fail fast on a misconfigured server — before touching existing state.
    api_id, api_hash = _require_api_credentials()

    async with _lifecycle_lock:
        _login_api_id   = api_id
        _login_api_hash = api_hash
        _login_phone    = phone

        # Remember the phone for display/resume; API creds stay in the env.
        await db.set_setting("phone", phone)

        # Drop any existing client/session so a fresh login never reuses an
        # old (possibly dead, or different-account) authorization key.
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
            client = None
        _pending_phone_code_hash = None
        _delete_session_files()

        try:
            # Build client and connect (no sign-in yet)
            client = _build_client(api_id, api_hash)
            await client.connect()

            if await client.is_user_authorized():
                _login_state = "connected"
                _attach_handlers()
                return "already_connected"

            result = await client.send_code_request(phone)
            _pending_phone_code_hash = result.phone_code_hash
            _login_state = "waiting_code"
            return "waiting_code"
        except Exception:
            # Leave no half-initialized client behind for the reconnect loop.
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            client = None
            _login_state = None
            raise


async def complete_login(code: str, password: Optional[str] = None) -> str:
    """
    Step 2: Submit OTP (and optional 2FA password).
    Returns "connected" on success, "waiting_2fa" if 2FA is required.
    """
    global _login_state, client

    if client is None or _login_state not in ("waiting_code", "waiting_2fa"):
        raise RuntimeError("No pending login session.")

    try:
        if _login_state == "waiting_2fa" and password:
            await client.sign_in(password=password)
        else:
            await client.sign_in(
                phone=_login_phone,
                code=code,
                phone_code_hash=_pending_phone_code_hash,
            )
        _login_state = "connected"
        await db.set_setting("authenticated", "1")
        _attach_handlers()
        logger.info("Telegram login successful.")
        return "connected"

    except errors.SessionPasswordNeededError:
        _login_state = "waiting_2fa"
        return "waiting_2fa"

    except errors.PhoneCodeInvalidError:
        raise ValueError("Invalid OTP code. Please try again.")

    except errors.PhoneCodeExpiredError:
        raise ValueError("OTP code expired. Please restart the login process.")


async def logout() -> None:
    """Sign out of Telegram, delete the on-disk session, and reset all
    login state — even if the server-side sign-out fails (dead auth key)."""
    global client, _login_state, _pending_phone_code_hash
    global _login_api_id, _login_api_hash, _login_phone
    global _dialog_cache, _dialog_cache_time

    async with _lifecycle_lock:
        if client:
            try:
                await client.log_out()   # invalidates the key server-side
            except Exception as exc:
                logger.warning(
                    "Server-side log_out failed (%s) — clearing local session anyway.", exc
                )
            try:
                await client.disconnect()
            except Exception:
                pass
            client = None

        _login_state = None
        _pending_phone_code_hash = None
        _login_api_id = _login_api_hash = _login_phone = None
        _dialog_cache = []
        _dialog_cache_time = 0.0
        _delete_session_files()
        await db.set_setting("authenticated", "0")


def get_login_state() -> Optional[str]:
    return _login_state


def is_connected() -> bool:
    return client is not None and client.is_connected()


async def is_authorized() -> bool:
    if client is None or not client.is_connected():
        return False
    try:
        return await client.is_user_authorized()
    except Exception:
        return False


# ── Auto-reconnect loop ────────────────────────────────────────────────────────

async def ensure_connected_loop():
    """
    Background task: keep the client connected.
    Runs forever, backing off exponentially on repeated failures.
    """
    global _login_state
    backoff = 1
    while True:
        # Work on a snapshot of the client so a login/logout that swaps the
        # global mid-await can never be affected by this stale attempt.
        c = client
        try:
            if c is not None and not c.is_connected():
                logger.warning("Telegram disconnected, reconnecting…")
                await c.connect()
                if await c.is_user_authorized() and c is client:
                    _login_state = "connected"
                    _attach_handlers()
                    logger.info("Reconnected to Telegram.")
                    backoff = 1
        except _DEAD_SESSION_ERRORS as exc:
            await _discard_dead_session(str(exc), failed=c)
            backoff = 1
        except Exception as exc:
            logger.error("Reconnect error: %s", exc)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


# ── Resume after reboot ───────────────────────────────────────────────────────

async def resume_session() -> bool:
    """
    Called on app startup. If credentials are stored and a session file exists,
    reconnect without user interaction.
    """
    global client, _login_state, _login_api_id, _login_api_hash, _login_phone

    try:
        api_id, api_hash = _require_api_credentials()
    except RuntimeError as exc:
        logger.warning("Skipping session resume: %s", exc)
        return False

    phone = await db.get_setting("phone")
    # Settings are stored as TEXT — "0" is truthy in Python, so parse explicitly.
    auth_raw = await db.get_setting("authenticated", "0")
    authenticated = str(auth_raw).strip().lower() in ("1", "true")

    if not authenticated or not Path(SESSION_NAME + ".session").exists():
        return False

    _login_api_id   = api_id
    _login_api_hash = api_hash
    _login_phone    = phone

    try:
        client = _build_client(api_id, api_hash)
        await client.connect()
        if await client.is_user_authorized():
            _login_state = "connected"
            _attach_handlers()
            logger.info("Session resumed successfully.")
            return True
        else:
            logger.warning("Session file present but not authorized.")
            _login_state = None
            return False
    except _DEAD_SESSION_ERRORS as exc:
        await _discard_dead_session(str(exc), failed=client)
        return False
    except Exception as exc:
        # Transient failure (e.g. network down): keep the session file and
        # client so the reconnect loop can retry.
        logger.error("Failed to resume session: %s", exc)
        return False


# ── Entity resolution ─────────────────────────────────────────────────────────

async def resolve_entity(identifier: str) -> dict:
    """
    Resolve a channel username, ID, or invite link to entity metadata.
    Returns {"entity_id": str, "title": str, "username": str}
    """
    if client is None or not await is_authorized():
        raise RuntimeError("Not connected to Telegram.")

    try:
        entity = await client.get_entity(identifier)
        entity_id = str(entity.id)
        title = getattr(entity, "title", None) or getattr(entity, "first_name", "") or entity_id
        username = getattr(entity, "username", None) or ""
        return {"entity_id": entity_id, "title": title, "username": username}
    except Exception as exc:
        raise ValueError(f"Could not resolve '{identifier}': {exc}")


async def get_me() -> Optional[dict]:
    """Return info about the currently logged-in user."""
    if not await is_authorized():
        return None
    try:
        me = await client.get_me()
        return {
            "id": me.id,
            "first_name": me.first_name,
            "last_name":  me.last_name or "",
            "username":   me.username  or "",
            "phone":      me.phone     or "",
        }
    except Exception:
        return None


# ── Dialogs (chat picker) ─────────────────────────────────────────────────────

_dialog_cache: list[dict] = []
_dialog_cache_time: float = 0.0


async def list_dialogs(search: str = "", force: bool = False) -> list[dict]:
    """
    List the user's dialogs (channels, groups, users) for the chat picker.
    Cached for 60 seconds to keep the picker snappy.
    """
    import time
    global _dialog_cache, _dialog_cache_time

    if client is None or not await is_authorized():
        raise RuntimeError("Not connected to Telegram.")

    if force or not _dialog_cache or (time.monotonic() - _dialog_cache_time) > 60:
        dialogs = []
        async for d in client.iter_dialogs(limit=500):
            ent = d.entity
            if ent is None:
                continue
            if getattr(ent, "is_self", False):
                chat_type = "saved"
            elif d.is_channel and not getattr(ent, "megagroup", False):
                chat_type = "channel"
            elif d.is_group:
                chat_type = "group"
            elif d.is_user:
                chat_type = "user"
            else:
                chat_type = "chat"
            dialogs.append({
                "chat_id": str(ent.id),
                "title": d.name or str(ent.id),
                "username": getattr(ent, "username", None) or "",
                "chat_type": chat_type,
            })
        _dialog_cache = dialogs
        _dialog_cache_time = time.monotonic()

    result = _dialog_cache
    if search:
        s = search.lower()
        result = [d for d in result
                  if s in d["title"].lower() or s in d["username"].lower()]
    return result


async def get_avatar_path(chat_id: str):
    """
    Download (and cache on disk) the profile photo for a chat.
    Returns a Path or None if the chat has no photo.
    """
    from config import DATA_DIR
    avatar_dir = DATA_DIR / "avatars"
    avatar_dir.mkdir(parents=True, exist_ok=True)
    path = avatar_dir / f"{chat_id}.jpg"
    if path.exists():
        return path

    if client is None or not await is_authorized():
        return None
    try:
        entity = await client.get_entity(int(chat_id))
        result = await client.download_profile_photo(entity, file=str(path))
        if result:
            return path
    except Exception as exc:
        logger.debug("Avatar fetch failed for %s: %s", chat_id, exc)
    return None


# ── Message handler registration ──────────────────────────────────────────────

def register_message_handler(handler):
    """Called by the forwarding engine to subscribe to new messages."""
    global _message_handler
    _message_handler = handler
    if client is not None and client.is_connected():
        _attach_handlers()


def _attach_handlers():
    """Attach Telethon event handlers to the current client."""
    if client is None or _message_handler is None:
        return
    # Remove old handlers to avoid duplicates on reconnect
    client.remove_event_handler(_message_handler)
    client.add_event_handler(_message_handler, events.NewMessage())
    logger.debug("Message handler attached.")
