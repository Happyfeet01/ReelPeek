from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
import shutil
import re
import smtplib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import accounts
from .database import Database
from .cleanup import prune_media
from .extractor import ExtractionError, canonicalize_instagram_url, extract


BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = Path(os.getenv("DATA_DIR", "/data"))
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me")
RETENTION_DAYS = max(1, int(os.getenv("RETENTION_DAYS", "14")))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() in {"1", "true", "yes"}

database = Database(DATA_ROOT / "reelpeek.sqlite3")



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
    purge_removed()
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
    accounts.normalize_email(ADMIN_EMAIL)
    if SESSION_SECRET == "change-me" or len(SESSION_SECRET) < 32:
        raise RuntimeError("SESSION_SECRET muss mindestens 32 Zeichen lang und zufällig sein.")
    (DATA_ROOT / "media").mkdir(parents=True, exist_ok=True)
    (DATA_ROOT / "thumbnails").mkdir(parents=True, exist_ok=True)
    database.initialize()
    accounts.provision_owner(database, ADMIN_EMAIL)
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
PUBLIC_PATHS = {"/login", "/health", "/hilfe", "/datenschutz", "/impressum", "/registrieren", "/aktivieren"}


@app.middleware("http")
async def authentication(request: Request, call_next):
    if request.url.path.startswith("/static/") or request.url.path == "/health":
        return await call_next(request)
    request.state.user = None
    user_id = request.session.get("user_id")
    if user_id:
        with database.connect() as conn:
            request.state.user = conn.execute("""SELECT id, email FROM accounts
                WHERE id=? AND state='active' AND generation=?""",
                (user_id, request.session.get("generation"))).fetchone()
        if not request.state.user:
            request.session.clear()
    if not request.state.user and request.url.path not in PUBLIC_PATHS:
        return RedirectResponse("/login", status_code=303, headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


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
        context={"items": database.list(view, owner_id=request.state.user["id"]), "view": view, "error": error},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.state.user:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(...), csrf_token: str = Form(""), password: str = Form(...)):
    check_csrf(request, csrf_token)
    with database.connect() as conn:
        user = conn.execute("SELECT * FROM accounts WHERE email=? AND state='active'",
                            (email.strip().lower(),)).fetchone()
    if accounts.check_password(password, user["password_hash"] if user else None) and user:
        request.session.clear()
        request.session.update(user_id=user["id"], generation=user["generation"])
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": "E-Mail-Adresse oder Passwort ungültig."},
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


def download_item(item, owner_id):
    item_id = item["id"]
    try:
        result = extract(item["source_url"], item_id, DATA_ROOT)
        database.set_download(item_id, **result)
    finally:
        # A deletion may finish while the extractor is still writing media.
        if not database.get(item_id, owner_id):
            with database.connect() as conn:
                conn.execute("INSERT OR IGNORE INTO removed_media VALUES (?)", (item_id,))
            purge_removed()
            raise HTTPException(404)


@app.post("/items", response_class=HTMLResponse)
def add_item(request: Request, csrf_token: str = Form(""), url: str = Form(...)):
    check_csrf(request, csrf_token)
    try:
        source_url = canonicalize_instagram_url(url)
    except ValueError as error:
        return render_index(request, error=str(error))

    existing = database.get_by_url(source_url, request.state.user["id"])
    if existing:
        return RedirectResponse(f"/items/{existing['id']}", status_code=303)

    item = database.create(source_url, request.state.user["id"])
    try:
        download_item(item, request.state.user["id"])
    except ExtractionError as error:
        database.set_error(item["id"], str(error))
        return templates.TemplateResponse(
            request=request,
            name="detail.html",
            context={"item": database.get(item["id"], request.state.user["id"]), "error": str(error)},
            status_code=422,
        )
    return RedirectResponse(f"/items/{item['id']}", status_code=303)


@app.get("/items/{item_id}", response_class=HTMLResponse)
def detail(request: Request, item_id: str):
    item = database.get(item_id, request.state.user["id"])
    if not item:
        raise HTTPException(status_code=404)
    if item["download_state"] == "ready":
        database.mark_seen(item_id)
        item = database.get(item_id, request.state.user["id"])
    return templates.TemplateResponse(
        request=request,
        name="detail.html",
        context={"item": item, "error": None},
    )


