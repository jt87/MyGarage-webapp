"""A small, made-up garage repo for the tests, built in a temp folder.

The app ships with no garage data of its own. These tools and projects are
fictional and only exercise the README formats the app parses (see the
"Data repo format" section of README.md).
"""

from pathlib import Path

INVENTORY = """# MyGarage

## Inventory

Values last refreshed: January 2027

| Tool | Category | Paid | Replacement value | Folder |
|------|----------|------|-------------------|--------|
| Acme TS10 10" table saw | Table saw | $150 | ~$300 | [tools/acme-ts10](tools/acme-ts10/) |
| Acme DD20 drill/driver | Drill | $80 | ~$120 | [tools/acme-dd20](tools/acme-dd20/) |
| **Total** | | | **~$420** (as of Jan 2027) | |
"""

TABLE_SAW = """# Acme TS10 — 10" Table Saw

| | |
|---|---|
| Category | Table saw |
| Model # | TS10 |
| Serial # | 0001 |
| Purchased | 2027-01-02, Example Hardware, $150 |
| Replacement value | ~$300 MSRP (Jan 2027) |

## Consumables & parts

| Item | Spec / part # | Where to buy | Typical price |
|------|---------------|--------------|---------------|
| Blade | Acme **B1040** 10" 40T | Home Depot, Lowe's | ~$40 |
| Drive belt | Acme **BELT-7** | Amazon | ~$12 |

## Settings & notes

Sibling tool: [drill](../acme-dd20/).

## Maintenance log

| Date | What |
|------|------|
| 2027-01-03 | Waxed the top. |
"""

DRILL = """# Acme DD20 — Drill/Driver

| | |
|---|---|
| Category | Drill |
| Purchased | 2027-01-02, $80 |
| Replacement value | ~$120 MSRP (Jan 2027) |

## Maintenance log

| Date | What |
|------|------|
| 2027-01-04 | Charged both batteries. |
"""

PROJECT = """# Bookshelf

Status: planning
Started: 2027-01
Finished:

## Goal

A simple bookshelf.

## Tools used

[Table saw](../../tools/acme-ts10/)
"""


def _jpeg() -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (8, 8), "gray").save(buf, "JPEG")
    return buf.getvalue()


def build(root: Path) -> Path:
    files = {
        "README.md": INVENTORY,
        "CLAUDE.md": "Not served.",
        "tools/acme-ts10/README.md": TABLE_SAW,
        "tools/acme-ts10/manual.pdf": b"%PDF-1.4\n%%EOF\n",
        "tools/acme-ts10/photo.jpg": _jpeg(),
        "tools/acme-ts10/receipt.pdf": b"%PDF-1.4\n%%EOF\n",
        "tools/acme-dd20/README.md": DRILL,
        "projects/2027-01-bookshelf/README.md": PROJECT,
        "projects/2027-01-bookshelf/plans.pdf": b"%PDF-1.4\n%%EOF\n",
        "projects/2027-01-bookshelf/design.f3d": b"x",
        "reference/README.md": "# Reference\n\nShop notes.\n",
    }
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    return root
