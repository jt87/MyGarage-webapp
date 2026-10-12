"""Write changes back into the data repo's Markdown, in the formats repo.py reads.

Every function here takes the current text of a file and returns new text, so
an edit is always applied to the latest copy (see RepoSync.write). Edits that
replace something the person saw on a form carry what they saw (`base`); if
the file changed underneath them in the same place, Conflict is raised instead
of quietly overwriting the other change.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from . import repo


class Conflict(Exception):
    """The same thing was changed somewhere else since the form was opened."""


class Invalid(Exception):
    """The submitted values can't be saved as they are."""


# --------------------------------------------------------------------------- cells & tables

def cell(text: str) -> str:
    """One Markdown table cell: a single line with pipes escaped."""
    text = re.sub(r"\s*[\r\n]+\s*", " ", str(text or "")).strip()
    return re.sub(r"(?<!\\)\|", r"\\|", text)


def row(cells: list[str]) -> str:
    return "|" + "|".join(f" {c} " if c else " " for c in map(cell, cells)) + "|"


def table(header: list[str], rows: list[list[str]]) -> str:
    sep = "|" + "|".join("-" * max(3, len(h) + 2) for h in header) + "|"
    return "\n".join([row(header), sep] + [row(r) for r in rows])


def _table_span(lines: list[str], start: int = 0, stop: int | None = None) -> tuple[int, int] | None:
    """(first line, line after last) of the first pipe table in lines[start:stop]."""
    stop = len(lines) if stop is None else stop
    for i in range(start, stop - 1):
        if lines[i].lstrip().startswith("|") and repo.is_sep(lines[i + 1]):
            j = i + 2
            while j < stop and lines[j].lstrip().startswith("|"):
                j += 1
            return i, j
    return None


def _section_span(lines: list[str], prefix: str) -> tuple[int, int] | None:
    """(heading line, line after the section) of the first `## <prefix>...` section."""
    for i, line in enumerate(lines):
        m = re.match(r"##\s+(.+)$", line)
        if m and m.group(1).strip().lower().startswith(prefix):
            j = i + 1
            while j < len(lines) and not re.match(r"#{1,2}\s", lines[j]):
                j += 1
            return i, j
    return None


def _preamble_stop(lines: list[str]) -> int:
    """Line index of the first `## ` heading (end of the title block)."""
    return next((i for i, l in enumerate(lines) if re.match(r"##\s", l)), len(lines))


def _join(lines: list[str]) -> str:
    return "\n".join(lines).rstrip("\n") + "\n"


def _blank_row(line: str) -> bool:
    return not any(c.strip() for c in repo.split_row(line))


# --------------------------------------------------------------------------- tool README

def split_title(text: str) -> tuple[str, str]:
    """("Brand Model", "Name") from `# Brand Model — Name`."""
    m = re.search(r"^#\s+(.+)$", text, re.M)
    title = m.group(1).strip() if m else ""
    if "—" in title:
        a, b = title.split("—", 1)
        return a.strip(), b.strip()
    return title, ""


def set_title(text: str, model_name: str, subtitle: str) -> str:
    title = f"{model_name.strip()} — {subtitle.strip()}" if subtitle.strip() else model_name.strip()
    if re.search(r"^#\s+.+$", text, re.M):
        return re.sub(r"^#\s+.+$", lambda _: f"# {title}", text, count=1, flags=re.M)
    return f"# {title}\n\n{text.lstrip()}"


def get_specs(text: str) -> list[tuple[str, str]]:
    """The key/value table under the title, as raw Markdown cells."""
    lines = text.splitlines()
    span = _table_span(lines, 0, _preamble_stop(lines))
    if not span:
        return []
    out = []
    for line in lines[span[0] + 2: span[1]]:
        cells = repo.split_row(line)
        if len(cells) >= 2 and (cells[0] or cells[1]):
            out.append((cells[0], cells[1]))
    return out


