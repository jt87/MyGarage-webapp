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
import re
import shutil
import threading
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

from . import __version__, auth, edit, releases, repo, storage
from .sync import FolderNotEmpty, RepoSync, Watcher, WriteError

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
GITHUB_REPO_ENV = setting("GITHUB_REPO")  # "owner/name", for "Edit on GitHub" links
# How stale the cached clone may get before a page view asks GitHub (seconds).
FRESHNESS_SECONDS = float(os.environ.get("FRESHNESS_SECONDS", "300"))
# READ_ONLY=true hides every way to change the data repo from the app.
READ_ONLY = os.environ.get("READ_ONLY", "").strip().lower() in ("1", "true", "yes", "on")
# GitHub rejects files over 100 MB and warns over 50 MB.
MAX_UPLOAD_MB = min(95, int(os.environ.get("MAX_UPLOAD_MB", "50")))

store = storage.StorageConfig(
    CONFIG_PATH.parent / "storage.json", REPO_PATH,
    env_url=setting("GIT_REPO_URL"), env_token=setting("GIT_TOKEN"),
    env_branch=os.environ.get("GIT_BRANCH", ""), env_webhook_secret=setting("WEBHOOK_SECRET"))
sync = RepoSync(
    REPO_PATH,
    url=store.url,
    branch=store.branch,
    token=store.token,
    interval=int(os.environ.get("SYNC_INTERVAL", "1800")),
    state_file=CACHE_PATH / "sync-state.json",
    mode=store.mode,
)
inventory = Watcher(sync, lambda: repo.load(REPO_PATH))

auth_cfg = auth.AuthConfig(CONFIG_PATH)
sessions = auth.Sessions(auth_cfg)
throttle = auth.LoginThrottle()
# Optional: seed the first login from the environment instead of the setup screen.
if not auth_cfg.configured and os.environ.get("AUTH_USERNAME") and os.environ.get("AUTH_PASSWORD"):
    auth_cfg.set_credentials(os.environ["AUTH_USERNAME"], os.environ["AUTH_PASSWORD"])

# UPDATE_CHECK=false stops the daily look at GitHub's release list.
versions = releases.Versions(
    CONFIG_PATH.parent, CACHE_PATH,
    check=os.environ.get("UPDATE_CHECK", "").strip().lower() not in ("0", "false", "no", "off"))
versions.startup(existing_install=auth_cfg.configured)

env = Environment(loader=FileSystemLoader(HERE / "templates"),
                  autoescape=select_autoescape(["html"]))
def static_version() -> str:
    """Changes whenever a file in static/ does: busts caches and updates the service worker."""
    h = hashlib.sha1()
    for p in sorted((HERE / "static").rglob("*")):
        if p.is_file():
            h.update(p.relative_to(HERE).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


STATIC_VERSION = static_version()

env.globals.update(site_title=SITE_TITLE, money=repo.fmt_money, sync=sync,
                   github_repo="", branch=sync.branch, auth=auth_cfg, store=store,
                   METHODS=auth.METHODS, REQUIRED=auth.REQUIRED, v=STATIC_VERSION,
                   app_version=__version__, versions=versions,
                   editable=not READ_ONLY, max_upload_mb=MAX_UPLOAD_MB)
env.filters["q"] = lambda s: quote(str(s))
env.filters["md"] = lambda s: repo._md().render(str(s))  # raw HTML is off, so this is safe


def apply_storage() -> None:
    """Point the app at the storage in `store` (at startup and after a change)."""
    sync.configure(store.mode, store.url, store.token, store.branch)
    env.globals.update(github_repo=GITHUB_REPO_ENV or storage.github_slug(store.url),
                       branch=sync.branch)
    inventory.invalidate()


async def reapply_storage() -> None:
    """apply_storage from a request: off the event loop, since it may wait for the sync lock."""
    await anyio.to_thread.run_sync(apply_storage)


env.globals.update(github_repo=GITHUB_REPO_ENV or storage.github_slug(store.url))


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
    ctx.setdefault("flash", request.query_params.get("msg", ""))
    ctx.setdefault("error", request.query_params.get("error", ""))
    # Log Out only makes sense for a forms login with a real session.
    ctx["can_logout"] = auth_cfg.method == "forms" and getattr(request.state, "via", "") == "session"
    versions.maybe_check()
    ctx.setdefault("whats_new", not versions.data.get("whats_new_seen", True))
    ctx.setdefault("major_popup", versions.major_popup())
    ctx.setdefault("update_banner", versions.update_banner())
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
    return render(request, "tool.html", t=t, back=("/tools", "Tools"), title=t.kind)


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


# --------------------------------------------------------------------------- editing

class EditIds:
    """Remembers recent edit ids so an edit re-sent from a device's outbox
    (the reply was lost, say) is saved once, not twice."""

    def __init__(self, path: Path, keep: int = 1000):
        self.path, self.keep = path, keep
        try:
            self.ids = list(json.loads(path.read_text()))
        except (OSError, ValueError):
            self.ids = []

    def __contains__(self, edit_id: str) -> bool:
        return bool(edit_id) and edit_id in self.ids

    def add(self, edit_id: str) -> None:
        if not edit_id or edit_id in self.ids:
            return
        self.ids = (self.ids + [edit_id])[-self.keep:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.ids))
            tmp.replace(self.path)
        except OSError as e:
            logging.getLogger("garage").warning("could not save edit ids: %s", e)


