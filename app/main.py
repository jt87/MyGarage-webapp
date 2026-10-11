"""MyGarage web app: browse tools, manuals, consumables, projects and receipts."""

from __future__ import annotations

import contextlib
import csv
import hashlib
import hmac
import io
import logging
import mimetypes
import os
import secrets
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import anyio
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                                 RedirectResponse, Response)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import repo
from .sync import RepoSync, Watcher

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

HERE = Path(__file__).parent
REPO_PATH = Path(os.environ.get("REPO_PATH", "/data/repo")).resolve()
CACHE_PATH = Path(os.environ.get("CACHE_PATH", "/data/cache")).resolve()
SITE_TITLE = os.environ.get("SITE_TITLE", "MyGarage")
AUTH_USER = os.environ.get("AUTH_USERNAME", "")
AUTH_PASS = os.environ.get("AUTH_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # "owner/name", for "Edit on GitHub" links
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
# How stale the cached clone may get before a page view asks GitHub (seconds).
FRESHNESS_SECONDS = float(os.environ.get("FRESHNESS_SECONDS", "300"))

sync = RepoSync(
    REPO_PATH,
    url=os.environ.get("GIT_REPO_URL", ""),
    branch=os.environ.get("GIT_BRANCH", "main"),
    token=os.environ.get("GIT_TOKEN", ""),
    interval=int(os.environ.get("SYNC_INTERVAL", "1800")),
    state_file=CACHE_PATH / "sync-state.json",
)
inventory = Watcher(sync, lambda: repo.load(REPO_PATH))

env = Environment(loader=FileSystemLoader(HERE / "templates"),
                  autoescape=select_autoescape(["html"]))
env.globals.update(site_title=SITE_TITLE, money=repo.fmt_money, sync=sync,
                   github_repo=GITHUB_REPO, branch=sync.branch, auth_enabled=bool(AUTH_PASS))
env.filters["q"] = lambda s: quote(str(s))


def ago(dt: datetime | None) -> str:
    if not dt:
        return "never"
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} hr ago"
    return dt.strftime("%b %-d")


env.filters["ago"] = ago

# Which tab a section belongs to (for the tab bar and sidebar highlight).
TAB_FOR = {"": "garage", "tools": "tools", "consumables": "shopping", "projects": "projects",
           "search": "search", "insurance": "garage", "reference": "garage"}


def render(request: Request, template: str, status: int = 200, **ctx) -> HTMLResponse:
    """Render a screen. `back` = (url, label) for the nav bar's back button,
    `title` = the inline nav bar title, `tab` = highlighted tab."""
    ctx.setdefault("inv", inventory.get())
    ctx["request"] = request
    ctx["path"] = request.url.path
    ctx.setdefault("tab", TAB_FOR.get(request.url.path.split("/")[1], ""))
    ctx.setdefault("back", None)
    ctx.setdefault("title", "")
    return HTMLResponse(env.get_template(template).render(**ctx), status_code=status,
                        headers={"Cache-Control": "no-cache"})


# --------------------------------------------------------------------------- pages

async def home(request: Request):
    inv = inventory.get()
    active = [p for p in inv.projects if p.status.lower() not in ("done", "finished")]
    return render(request, "home.html", active=active, recent=inv.log[:5], title=SITE_TITLE)


async def tools(request: Request):
    return render(request, "tools.html", title="Tools")


async def tool(request: Request):
    inv = inventory.get()
    t = inv.tool(request.path_params["slug"])
    if not t:
        raise HTTPException(404)
    return render(request, "tool.html", t=t, back=("/tools", "Tools"), title=t.model_name)


async def consumables(request: Request):
    inv = inventory.get()
    groups: dict[str, list] = {}
    for c in inv.consumables:
        groups.setdefault(c.tool_slug, []).append(c)
    return render(request, "consumables.html", title="Shopping List",
                  groups=[(inv.tool(s), cs) for s, cs in groups.items()])


async def projects(request: Request):
    return render(request, "projects.html", title="Projects")


async def project(request: Request):
    inv = inventory.get()
    p = inv.project(request.path_params["slug"])
    if not p:
        raise HTTPException(404)
    return render(request, "project.html", p=p, back=("/projects", "Projects"), title=p.title,
                  used=[inv.tool(s) for s in p.tools if inv.tool(s)])


async def reference(request: Request):
    inv = inventory.get()
    slug = request.path_params.get("slug", "index")
    page = inv.reference(slug)
    if not page:
        raise HTTPException(404)
    back = ("/", SITE_TITLE) if slug == "index" else ("/reference", "Reference")
    return render(request, "reference.html", page=page, back=back, title=page.title)


async def insurance(request: Request):
    inv = inventory.get()
    missing = [t for t in inv.tools if not t.proofs]
    return render(request, "insurance.html", missing=missing, back=("/", SITE_TITLE),
                  items=sorted(inv.tools, key=lambda t: -(t.value or 0)),
                  title="Insurance")