def set_specs(text: str, specs: list[tuple[str, str]]) -> str:
    lines = text.splitlines()
    specs = [(k, v) for k, v in specs if k.strip()]
    stop = _preamble_stop(lines)
    span = _table_span(lines, 0, stop)
    new = table(["", ""], [[k, v] for k, v in specs]).splitlines() if specs else []
    if span and new:
        lines[span[0] + 2: span[1]] = new[2:]  # keep the header as written
    elif span:
        lines[span[0]: span[1]] = []
    elif new:
        at = next((i + 1 for i, l in enumerate(lines[:stop]) if re.match(r"#\s", l)), 0)
        lines[at:at] = ([""] if at else []) + new + [""]
    return _join(lines)


def get_table(text: str, prefix: str) -> list[list[str]]:
    """Rows of the first table in the `## <prefix>` section."""
    lines = text.splitlines()
    sec = _section_span(lines, prefix)
    span = sec and _table_span(lines, sec[0] + 1, sec[1])
    if not span:
        return []
    rows = [repo.split_row(l) for l in lines[span[0] + 2: span[1]]]
    return [r for r in rows if any(c for c in r)]


def set_table(text: str, prefix: str, title: str, header: list[str],
              rows: list[list[str]]) -> str:
    """Replace the first table in `## <prefix>` (creating the section if needed)."""
    lines = text.splitlines()
    sec = _section_span(lines, prefix)
    rows = [(r + [""] * len(header))[: len(header)] for r in rows]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if sec:
        span = _table_span(lines, sec[0] + 1, sec[1])
        if span:
            if len(repo.split_row(lines[span[0]])) == len(header):
                lines[span[0] + 2: span[1]] = [row(r) for r in rows]
            else:
                lines[span[0]: span[1]] = table(header, rows).splitlines()
        else:
            at = sec[1]
            while at > sec[0] + 1 and not lines[at - 1].strip():
                at -= 1
            lines[at:at] = [""] + table(header, rows).splitlines()
        return _join(lines)
    return _append_section(lines, title, table(header, rows))


def _append_section(lines: list[str], title: str, body: str,
                    before: str | None = None) -> str:
    block = [f"## {title}", ""] + body.splitlines() + [""]
    at = len(lines)
    if before and (sec := _section_span(lines, before)):
        at = sec[0]
    while at > 0 and not lines[at - 1].strip():
        at -= 1
    lines[at:at] = [""] + block
    return _join(lines)