edit_ids = EditIds(CACHE_PATH / "edit-ids.json")


def wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def with_query(url: str, key: str, value: str) -> str:
    base, _, frag = url.partition("#")
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{key}={quote(value)}" + (f"#{frag}" if frag else "")


def edit_done(request: Request, url: str, msg: str) -> Response:
    if wants_json(request):
        return JSONResponse({"ok": True, "url": url, "message": msg})
    return RedirectResponse(with_query(url, "msg", msg), status_code=303)


def edit_failed(request: Request, back: str, msg: str, status: int, retry: bool = False) -> Response:
    if wants_json(request):
        return JSONResponse({"ok": False, "error": msg, "retry": retry}, status_code=status)
    return RedirectResponse(with_query(back, "error", msg), status_code=303)


def editor_name(request: Request) -> str:
    via = getattr(request.state, "via", "")
    who = "API key" if via == "api" else auth_cfg.username if via in ("session", "basic") else ""
    return f"{who} via {SITE_TITLE}" if who else SITE_TITLE


async def save_edit(request: Request, form, mutate, message: str, back: str,
                    ok_url, ok_msg: str, writer=None) -> Response:
    """Run `mutate` against the latest copy of the repo and publish it.

    `ok_url` may be a callable, for edits (a new tool) whose page isn't
    known until the change is made.
    """
    if READ_ONLY:
        return edit_failed(request, back, "Editing is turned off (READ_ONLY).", 403)
    edit_id = str(form.get("edit_id", ""))[:64]
    url = ok_url() if callable(ok_url) else ok_url
    if edit_id in edit_ids:
        return edit_done(request, url, ok_msg)
    try:
        await anyio.to_thread.run_sync(writer or sync.write, mutate, message, editor_name(request))
    except edit.Conflict as e:
        return edit_failed(request, back, f"{e} was changed on another device while you were "
                           "editing, so nothing was saved. Reload to see the latest, then try "
                           "again.", 409)
    except edit.Invalid as e:
        return edit_failed(request, back, str(e), 400)
    except WriteError as e:
        return edit_failed(request, back, str(e), 503 if e.retry else 502, retry=e.retry)
    edit_ids.add(edit_id)
    inventory.invalidate()
    return edit_done(request, ok_url() if callable(ok_url) else url, ok_msg)


def tool_or_404(request: Request) -> repo.Tool:
    t = inventory.get().tool(request.path_params["slug"])
    if not t:
        raise HTTPException(404)
    return t


def tool_readme(root: Path, slug: str) -> Path:
    p = root / "tools" / slug / "README.md"
    if not p.parent.is_dir():
        raise edit.Invalid("This tool isn't in the repo any more.")
    return p


