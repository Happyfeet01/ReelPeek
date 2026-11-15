from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .database import Database
from .cleanup import prune_media
from .extractor import ExtractionError, canonicalize_instagram_url, extract


BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = Path(os.getenv("DATA_DIR", "/data"))
PASSWORD = os.getenv("REELPEEK_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me")
RETENTION_DAYS = max(1, int(os.getenv("RETENTION_DAYS", "14")))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() in {"1", "true", "yes"}

database = Database(DATA_ROOT / "reelpeek.sqlite3")
(DATA_ROOT / "media").mkdir(parents=True, exist_ok=True)
(DATA_ROOT / "thumbnails").mkdir(parents=True, exist_ok=True)


def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        request.session["csrf_token"] = token
    return token


def csrf_context(request: Request) -> dict:
    return {"csrf_token": get_csrf_token(request)}


templates = Jinja2Templates(
    directory=str(BASE_DIR / "templates"),
    context_processors=[csrf_context],
)


def check_csrf(request: Request, token: str) -> None:
    expected = request.session.get("csrf_token", "")
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Ungültiges CSRF-Token.")


def cleanup_cache() -> int:
    return prune_media(database, DATA_ROOT, days=RETENTION_DAYS)


async def retention_worker() -> None:
    # Cleanup does not depend on users visiting the website.
    while True:
        await asyncio.sleep(24 * 60 * 60)
        try:
            await asyncio.to_thread(cleanup_cache)
        except Exception:
            logging.getLogger(__name__).exception("ReelPeek retention cleanup failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not PASSWORD:
        raise RuntimeError("REELPEEK_PASSWORD muss gesetzt sein.")
    if SESSION_SECRET == "change-me" or len(SESSION_SECRET) < 32:
        raise RuntimeError("SESSION_SECRET muss mindestens 32 Zeichen lang und zufällig sein.")
    (DATA_ROOT / "media").mkdir(parents=True, exist_ok=True)
    (DATA_ROOT / "thumbnails").mkdir(parents=True, exist_ok=True)
    database.initialize()
    cleanup_cache()
    worker = asyncio.create_task(retention_worker())
    try:
        yield
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


app = FastAPI(title="ReelPeek", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/media", StaticFiles(directory=str(DATA_ROOT / "media")), name="media")
app.mount("/thumbnails", StaticFiles(directory=str(DATA_ROOT / "thumbnails")), name="thumbnails")


@app.middleware("http")
async def authentication(request: Request, call_next):
    public_paths = {"/login", "/health"}
    if request.url.path.startswith("/static/") or request.url.path in public_paths:
        return await call_next(request)
    if not request.session.get("authenticated"):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# Starlette applies the last-added middleware first. SessionMiddleware therefore
# has to be registered after the authentication middleware above.
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=COOKIE_SECURE,
    same_site="lax",
    max_age=60 * 60 * 24 * 7,
)


def render_index(request: Request, view: str = "all", error: str | None = None):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"items": database.list(view), "view": view, "error": error},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("authenticated"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, csrf_token: str = Form(""), password: str = Form(...)):
    check_csrf(request, csrf_token)
    if hmac.compare_digest(password.encode(), PASSWORD.encode()):
        request.session.clear()
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": "Das Passwort stimmt nicht."},
        status_code=401,
    )


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, view: str = "all"):
    cleanup_cache()
    return render_index(request, view)


@app.post("/items", response_class=HTMLResponse)
def add_item(request: Request, csrf_token: str = Form(""), url: str = Form(...)):
    check_csrf(request, csrf_token)
    try:
        source_url = canonicalize_instagram_url(url)
    except ValueError as error:
        return render_index(request, error=str(error))

    existing = database.get_by_url(source_url)
    if existing:
        return RedirectResponse(f"/items/{existing['id']}", status_code=303)

    item = database.create(source_url)
    try:
        result = extract(source_url, item["id"], DATA_ROOT)
        database.set_download(item["id"], **result)
    except ExtractionError as error:
        database.set_error(item["id"], str(error))
        return templates.TemplateResponse(
            request=request,
            name="detail.html",
            context={"item": database.get(item["id"]), "error": str(error)},
            status_code=422,
        )
    return RedirectResponse(f"/items/{item['id']}", status_code=303)


@app.get("/items/{item_id}", response_class=HTMLResponse)
def detail(request: Request, item_id: str):
    item = database.get(item_id)
    if not item:
        raise HTTPException(status_code=404)
    if item["download_state"] == "ready":
        database.mark_seen(item_id)
        item = database.get(item_id)
    return templates.TemplateResponse(
        request=request,
        name="detail.html",
        context={"item": item, "error": None},
    )


@app.post("/items/{item_id}/refresh")
def refresh_item(request: Request, item_id: str, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    item = database.get(item_id)
    if not item:
        raise HTTPException(status_code=404)
    shutil.rmtree(DATA_ROOT / "media" / item_id, ignore_errors=True)
    try:
        result = extract(item["source_url"], item_id, DATA_ROOT)
        database.set_download(item_id, **result)
    except ExtractionError as error:
        database.set_error(item_id, str(error))
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/favorite")
def favorite(request: Request, item_id: str, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    if not database.get(item_id):
        raise HTTPException(status_code=404)
    database.toggle_favorite(item_id)
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/delete")
def delete(request: Request, item_id: str, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    if not database.get(item_id):
        raise HTTPException(status_code=404)
    database.delete(item_id)
    shutil.rmtree(DATA_ROOT / "media" / item_id, ignore_errors=True)
    thumbnail = DATA_ROOT / "thumbnails" / f"{item_id}.jpg"
    thumbnail.unlink(missing_ok=True)
    return RedirectResponse("/", status_code=303)