def add_log_entry(text: str, date: str, what: str) -> str:
    """Add a row to `## Maintenance log`, kept in date order (oldest first)."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        raise Invalid("Enter the date as YYYY-MM-DD.")
    if not what.strip():
        raise Invalid("Describe what was done.")
    new = row([date, what])
    lines = text.splitlines()
    sec = _section_span(lines, "maintenance")
    if not sec:
        return _append_section(lines, "Maintenance log", table(["Date", "What"], [[date, what]]))
    span = _table_span(lines, sec[0] + 1, sec[1])
    if not span:
        at = sec[1]
        while at > sec[0] + 1 and not lines[at - 1].strip():
            at -= 1
        lines[at:at] = [""] + table(["Date", "What"], [[date, what]]).splitlines()
        return _join(lines)
    body = list(range(span[0] + 2, span[1]))
    # A template's empty placeholder row is filled in rather than left above.
    blank = next((i for i in body if _blank_row(lines[i])), None)
    if blank is not None:
        lines[blank] = new
        return _join(lines)
    at = span[1]
    for i in reversed(body):
        if (repo.split_row(lines[i]) or [""])[0].strip() <= date:
            break
        at = i
    lines.insert(at, new)
    return _join(lines)


# --------------------------------------------------------------------------- inventory (root README)

INV_HEADER = ["Tool", "Category", "Paid", "Replacement value", "Folder"]


def _inventory_lines(text: str) -> tuple[list[str], tuple[int, int] | None]:
    lines = text.splitlines()
    sec = _section_span(lines, "inventory")
    span = _table_span(lines, sec[0] + 1, sec[1]) if sec else None
    return lines, span


def _cols(header: list[str]) -> dict[str, int | None]:
    low = [h.lower() for h in header]

    def col(name):
        return next((i for i, c in enumerate(low) if name in c), None)

    return {"tool": col("tool"), "category": col("category"), "paid": col("paid"),
            "value": col("replacement"), "folder": col("folder")}


def get_inventory_row(text: str, slug: str) -> dict[str, str]:
    lines, span = _inventory_lines(text)
    if not span:
        return {}
    cols = _cols(repo.split_row(lines[span[0]]))
    for line in lines[span[0] + 2: span[1]]:
        cells = repo.split_row(line)
        folder = cells[cols["folder"]] if cols["folder"] is not None and cols["folder"] < len(cells) else ""
        if re.search(rf"tools/{re.escape(slug)}(?:[/)\]]|$)", folder):
            return {k: (cells[i] if i is not None and i < len(cells) else "")
                    for k, i in cols.items()}
    return {}


def set_inventory_row(text: str, slug: str, values: dict[str, str]) -> str:
    """Update (or add) the tool's row in `## Inventory` and refresh the total.

    `values` may hold tool, category, paid and value; other cells are kept.
    """
    lines, span = _inventory_lines(text)
    if not span:
        body = table(INV_HEADER, [[values.get("tool", slug), values.get("category", ""),
                                   values.get("paid", ""), values.get("value", ""),
                                   f"[tools/{slug}](tools/{slug}/)"]])
        return _append_section(lines, "Inventory", body)
    header = repo.split_row(lines[span[0]])
    cols = _cols(header)
    found = total = None
    for i in range(span[0] + 2, span[1]):
        cells = repo.split_row(lines[i])
        get = lambda k: cells[cols[k]] if cols[k] is not None and cols[k] < len(cells) else ""
        if "total" in repo.plain(get("tool")).lower():
            total = i
        elif re.search(rf"tools/{re.escape(slug)}(?:[/)\]]|$)", get("folder")):
            found = i
    if found is None:
        cells = [""] * len(header)
        if cols["folder"] is not None:
            cells[cols["folder"]] = f"[tools/{slug}](tools/{slug}/)"
        found = total if total is not None else span[1]
        lines.insert(found, row(cells))
        if total is not None:
            total += 1
        span = (span[0], span[1] + 1)
    cells = (repo.split_row(lines[found]) + [""] * len(header))[: len(header)]
    for k, v in values.items():
        if cols.get(k) is not None:
            cells[cols[k]] = v
    lines[found] = row(cells)
    if total is not None and cols["value"] is not None:
        lines[total] = _refresh_total(lines, span, total, cols["value"])
    return _join(lines)


def _refresh_total(lines: list[str], span: tuple[int, int], total: int, vcol: int) -> str:
    """Rewrite the first dollar amount in the Total row as the sum of the column."""
    s = 0.0
    for i in range(span[0] + 2, span[1]):
        if i == total:
            continue
        cells = repo.split_row(lines[i])
        if vcol < len(cells):
            s += repo.parse_money(cells[vcol]) or 0
    cells = repo.split_row(lines[total])
    if vcol >= len(cells) or not repo.MONEY_RE.search(cells[vcol]):
        return lines[total]
    cells[vcol] = repo.MONEY_RE.sub(lambda _: repo.fmt_money(s), cells[vcol], count=1)
    return row(cells)


# --------------------------------------------------------------------------- three-way merge

def merge_value(name: str, base, mine, current):
    """What to save for one field: mine if it changed, unless it changed elsewhere too."""
    if mine == base or mine == current:
        return current
    if current == base:
        return mine
    raise Conflict(name)


def merge_specs(base: list[tuple[str, str]], mine: list[tuple[str, str]],
                current: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge key/value rows by key (case-insensitive), keeping the file's order."""
    norm = lambda rows: {k.strip().lower(): (k, v) for k, v in rows}
    b, m, c = norm(base), norm(mine), norm(current)
    out: dict[str, tuple[str, str] | None] = dict(c)
    for key in list(dict.fromkeys([*c, *m, *b])):
        result = merge_value(f"“{(m.get(key) or c.get(key) or b.get(key))[0]}”",
                             b.get(key), m.get(key), c.get(key))
        out[key] = result
    # New rows from the form go where the form put them (after the row before them).
    order = list(c)
    mine_keys = list(m)
    for idx, key in enumerate(mine_keys):
        if key in order:
            continue
        prev = next((k for k in reversed(mine_keys[:idx]) if k in order), None)
        order.insert(order.index(prev) + 1 if prev else len(order), key)
    return [out[k] for k in order if out.get(k)]


