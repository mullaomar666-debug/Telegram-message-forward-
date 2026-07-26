import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import database as db
import telegram_client as tc
import forwarder_engine as engine

import auth, chats, rules, logs, settings as settings_routes


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting TM Forwarder…")
    await db.get_db()

    resumed = await tc.resume_session()
    if resumed and await db.get_setting("engine_enabled", "1") == "1":
        logger.info("Session resumed — starting forwarding engine.")
        await engine.start()

    asyncio.create_task(tc.ensure_connected_loop())
    yield
    logger.info("Shutting down…")
    await engine.stop(persist_intent=False)
    await db.close_db()


app = FastAPI(
    title="TM Forwarder",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/fapi/docs",
    redoc_url=None,
)

BASE_DIR = Path(__file__).parent

# static ফোল্ডার না থাকলে পাইথন নিজে তৈরি করে নেবে
os.makedirs(BASE_DIR / "static", exist_ok=True)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=BASE_DIR / "templates")

PAGE_ROUTES = ["/", "/dashboard", "/logs", "/settings", "/rules"]


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if (
        path.startswith("/static")
        or path.startswith("/auth")
        or path == "/login"
    ):
        return await call_next(request)
    if path.startswith("/fapi/"):
        if not await tc.is_authorized():
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
        return await call_next(request)
    if path in PAGE_ROUTES or any(path.startswith(p + "/") for p in PAGE_ROUTES[1:]):
        if not await tc.is_authorized():
            return RedirectResponse(url="/login")
    return await call_next(request)


app.include_router(auth.router)
app.include_router(chats.router)
app.include_router(rules.router)
app.include_router(logs.router)
app.include_router(settings_routes.router)


# Pages

async def _ctx(request: Request, page: str) -> dict:
    return {
        "request": request,
        "page": page,
        "engine_running": engine.is_running(),
        "user": await tc.get_me(),
    }


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await tc.is_authorized() and await tc.get_me() is not None:
        return RedirectResponse(url="/")
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    ctx = await _ctx(request, "home")
    ctx["rules"] = await db.list_rules()
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
        return RedirectResponse(url="/")
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
    await engine.start()
    return {"status": "started"}


if __name__ == "__main__":
    from config import HOST, PORT
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False, log_level="info")