def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_text(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def money_text(value: str, approx: bool = False) -> str:
    """"150" -> "$150" ("~$150" for an estimate); anything else as typed."""
    value = value.strip()
    if re.fullmatch(r"[\d,]+(?:\.\d{1,2})?", value):
        return ("~$" if approx else "$") + value
    return value


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


async def tool_log(request: Request):
    """Add a maintenance log entry."""
    t = tool_or_404(request)
    back = f"{t.url}/log"
    if request.method == "GET":
        return render(request, "edit_log.html", t=t, back=(t.url, t.kind),
                      title="Log Maintenance", today=today())
    form = await request.form()
    date, what = str(form.get("date", "")).strip(), str(form.get("what", "")).strip()

    def mutate(root: Path) -> list[str]:
        p = tool_readme(root, t.slug)
        write_text(p, edit.add_log_entry(read_text(p), date, what))
        return [f"tools/{t.slug}/README.md"]

    return await save_edit(request, form, mutate, f"{t.model_name}: {what[:60]}", back,
                           f"{t.url}#log", "Maintenance logged")


def tool_form_state(t: repo.Tool, text: str, main_text: str) -> dict:
    """What the details form shows, also sent back as `base` for merging."""
    model_name, subtitle = edit.split_title(text)
    inv_row = edit.get_inventory_row(main_text, t.slug)
    specs = [(k, v) for k, v in edit.get_specs(text) if k.strip().lower() != "category"]
    return {
        "model_name": model_name or t.model_name,
        "subtitle": subtitle,
        "category": "" if t.category == "Uncategorized" else t.category,
        "paid": inv_row.get("paid", ""),
        "value": inv_row.get("value", ""),
        "specs": [list(r) for r in specs],
        "parts": [(r + ["", "", "", ""])[:4] for r in edit.get_table(text, "consumables")],
    }


def current_category(text: str, main_text: str, slug: str) -> str:
    inv_row = edit.get_inventory_row(main_text, slug)
    if repo.plain(inv_row.get("category", "")):
        return repo.plain(inv_row["category"])
    spec = dict((k.strip().lower(), v) for k, v in edit.get_specs(text))
    return repo.plain(spec.get("category", "")).split("(")[0].strip()


PART_HEADER = ["Item", "Spec / part #", "Where to buy", "Typical price"]


async def tool_edit(request: Request):
    """Edit a tool's name, inventory values, details table and parts."""
    t = tool_or_404(request)
    back = f"{t.url}/edit"
    if request.method == "GET":
        state = tool_form_state(t, read_text(REPO_PATH / "tools" / t.slug / "README.md"),
                                read_text(REPO_PATH / "README.md"))
        return render(request, "edit_tool.html", t=t, s=state, base=json.dumps(state),
                      back=(t.url, t.kind), title="Edit Tool")
    form = await request.form()
    try:
        base = json.loads(str(form.get("base", "")))
    except ValueError:
        return edit_failed(request, back, "Reload the page and try again.", 400)
    pairs = lambda *names: [list(r) for r in zip(*(form.getlist(n) for n in names))]
    clean = lambda rows: [[str(c).strip() for c in r] for r in rows if any(str(c).strip() for c in r)]
    mine = {
        "model_name": str(form.get("model_name", "")).strip(),
        "subtitle": str(form.get("subtitle", "")).strip(),
        "category": str(form.get("category", "")).strip(),
        "paid": money_text(str(form.get("paid", ""))),
        "value": money_text(str(form.get("value", "")), approx=True),
        "specs": clean(pairs("spec_k", "spec_v")),
        "parts": clean(pairs("part_item", "part_spec", "part_where", "part_price")),
    }
    if not mine["model_name"]:
        return edit_failed(request, back, "Enter the brand and model.", 400)
    if any(not k for k, _ in mine["specs"]):
        return edit_failed(request, back, "Every detail needs a label.", 400)

    def mutate(root: Path) -> list[str]:
        p, main_p = tool_readme(root, t.slug), root / "README.md"
        text, main_text = read_text(p), read_text(main_p)
        cur = tool_form_state(t, text, main_text)
        cur["category"] = current_category(text, main_text, t.slug)
        m = edit.merge_value
        model_name = m("The name", base["model_name"], mine["model_name"], cur["model_name"])
        subtitle = m("The name", base["subtitle"], mine["subtitle"], cur["subtitle"])
        category = m("The category", base["category"], mine["category"], cur["category"])
        paid = m("The price paid", base["paid"], mine["paid"], cur["paid"])
        value = m("The replacement value", base["value"], mine["value"], cur["value"])
        specs = edit.merge_specs([tuple(r) for r in base["specs"]],
                                 [tuple(r) for r in mine["specs"]],
                                 [tuple(r) for r in cur["specs"]])
        parts = m("The parts list", [list(r) for r in base["parts"]], mine["parts"], cur["parts"])

        new = edit.set_title(text, model_name, subtitle)
        all_specs = edit.get_specs(text)
        cat_row = next((i for i, (k, _) in enumerate(all_specs) if k.strip().lower() == "category"), None)
        has_inv = bool(edit.get_inventory_row(main_text, t.slug))
        if cat_row is not None:
            specs.insert(min(cat_row, len(specs)), (all_specs[cat_row][0],
                         category if category != cur["category"] else all_specs[cat_row][1]))
        elif category and not has_inv:
            specs.insert(0, ("Category", category))
        new = edit.set_specs(new, specs)
        if parts != cur["parts"]:
            new = edit.set_table(new, "consumables", "Consumables & parts", PART_HEADER, parts)
        changed = []
        if new != text:
            write_text(p, new)
            changed.append(f"tools/{t.slug}/README.md")
        inv_values = {k: v for k, v in (("category", category), ("paid", paid), ("value", value))
                      if v != cur[k]}
        if inv_values and (has_inv or main_p.exists()):
            if not has_inv:
                inv_values["tool"] = f"{model_name} {subtitle}".strip()
            new_main = edit.set_inventory_row(main_text, t.slug, inv_values)
            if new_main != main_text:
                write_text(main_p, new_main)
                changed.append("README.md")
        return changed

    return await save_edit(request, form, mutate, f"Edit {mine['model_name']}", back, t.url,
                           "Changes saved")


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


async def tool_markdown(request: Request):
    """1.1 URL for the README editor, now shared by every page."""
    return RedirectResponse(f"/edit/tools/{request.path_params['slug']}", status_code=301)


UPLOAD_KINDS = {"photo": "Photo", "receipt": "Receipt", "manual": "Manual", "document": "Document"}


async def tool_add(request: Request):
    """Add photos, receipts, manuals or documents to a tool's folder."""
    t = tool_or_404(request)
    back = f"{t.url}/add"
    if request.method == "GET":
        kind = request.query_params.get("kind", "photo")
        return render(request, "edit_add.html", t=t, kinds=UPLOAD_KINDS, action=back,
                      kind=kind if kind in UPLOAD_KINDS else "photo",
                      back=(t.url, t.kind), title="Add Files")
    return await add_files(request, f"tools/{t.slug}", t.model_name, back, t.url)


async def add_files(request: Request, rel: str, name: str, back: str, done: str,
                    project: bool = False) -> Response:
    """Save uploaded files into a tool or project folder (one commit)."""
    if int(request.headers.get("content-length") or 0) > (MAX_UPLOAD_MB * 4 + 1) * 1024 * 1024:
        return edit_failed(request, back, f"Add fewer files at once (up to {MAX_UPLOAD_MB * 4} MB).", 413)
    async with request.form(max_files=20) as form:
        kind = "document" if project else str(form.get("kind", "photo"))
        if kind not in UPLOAD_KINDS:
            kind = "photo"
        full_size = form.get("full_size") == "on"
        uploads = [f for f in form.getlist("file") if hasattr(f, "filename") and f.filename]
        if not uploads:
            return edit_failed(request, back, "Choose a file to add.", 400)
        for f in uploads:
            if (f.size or 0) > MAX_UPLOAD_MB * 1024 * 1024:
                return edit_failed(request, back,
                                   f"{f.filename} is over {MAX_UPLOAD_MB} MB, too big for GitHub.", 413)
        now = datetime.now()
        to_crop: list[str] = []  # photos that aren't square, to choose their square next

        def mutate(root: Path) -> list[str]:
            folder = root / rel
            if not folder.is_dir():
                raise edit.Invalid("This isn't in the repo any more.")
            existing = {p.name for p in folder.iterdir()}
            changed = []
            to_crop.clear()
            for f in uploads:
                f.file.seek(0)
                filename = f.filename
                small = None
                if kind == "photo" and not full_size:
                    small = edit.shrink_photo(f.file, filename)
                    f.file.seek(0)
                    if small:
                        filename = small[1]
                out_name = edit.upload_name(filename, kind, existing, now)
                existing.add(out_name)
                tmp = folder / f".{out_name}.tmp"
                with open(tmp, "wb") as out:
                    if small:
                        out.write(small[0])
                    else:
                        shutil.copyfileobj(f.file, out)
                tmp.replace(folder / out_name)
                changed.append(f"{rel}/{out_name}")
                if kind == "photo" and not project and not edit.is_square(folder / out_name):
                    to_crop.append(out_name)
            return changed

        n = len(uploads)
        label = ("file" if project else UPLOAD_KINDS[kind].lower()) + ("s" if n > 1 else "")
        msg = f"Added {n} {label}" if n > 1 else f"{label.capitalize()} added"
        # Photos are shown square: go straight to choosing the square of the first
        # one that isn't. The others can be adjusted from Manage Photos & Files.
        return await save_edit(request, form, mutate,
                               f"Add {n if n > 1 else 'a'} {label} to {name}", back,
                               lambda: f"{done}/crop/{quote(to_crop[0])}" if to_crop else done,
                               f"{msg}. Drag to choose the square that's shown." if to_crop else msg)


async def tool_new(request: Request):
    """Create a tool folder and README, and add it to the inventory table."""
    back = "/tools/new"
    if request.method == "GET":
        return render(request, "edit_new.html", back=("/tools", "Tools"), title="New Tool",
                      categories=[c for c, _ in inventory.get().categories if c != "Uncategorized"])
    form = await request.form()
    f = {k: str(form.get(k, "")).strip() for k in
         ("model_name", "subtitle", "category", "model_no", "serial", "purchased", "paid", "value")}
    if not f["model_name"]:
        return edit_failed(request, back, "Enter the brand and model.", 400)
    paid, value = money_text(f["paid"]), money_text(f["value"], approx=True)
    created = {"slug": ""}

    def mutate(root: Path) -> list[str]:
        base_slug = edit.slugify(f["model_name"]) or "tool"
        slug, n = base_slug, 2
        while (root / "tools" / slug).exists() or slug in edit.RESERVED_SLUGS:
            slug, n = f"{base_slug}-{n}", n + 1
        created["slug"] = slug
        specs = [("Category", f["category"]), ("Model #", f["model_no"]), ("Serial #", f["serial"]),
                 ("Purchased", f["purchased"]), ("Replacement value", value)]
        write_text(root / "tools" / slug / "README.md",
                   edit.new_tool_readme(f["model_name"], f["subtitle"], specs))
        main_p = root / "README.md"
        write_text(main_p, edit.set_inventory_row(read_text(main_p), slug, {
            "tool": f"{f['model_name']} {f['subtitle']}".strip(), "category": f["category"],
            "paid": paid, "value": value}))
        return [f"tools/{slug}/README.md", "README.md"]

    return await save_edit(request, form, mutate, f"Add {f['model_name']}", back,
                           lambda: f"/tools/{created['slug']}" if created["slug"] else "/tools",
                           "Tool added")


# --------------------------------------------------------------------------- offline

async def offline_page(request: Request):
    return render(request, "offline.html", title="Offline", tab="")


async def offline_manifest(request: Request):
    """Everything a device should keep for offline use, and a version that
    changes whenever the repo does (the service worker re-downloads then)."""
    inv = inventory.get()
    pages = ["/", "/tools", "/consumables", "/projects", "/insurance", "/reference", "/search",
             "/offline"]
    thumbs: list[str] = []
    files: list[dict] = []

    def add_files(items):
        for f in items:
            if f.kind in ("pdf", "model", "image"):
                pages.append("/view/" + quote(f.path))
            files.append({"url": f.url, "size": f.size, "kind": f.kind})

    for t in inv.tools:
        pages.append(t.url)
        if not READ_ONLY:
            pages += [f"{t.url}/edit", f"{t.url}/log", f"{t.url}/add", f"{t.url}/files",
                      f"/edit/tools/{t.slug}"]
            pages += [f"{t.url}/log/{i}" for i in range(len(t.log))]
        if t.cover:
            thumbs += [t.cover.square + "&w=320", t.cover.square + "&w=640"]
        thumbs += [p.square + "&w=1280" for p in t.photos]
        if not READ_ONLY:  # adjusting a photo's square works offline too
            pages += [f"{t.url}/crop/{quote(p.name)}" for p in t.photos]
            thumbs += [p.thumb + "&w=640" for p in t.photos]
        add_files(t.photos + t.manuals + t.documents + t.proofs + t.other_files)
    for p in inv.projects:
        pages.append(p.url)
        if not READ_ONLY:
            pages += [f"{p.url}/edit", f"{p.url}/add", f"{p.url}/files", f"/edit/projects/{p.slug}"]
        thumbs += [f.thumb + "&w=640" for f in p.files if f.kind == "image"]
        add_files(p.files)
    pages += [r.url for r in inv.references]
    if not READ_ONLY:
        pages += ["/tools/new", "/projects/new", "/reference/new"]
        pages += [f"/edit/reference/{r.slug}" for r in inv.references]
    version = sync.fingerprint()
    return JSONResponse({"version": f"{version}:{STATIC_VERSION}",
                         "pages": list(dict.fromkeys(pages)),
                         "thumbs": list(dict.fromkeys(thumbs)), "files": files},
                        headers={"Cache-Control": "no-store"})


async def service_worker(request: Request):
    js = (HERE / "static" / "sw.js").read_text().replace("__VERSION__", STATIC_VERSION)
    return Response(js, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


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


def crop_box(size: tuple[int, int], crop) -> tuple[int, int, int, int]:
    """The square of an image of `size` that a crop (x, y, zoom) keeps."""
    w, h = size
    x, y, z = crop
    side = max(1, round(min(w, h) / z))
    left = min(max(round(x * w - side / 2), 0), w - side)
    top = min(max(round(y * h - side / 2), 0), h - side)
    return left, top, left + side, top + side


def make_thumb(src: Path, out: Path, w: int, crop=None) -> bool:
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
            if crop:
                im = im.crop(crop_box(im.size, crop))
                im.thumbnail((w, w), Image.Resampling.LANCZOS)
            else:
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
    # &sq=x,y,zoom: the photo's square crop (photos are always shown square).
    crop = repo.clean_crop(request.query_params["sq"].split(",")) if "sq" in request.query_params else None
    st = full.stat()
    key = hashlib.sha1(f"{full}:{st.st_mtime_ns}:{st.st_size}:{w}:{crop}".encode()).hexdigest()
    out = CACHE_PATH / "thumbs" / key[:2] / f"{key}.jpg"
    if not out.exists() and not await anyio.to_thread.run_sync(make_thumb, full, out, w, crop):
        return FileResponse(full)
    # The URL carries ?v=<mtime> (and the crop), so a changed photo gets a new URL.
    return FileResponse(out, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"
                                 if "v" in request.query_params else "private, no-cache"})


# --------------------------------------------------------------------------- admin

async def do_sync(request: Request):
    """"Check for Updates": asks GitHub, at most once per 15 seconds."""
    if sync.remote:
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
        "version": __version__,
        "mode": sync.mode or "setup",
        "backup_error": sync.backup_error or None,
        "head": sync.head[:7],
        "last_checked": sync.last_checked.isoformat() if sync.last_checked else None,
        "checks_today": sync.checks_today,
        "fetches_today": sync.fetches_today,
        "error": sync.last_error or None,
        "tools": len(inventory.get().tools),
    }