# --------------------------------------------------------------------------- new tools & files

def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")


def new_tool_readme(model_name: str, subtitle: str, specs: list[tuple[str, str]]) -> str:
    text = set_title("", model_name, subtitle)
    text = set_specs(text, specs)
    text = _append_section(text.splitlines(), "Consumables & parts",
                           table(["Item", "Spec / part #", "Where to buy", "Typical price"], []))
    return _append_section(text.splitlines(), "Maintenance log", table(["Date", "What"], []))


SAFE_NAME = re.compile(r"[^a-z0-9._-]+")
BLOCKED_EXT = {".md", ".html", ".htm", ".js", ".svg"}


def upload_name(original: str, kind: str, existing: set[str], now: datetime) -> str:
    """A file name that makes the app file the upload where it was meant to go."""
    stem, dot, ext = original.rpartition(".")
    if not dot:
        stem, ext = original, ""
    ext = "." + SAFE_NAME.sub("", ext.lower()) if ext else ""
    if ext == ".jpeg":
        ext = ".jpg"
    if ext in BLOCKED_EXT or not ext:
        raise Invalid(f"{original or 'That file'} can't be added. Use a photo, PDF or STL.")
    stem = SAFE_NAME.sub("-", stem.lower()).strip("-._") or "file"
    stamp = now.strftime("%Y%m%d-%H%M%S")
    if kind == "photo":
        if ext not in repo.IMAGE_EXT:
            raise Invalid(f"{original} isn't a photo.")
        has_photo = any(n.startswith("photo.") for n in existing)
        base = f"photo-{stamp}" if has_photo else "photo"
        if repo.PROOF_RE.search(base):  # never, but keep photos out of receipts
            base = "photo"
    elif kind == "receipt":
        base = "receipt" if not any(n.startswith("receipt.") for n in existing) else f"receipt-{stamp}"
    elif kind == "manual":
        if ext != ".pdf":
            raise Invalid("Manuals must be PDFs.")
        base = stem if repo.MANUAL_RE.search(stem) else f"manual-{stem}"
    else:
        # A document that happens to be called "order-form" would be filed as
        # a receipt; make the name say what it is.
        base = stem if not repo.PROOF_RE.search(stem) else f"doc-{stem}"
    base = base[:80]
    name, n = f"{base}{ext}", 2
    while name in existing:
        name, n = f"{base}-{n}{ext}", n + 1
    return name


# --------------------------------------------------------------------------- 1.1.5: log entries

def log_rows(text: str) -> list[list[str]]:
    """Maintenance rows in file order, as the tool page lists them."""
    return [r for r in get_table(text, "maintenance") if len(r) >= 2 and r[0].strip()]


def change_log_entry(text: str, old: tuple[str, str], new: tuple[str, str] | None) -> str:
    """Replace (or with new=None, remove) the log row that reads `old`."""
    lines = text.splitlines()
    sec = _section_span(lines, "maintenance")
    span = sec and _table_span(lines, sec[0] + 1, sec[1])
    if span:
        for i in range(span[0] + 2, span[1]):
            cells = repo.split_row(lines[i])
            if len(cells) >= 2 and (cells[0].strip(), cells[1].strip()) == (old[0].strip(), old[1].strip()):
                del lines[i]
                text = _join(lines)
                return add_log_entry(text, *new) if new else text
    raise Conflict("That log entry")


# --------------------------------------------------------------------------- 1.1.5: inventory & projects

