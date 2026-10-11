"""Read the garage repo (Markdown + files) into structured data.

The repo stays the source of truth: nothing here writes to it. Everything is
parsed from the README conventions described in CLAUDE.md, and anything that
doesn't fit a known shape is still rendered as plain Markdown.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

from markdown_it import MarkdownIt

# HEIC/HEIF is what an iPhone camera saves; thumbnails convert it to JPEG.
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}
MODEL_EXT = {".stl"}
VIEWABLE_EXT = IMAGE_EXT | MODEL_EXT | {".pdf"}
PROOF_RE = re.compile(r"receipt|invoice|proof|order|purchase|bill-of-sale", re.I)
MANUAL_RE = re.compile(r"manual|guide|quick-?start|instructions", re.I)
SERVED_DIRS = ("tools", "projects", "reference")

# Spec rows that get their own place on the tool page instead of the spec table.
SPECIAL_ROWS = {"photo", "photos", "purchased", "replacement value", "manual", "category"}


# --------------------------------------------------------------------------- markdown

def _md() -> MarkdownIt:
    return MarkdownIt("commonmark", {"html": False, "typographer": False}).enable(
        ["table", "strikethrough"]
    )


def rewrite_href(href: str, base_dir: str, root: Path) -> str:
    """Map a link written relative to a README to a URL inside the web app.

    `base_dir` is the README's folder relative to the repo root (e.g.
    "tools/acme-ts10"). Absolute URLs, anchors and mailto: pass through.
    """
    if not href or href.startswith("#"):
        return href
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        return href
    target = posixpath.normpath(posixpath.join(base_dir, parts.path)).lstrip("/")
    if target.startswith(".."):
        return href
    frag = f"#{parts.fragment}" if parts.fragment else ""
    segs = target.split("/")
    full = root / target
    if full.is_dir() or target.endswith("README.md") or parts.path.endswith("/"):
        if target.endswith("README.md"):
            segs = segs[:-1]
        if len(segs) == 2 and segs[0] in ("tools", "projects"):
            return f"/{segs[0]}/{segs[1]}{frag}"
        if segs and segs[0] == "reference":
            return f"/reference{frag}"
        return f"/{frag}" if not segs or segs == [""] else href
    if target.endswith(".md") and segs[0] == "reference":
        return f"/reference/{quote(posixpath.splitext(segs[-1])[0])}{frag}"
    if segs[0] in SERVED_DIRS:
        ext = posixpath.splitext(target)[1].lower()
        # Viewable files open in the in-app viewer (which has a back button);
        # a raw PDF in a Home Screen app is a dead end.
        prefix = "/view/" if ext in VIEWABLE_EXT else "/files/"
        return f"{prefix}{quote(target)}{frag}"
    return href


class Renderer:
    """Markdown renderer that rewrites relative links per document."""

    def __init__(self, root: Path):
        self.root = root
        self.md = _md()
        self._base = ""
        default_link = self.md.renderer.rules.get("link_open")
        default_image = self.md.renderer.rules.get("image")

        def link_open(tokens, idx, options, env):
            tok = tokens[idx]
            href = tok.attrGet("href") or ""
            new = rewrite_href(href, self._base, self.root)
            tok.attrSet("href", new)
            if new.startswith("http"):
                tok.attrSet("target", "_blank")
                tok.attrSet("rel", "noopener noreferrer")
            if default_link:
                return default_link(tokens, idx, options, env)
            return self.md.renderer.renderToken(tokens, idx, options, env)

        def image(tokens, idx, options, env):
            tok = tokens[idx]
            src = tok.attrGet("src") or ""
            new = rewrite_href(src, self._base, self.root)
            for prefix in ("/files/", "/view/"):
                if new.startswith(prefix):
                    new = "/thumb/" + new[len(prefix):].split("#")[0] + "?w=1280"
            tok.attrSet("src", new)
            tok.attrSet("loading", "lazy")
            return default_image(tokens, idx, options, env)

        self.md.renderer.rules["link_open"] = link_open
        self.md.renderer.rules["image"] = image

    def block(self, text: str, base_dir: str) -> str:
        self._base = base_dir
        html = self.md.render(text)
        return html.replace("<table>", '<div class="table-wrap"><table>').replace(
            "</table>", "</table></div>"
        )

    def inline(self, text: str, base_dir: str) -> str:
        self._base = base_dir
        return self.md.renderInline(text)


def plain(text: str) -> str:
    """Strip common inline Markdown (links, bold, code) to plain text."""
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`]{1,3}", "", text)
    text = re.sub(r"<([^>]+)>", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- tables & sections

def split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    cells = re.split(r"(?<!\\)\|", line)
    return [c.strip().replace("\\|", "|") for c in cells]


def is_sep(line: str) -> bool:
    return bool(re.fullmatch(r"\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*", line))


def extract_table(text: str) -> tuple[list[str], list[list[str]], str]:
    """Pull the first pipe table out of `text`.

    Returns (header, rows, text_without_table). Rows that are entirely empty
    (template placeholders) are dropped.
    """
    lines = text.splitlines()
    for i in range(len(lines) - 1):
        if lines[i].lstrip().startswith("|") and is_sep(lines[i + 1]):
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            header = split_row(lines[i])
            rows = [split_row(l) for l in lines[i + 2 : j]]
            rows = [r for r in rows if any(c for c in r)]
            rest = "\n".join(lines[:i] + lines[j:])
            return header, rows, rest
    return [], [], text


@dataclass
class Section:
    title: str
    body: str


def split_sections(text: str) -> tuple[str, str, list[Section]]:
    """Return (h1 title, preamble, [## sections])."""
    title = ""
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        title = m.group(1).strip()
        text = text[: m.start()] + text[m.end() :]
    chunks = re.split(r"^##\s+(.+)$", text, flags=re.M)
    preamble = chunks[0]
    sections = [
        Section(chunks[k].strip(), chunks[k + 1].strip()) for k in range(1, len(chunks), 2)
    ]
    return title, preamble.strip(), sections


# --------------------------------------------------------------------------- files

@dataclass
class FileItem:
    name: str
    path: str  # relative to repo root
    size: int
    kind: str  # image | pdf | model | other
    mtime: int = 0

    @property
    def url(self) -> str:
        return "/files/" + quote(self.path)

    @property
    def thumb(self) -> str:
        """JPEG thumbnail URL; append "&w=<px>". Versioned so it can be cached."""
        return f"/thumb/{quote(self.path)}?v={self.mtime}"

    @property
    def ext(self) -> str:
        return Path(self.name).suffix.lower().lstrip(".")

    @property
    def label(self) -> str:
        stem = Path(self.name).stem.replace("-", " ").replace("_", " ")
        return stem[:1].upper() + stem[1:]

    @property
    def human_size(self) -> str:
        n = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
            n /= 1024
        return ""


def folder_files(root: Path, rel: str) -> list[FileItem]:
    out = []
    for p in sorted((root / rel).iterdir()):
        if p.is_dir() or p.name.startswith(".") or p.name == "README.md":
            continue
        ext = p.suffix.lower()
        kind = (
            "image" if ext in IMAGE_EXT
            else "pdf" if ext == ".pdf"
            else "model" if ext in MODEL_EXT
            else "other"
        )
        st = p.stat()
        out.append(FileItem(p.name, f"{rel}/{p.name}", st.st_size, kind, int(st.st_mtime)))
    return out


def sort_photos(photos: list[FileItem]) -> list[FileItem]:
    """Main photo first, then other photos, nameplates last."""
    def key(f: FileItem):
        n = f.name.lower()
        return (0 if n.startswith("photo.") else 2 if "nameplate" in n else 1, n)
    return sorted(photos, key=key)


# --------------------------------------------------------------------------- buy links

RETAILERS = [
    ("Amazon", ("amazon",), "https://www.amazon.com/s?k={q}"),
    ("Home Depot", ("home depot",), "https://www.homedepot.com/s/{q}"),
    ("Lowe's", ("lowe",), "https://www.lowes.com/search?searchTerm={q}"),
    ("Walmart", ("walmart",), "https://www.walmart.com/search?q={q}"),
    ("eBay", ("ebay",), "https://www.ebay.com/sch/i.html?_nkw={q}"),
    ("Harbor Freight", ("harbor freight",), "https://www.harborfreight.com/search?q={q}"),
    ("Menards", ("menards",), "https://www.menards.com/main/search.html?search={q}"),
]
WEB_SEARCH = "https://www.google.com/search?tbm=shop&q={q}"


@dataclass
class BuyLink:
    label: str
    url: str


def buy_links(where: str, query: str) -> list[BuyLink]:
    """Search links for the retailers named in a "Where to buy" cell.

    Always ends with a general shopping search so there's a link even when
    the source is "welding supply" or "Acme parts dealers".
    """
    q = quote(query)
    low = where.lower()
    links = [
        BuyLink(name, url.format(q=q))
        for name, keys, url in RETAILERS
        if any(k in low for k in keys)
    ]
    if "anywhere" in low and not links:
        links = [BuyLink(n, u.format(q=q)) for n, _, u in RETAILERS[:3]]
    links.append(BuyLink("Search web", WEB_SEARCH.format(q=q)))
    return links


def part_numbers(cell: str, brand: str) -> list[tuple[str, str]]:
    """Bold runs in a spec cell are part numbers by convention.

    Returns [(part, search query)]. The query is prefixed with the brand
    written just before the part in the cell ("Bladeco **BC1040X**"), else the
    tool's own brand. Bold used for emphasis ('**91-1/2"** long') is skipped.
    """
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"\*\*([^*]+)\*\*", cell):
        part = m.group(1).strip()
        first = part.split()[0] if part else ""
        if (not re.match(r"[A-Za-z0-9]", first) or "," in first or '"' in part
                or not re.search(r"\d", first) or len(part) > 30):
            continue
        if any(p == part for p, _ in out):
            continue
        before = re.findall(r"[A-Za-z][\w'&-]*", cell[: m.start()].split(";")[-1].split(",")[-1])
        caps = []
        for w in reversed(before[-2:]):
            if w[0].isupper() and w.lower() not in {"the", "or", "and", "use"}:
                caps.insert(0, w)
            else:
                break
        out.append((part, f"{' '.join(caps) or brand} {part}"))
    return out


def fallback_query(item: str, spec: str) -> str:
    item = re.sub(r"\([^)]*\)", "", plain(item)).strip()
    spec = re.split(r"[;,(]|\. ", plain(spec))[0].strip()
    q = f"{item} {spec}".strip()
    return q[:60].rsplit(" ", 1)[0] if len(q) > 60 else q


# --------------------------------------------------------------------------- money

MONEY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")


def parse_money(text: str) -> float | None:
    m = MONEY_RE.search(text or "")
    return float(m.group(1).replace(",", "")) if m else None


def fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}" if v % 1 else f"${v:,.0f}"


