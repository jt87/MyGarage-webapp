"""Smoke tests against the real repo content (REPO_PATH=..)."""

import os
from pathlib import Path

os.environ.setdefault("REPO_PATH", str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("CACHE_PATH", "/tmp/garage-test-cache")

from starlette.testclient import TestClient  # noqa: E402

from app import repo  # noqa: E402
from app.main import app  # noqa: E402

ROOT = Path(os.environ["REPO_PATH"])
client = TestClient(app)


def test_every_tool_folder_is_listed_and_renders():
    inv = repo.load(ROOT)
    folders = {d.name for d in (ROOT / "tools").iterdir() if d.is_dir() and not d.name.startswith(".")}
    assert {t.slug for t in inv.tools} == folders
    for t in inv.tools:
        assert client.get(t.url).status_code == 200, t.slug
        assert t.value is not None, f"{t.slug}: no replacement value in README.md inventory"


def test_pages():
    for url in ["/", "/tools", "/consumables", "/projects", "/insurance", "/insurance.csv",
                "/reference", "/search?q=belt", "/healthz"]:
        assert client.get(url).status_code == 200, url
    for p in repo.load(ROOT).projects:
        assert client.get(p.url).status_code == 200, p.slug


def test_files_cannot_escape_repo():
    for url in ["/files/../CLAUDE.md", "/files/tools/../CLAUDE.md", "/files/CLAUDE.md",
                "/files/.git/config", "/thumb/tools/../README.md"]:
        assert client.get(url).status_code == 404, url


def test_relative_links_rewrite():
    assert repo.rewrite_href("../klein-ncvt-3p/", "tools/klein-mm320", ROOT) == "/tools/klein-ncvt-3p"
    assert repo.rewrite_href("../../tools/ryobi-rts10g/", "projects/x", ROOT) == "/tools/ryobi-rts10g"
    assert repo.rewrite_href("manual.pdf", "tools/delta-22-540", ROOT) == "/files/tools/delta-22-540/manual.pdf"
    assert repo.rewrite_href("https://example.com", "tools/x", ROOT) == "https://example.com"


def test_part_numbers_and_buy_links():
    parts = repo.part_numbers('Diablo **D1040X** or **91-1/2"** long, **≥12,000 RPM**', "Ryobi")
    assert parts == [("D1040X", "Diablo D1040X")]
    labels = [l.label for l in repo.buy_links("Home Depot, Lowe's", "x")]
    assert labels == ["Home Depot", "Lowe's", "Search web"]


def test_proof_of_purchase_detection(tmp_path):
    d = tmp_path / "tools" / "acme-1"
    d.mkdir(parents=True)
    (d / "README.md").write_text("# Acme 1 — Thing\n\n| | |\n|---|---|\n| Category | Test |\n")
    for n in ("manual.pdf", "receipt-2026-01-01.jpg", "photo.jpg", "parts-list.pdf"):
        (d / n).write_bytes(b"x")
    t = repo.load(tmp_path).tools[0]
    assert [f.name for f in t.proofs] == ["receipt-2026-01-01.jpg"]
    assert [f.name for f in t.manuals] == ["manual.pdf"]
    assert [f.name for f in t.documents] == ["parts-list.pdf"]
    assert [f.name for f in t.photos] == ["photo.jpg"]