def remove_inventory_row(text: str, slug: str) -> str:
    lines, span = _inventory_lines(text)
    if not span:
        return text
    cols = _cols(repo.split_row(lines[span[0]]))
    total = None
    for i in range(span[1] - 1, span[0] + 1, -1):
        cells = repo.split_row(lines[i])
        get = lambda k: cells[cols[k]] if cols[k] is not None and cols[k] < len(cells) else ""
        if re.search(rf"tools/{re.escape(slug)}(?:[/)\]]|$)", get("folder")):
            del lines[i]
            span = (span[0], span[1] - 1)
    for i in range(span[0] + 2, span[1]):
        cells = repo.split_row(lines[i])
        if cols["tool"] is not None and "total" in repo.plain(cells[cols["tool"]] if cols["tool"] < len(cells) else "").lower():
            total = i
    if total is not None and cols["value"] is not None:
        lines[total] = _refresh_total(lines, span, total, cols["value"])
    return _join(lines)


PROJECT_FIELDS = ("Status", "Started", "Finished")


def get_project_field(text: str, name: str) -> str:
    m = re.search(rf"^{name}:[ \t]*(.*)$", text, re.M | re.I)
    return m.group(1).strip() if m else ""


def set_project_field(text: str, name: str, value: str) -> str:
    value = cell(value).replace("\\|", "|")
    if re.search(rf"^{name}:.*$", text, re.M | re.I):
        return re.sub(rf"^{name}:.*$", lambda _: f"{name}: {value}".rstrip(), text, count=1,
                      flags=re.M | re.I)
    lines = text.splitlines()
    # After the title and any other header fields.
    at = next((i + 1 for i, l in enumerate(lines) if re.match(r"#\s", l)), 0)
    while at < len(lines) and (not lines[at].strip() or re.match(r"\w+:", lines[at])):
        if not lines[at].strip() and at + 1 < len(lines) and not re.match(r"\w+:", lines[at + 1]):
            break
        at += 1
    lines.insert(at, f"{name}: {value}".rstrip())
    return _join(lines)


def project_tools(text: str) -> list[str]:
    return sorted(set(re.findall(r"tools/([a-z0-9][a-z0-9._-]*)", text)))


def set_project_tools(text: str, tools: list[tuple[str, str]]) -> str:
    """Rewrite `## Tools used` as a list of links: tools = [(slug, name)]."""
    body = "\n".join(f"- [{name}](../../tools/{slug}/)" for slug, name in tools)
    lines = text.splitlines()
    sec = _section_span(lines, "tools used")
    if sec:
        lines[sec[0] + 1: sec[1]] = [""] + (body.splitlines() if body else []) + [""]
        return _join(lines)
    return _append_section(lines, "Tools used", body) if body else text


def new_project_readme(title: str, status: str, started: str, goal: str,
                       tools: list[tuple[str, str]]) -> str:
    text = f"# {cell(title)}\n\nStatus: {cell(status) or 'planning'}\nStarted: {cell(started)}\nFinished:\n"
    text = _append_section(text.splitlines(), "Goal", goal.strip() or "What this project is for.")
    return set_project_tools(text, tools) if tools else text


def rewrite_links(text: str, old: str, new: str | None) -> str:
    """Point README links at a renamed file (links to a deleted one are left as they are)."""
    if new is None:
        return text
    return re.sub(rf"\]\((\./)?{re.escape(old)}([)#\s])", lambda m: f"]({m.group(1) or ''}{new}{m.group(2)}", text)


# --------------------------------------------------------------------------- 1.1.5: photos

MAX_PHOTO_EDGE = 2560


def shrink_photo(src, name: str) -> tuple[bytes, str] | None:
    """A photo resized to MAX_PHOTO_EDGE as JPEG, or None to keep the original.

    iPhone photos are 3-5 MB; at this size they're ~1 MB and still sharp on
    an iPad. Small JPEGs are kept as they are.
    """
    from io import BytesIO

    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    try:
        with Image.open(src) as im:
            if max(im.size) <= MAX_PHOTO_EDGE and (im.format or "").upper() == "JPEG":
                return None
            im = ImageOps.exif_transpose(im)
            im.thumbnail((MAX_PHOTO_EDGE, MAX_PHOTO_EDGE))
            buf = BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
    except Exception:
        return None
    stem = name.rsplit(".", 1)[0]
    return buf.getvalue(), f"{stem}.jpg"
