"""MyGarage web app: browse tools, manuals, consumables, projects and receipts."""

from __future__ import annotations

import base64
import contextlib
import csv
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import anyio
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                                 RedirectResponse, Response)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import auth, repo
from .sync import RepoSync, Watcher

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

HERE = Path(__file__).parent


def setting(name: str, default: str = "") -> str:
    """An environment value, ignoring the compose file's CHANGE_ME placeholders.

    A placeholder left in WEBHOOK_SECRET would be a publicly known secret, and
    one left in GIT_TOKEN makes GitHub refuse even a public repo.
    """
    value = os.environ.get(name, default)
    if "CHANGE_ME" in value:
        logging.getLogger("garage").warning("%s still says CHANGE_ME; ignoring it", name)
        return default
    return value


REPO_PATH = Path(os.environ.get("REPO_PATH", "/data/repo")).resolve()
CACHE_PATH = Path(os.environ.get("CACHE_PATH", "/data/cache")).resolve()
SITE_TITLE = os.environ.get("SITE_TITLE", "MyGarage")
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/data/config/auth.json"))
GITHUB_REPO = setting("GITHUB_REPO")  # "owner/name", for "Edit on GitHub" links
WEBHOOK_SECRET = setting("WEBHOOK_SECRET")
# How stale the cached clone may get before a page view asks GitHub (seconds).
FRESHNESS_SECONDS = float(os.environ.get("FRESHNESS_SECONDS", "300"))

sync = RepoSync(
    REPO_PATH,
    url=setting("GIT_REPO_URL"),
    branch=os.environ.get("GIT_BRANCH", "main"),
    token=setting("GIT_TOKEN"),
    interval=int(os.environ.get("SYNC_INTERVAL", "1800")),
    state_file=CACHE_PATH / "sync-state.json",
)
inventory = Watcher(sync, lambda: repo.load(REPO_PATH))

auth_cfg = auth.AuthConfig(CONFIG_PATH)
sessions = auth.Sessions(auth_cfg)
throttle = auth.LoginThrottle()
# Optional: seed the first login from the environment instead of the setup screen.
if not auth_cfg.configured and os.environ.get("AUTH_USERNAME") and os.environ.get("AUTH_PASSWORD"):
    auth_cfg.set_credentials(os.environ["AUTH_USERNAME"], os.environ["AUTH_PASSWORD"])

env = Environment(loader=FileSystemLoader(HERE / "templates"),
                  autoescape=select_autoescape(["html"]))
env.globals.update(site_title=SITE_TITLE, money=repo.fmt_money, sync=sync,
                   github_repo=GITHUB_REPO, branch=sync.branch, auth=auth_cfg,
                   METHODS=auth.METHODS, REQUIRED=auth.REQUIRED)
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
    # Log Out only makes sense for a forms login with a real session.
    ctx["can_logout"] = auth_cfg.method == "forms" and getattr(request.state, "via", "") == "session"
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
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    base = f"{'https' if is_https(request) else 'http'}://{host}"

    def cell(v):
        v = str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v

    for t in inv.tools:
        w.writerow([cell(v) for v in (
            t.title, t.category, t.model_no, t.serial, t.spec_text.get("purchased", ""),
            t.paid_text, t.value_text, t.spec_text.get("replacement value", ""),
            "; ".join(f.name for f in t.proofs) or "none")] + [base + t.url])
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
    # SVG can carry script, so it downloads instead of opening on this origin.
    inline = (media.startswith("image/") and media != "image/svg+xml"
              or media in ("application/pdf", "text/plain"))
    disp = "inline" if inline and "download" not in request.query_params else "attachment"
    ascii_name = full.name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "")
    return FileResponse(full, media_type=media, headers={
        "Content-Disposition": f"{disp}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(full.name)}",
        "Cache-Control": "private, no-cache"})


THUMB_WIDTHS = (320, 640, 1280, 2048)


def make_thumb(src: Path, out: Path, w: int) -> bool:
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    tmp = out.with_suffix(f".{os.getpid()}.tmp")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((w, w * 2))
            im.convert("RGB").save(tmp, "JPEG", quality=82, optimize=True, progressive=True)
        tmp.replace(out)
        return True
    except Exception as e:  # unreadable or unsupported image: serve the original
        logging.getLogger("garage").warning("thumbnail failed for %s: %s", src.name, e)
        tmp.unlink(missing_ok=True)
        return False


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
    if not out.exists() and not await anyio.to_thread.run_sync(make_thumb, full, out, w):
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
    ref = urlsplit(request.headers.get("referer", ""))
    same_site = not ref.netloc or ref.netloc in (request.headers.get("host"),
                                                 request.headers.get("x-forwarded-host"))
    return RedirectResponse(safe_next(ref.path) if same_site else "/", status_code=303)


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
    if int(request.headers.get("content-length") or 0) > 2_000_000:
        return PlainTextResponse("too large", status_code=413)
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
        # Only pushes to the branch we serve cost a fetch. ensure_fresh keeps
        # the hourly cap, so replayed or flooded deliveries can't hammer GitHub.
        if ref == f"refs/heads/{sync.branch}" and await anyio.to_thread.run_sync(
                sync.ensure_fresh, 5):
            inventory.invalidate()
    return PlainTextResponse("ok")


