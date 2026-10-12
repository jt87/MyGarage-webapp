"""Managing the garage from the app: files, deleting, projects, reference
pages, log entries, history and undo.

Every change goes through main.save_edit, so it's one commit (or one write
to a plain folder), works from a device's offline outbox, and is saved once
even if it's sent twice.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

import anyio
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.routing import Route

from . import edit, main as m, repo

# --------------------------------------------------------------------------- helpers


def folder_of(kind: str, slug: str) -> str:
    return f"{'tools' if kind == 'tool' else 'projects'}/{slug}"


def owner_or_404(request: Request, kind: str):
    inv = m.inventory.get()
    item = (inv.tool if kind == "tool" else inv.project)(request.path_params["slug"])
    if not item:
        raise HTTPException(404)
    return item


def existing_folder(root: Path, rel: str) -> Path:
    p = root / rel
    if not p.is_dir():
        raise edit.Invalid("This isn't in the repo any more.")
    return p


def item_files(item) -> list[repo.FileItem]:
    if isinstance(item, repo.Tool):
        return item.photos + item.proofs + item.manuals + item.documents + item.other_files
    return item.files


# --------------------------------------------------------------------------- files

FILE_KINDS = {"photo": "Photo", "receipt": "Receipt", "manual": "Manual", "document": "Document"}


def kind_of(item, f: repo.FileItem) -> str:
    if isinstance(item, repo.Tool):
        if f in item.proofs:
            return "receipt"
        if f in item.manuals:
            return "manual"
        if f.kind == "image":
            return "photo"
    return "document"


async def manage_files(request: Request, kind: str):
    item = owner_or_404(request, kind)
    rel = folder_of(kind, item.slug)
    back = f"{item.url}/files"
    name = item.model_name if kind == "tool" else item.title
    if request.method == "GET":
        files = [(f, kind_of(item, f)) for f in item_files(item)]
        cover = item.cover.name if item.cover else ""
        return m.render(request, "manage_files.html", item=item, files=files, cover=cover,
                        is_tool=kind == "tool", kinds=FILE_KINDS, back=(item.url, name),
                        title="Files")
    form = await request.form()
    action = str(form.get("action", ""))
    old = str(form.get("name", ""))
    new_name = str(form.get("new_name", "")).strip()
    new_kind = str(form.get("kind", ""))
    now = datetime.now()

    def mutate(root: Path) -> list[str]:
        folder = existing_folder(root, rel)
        src = folder / old
        if not old or "/" in old or old.startswith(".") or old == "README.md" or not src.is_file():
            raise edit.Invalid("That file isn't there any more.")
        readme = folder / "README.md"
        if action == "delete":
            src.unlink()
            return [f"{rel}/{old}"]
        existing = {p.name for p in folder.iterdir()} - {old}
        if action == "cover":
            target = "photo" + Path(old).suffix.lower().replace(".jpeg", ".jpg")
            changed = []
            for p in list(folder.iterdir()):
                if p.name.startswith("photo.") and p.name != old:
                    moved = edit.upload_name(p.name, "photo", existing | {"photo.x"}, now)
                    p.rename(folder / moved)
                    existing.discard(p.name)
                    existing.add(moved)
                    changed += [f"{rel}/{p.name}", f"{rel}/{moved}"]
            if old == target:
                return changed
            dest = target
        elif action == "rename":
            if kind == "tool" and new_kind in FILE_KINDS and new_kind != str(form.get("old_kind", "")):
                dest = edit.upload_name(old, new_kind, existing, now)
            else:
                stem = edit.SAFE_NAME.sub("-", new_name.lower()).strip("-._")
                if not stem:
                    raise edit.Invalid("Enter a name.")
                ext = Path(old).suffix.lower()
                stem = stem.removesuffix(ext)
                dest, n = f"{stem}{ext}", 2
                while dest in existing:
                    dest, n = f"{stem}-{n}{ext}", n + 1
            if dest == old:
                return []
            changed = []
        else:
            raise edit.Invalid("Choose what to do with the file.")
        src.rename(folder / dest)
        changed += [f"{rel}/{old}", f"{rel}/{dest}"]
        if readme.exists():
            text = m.read_text(readme)
            new_text = edit.rewrite_links(text, old, dest)
            if new_text != text:
                m.write_text(readme, new_text)
                changed.append(f"{rel}/README.md")
        return changed

    verbs = {"delete": "Delete", "cover": "Set cover photo", "rename": "Rename"}
    msgs = {"delete": "File deleted", "cover": "Cover photo set", "rename": "File renamed"}
    return await m.save_edit(request, form, mutate, f"{verbs.get(action, 'Change')} {old} ({name})",
                             back, back, msgs.get(action, "Saved"))


async def tool_files(request: Request):
    return await manage_files(request, "tool")


async def project_files(request: Request):
    return await manage_files(request, "project")


# --------------------------------------------------------------------------- deleting

async def delete_item(request: Request, kind: str):
    item = owner_or_404(request, kind)
    rel = folder_of(kind, item.slug)
    name = item.model_name if kind == "tool" else item.title
    form = await request.form()
    if form.get("confirm") != item.slug:
        return m.edit_failed(request, f"{item.url}/edit", "Confirm by typing the folder name.", 400)

    def mutate(root: Path) -> list[str]:
        shutil.rmtree(existing_folder(root, rel))
        changed = [rel]
        if kind == "tool":
            main_p = root / "README.md"
            text = m.read_text(main_p)
            new = edit.remove_inventory_row(text, item.slug)
            if new != text:
                m.write_text(main_p, new)
                changed.append("README.md")
        return changed

    listing = "/tools" if kind == "tool" else "/projects"
    return await m.save_edit(request, form, mutate, f"Delete {name}", f"{item.url}/edit",
                             listing, f"{name} deleted")


async def tool_delete(request: Request):
    return await delete_item(request, "tool")


async def project_delete(request: Request):
    return await delete_item(request, "project")


# --------------------------------------------------------------------------- log entries

async def log_entry(request: Request):
    """Edit or delete one maintenance log entry (/tools/<slug>/log/<n>)."""
    t = m.tool_or_404(request)
    readme = m.REPO_PATH / "tools" / t.slug / "README.md"
    try:
        n = int(request.path_params["n"])
        date, what = edit.log_rows(m.read_text(readme))[n][:2]
    except (ValueError, IndexError):
        raise HTTPException(404)
    back = f"{t.url}/log/{n}"
    if request.method == "GET":
        return m.render(request, "edit_log.html", t=t, back=(t.url, t.model_name),
                        title="Edit Entry", today=date, entry=(date, what))
    form = await request.form()
    old = (str(form.get("old_date", "")), str(form.get("old_what", "")))
    delete = form.get("action") == "delete"
    new = None if delete else (str(form.get("date", "")).strip(), str(form.get("what", "")).strip())

    def mutate(root: Path) -> list[str]:
        p = m.tool_readme(root, t.slug)
        m.write_text(p, edit.change_log_entry(m.read_text(p), old, new))
        return [f"tools/{t.slug}/README.md"]

    return await m.save_edit(request, form, mutate,
                             f"{t.model_name}: {'remove' if delete else 'edit'} log entry {old[0]}",
                             back, f"{t.url}#log", "Entry deleted" if delete else "Entry saved")


# --------------------------------------------------------------------------- projects

PROJECT_STATUSES = ["planning", "in progress", "on hold", "done"]


def project_state(text: str) -> dict:
    title = re.search(r"^#\s+(.+)$", text, re.M)
    return {"title": title.group(1).strip() if title else "",
            **{f.lower(): edit.get_project_field(text, f) for f in edit.PROJECT_FIELDS},
            "tools": edit.project_tools(text)}


async def project_edit(request: Request):
    p = owner_or_404(request, "project")
    rel = f"projects/{p.slug}/README.md"
    back = f"{p.url}/edit"
    if request.method == "GET":
        s = project_state(m.read_text(m.REPO_PATH / rel))
        return m.render(request, "edit_project.html", p=p, s=s, base=m.json.dumps(s),
                        statuses=PROJECT_STATUSES, tools=m.inventory.get().tools,
                        back=(p.url, p.title), title="Edit Project")
    form = await request.form()
    try:
        base = m.json.loads(str(form.get("base", "")))
    except ValueError:
        return m.edit_failed(request, back, "Reload the page and try again.", 400)
    mine = {"title": str(form.get("title", "")).strip(),
            "status": str(form.get("status", "")).strip(),
            "started": str(form.get("started", "")).strip(),
            "finished": str(form.get("finished", "")).strip(),
            "tools": sorted(set(form.getlist("tools")))}
    if not mine["title"]:
        return m.edit_failed(request, back, "Enter a title.", 400)
    names = {t.slug: t.model_name for t in m.inventory.get().tools}

    def mutate(root: Path) -> list[str]:
        path = existing_folder(root, f"projects/{p.slug}") / "README.md"
        text = m.read_text(path)
        cur = project_state(text)
        merged = {k: edit.merge_value(f"The {k}", base[k], mine[k], cur[k])
                  for k in ("title", "status", "started", "finished", "tools")}
        new = edit.set_title(text, merged["title"], "") if merged["title"] != cur["title"] else text
        for f in edit.PROJECT_FIELDS:
            if merged[f.lower()] != cur[f.lower()]:
                new = edit.set_project_field(new, f, merged[f.lower()])
        if merged["tools"] != cur["tools"]:
            new = edit.set_project_tools(new, [(s, names.get(s, s)) for s in merged["tools"]])
        if new == text:
            return []
        m.write_text(path, new)
        return [rel]

    return await m.save_edit(request, form, mutate, f"Edit {mine['title']}", back, p.url,
                             "Changes saved")


async def project_new(request: Request):
    back = "/projects/new"
    tools = m.inventory.get().tools
    if request.method == "GET":
        return m.render(request, "edit_project.html", p=None, s={"status": "planning",
                        "started": datetime.now().strftime("%Y-%m"), "tools": []}, base="",
                        statuses=PROJECT_STATUSES, tools=tools, back=("/projects", "Projects"),
                        title="New Project")
    form = await request.form()
    title = str(form.get("title", "")).strip()
    if not title:
        return m.edit_failed(request, back, "Enter a title.", 400)
    started = str(form.get("started", "")).strip()
    names = {t.slug: t.model_name for t in tools}
    chosen = [(s, names.get(s, s)) for s in sorted(set(form.getlist("tools")))]
    created = {"slug": ""}

    def mutate(root: Path) -> list[str]:
        prefix = re.match(r"\d{4}-\d{2}", started)
        base_slug = "-".join(filter(None, [prefix.group(0) if prefix else datetime.now().strftime("%Y-%m"),
                                           edit.slugify(title)]))
        slug, n = base_slug, 2
        while (root / "projects" / slug).exists():
            slug, n = f"{base_slug}-{n}", n + 1
        created["slug"] = slug
        m.write_text(root / "projects" / slug / "README.md", edit.new_project_readme(
            title, str(form.get("status", "planning")), started, str(form.get("goal", "")), chosen))
        return [f"projects/{slug}/README.md"]

    return await m.save_edit(request, form, mutate, f"Add project {title}", back,
                             lambda: f"/projects/{created['slug']}" if created["slug"] else "/projects",
                             "Project added")


async def project_add(request: Request):
    """Add plans, photos or 3D files to a project folder."""
    p = owner_or_404(request, "project")
    back = f"{p.url}/add"
    if request.method == "GET":
        return m.render(request, "edit_add.html", t=p, kinds={"document": "File"}, kind="document",
                        action=f"{p.url}/add", back=(p.url, p.title), title="Add Files")
    return await m.add_files(request, f"projects/{p.slug}", p.title, back, p.url, project=True)


# --------------------------------------------------------------------------- Markdown pages

def markdown_target(request: Request) -> tuple[str, str, tuple[str, str], str]:
    """(repo path, page URL, back link, label) for a Markdown editor URL."""
    kind = request.path_params["kind"]
    slug = request.path_params["slug"]
    inv = m.inventory.get()
    if kind == "tools" and (t := inv.tool(slug)):
        return f"tools/{slug}/README.md", t.url, (f"{t.url}/edit", "Edit Tool"), t.model_name
    if kind == "projects" and (p := inv.project(slug)):
        return f"projects/{slug}/README.md", p.url, (f"{p.url}/edit", "Edit Project"), p.title
    if kind == "reference" and (r := inv.reference(slug)):
        rel = "reference/README.md" if slug == "index" else f"reference/{slug}.md"
        return rel, r.url, (r.url, r.title), r.title
    raise HTTPException(404)


async def markdown(request: Request):
    """Edit a page's Markdown as text, for anything the forms don't cover."""
    rel, url, back_link, label = markdown_target(request)
    back = request.url.path
    if request.method == "GET":
        text = m.read_text(m.REPO_PATH / rel)
        return m.render(request, "edit_markdown.html", rel=rel, label=label, text=text,
                        base=m.text_hash(text), action=back, done=url,
                        delete=rel.startswith("reference/") and rel != "reference/README.md",
                        back=back_link, title=Path(rel).name)
    form = await request.form()
    if form.get("action") == "delete":
        def remove(root: Path) -> list[str]:
            (root / rel).unlink(missing_ok=True)
            return [rel]
        return await m.save_edit(request, form, remove, f"Delete {label}", back, "/reference",
                                 "Page deleted")
    new = str(form.get("text", "")).replace("\r\n", "\n").rstrip() + "\n"
    base, force = str(form.get("base", "")), form.get("force") == "1"
    if not new.strip():
        return m.edit_failed(request, back, "The page can't be empty.", 400)

    def mutate(root: Path) -> list[str]:
        path = root / rel
        if not path.parent.is_dir():
            raise edit.Invalid("This isn't in the repo any more.")
        cur = m.read_text(path)
        if m.text_hash(cur) != base and cur != new and not force:
            raise edit.Conflict("This page")
        if cur == new:
            return []
        m.write_text(path, new)
        return [rel]

    return await m.save_edit(request, form, mutate, f"Edit {label}", back, url, "Changes saved")


async def reference_new(request: Request):
    back = "/reference/new"
    if request.method == "GET":
        return m.render(request, "edit_reference.html", back=("/reference", "Reference"),
                        title="New Page")
    form = await request.form()
    title = str(form.get("title", "")).strip()
    body = str(form.get("text", "")).replace("\r\n", "\n").strip()
    if not title:
        return m.edit_failed(request, back, "Enter a title.", 400)
    created = {"slug": ""}

    def mutate(root: Path) -> list[str]:
        base_slug = edit.slugify(title) or "page"
        slug, n = base_slug, 2
        while (root / "reference" / f"{slug}.md").exists() or slug in ("index", "readme", "new"):
            slug, n = f"{base_slug}-{n}", n + 1
        created["slug"] = slug
        m.write_text(root / "reference" / f"{slug}.md", f"# {edit.cell(title)}\n\n{body}\n")
        return [f"reference/{slug}.md"]

    return await m.save_edit(request, form, mutate, f"Add reference page {title}", back,
                             lambda: f"/reference/{created['slug']}" if created["slug"] else "/reference",
                             "Page added")


# --------------------------------------------------------------------------- history & undo

async def history(request: Request, kind: str = ""):
    slug = request.path_params.get("slug", "")
    inv = m.inventory.get()
    if kind:
        item = inv.tool(slug) if kind == "tools" else inv.project(slug) if kind == "projects" else None
        if not item:
            raise HTTPException(404)
        name = item.model_name if kind == "tools" else item.title
        paths, back = [f"{kind}/{slug}"], (item.url, name)
    else:
        name, paths, back = "", [], ("/settings", "Settings")
    entries = await anyio.to_thread.run_sync(m.sync.history, paths)
    return m.render(request, "history.html", entries=entries, name=name, back=back,
                    title="History", here=request.url.path, versioned=m.sync.versioned)


async def tool_history(request: Request):
    return await history(request, "tools")


async def project_history(request: Request):
    return await history(request, "projects")


def soft_revert(root: Path, sha: str) -> list[str] | None:
    """Reverse one commit's changes where the lines it wrote are still there.

    Returns the changed paths, or None (and changes nothing) if any part of
    the commit can't be reversed unambiguously.
    """
    g = m.sync
    status = g.git("diff-tree", "--no-commit-id", "-r", "--no-renames", "--name-status", sha)
    plan: dict[str, bytes | None] = {}
    for line in status.splitlines():
        code, _, path = line.partition("\t")
        target = root / path
        now = target.read_bytes() if target.is_file() else None
        before = g.git_bytes("show", f"{sha}^1:{path}")
        after = g.git_bytes("show", f"{sha}:{path}")
        if code == "A":
            if now is not None and now != after:
                return None
            plan[path] = None
        elif code == "D":
            if now is not None:
                return None
            plan[path] = before
        elif now == after:
            plan[path] = before
        elif now is None or before is None or after is None or b"\0" in now:
            return None
        else:
            text = reverse_hunks(now.decode("utf-8", "replace"), g.git(
                "diff", "-U0", "--no-color", f"{sha}^1", sha, "--", path),
                before.decode("utf-8", "replace"))
            if text is None:
                return None
            plan[path] = text.encode()
    for path, data in plan.items():
        target = root / path
        if data is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return list(plan)


def reverse_hunks(text: str, diff: str, old_text: str) -> str | None:
    """Apply a zero-context diff backwards, finding each changed line by its content."""
    lines = text.split("\n")
    hunks, cur = [], None
    for d in diff.splitlines():
        if d.startswith("@@"):
            mm = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)", d)
            cur = {"old_start": int(mm.group(1)), "minus": [], "plus": []}
            hunks.append(cur)
        elif cur is not None and d.startswith("-") and not d.startswith("---"):
            cur["minus"].append(d[1:])
        elif cur is not None and d.startswith("+") and not d.startswith("+++"):
            cur["plus"].append(d[1:])
    for h in reversed(hunks):
        if h["plus"]:
            idx = [i for i, l in enumerate(lines) if l == h["plus"][0]]
            if len(idx) != 1 or lines[idx[0]: idx[0] + len(h["plus"])] != h["plus"]:
                return None
            at = idx[0]
            lines[at: at + len(h["plus"])] = h["minus"]
        else:  # the commit only removed lines: put them back after the line before them
            if h["old_start"] <= 1:
                lines[0:0] = h["minus"]
                continue
            anchor = old_text.split("\n")[h["old_start"] - 2]
            idx = [i for i, l in enumerate(lines) if l == anchor]
            if len(idx) != 1:
                return None
            lines[idx[0] + 1: idx[0] + 1] = h["minus"]
    return "\n".join(lines)


async def undo(request: Request):
    """Undo one commit by adding a commit that reverses it (history is kept)."""
    form = await request.form()
    sha = str(form.get("sha", ""))
    subject = str(form.get("subject", ""))[:80]
    back = m.safe_next(str(form.get("back", "/history")))
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not m.sync.versioned:
        return m.edit_failed(request, back, "That change can't be undone.", 400)

    def mutate(root: Path) -> list[str]:
        try:
            m.sync.git("cat-file", "-e", f"{sha}^{{commit}}")
            m.sync.git("cat-file", "-e", f"{sha}^1^{{commit}}")
        except RuntimeError:
            raise edit.Invalid("That change is too old to undo here.")
        try:
            m.sync.git("revert", "--no-commit", sha)
            return [p for p in m.sync.git("diff", "--cached", "--name-only").splitlines() if p] or ["."]
        except RuntimeError:
            try:
                m.sync.git("revert", "--abort")
            except RuntimeError:
                m.sync.git("reset", "-q", "--hard", "HEAD")
        # Git calls it a conflict when a later change sits on the next line (a
        # newer log entry right below, say). Undo it line by line instead.
        changed = soft_revert(root, sha)
        if changed is None:
            raise edit.Invalid("A later change touched the same lines, so this can't be undone "
                               "automatically. Edit the page by hand instead.")
        return changed

    def write(mut, message, author):
        return m.sync.write(mut, message, author, depth=m.sync.HISTORY_DEPTH)

    return await m.save_edit(request, form, mutate, f'Undo "{subject}"', back, back,
                             "Change undone", writer=write)


routes_before = [  # must come before main's /tools/{slug}, /projects/{slug}, /reference/{slug}
    Route("/projects/new", project_new, methods=["GET", "POST"]),
    Route("/reference/new", reference_new, methods=["GET", "POST"]),
]
routes = [
    Route("/tools/{slug}/files", tool_files, methods=["GET", "POST"]),
    Route("/tools/{slug}/delete", tool_delete, methods=["POST"]),
    Route("/tools/{slug}/log/{n:int}", log_entry, methods=["GET", "POST"]),
    Route("/projects/{slug}/edit", project_edit, methods=["GET", "POST"]),
    Route("/projects/{slug}/add", project_add, methods=["GET", "POST"]),
    Route("/projects/{slug}/files", project_files, methods=["GET", "POST"]),
    Route("/projects/{slug}/delete", project_delete, methods=["POST"]),
    Route("/edit/{kind}/{slug}", markdown, methods=["GET", "POST"]),
    Route("/history", history),
    Route("/tools/{slug}/history", tool_history),
    Route("/projects/{slug}/history", project_history),
    Route("/undo", undo, methods=["POST"]),
]