async def github_webhook(request: Request):
    """GitHub push webhook: pull right away instead of waiting for a page view."""
    secret = store.webhook_secret
    if not secret or not sync.remote:
        raise HTTPException(404)
    if int(request.headers.get("content-length") or 0) > 2_000_000:
        return PlainTextResponse("too large", status_code=413)
    body = await request.body()
    sig = request.headers.get("x-hub-signature-256", "")
    want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
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
    # Healthy while waiting for setup or the first download, too.
    ok = not sync.configured or bool(sync.head) or (REPO_PATH / "README.md").exists()
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
            nxt = "/" if sync.configured else "/setup/storage"
            return set_session(RedirectResponse(nxt, status_code=303), request, remember=True)
    return plain_page(request, "setup.html", local=local, error=error,
                      status=400 if error else 200)


# --------------------------------------------------------------------------- storage setup

setup_lock = anyio.Lock()  # one first-run setup at a time (a double tap, a second tab)


async def setup_storage(request: Request):
    """First run, step 2: where the garage data lives."""
    if sync.configured:
        return RedirectResponse("/", status_code=303)
    ctx = {"where": "server", "starter": "empty", "repo": "", "branch": "main", "error": ""}
    if request.method == "GET":
        return plain_page(request, "setup_storage.html", **{**ctx, "error": setup_error})
    async with setup_lock:
        if sync.configured:  # the other request finished setting up
            return RedirectResponse("/", status_code=303)
        return await run_setup(request, ctx)