async def insurance_csv(request: Request):
    inv = inventory.get()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Tool", "Category", "Model #", "Serial #", "Purchased", "Paid",
                "Replacement value", "Replacement value details", "Proof of purchase", "Link"])
    base = str(request.base_url).rstrip("/")
    for t in inv.tools:
        w.writerow([t.title, t.category, t.model_no, t.serial, t.spec_text.get("purchased", ""),
                    t.paid_text, t.value_text, t.spec_text.get("replacement value", ""),
                    "; ".join(f.name for f in t.proofs) or "none", base + t.url])
    w.writerow(["Total", "", "", "", "", "", inv.total_value_text or repo.fmt_money(inv.total_value)])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="mygarage-inventory.csv"'})


async def more(request: Request):
    return RedirectResponse("/", status_code=301)


async def search(request: Request):
    q = request.query_params.get("q", "").strip()
    hits = repo.search(inventory.get(), q) if q else []
    groups: dict[str, list] = {}
    for h in hits:
        groups.setdefault(h.kind, []).append(h)
    order = ["Tool", "Consumable", "Project", "Reference"]
    return render(request, "search.html", q=q, hits=hits, title="Search",
                  groups=sorted(groups.items(), key=lambda g: order.index(g[0])))


async def viewer(request: Request):
    """PDF / STL / image viewer page with download + back links."""
    rel = request.path_params["path"]
    full = safe_path(rel)
    owner = owner_link(rel)
    ext = full.suffix.lower()
    kind = "pdf" if ext == ".pdf" else "model" if ext == ".stl" else \
        "image" if ext in repo.IMAGE_EXT else "other"
    tab = TAB_FOR.get(rel.split("/")[0], "")
    return render(request, "viewer.html", rel=rel, name=full.name, kind=kind,
                  url="/files/" + quote(rel), owner=owner, back=owner, title=full.name, tab=tab,
                  thumb=f"/thumb/{quote(rel)}?v={int(full.stat().st_mtime)}")


# --------------------------------------------------------------------------- files

def safe_path(rel: str) -> Path:
    if not rel or rel.split("/", 1)[0] not in repo.SERVED_DIRS:
        raise HTTPException(404)
    full = (REPO_PATH / rel).resolve()
    if REPO_PATH not in full.parents or not full.is_file() or ".git" in full.parts:
        raise HTTPException(404)
    return full


def owner_link(rel: str) -> tuple[str, str] | None:
    segs = rel.split("/")
    inv = inventory.get()
    if len(segs) >= 3 and segs[0] == "tools" and (t := inv.tool(segs[1])):
        return t.url, t.model_name
    if len(segs) >= 3 and segs[0] == "projects" and (p := inv.project(segs[1])):
        return p.url, p.title
    if segs[0] == "reference":
        return "/reference", "Reference"
    return None


async def files(request: Request):
    full = safe_path(request.path_params["path"])
    media = mimetypes.guess_type(full.name)[0] or "application/octet-stream"
    inline = media.startswith("image/") or media in ("application/pdf", "text/plain")
    disp = "inline" if inline and "download" not in request.query_params else "attachment"
    return FileResponse(full, media_type=media,
                        headers={"Content-Disposition": f"{disp}; filename=\"{full.name}\"",
                                 "Cache-Control": "private, no-cache"})


THUMB_WIDTHS = (320, 640, 1280, 2048)


async def thumb(request: Request):
    full = safe_path(request.path_params["path"])
    if full.suffix.lower() not in repo.IMAGE_EXT:
        raise HTTPException(404)
    try:
        w = int(request.query_params.get("w", "640"))
    except ValueError:
        w = 640
    w = min(THUMB_WIDTHS, key=lambda x: abs(x - w))
    st = full.stat()
    key = hashlib.sha1(f"{full}:{st.st_mtime_ns}:{st.st_size}:{w}".encode()).hexdigest()
    out = CACHE_PATH / "thumbs" / key[:2] / f"{key}.jpg"
    if not out.exists():
        from PIL import Image, ImageOps
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(full) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((w, w * 2))
                tmp = out.with_suffix(".tmp")
                im.convert("RGB").save(tmp, "JPEG", quality=82, optimize=True, progressive=True)
                tmp.replace(out)
        except OSError:
            return FileResponse(full)
    # The URL carries ?v=<mtime>, so a changed photo gets a new URL.
    return FileResponse(out, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"
                                 if "v" in request.query_params else "private, no-cache"})


# --------------------------------------------------------------------------- admin

async def do_sync(request: Request):
    """"Check for Updates": asks GitHub, at most once per 15 seconds."""
    if sync.git_mode:
        await anyio.to_thread.run_sync(sync.ensure_fresh, 15)
    inventory.invalidate()
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(status())
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


def status() -> dict:
    return {
        "mode": "git" if sync.git_mode else "mounted",
        "head": sync.head[:7],
        "last_checked": sync.last_checked.isoformat() if sync.last_checked else None,
        "checks_today": sync.checks_today,
        "fetches_today": sync.fetches_today,
        "error": sync.last_error or None,
        "tools": len(inventory.get().tools),
    }