# --------------------------------------------------------------------------- models

@dataclass
class Consumable:
    tool_slug: str
    tool_name: str
    item_html: str
    spec_html: str
    where_html: str
    price_html: str
    item: str
    spec: str
    where: str
    price: str
    parts: list[str]
    links: list[BuyLink]
    part_links: list[tuple[str, list[BuyLink]]]

    @property
    def buy_json(self) -> str:
        """Data for the "where to buy" action sheet."""
        return json.dumps({
            "title": self.item,
            "message": self.spec[:140],
            "links": [{"label": l.label, "url": l.url} for l in self.links],
            "parts": self.parts,
            "more": [{"part": p, "url": links[0].url} for p, links in self.part_links],
        })


@dataclass
class LogEntry:
    date: str
    what_html: str
    what: str
    slug: str
    title: str
    kind: str  # tool | project


@dataclass
class Tool:
    slug: str
    title: str
    brand: str
    model_name: str
    subtitle: str
    category: str
    specs: list[tuple[str, str]]  # (label, html)
    spec_text: dict[str, str]
    purchased_html: str
    replacement_html: str
    paid_text: str
    value_text: str
    value: float | None
    paid: float | None
    photos: list[FileItem]
    manuals: list[FileItem]
    documents: list[FileItem]
    proofs: list[FileItem]
    other_files: list[FileItem]
    consumables: list[Consumable]
    consumables_notes_html: str
    sections_html: list[tuple[str, str]]
    log: list[LogEntry]
    log_notes_html: str
    raw: str
    projects: list[tuple[str, str]] = field(default_factory=list)  # (slug, title)

    @property
    def url(self) -> str:
        return f"/tools/{self.slug}"

    @property
    def cover(self) -> FileItem | None:
        return self.photos[0] if self.photos else None

    @property
    def serial(self) -> str:
        return self.spec_text.get("serial #", "")

    @property
    def model_no(self) -> str:
        return self.spec_text.get("model #", "")