async def run_setup(request: Request, ctx: dict) -> Response:
    global setup_error
    setup_error = ""
    form = await request.form()
    ctx.update({k: str(form.get(k, ctx[k])).strip() for k in ("where", "starter", "repo", "branch")})
    starter = storage.STARTERS / ("sample" if ctx["starter"] == "sample" else "empty")
    token, branch = str(form.get("token", "")).strip(), ctx["branch"] or "main"
    was_empty = not folder_has_files()
    stray = [p for p in REPO_PATH.iterdir() if p.name != "lost+found" and not p.name.startswith(".")] \
        if REPO_PATH.exists() else []
    if ctx["where"] != "github" and stray and not (REPO_PATH / "README.md").exists():
        # Something's there, but not a garage: don't mix a starter garage into it.
        return plain_page(request, "setup_storage.html", status=400, **{**ctx, "error": (
            f"The data folder ({REPO_PATH}) has files but no garage in it. Empty it to "
            "start a new garage, or put your garage's files there first.")})
    try:
        if ctx["where"] == "github":
            msg = await connect_github_source(ctx["repo"], token, branch, starter)
        elif ctx["where"] == "server_backup":
            msg = await connect_server_with_backup(ctx["repo"], token, branch, starter)
        else:
            store.set(mode="server", url="", token="", branch=sync.branch)
            await reapply_storage()
            await anyio.to_thread.run_sync(sync.init_local, starter)
            msg = "Your garage is ready"
    except Exception as e:
        # Whatever failed, go back to "not set up" so the screen can be tried again,
        # emptying the folder only if setup started with it empty.
        error = str(e) if isinstance(e, WriteError) else f"Couldn't set up the garage: {e}"
        if not isinstance(e, WriteError):
            logging.getLogger("garage").exception("first-run setup failed")
        error += await anyio.to_thread.run_sync(reset_setup, was_empty)
        return plain_page(request, "setup_storage.html", status=400, **{**ctx, "error": error})
    inventory.invalidate()
    return RedirectResponse(with_query("/", "msg", msg), status_code=303)