async def github_webhook(request: Request):
    """GitHub push webhook: pull right away instead of waiting for a page view."""
    if not WEBHOOK_SECRET:
        raise HTTPException(404)
    body = await request.body()
    sig = request.headers.get("x-hub-signature-256", "")
    want = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, want):
        return PlainTextResponse("bad signature", status_code=401)
    if request.headers.get("x-github-event") == "push":
        try:
            ref = json.loads(body or b"{}").get("ref", "")
        except ValueError:
            ref = ""
        # Only pushes to the branch we serve cost a fetch.
        if ref == f"refs/heads/{sync.branch}" and await anyio.to_thread.run_sync(sync.sync_once):
            inventory.invalidate()
    return PlainTextResponse("ok")


async def healthz(request: Request):
    ok = (REPO_PATH / "README.md").exists()
    return JSONResponse({"ok": ok, **status()}, status_code=200 if ok else 503)


# --------------------------------------------------------------------------- auth

_failures: dict[str, list[float]] = {}


async def login(request: Request):
    if not AUTH_PASS:
        return RedirectResponse("/", status_code=303)
    error = ""
    nxt = request.query_params.get("next", "/")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    if request.method == "POST":
        ip = request.client.host if request.client else "?"
        recent = [t for t in _failures.get(ip, []) if time.time() - t < 900]
        form = await request.form()
        user_ok = hmac.compare_digest(str(form.get("username", "")), AUTH_USER)
        pass_ok = hmac.compare_digest(str(form.get("password", "")), AUTH_PASS)
        if len(recent) >= 10:
            error = "Too many attempts. Try again in 15 minutes."
        elif user_ok and pass_ok:
            request.session["user"] = AUTH_USER
            _failures.pop(ip, None)
            return RedirectResponse(nxt, status_code=303)
        else:
            recent.append(time.time())
            _failures[ip] = recent
            error = "Wrong username or password."
    html = env.get_template("login.html").render(error=error, next=nxt, request=request)
    return HTMLResponse(html, status_code=401 if error else 200)


async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


OPEN_PATHS = ("/login", "/static/", "/healthz", "/manifest.webmanifest", "/webhook/github")
# Requests that don't render repo content skip the freshness check.
NO_FRESH_PATHS = ("/static/", "/thumb/", "/files/", "/healthz", "/manifest.webmanifest",
                  "/webhook/", "/login", "/logout", "/sync")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if AUTH_PASS and not request.url.path.startswith(OPEN_PATHS):
            if request.session.get("user") != AUTH_USER:
                return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)
        if request.method == "GET" and not request.url.path.startswith(NO_FRESH_PATHS):
            # Pages come from the cached clone. If it hasn't been checked
            # against GitHub for FRESHNESS_SECONDS, check once first (off the
            # event loop). The webhook usually makes this a no-op.
            await anyio.to_thread.run_sync(sync.ensure_fresh, FRESHNESS_SECONDS)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return resp


async def manifest(request: Request):
    return JSONResponse({
        "name": SITE_TITLE, "short_name": SITE_TITLE, "start_url": "/", "display": "standalone",
        "background_color": "#f2f2f7", "theme_color": "#f2f2f7",
        "icons": [{"src": "/static/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
                  {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }, media_type="application/manifest+json")


async def not_found(request: Request, exc: HTTPException):
    if exc.status_code == 404:
        try:
            return render(request, "404.html", status=404)
        except Exception:
            pass
    return PlainTextResponse(str(exc.detail), status_code=exc.status_code)


routes = [
    Route("/", home),
    Route("/tools", tools),
    Route("/tools/{slug}", tool),
    Route("/consumables", consumables),
    Route("/projects", projects),
    Route("/projects/{slug}", project),
    Route("/reference", reference),
    Route("/reference/{slug}", reference),
    Route("/insurance", insurance),
    Route("/insurance.csv", insurance_csv),
    Route("/search", search),
    Route("/more", more),
    Route("/webhook/github", github_webhook, methods=["POST"]),
    Route("/view/{path:path}", viewer),
    Route("/files/{path:path}", files),
    Route("/thumb/{path:path}", thumb),
    Route("/sync", do_sync, methods=["POST"]),
    Route("/healthz", healthz),
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/manifest.webmanifest", manifest),
    Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
]

class PageGZip(GZipMiddleware):
    """Compress pages, not files: PDFs are fetched with byte ranges."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(("/files/", "/thumb/")):
            await self.app(scope, receive, send)
        else:
            await super().__call__(scope, receive, send)


middleware = [
    Middleware(PageGZip, minimum_size=1024),
    Middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="garage_session",
               max_age=60 * 60 * 24 * 30, same_site="lax", https_only=COOKIE_SECURE),
    Middleware(AuthMiddleware),
]


@contextlib.asynccontextmanager
async def lifespan(app):
    CACHE_PATH.mkdir(parents=True, exist_ok=True)
    if sync.git_mode:
        if not (REPO_PATH / ".git").exists():
            await anyio.to_thread.run_sync(sync.sync_once)  # first clone before serving
        # An existing clone is served right away; the loop checks GitHub when due.
        sync.start(inventory.invalidate)
    yield


app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan,
                exception_handlers={HTTPException: not_found})