async def healthz(request: Request):
    ok = (REPO_PATH / "README.md").exists()
    return JSONResponse({"ok": ok}, status_code=200 if ok else 503)


# --------------------------------------------------------------------------- auth

def is_cross_site(request: Request) -> bool:
    """True for a form posted from another website."""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site:                       # Safari 16.4+, Chrome, Firefox
        return fetch_site == "cross-site"
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return False
    hosts = {request.headers.get("host"), *request.headers.get("x-forwarded-host", "").split(",")}
    return urlsplit(origin).netloc not in {h.strip() for h in hosts if h}


def safe_next(value: str | None) -> str:
    ok = (value and value.startswith("/") and not value.startswith(("//", "/\\"))
          and value.isprintable())
    return value if ok else "/"


def is_https(request: Request) -> bool:
    return (request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https")


def set_session(resp: Response, request: Request, remember: bool) -> Response:
    token, max_age = sessions.issue(remember)
    resp.set_cookie(auth.COOKIE, token, max_age=max_age, httponly=True, samesite="lax",
                    secure=is_https(request), path="/")
    return resp


def plain_page(request: Request, template: str, status: int = 200, **ctx) -> HTMLResponse:
    """Full-screen pages without the app chrome (setup, login)."""
    html = env.get_template(template).render(request=request, **ctx)
    return HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-store"})


def valid_username(name: str) -> bool:
    return 1 <= len(name) <= 64 and name.strip() == name and all(c.isprintable() for c in name)


async def setup(request: Request):
    """First run: create the login (like the *arr "Authentication Required" dialog)."""
    if auth_cfg.configured:
        return RedirectResponse("/", status_code=303)
    local = auth.is_local(request.scope)
    error = ""
    if request.method == "POST" and local:
        form = await request.form()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        method = str(form.get("method", "forms"))
        required = str(form.get("required", "enabled"))
        if not valid_username(username):
            error = "Enter a username."
        elif len(password) < auth.MIN_PASSWORD:
            error = f"Use at least {auth.MIN_PASSWORD} characters for the password."
        elif password != form.get("confirm"):
            error = "The passwords don't match."
        elif method not in auth.METHODS or method == "external" or required not in auth.REQUIRED:
            error = "Choose a login method."
        else:
            await anyio.to_thread.run_sync(auth_cfg.set_credentials, username, password)
            auth_cfg.data.update(method=method, required=required)
            auth_cfg.save()
            return set_session(RedirectResponse("/", status_code=303), request, remember=True)
    return plain_page(request, "setup.html", local=local, error=error,
                      status=400 if error else 200)


async def login(request: Request):
    if not auth_cfg.configured:
        return RedirectResponse("/setup", status_code=303)
    if auth_cfg.method != "forms":
        return RedirectResponse("/", status_code=303)
    nxt = safe_next(request.query_params.get("next"))
    error = ""
    if request.method == "POST":
        form = await request.form()
        key = auth.client_key(request.scope)
        if throttle.blocked(key):
            error = "Too many attempts. Try again in 15 minutes."
        else:
            ok = await anyio.to_thread.run_sync(
                auth_cfg.check, str(form.get("username", "")), str(form.get("password", "")))
            if ok:
                throttle.succeed(key)
                return set_session(RedirectResponse(nxt, status_code=303), request,
                                   remember=form.get("remember") == "on")
            throttle.fail(key)
            log_auth.warning("failed login from %s", key)
            error = "Incorrect username or password."
    return plain_page(request, "login.html", error=error, next=nxt, status=401 if error else 200)


async def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


async def settings(request: Request):
    return render(request, "settings.html", back=("/", SITE_TITLE), title="Settings", tab="garage",
                  msg=request.query_params.get("msg", ""), error=request.query_params.get("error", ""))


def back_to_settings(msg: str = "", error: str = "") -> RedirectResponse:
    q = f"?msg={quote(msg)}" if msg else f"?error={quote(error)}" if error else ""
    return RedirectResponse(f"/settings{q}", status_code=303)


async def settings_security(request: Request):
    form = await request.form()
    if not await anyio.to_thread.run_sync(auth_cfg.check, auth_cfg.username,
                                          str(form.get("current", ""))):
        return back_to_settings(error="Your current password is incorrect.")
    method, required = str(form.get("method")), str(form.get("required"))
    if method not in auth.METHODS or required not in auth.REQUIRED:
        return back_to_settings(error="Choose a login method.")
    if method == "external" and form.get("confirm_external") != "on":
        return back_to_settings(error="Confirm that your reverse proxy requires a login before choosing External.")
    auth_cfg.data.update(method=method, required=required)
    auth_cfg.save()
    resp = back_to_settings(msg="Security settings saved.")
    return set_session(resp, request, remember=True) if method == "forms" else resp


async def settings_account(request: Request):
    form = await request.form()
    if not await anyio.to_thread.run_sync(auth_cfg.check, auth_cfg.username,
                                          str(form.get("current", ""))):
        return back_to_settings(error="Your current password is incorrect.")
    username = str(form.get("username", "")).strip() or auth_cfg.username
    new = str(form.get("password", ""))
    if not valid_username(username):
        return back_to_settings(error="Enter a username.")
    if new and len(new) < auth.MIN_PASSWORD:
        return back_to_settings(error=f"Use at least {auth.MIN_PASSWORD} characters for the password.")
    if new and new != form.get("confirm"):
        return back_to_settings(error="The new passwords don't match.")
    if new:
        # Changing the password signs out every other device, as it should.
        await anyio.to_thread.run_sync(auth_cfg.set_credentials, username, new)
    else:
        auth_cfg.data["username"] = username
        auth_cfg.revoke_sessions()
    return set_session(back_to_settings(msg="Account updated. Other devices were logged out."),
                       request, remember=True)


async def settings_apikey(request: Request):
    auth_cfg.regenerate_api_key()
    return back_to_settings(msg="New API key created. Update any scripts that use the old one.")


async def settings_sessions(request: Request):
    auth_cfg.revoke_sessions()
    resp = back_to_settings(msg="All other devices were logged out.")
    return set_session(resp, request, remember=True) if auth_cfg.method == "forms" else resp


log_auth = logging.getLogger("garage.auth")
_basic_ok: dict[str, float] = {}   # sha256(Authorization header) -> expiry; skips re-hashing


def _api_key_ok(request: Request) -> bool:
    key = request.headers.get("x-api-key") or request.query_params.get("apikey") or ""
    return bool(key) and hmac.compare_digest(key, auth_cfg.api_key)


async def _basic_ok_for(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    digest = hashlib.sha256((header + str(auth_cfg.data["session_version"])).encode()).hexdigest()
    if _basic_ok.get(digest, 0) > time.time():
        return True
    key = auth.client_key(request.scope)
    if throttle.blocked(key):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    if await anyio.to_thread.run_sync(auth_cfg.check, user, pw):
        now = time.time()
        for k in [k for k, exp in _basic_ok.items() if exp < now]:
            del _basic_ok[k]
        _basic_ok[digest] = now + 600
        throttle.succeed(key)
        return True
    throttle.fail(key)
    return False


async def authenticate(request: Request) -> str | None:
    """How this request is allowed in: external, local, api, session or basic."""
    if auth_cfg.method == "external":
        return "external"
    if auth_cfg.required == "local" and auth.is_local(request.scope):
        return "local"
    if _api_key_ok(request):
        return "api"
    if auth_cfg.method == "basic":
        return "basic" if await _basic_ok_for(request) else None
    return "session" if sessions.user(request.cookies.get(auth.COOKIE)) else None


# Never need a login: assets, the webhook (it checks GitHub's signature) and the health check.
OPEN_PATHS = ("/static/", "/manifest.webmanifest", "/webhook/github", "/healthz")
# Requests that don't render repo content skip the freshness check.
NO_FRESH_PATHS = ("/static/", "/thumb/", "/files/", "/healthz", "/manifest.webmanifest",
                  "/webhook/", "/login", "/logout", "/setup", "/settings", "/sync")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if not path.startswith(OPEN_PATHS):
            # Cross-site form posts are refused (SameSite cookies already block most).
            if request.method == "POST" and is_cross_site(request):
                return PlainTextResponse("Cross-site request refused", status_code=403)
            if not auth_cfg.configured:
                if path != "/setup":
                    return RedirectResponse("/setup", status_code=303)
            elif path != "/login":
                via = await authenticate(request)
                if not via:
                    if auth_cfg.method == "basic":
                        return PlainTextResponse("Login required", status_code=401, headers={
                            "WWW-Authenticate": f'Basic realm="{SITE_TITLE}", charset="UTF-8"'})
                    if request.method == "GET" and "text/html" in request.headers.get("accept", "text/html"):
                        nxt = path + (f"?{request.url.query}" if request.url.query else "")
                        return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
                    return PlainTextResponse("Login required", status_code=401)
                if via == "api" and path.startswith("/settings"):
                    return PlainTextResponse("The API key can't change security settings",
                                             status_code=403)
                request.state.via = via
        if request.method == "GET" and not path.startswith(NO_FRESH_PATHS):
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
    Route("/setup", setup, methods=["GET", "POST"]),
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/settings", settings),
    Route("/settings/security", settings_security, methods=["POST"]),
    Route("/settings/account", settings_account, methods=["POST"]),
    Route("/settings/apikey", settings_apikey, methods=["POST"]),
    Route("/settings/sessions", settings_sessions, methods=["POST"]),
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