def folder_has_files() -> bool:
    return REPO_PATH.exists() and any(REPO_PATH.iterdir())


setup_error = ""  # why a setup finishing in the background failed, for the setup screen


def reset_setup(empty_folder: bool) -> str:
    """Back to "not set up" after a failed setup. Returns anything that also
    went wrong doing that, to add to the error shown."""
    problems = ""
    try:
        sync.reset_unconfigured(empty_folder)
    except Exception as e:
        logging.getLogger("garage").error("couldn't empty the data folder: %s", e)
        problems += f" The data folder couldn't be emptied afterwards ({e}); empty {REPO_PATH} and try again."
    try:
        # needs_setup keeps the setup screen reachable even if files were left behind.
        store.set(mode="", url="", token="", needs_setup=True)
        apply_storage()
    except OSError as e:
        # Leave sync "not set up" (reset_unconfigured did that) rather than
        # re-applying the old saved mode to what may now be an empty folder.
        logging.getLogger("garage").error("couldn't reset the storage setting: %s", e)
        problems += f" The storage setting couldn't be saved either ({e})."
    return problems


def clone_or_reset() -> None:
    """Bring an existing GitHub garage here; if that fails, back to the setup
    screen (nothing retries a half-made server copy by itself)."""
    global setup_error
    try:
        sync.clone_full()
    except Exception as e:
        setup_error = f"Couldn't bring the garage over from GitHub: {e}"
        logging.getLogger("garage").warning(setup_error)
        # Files that appeared in the folder meanwhile aren't ours to delete.
        setup_error += reset_setup(empty_folder=not isinstance(e, FolderNotEmpty))


