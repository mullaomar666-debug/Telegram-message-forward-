# Telegram Auto Forwarder

A production-ready, self-hosted Telegram message forwarding tool with a modern web dashboard.

All data is stored **locally** — no cloud database, no analytics, no telemetry.

---

## Features

- **Unlimited** source channels, destinations, and forwarding rules
- **Filters**: Contract Address (CA), keywords, regex, text-only, media-only, photo+caption
- **Duplicate protection**: CA addresses are never forwarded twice
- **Priority & delay** per rule
- **Dark mode** toggle
- **Export / Import** settings (JSON)
- **Database backup** (ZIP)
- **Auto-reconnect** on disconnect, auto-start on reboot
- Full **audit log** with status per forwarded message

---

## Quick Start

### 1. Prerequisites

- Python 3.12+
- A Telegram account with API credentials from [my.telegram.org](https://my.telegram.org)

### 2. Install dependencies

```bash
cd forwarder
pip install -r requirements.txt
```

### 3. Run

```bash
python main.py
```

Open **http://localhost:8000** in your browser.

### 4. Login

1. Enter your **API ID**, **API Hash**, and **phone number**.
2. Enter the **OTP** sent to your Telegram app.
3. (Optional) Enter your **2FA password** if enabled.

After login, the forwarding engine starts automatically.

---

## Running on Replit

The app is pre-configured to run on Replit at port 8000 via the "Telegram Forwarder" workflow.
Click **Run** and open the webview.

---

## Running on Ubuntu VPS

```bash
# 1. Clone or upload the project
cd /opt
git clone <your-repo> tg-forwarder
cd tg-forwarder/forwarder

# 2. Install Python 3.12
sudo apt update && sudo apt install -y python3.12 python3.12-venv python3-pip

# 3. Create virtualenv
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. Run (foreground)
python main.py

# ── Systemd service (auto-start on reboot) ──

sudo nano /etc/systemd/system/tg-forwarder.service
```

Paste this into the service file:

```ini
[Unit]
Description=Telegram Auto Forwarder
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/tg-forwarder/forwarder
ExecStart=/opt/tg-forwarder/forwarder/venv/bin/python main.py
Restart=always
RestartSec=5
Environment=HOST=0.0.0.0
Environment=PORT=8000

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable tg-forwarder
sudo systemctl start tg-forwarder
sudo systemctl status tg-forwarder
```

Access at **http://<your-vps-ip>:8000**

> **Tip**: Place Nginx in front as a reverse proxy with SSL via Let's Encrypt for production use.

---

## Project Structure

```
forwarder/
├── main.py               # FastAPI app, lifespan, page routes
├── config.py             # Paths, constants, CA regex patterns
├── database.py           # SQLite schema + all queries (aiosqlite)
├── models.py             # Pydantic models, enums (FilterType, etc.)
├── telegram_client.py    # Telethon client, auth flow, auto-reconnect
├── forwarder_engine.py   # Message handler, queue, forward + retry logic
├── filter_engine.py      # Filter evaluation, CA extraction
├── routes/
│   ├── auth.py           # Login, OTP, logout, status
│   ├── sources.py        # Source channels CRUD
│   ├── destinations.py   # Destinations CRUD
│   ├── rules.py          # Rules CRUD
│   ├── filters.py        # Filters CRUD
│   ├── logs.py           # Log viewing, CA history
│   └── settings.py       # Settings, export/import, backup, engine control
├── templates/            # Jinja2 + Bootstrap 5 HTML templates
├── static/
│   ├── css/custom.css    # Dark mode, layout, animations
│   └── js/app.js         # Toast notifications, sidebar, polling
├── data/                 # Created at runtime
│   ├── forwarder.db      # SQLite database
│   ├── sessions/         # Telethon session file
│   └── backups/          # Backup archives
└── requirements.txt
```

---

## Security

- API credentials are stored only in the local SQLite database.
- The Telethon session file is stored locally and never transmitted.
- The web dashboard has no authentication by default — **bind to 127.0.0.1 or use a firewall** if running on a public VPS.
- For public access, add a reverse proxy with HTTP Basic Auth or run on a VPN.

---

## API Reference

The REST API is self-documented at **http://localhost:8000/api/docs** (Swagger UI).

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Not connected to Telegram" | Check API ID / Hash; ensure phone has Telegram |
| FloodWait errors | Normal rate limiting — the app retries automatically |
| Messages not forwarding | Check that the rule is enabled, source is in the channel, bot has send permission in destination |
| CA filter not working | Ensure the message contains a valid EVM (`0x…`) or Solana address |
