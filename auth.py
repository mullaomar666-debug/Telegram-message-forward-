"""
routes/auth.py — Login / logout / session endpoints.

Flow:
  POST /auth/login       → sends OTP
  POST /auth/verify      → submits OTP (+ optional 2FA password)
  POST /auth/logout      → signs out
  GET  /auth/status      → current connection state
"""

import logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

import telegram_client as tc
import database as db
import forwarder_engine as engine
from models import LoginRequest, OTPVerify

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(req: LoginRequest):
    """Step 1: Request OTP from Telegram (phone number only — the app-level
    API credentials come from the server environment)."""
    try:
        state = await tc.start_login(req.phone)
        return {"state": state, "message": "OTP sent to your Telegram app." if state == "waiting_code" else "Already connected."}
    except Exception as exc:
        logger.error("Login error: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/verify")
async def verify(otp: OTPVerify):
    """Step 2: Submit OTP code (and optional 2FA password)."""
    try:
        state = await tc.complete_login(otp.code, otp.password)
        return {"state": state, "message": "Login successful!" if state == "connected" else "2FA password required."}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("OTP verify error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/logout")
async def logout():
    """Sign out of Telegram, delete the session file, and clear saved
    credentials so a different account can log in cleanly."""
    try:
        # Mechanical stop — logging out is not the user pausing the engine.
        await engine.stop(persist_intent=False)
        await tc.logout()

        for key in ("api_id", "api_hash", "phone"):
            await db.set_setting(key, "")

        logger.info("Logged out — session file and saved credentials cleared.")
        return {"message": "Logged out successfully."}
    except Exception as exc:
        logger.error("Logout error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/status")
async def status():
    """Return current connection/login state."""
    state = tc.get_login_state()
    connected = tc.is_connected()
    authorized = await tc.is_authorized()
    me = await tc.get_me() if authorized else None

    return {
        "state": state,
        "connected": connected,
        "authorized": authorized,
        "user": me,
    }