def in_background(name: str, fn) -> None:
    """Downloads can take a while with lots of photos and manuals; the home
    screen says what's happening meanwhile."""
    def run():
        try:
            fn()
        except Exception as e:  # shown on the home screen
            sync.last_error = str(e)
            logging.getLogger("garage").warning("%s failed: %s", name, e)
        inventory.invalidate()
    threading.Thread(target=run, name=name, daemon=True).start()


async def connect_server_with_backup(repo_text: str, token: str, branch: str, starter: Path) -> str:
    """This server is the primary copy; a GitHub repo is its backup."""
    url = storage.normalize_repo_url(repo_text)
    if not url:
        raise WriteError("Enter the GitHub repo, like owner/my-garage.")
    tip = await anyio.to_thread.run_sync(sync.check_remote, url, token, branch)
    if tip:
        # A garage already on GitHub: bring all of it here, then keep it backed up.
        if folder_has_files():
            raise WriteError(f"The data folder ({REPO_PATH}) already has files. Empty it to "
                             "bring in the GitHub garage, or choose On This Server to keep them.")
        store.set(mode="server", url=url, token=token, branch=branch)
        await reapply_storage()
        in_background("first-clone", clone_or_reset)
        return "Bringing your garage over from GitHub"
    store.set(mode="server", url=url, token=token, branch=branch)
    await reapply_storage()
    await anyio.to_thread.run_sync(sync.init_local, starter)
    await anyio.to_thread.run_sync(sync.sync_backup)
    return "Your garage is ready and backed up to GitHub"


async def connect_github_source(repo_text: str, token: str, branch: str, starter: Path) -> str:
    url = storage.normalize_repo_url(repo_text)
    if not url:
        raise WriteError("Enter the GitHub repo, like owner/my-garage.")
    tip = await anyio.to_thread.run_sync(sync.check_remote, url, token, branch)
    # GitHub as the primary copy starts from an empty folder: never commit into,
    # or reset, files (or a git checkout) that are already there.
    if folder_has_files():
        raise WriteError(f"The data folder ({REPO_PATH}) already has files. Empty it to use "
                         "GitHub, or choose On This Server to keep them.")
    if not tip:
        # An empty repo: start it here and send it up.
        sync.configure("server", url, token, branch)
        await anyio.to_thread.run_sync(sync.seed_remote, starter)  # run_setup undoes a failure
    store.set(mode="github", url=url, token=token, branch=branch)
    await reapply_storage()
    if not tip:
        return "Your garage is ready and saved to GitHub"
    in_background("first-clone", sync.sync_once)
    return "Downloading your garage from GitHub"


async def settings_storage(request: Request):
    """Change where the garage lives: which copy is primary, and the GitHub backup."""
    form = await request.form()
    action = str(form.get("action", ""))
    if store.env_locked:
        return back_to_settings(error="Storage is set in the app's YAML (GIT_REPO_URL).")
    if not await anyio.to_thread.run_sync(auth_cfg.check, auth_cfg.username,
                                          str(form.get("current", ""))):
        return back_to_settings(error="Your current password is incorrect.")
    if action == "disconnect" and sync.mode == "server":
        try:
            store.set(url="", token="")
        except OSError as e:
            return back_to_settings(error=f"Couldn't save the setting ({e}).")
        await reapply_storage()
        return back_to_settings(msg="GitHub backup disconnected. Nothing was deleted on GitHub.")
    token = str(form.get("token", "")).strip()
    try:
        if action in ("connect", "primary_github") and sync.mode == "server" and not sync.url:
            # Switching to GitHub from a server-only garage connects it first.
            url = storage.normalize_repo_url(str(form.get("repo", "")))
            if not url:
                return back_to_settings(error="Enter the GitHub repo, like owner/my-garage.")
            await anyio.to_thread.run_sync(sync.check_remote, url, token, sync.branch)
            store.set(url=url, token=token)
            await reapply_storage()
            await anyio.to_thread.run_sync(sync.sync_backup)
            inventory.invalidate()
            if sync.backup_error:
                return back_to_settings(error=f"Connected, but the backup didn't finish: {sync.backup_error}")
            if action == "connect":
                return back_to_settings(msg="Backed up to GitHub. Every change from now on is sent there too.")
        if action == "primary_github" and sync.mode == "server":
            await anyio.to_thread.run_sync(sync.make_github_primary)
            store.set(mode="github")
            await reapply_storage()
            return back_to_settings(msg="GitHub is now the primary copy. This server keeps a copy "
                                        "of it, and edits made here are saved to GitHub.")
        if action == "primary_server" and sync.mode == "github":
            await anyio.to_thread.run_sync(sync.make_server_primary)
            store.set(mode="server")
            await reapply_storage()
            return back_to_settings(msg="This server is now the primary copy, with its full "
                                        "history. GitHub stays connected as the backup.")
        if action == "history" and sync.mode == "folder":
            try:
                await anyio.to_thread.run_sync(sync.init_local, None)
            except RuntimeError as e:
                return back_to_settings(error=f"Couldn't start the history: {e}")
            store.set(mode="server", url="", token="")  # only once the repo is ready
            await reapply_storage()
            inventory.invalidate()
            return back_to_settings(msg="This server now keeps a history of every change.")
        if action == "token" and sync.remote:
            await anyio.to_thread.run_sync(sync.check_remote, store.url, token, sync.branch)
            store.set(token=token)
            await reapply_storage()
            return back_to_settings(msg="Token updated.")
    except WriteError as e:
        await reapply_storage()  # whatever happened, run with what's saved
        return back_to_settings(error=str(e))
    except OSError as e:
        await reapply_storage()
        return back_to_settings(error=f"Couldn't save the setting ({e}). Check that MyGarage can write to its config folder.")
    return back_to_settings(error="That change isn't available for this storage.")


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
                  flash="", msg=request.query_params.get("msg", ""), error=request.query_params.get("error", ""))


