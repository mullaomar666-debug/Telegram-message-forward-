"""
config.py — Application-wide constants and path setup.
All data is stored locally. No external services, no telemetry.
"""

import os
from pathlib import Path

# ── Base directories ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
SESSION_DIR = DATA_DIR / "sessions"
BACKUP_DIR = DATA_DIR / "backups"
EXPORT_DIR = DATA_DIR / "exports"

# Create directories if they don't exist
for _d in [DATA_DIR, SESSION_DIR, BACKUP_DIR, EXPORT_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_PATH = DATA_DIR / "forwarder.db"

# ── Telethon session ──────────────────────────────────────────────────────────
# Telethon stores session as a .session file (SQLite-backed, encrypted)
SESSION_NAME = str(SESSION_DIR / "telegram")

# ── Server ───────────────────────────────────────────────────────────────────
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# ── Telegram API credentials ──────────────────────────────────────────────────
# App-level credentials (they identify this application to Telegram, not a
# user account). Loaded from environment/Replit Secrets — never stored in the
# database or entered in the UI. Any Telegram account can log in through them
# with just a phone number + login code.
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()

# ── App metadata ──────────────────────────────────────────────────────────────
APP_NAME = "Telegram Auto Forwarder"
APP_VERSION = "1.0.0"

# ── Forwarding defaults ───────────────────────────────────────────────────────
DEFAULT_DELAY_MS = 0          # Milliseconds between forward and send
MAX_RETRY_ATTEMPTS = 3        # Retry failed forwards up to N times
RETRY_DELAY_SECONDS = 5       # Seconds to wait between retries
LOG_MAX_ROWS = 5000           # Maximum log rows to keep in DB

# ── Filter — Contract Address patterns ───────────────────────────────────────
# Ethereum / EVM-compatible (0x + 40 hex chars)
CA_PATTERN_EVM = r"0x[a-fA-F0-9]{40}"
# Solana (base58, 32-44 chars, starts with specific chars)
CA_PATTERN_SOLANA = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