@dataclass
class Project:
    slug: str
    title: str
    status: str
    started: str
    finished: str
    goal: str
    html: str
    files: list[FileItem]
    tools: list[str]
    raw: str

    @property
    def url(self) -> str:
        return f"/projects/{self.slug}"

    @property
    def cover(self) -> FileItem | None:
        imgs = [f for f in self.files if f.kind == "image"]
        return imgs[0] if imgs else None


@dataclass
class RefPage:
    slug: str
    title: str
    html: str
    raw: str

    @property
    def url(self) -> str:
        return f"/reference/{self.slug}"


@dataclass
class Inventory:
    tools: list[Tool]
    projects: list[Project]
    references: list[RefPage]
    consumables: list[Consumable]
    log: list[LogEntry]
    refreshed: str
    total_value_text: str
    total_value: float
    total_paid: float
    readme_html: str

    @property
    def total_display(self) -> str:
        return (self.total_value_text.split(" (")[0] if self.total_value_text
                else fmt_money(self.total_value))

    @property
    def tools_by_category(self) -> list[tuple[str, list[Tool]]]:
        groups: dict[str, list[Tool]] = {}
        for t in sorted(self.tools, key=lambda t: t.model_name.lower()):
            groups.setdefault(t.category, []).append(t)
        return sorted(groups.items())

    def tool(self, slug: str) -> Tool | None:
        return next((t for t in self.tools if t.slug == slug), None)

    def project(self, slug: str) -> Project | None:
        return next((p for p in self.projects if p.slug == slug), None)

    def reference(self, slug: str) -> RefPage | None:
        return next((r for r in self.references if r.slug == slug), None)

    @property
    def categories(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for t in self.tools:
            counts[t.category] = counts.get(t.category, 0) + 1
        return sorted(counts.items())


# --------------------------------------------------------------------------- loaders

def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def parse_inventory_table(text: str) -> tuple[dict[str, dict], str, str]:
    """Root README inventory: {slug: {name, category, paid, value}}, refreshed, total."""
    rows: dict[str, dict] = {}
    total = ""
    refreshed = ""
    m = re.search(r"Values last refreshed:\s*(.+)", text)
    if m:
        refreshed = m.group(1).strip()
    sec = text.split("## Inventory", 1)[-1]
    header, body, _ = extract_table(sec)
    if not header:
        return rows, refreshed, total
    cols = [h.lower() for h in header]

    def col(name):
        return next((i for i, c in enumerate(cols) if name in c), None)

    i_tool, i_cat, i_paid, i_val, i_folder = (
        col("tool"), col("category"), col("paid"), col("replacement"), col("folder"),
    )
    for r in body:
        get = lambda i: r[i] if i is not None and i < len(r) else ""
        if "total" in plain(get(i_tool)).lower():
            total = plain(get(i_val))
            continue
        fm = re.search(r"tools/([^/)\]]+)", get(i_folder))
        if fm:
            rows[fm.group(1)] = {
                "name": plain(get(i_tool)),
                "category": plain(get(i_cat)),
                "paid": plain(get(i_paid)),
                "value": plain(get(i_val)),
            }
    return rows, refreshed, total


def load_tool(root: Path, slug: str, r: Renderer, inv_row: dict) -> Tool:
    rel = f"tools/{slug}"
    raw = _read(root / rel / "README.md")
    title, preamble, sections = split_sections(raw)
    title = title or inv_row.get("name") or slug
    if "—" in title:
        name_part, subtitle = [s.strip() for s in title.split("—", 1)]
    else:
        name_part, subtitle = title, inv_row.get("name", "")
    brand = name_part.split()[0] if name_part else slug.split("-")[0].title()

    _, kv_rows, pre_rest = extract_table(preamble)
    specs, spec_text = [], {}
    purchased_html = replacement_html = ""
    category = inv_row.get("category", "")
    for row in kv_rows:
        if len(row) < 2:
            continue
        key, val = plain(row[0]), row[1]
        k = key.lower()
        spec_text[k] = plain(val)
        if not val.strip():
            continue
        html = r.inline(val, rel)
        if k == "purchased":
            purchased_html = html
        elif k == "replacement value":
            replacement_html = html
        elif k == "category" and not category:
            category = plain(val).split("(")[0].strip()
        if k not in SPECIAL_ROWS:
            specs.append((key, html))
    category = category or "Uncategorized"

    files = folder_files(root, rel)
    photos, manuals, documents, proofs, other = [], [], [], [], []
    for f in files:
        if PROOF_RE.search(f.name):
            proofs.append(f)
        elif f.kind == "image":
            photos.append(f)
        elif f.kind == "pdf" and MANUAL_RE.search(f.name):
            manuals.append(f)
        elif f.kind == "pdf":
            documents.append(f)
        else:
            other.append(f)

    tool_name = subtitle or name_part
    consumables: list[Consumable] = []
    cons_notes = ""
    sections_html: list[tuple[str, str]] = []
    log: list[LogEntry] = []
    log_notes = ""
    for s in sections:
        st = s.title.lower()
        if st.startswith("consumables"):
            header, rows, rest = extract_table(s.body)
            for row in rows:
                row = (row + ["", "", "", ""])[:4]
                item, spec, where, price = row
                found = part_numbers(spec, brand)
                parts = [p for p, _ in found]
                query_base = found[0][1] if found else fallback_query(item, spec)
                consumables.append(
                    Consumable(
                        tool_slug=slug,
                        tool_name=f"{name_part}",
                        item_html=r.inline(item, rel),
                        spec_html=r.inline(spec, rel),
                        where_html=r.inline(where, rel),
                        price_html=r.inline(price, rel),
                        item=plain(item),
                        spec=plain(spec),
                        where=plain(where),
                        price=plain(price),
                        parts=parts,
                        links=buy_links(plain(where), query_base),
                        part_links=[(p, buy_links(plain(where), q)) for p, q in found[1:]],
                    )
                )
            cons_notes = r.block(rest.strip(), rel) if rest.strip() else ""
            continue
        if st.startswith("maintenance"):
            header, rows, rest = extract_table(s.body)
            for row in rows:
                if len(row) >= 2 and row[0].strip():
                    log.append(
                        LogEntry(plain(row[0]), r.inline(row[1], rel), plain(row[1]),
                                 slug, name_part, "tool")
                    )
            if header:  # shown as list rows; keep any text around the table
                log_notes = r.block(rest.strip(), rel) if rest.strip() else ""
                continue
        sections_html.append((s.title, r.block(s.body, rel)))
    if pre_rest.strip():
        sections_html.insert(0, ("Notes", r.block(pre_rest, rel)))

    paid_text = inv_row.get("paid", "")
    value_text = inv_row.get("value", "")
    if not value_text:
        v = parse_money(spec_text.get("replacement value", ""))
        value_text = f"~{fmt_money(v)}" if v else ""
    return Tool(
        slug=slug,
        title=title,
        brand=brand,
        model_name=name_part,
        subtitle=subtitle,
        category=category,
        specs=specs,
        spec_text=spec_text,
        purchased_html=purchased_html,
        replacement_html=replacement_html,
        paid_text=paid_text or plain(spec_text.get("purchased", "")),
        value_text=value_text,
        value=parse_money(value_text),
        paid=parse_money(paid_text),
        photos=sort_photos(photos),
        manuals=manuals,
        documents=documents,
        proofs=proofs,
        other_files=other,
        consumables=consumables,
        consumables_notes_html=cons_notes,
        sections_html=sections_html,
        log=log,
        log_notes_html=log_notes,
        raw=raw,
    )


def load_project(root: Path, slug: str, r: Renderer) -> Project:
    rel = f"projects/{slug}"
    raw = _read(root / rel / "README.md")
    title, preamble, sections = split_sections(raw)

    def field_of(name):
        m = re.search(rf"^{name}:[ \t]*(.*)$", preamble, re.M | re.I)
        return m.group(1).strip() if m else ""

    status = field_of("Status") or "unknown"
    started, finished = field_of("Started"), field_of("Finished")
    body = re.sub(r"^(Status|Started|Finished):.*$\n?", "", raw, flags=re.M | re.I)
    body = re.sub(r"^#\s+.+$\n?", "", body, count=1, flags=re.M)
    goal = ""
    for s in sections:
        if s.title.lower() == "goal":
            goal = plain(s.body.split("\n\n")[0])
    tools = sorted(set(re.findall(r"tools/([a-z0-9][a-z0-9._-]*)", raw)))
    return Project(
        slug=slug,
        title=title or slug,
        status=status,
        started=started,
        finished=finished,
        goal=goal,
        html=r.block(body, rel),
        files=folder_files(root, rel),
        tools=tools,
        raw=raw,
    )


def load(root: Path) -> Inventory:
    r = Renderer(root)
    main = _read(root / "README.md")
    inv_rows, refreshed, total_text = parse_inventory_table(main)

    tools = []
    tools_dir = root / "tools"
    if tools_dir.is_dir():
        for d in sorted(tools_dir.iterdir()):
            if d.is_dir() and not d.name.startswith((".", "_")):
                tools.append(load_tool(root, d.name, r, inv_rows.get(d.name, {})))

    projects = []
    proj_dir = root / "projects"
    if proj_dir.is_dir():
        for d in sorted(proj_dir.iterdir(), reverse=True):
            if d.is_dir() and not d.name.startswith((".", "_")):
                projects.append(load_project(root, d.name, r))

    by_slug = {t.slug: t for t in tools}
    for p in projects:
        for s in p.tools:
            if s in by_slug:
                by_slug[s].projects.append((p.slug, p.title))

    refs = []
    ref_dir = root / "reference"
    if ref_dir.is_dir():
        for f in sorted(ref_dir.glob("*.md")):
            raw = _read(f)
            title, _, _ = split_sections(raw)
            body = re.sub(r"^#\s+.+$\n?", "", raw, count=1, flags=re.M)
            slug = "index" if f.name == "README.md" else f.stem
            refs.append(RefPage(slug, title or f.stem.title(), r.block(body, "reference"), raw))
        refs.sort(key=lambda x: (x.slug != "index", x.title))

    log = [e for t in tools for e in t.log]
    log.sort(key=lambda e: e.date, reverse=True)

    total_value = sum(t.value or 0 for t in tools)
    total_paid = sum(t.paid or 0 for t in tools)
    readme_body = re.sub(r"^#\s+.+$\n?", "", main, count=1, flags=re.M)
    return Inventory(
        tools=tools,
        projects=projects,
        references=refs,
        consumables=[c for t in tools for c in t.consumables],
        log=log,
        refreshed=refreshed,
        total_value_text=total_text,
        total_value=total_value,
        total_paid=total_paid,
        readme_html=r.block(readme_body, ""),
    )


# --------------------------------------------------------------------------- search

@dataclass
class Hit:
    kind: str
    title: str
    url: str
    snippet: str
    score: int


def search(inv: Inventory, q: str, limit: int = 50) -> list[Hit]:
    terms = [t for t in re.split(r"\s+", q.lower().strip()) if t]
    if not terms:
        return []
    docs = (
        [("Tool", t.title, t.url, plain(t.raw)) for t in inv.tools]
        + [("Project", p.title, p.url, plain(p.raw)) for p in inv.projects]
        + [("Reference", r.title, r.url, plain(r.raw)) for r in inv.references]
        + [
            ("Consumable", f"{c.item} — {c.tool_name}", f"/tools/{c.tool_slug}#consumables",
             f"{c.item} {c.spec} {c.where} {c.price}")
            for c in inv.consumables
        ]
    )
    hits = []
    for kind, title, url, text in docs:
        # Table pipes and separator dashes make unreadable snippets.
        text = re.sub(r"\s*\|[\s|:-]*", " · ", text)
        text = re.sub(r"(?:\s*·\s*){2,}", " · ", text).strip(" ·")
        low, tlow = text.lower(), title.lower()
        if not all(t in low or t in tlow for t in terms):
            continue
        score = sum(10 if t in tlow else 0 for t in terms) + sum(low.count(t) for t in terms)
        pos = low.find(terms[0])
        start = max(0, pos - 80)
        snip = text[start : start + 220]
        hits.append(Hit(kind, title, url, ("…" if start else "") + snip + "…", score))
    hits.sort(key=lambda h: -h.score)
    return hits[:limit]