async def settings_about(request: Request):
    # Reading the notes answers both notices.
    versions.mark_whats_new_seen()
    if major := versions.major_popup():
        versions.dismiss_major(major.version)
    md = repo._md()
    newer = [(r, md.render(r.notes)) for r in versions.newer()]
    installed = [(r, md.render(r.notes)) for r in versions.installed_notes()]
    return render(request, "about.html", back=("/settings", "Settings"), title="What's New",
                  tab="garage", whats_new=False, newer=newer, installed=installed)


async def settings_updates(request: Request):
    """Dismiss the "Updated" or "available" banner, or the new-major-version notice."""
    form = await request.form()
    action = form.get("action")
    if action == "seen":
        versions.mark_whats_new_seen()
    elif action == "dismiss_major":
        versions.dismiss_major(str(form.get("version", "")))
    elif action == "dismiss_update":
        versions.dismiss_update(str(form.get("version", "")))
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": True})
    return RedirectResponse(safe_next(str(form.get("next", ""))), status_code=303)


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
OPEN_PATHS = ("/static/", "/manifest.webmanifest", "/webhook/github", "/healthz", "/sw.js")
# Requests that don't render repo content skip the freshness check.
NO_FRESH_PATHS = ("/static/", "/thumb/", "/files/", "/healthz", "/manifest.webmanifest",
                  "/webhook/", "/login", "/logout", "/setup", "/settings", "/sync", "/sw.js",
                  "/offline")


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
                if not sync.configured and not path.startswith(("/setup", "/logout")):
                    if request.method == "GET" and "text/html" in request.headers.get("accept", "text/html"):
                        return RedirectResponse("/setup/storage", status_code=303)
                    return PlainTextResponse("Finish setting up first", status_code=503)
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


from . import manage  # noqa: E402  (uses the helpers above)

routes = [
    Route("/", home),
    *manage.routes_before,
    Route("/tools", tools),
    Route("/tools/new", tool_new, methods=["GET", "POST"]),
    Route("/tools/{slug}", tool),
    Route("/tools/{slug}/log", tool_log, methods=["GET", "POST"]),
    Route("/tools/{slug}/edit", tool_edit, methods=["GET", "POST"]),
    Route("/tools/{slug}/markdown", tool_markdown),
    Route("/tools/{slug}/add", tool_add, methods=["GET", "POST"]),
    *manage.routes,
    Route("/offline", offline_page),
    Route("/offline/manifest.json", offline_manifest),
    Route("/sw.js", service_worker),
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
    Route("/setup/storage", setup_storage, methods=["GET", "POST"]),
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/settings", settings),
    Route("/settings/security", settings_security, methods=["POST"]),
    Route("/settings/account", settings_account, methods=["POST"]),
    Route("/settings/apikey", settings_apikey, methods=["POST"]),
    Route("/settings/sessions", settings_sessions, methods=["POST"]),
    Route("/settings/storage", settings_storage, methods=["POST"]),
    Route("/settings/about", settings_about),
    Route("/settings/updates", settings_updates, methods=["POST"]),
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
    if sync.git_mode and not (REPO_PATH / ".git").exists():
        await anyio.to_thread.run_sync(sync.sync_once)  # first clone before serving
    elif sync.mode == "server" and not (REPO_PATH / ".git").exists():
        await anyio.to_thread.run_sync(sync.init_local, None)
    # An existing copy is served right away; the loop checks GitHub when due.
    sync.start(inventory.invalidate)
    yield


app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan,
                exception_handlers={HTTPException: not_found})