@app.post("/items/{item_id}/refresh")
def refresh_item(request: Request, item_id: str, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    item = database.get(item_id, request.state.user["id"])
    if not item:
        raise HTTPException(status_code=404)
    shutil.rmtree(DATA_ROOT / "media" / item_id, ignore_errors=True)
    try:
        download_item(item, request.state.user["id"])
    except ExtractionError as error:
        database.set_error(item_id, str(error))
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/favorite")
def favorite(request: Request, item_id: str, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    if not database.get(item_id, request.state.user["id"]):
        raise HTTPException(status_code=404)
    database.toggle_favorite(item_id)
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/delete")
def delete(request: Request, item_id: str, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    if not database.get(item_id, request.state.user["id"]):
        raise HTTPException(status_code=404)
    database.delete(item_id)
    shutil.rmtree(DATA_ROOT / "media" / item_id, ignore_errors=True)
    thumbnail = DATA_ROOT / "thumbnails" / f"{item_id}.jpg"
    thumbnail.unlink(missing_ok=True)
    return RedirectResponse("/", status_code=303)


@app.get("/media/{item_id}/{filename}")
def media_file(request: Request, item_id: str, filename: str):
    item = database.get(item_id, request.state.user["id"])
    if not item or not any(entry["url"] == f"/media/{item_id}/{filename}" for entry in item["media"]):
        raise HTTPException(404)
    path = DATA_ROOT / "media" / item_id / filename
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/thumbnails/{item_id}.jpg")
def thumbnail_file(request: Request, item_id: str):
    item = database.get(item_id, request.state.user["id"])
    path = DATA_ROOT / "thumbnails" / f"{item_id}.jpg"
    if not item or not item["thumbnail_path"] or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


def page(request, name, status=200, **context):
    return templates.TemplateResponse(request=request, name=name, context=context, status_code=status)


@app.get("/hilfe")
def help_page(request: Request):
    return page(request, "hilfe.html", days=RETENTION_DAYS)


@app.get("/datenschutz")
@app.get("/impressum")
def legal_page(request: Request):
    keys = ("OPERATOR_NAME", "OPERATOR_ADDRESS", "OPERATOR_EMAIL", "IMPRINT_EXTRA",
            "PRIVACY_OPERATIONS", "PRIVACY_AUTHORITY")
    return page(request, request.url.path[1:] + ".html", days=RETENTION_DAYS,
                **{key: os.getenv(key, "") for key in keys})


@app.get("/registrieren")
@app.get("/aktivieren")
def registration_page(request: Request):
    return page(request, "register.html", activate=request.url.path == "/aktivieren")


def deliver_link(email):
    try:
        accounts.issue_link(database, email)
    except (OSError, smtplib.SMTPException, ValueError, KeyError):
        logging.getLogger(__name__).error("Aktivierungsmail fehlgeschlagen. SMTP-Konfiguration prüfen.")
        raise HTTPException(503, "E-Mail-Versand fehlgeschlagen. Bitte später erneut versuchen.")


@app.post("/registrieren")
def request_link(request: Request, email: str = Form(...), csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    deliver_link(email.strip().lower())
    return page(request, "register.html", notice="Wenn deine Adresse eingeladen ist und die Bestätigung aussteht, erhältst du eine E-Mail.")


@app.post("/aktivieren")
def confirm_email(request: Request, token: str = Form(...), password: str = Form(...), csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    try:
        accounts.activate(database, token, password)
    except ValueError as exc:
        return page(request, "register.html", 400, activate=True, error=str(exc), token=token)
    return page(request, "login.html", notice="Deine Adresse ist bestätigt. Du kannst dich jetzt anmelden.")


def require_admin(request):
    if request.state.user["id"] != 1:
        raise HTTPException(403)


@app.get("/admin")
def admin_page(request: Request):
    require_admin(request)
    with database.connect() as conn:
        users = conn.execute("SELECT id,email,state FROM accounts ORDER BY email").fetchall()
    return page(request, "admin.html", users=users)


@app.post("/admin/invite")
def invite(request: Request, email: str = Form(...), csrf_token: str = Form("")):
    require_admin(request)
    check_csrf(request, csrf_token)
    try:
        email = accounts.normalize_email(email)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    with database.connect() as conn:
        conn.execute("INSERT INTO accounts(email) VALUES (?) ON CONFLICT(email) DO NOTHING", (email,))
    deliver_link(email)
    return RedirectResponse("/admin", 303)


@app.post("/admin/{user_id}/{action}")
def manage_account(request: Request, user_id: int, action: str, csrf_token: str = Form(""), confirm: str = Form("")):
    require_admin(request)
    check_csrf(request, csrf_token)
    if user_id == 1 or action not in {"sperren", "freigeben", "loeschen"}:
        raise HTTPException(400)
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT * FROM accounts WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(404)
        if action == "loeschen":
            if confirm != user["email"]:
                raise HTTPException(400, "Zum Löschen die vollständige E-Mail-Adresse eingeben.")
            conn.execute("INSERT OR IGNORE INTO removed_media SELECT id FROM items WHERE owner_id=?", (user_id,))
            conn.execute("DELETE FROM accounts WHERE id=?", (user_id,))
        else:
            state = "disabled" if action == "sperren" else ("active" if user["password_hash"] else "pending")
            conn.execute("""UPDATE accounts SET state=?, generation=generation+1, token_hash=NULL,
                token_until=NULL, last_mail=0 WHERE id=?""", (state, user_id))
    if action == "loeschen":
        purge_removed()
    return RedirectResponse("/admin", 303)


def purge_removed():
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for row in conn.execute("SELECT id FROM removed_media").fetchall():
            item_id = row["id"]
            if not re.fullmatch("[0-9a-f]{32}", item_id):
                continue
            path = DATA_ROOT / "media" / item_id
            if path.is_dir():
                shutil.rmtree(path)
            (DATA_ROOT / "thumbnails" / f"{item_id}.jpg").unlink(missing_ok=True)
            conn.execute("DELETE FROM removed_media WHERE id=?", (item_id,))
