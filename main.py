import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

import database as db
import telegram_client as tc
import forwarder_engine as engine

import auth, chats, rules, logs, settings as settings_routes

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("TMForwarder")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(" Starting TM Forwarder Engine…")
    try:
        await db.get_db()

        resumed = await tc.resume_session()
        if resumed and await db.get_setting("engine_enabled", "1") == "1":
            logger.info("Session resumed — starting forwarding engine.")
            await engine.start()

        asyncio.create_task(tc.ensure_connected_loop())
    except Exception as e:
        logger.error(f"Error during startup lifespan: {e}", exc_info=True)
    
    yield
    
    logger.info(" Shutting down TM Forwarder…")
    try:
        await engine.stop(persist_intent=False)
        await db.close_db()
    except Exception as e:
        logger.error(f"Error during shutdown lifespan: {e}", exc_info=True)


app = FastAPI(
    title="TM Forwarder",
    version="2.1.0",
    lifespan=lifespan,
    docs_url="/fapi/docs",
    redoc_url=None,
)

# Performance Middlewares
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR)
app.mount("/static", StaticFiles(directory=BASE_DIR, html=True), name="static")

PAGE_ROUTES = ["/", "/dashboard", "/logs", "/settings", "/rules"]

# Global Error Handler to Prevent Crashes
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error at {request.url.path}: {exc}", exc_info=True)
    if request.url.path.startswith("/fapi/") or request.headers.get("accept") == "application/json":
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": "Internal Server Error"}
        )
    return HTMLResponse("<h3>500 - Internal Server Error</h3><p>Something went wrong on the server.</p>", status_code=500)

# Authentication & Route Guard Middleware
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    
    # 1. Allow Ping & Public Health Checks without Auth
    if path in ["/ping", "/health"]:
        return await call_next(request)

    # 2. Allow Authentication Routes
    if path.startswith("/auth") or path == "/login":
        return await call_next(request)

    # 3. Handle API Requests
    if path.startswith("/fapi/"):
        if not await tc.is_authorized():
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
        return await call_next(request)

    # 4. Handle Page Routes
    if path in PAGE_ROUTES or any(path.startswith(p + "/") for p in PAGE_ROUTES[1:]):
        if not await tc.is_authorized():
            return RedirectResponse(url="/login", status_code=303)

    return await call_next(request)

# Include API Routers
app.include_router(auth.router)
app.include_router(chats.router)
app.include_router(rules.router)
app.include_router(logs.router)
app.include_router(settings_routes.router)

# --- Keep-Alive & Health Ping Endpoint ---
@app.get("/ping")
@app.get("/health")
async def ping():
    telegram_ok = False
    try:
        telegram_ok = await tc.is_authorized()
    except Exception:
        pass

    return {
        "status": "ok",
        "engine_running": engine.is_running(),
        "telegram_authorized": telegram_ok
    }

# --- Context Helper ---
async def _ctx(request: Request, page: str) -> dict:
    user_info = None
    try:
        user_info = await tc.get_me()
    except Exception as e:
        logger.warning(f"Could not fetch user info: {e}")

    return {
        "request": request,
        "page": page,
        "engine_running": engine.is_running(),
        "user": user_info,
    }

# --- Page Routes ---
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await tc.is_authorized() and await tc.get_me() is not None:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    ctx = await _ctx(request, "home")
    try:
        ctx["rules"] = await db.list_rules()
    except Exception as e:
        logger.error(f"Failed to fetch rules: {e}")
        ctx["rules"] = []
    return templates.TemplateResponse("home.html", ctx)

@app.get("/rules/new", response_class=HTMLResponse)
async def rule_new_page(request: Request):
    ctx = await _ctx(request, "rule_edit")
    ctx["rule"] = None
    return templates.TemplateResponse("rule_edit.html", ctx)

@app.get("/rules/{rule_id}/edit", response_class=HTMLResponse)
async def rule_edit_page(request: Request, rule_id: int):
    rule = await db.get_rule(rule_id)
    if not rule:
        return RedirectResponse(url="/", status_code=303)
    ctx = await _ctx(request, "rule_edit")
    ctx["rule"] = rule
    return templates.TemplateResponse("rule_edit.html", ctx)

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return templates.TemplateResponse("dashboard.html", await _ctx(request, "dashboard"))

@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    return templates.TemplateResponse("logs.html", await _ctx(request, "logs"))

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", await _ctx(request, "settings"))

@app.post("/auth/start-engine")
async def start_engine_after_login():
    if not await tc.is_authorized():
        return {"status": "not_authenticated"}
    if await db.get_setting("engine_enabled", "1") != "1":
        return {"status": "paused_by_user"}
    
    try:
        await engine.start()
        return {"status": "started"}
    except Exception as e:
        logger.error(f"Failed to start engine: {e}")
        return {"status": "error", "detail": str(e)}

if __name__ == "__main__":
    from config import HOST, PORT
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False, log_level="info")
