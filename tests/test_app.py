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
    assert repo.rewrite_href("../acme-tester/", "tools/acme-meter", ROOT) == "/tools/acme-tester"
    assert repo.rewrite_href("../../tools/acme-dd20/", "projects/x", ROOT) == "/tools/acme-dd20"
    assert repo.rewrite_href("manual.pdf", "tools/acme-ts10", ROOT) == "/files/tools/acme-ts10/manual.pdf"
    assert repo.rewrite_href("https://example.com", "tools/x", ROOT) == "https://example.com"


def test_part_numbers_and_buy_links():
    parts = repo.part_numbers('Bladeco **BC1040X** or **91-1/2"** long, **≥12,000 RPM**', "Acme")
    assert parts == [("BC1040X", "Bladeco BC1040X")]
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


def test_page_view_pulls_new_commit(tmp_path):
    """A push to the remote shows up on the next freshness check."""
    import subprocess

    from app.sync import RepoSync

    def git(*a, cwd):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd,
                       check=True, capture_output=True)

    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    (work / "README.md").write_text("one")
    git("add", ".", cwd=work)
    git("commit", "-qm", "1", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)

    s = RepoSync(tmp_path / "clone", url=str(origin), branch="main")
    assert s.sync_once()
    assert s.ensure_fresh(0) is False  # nothing new
    (work / "README.md").write_text("two")
    git("commit", "-qam", "2", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    assert s.ensure_fresh(60) is False  # throttled: checked moments ago
    assert s.ensure_fresh(0) is True
    assert (tmp_path / "clone" / "README.md").read_text() == "two"


def test_webhook_requires_signature(monkeypatch):
    import hashlib
    import hmac as _hmac

    from app import main

    monkeypatch.setattr(main, "WEBHOOK_SECRET", "s3cret")
    body = b'{"ref":"refs/heads/main"}'
    good = "sha256=" + _hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    hdr = {"X-GitHub-Event": "ping", "Content-Type": "application/json"}
    assert client.post("/webhook/github", content=body, headers={**hdr, "X-Hub-Signature-256": "sha256=0"}).status_code == 401
    assert client.post("/webhook/github", content=body, headers={**hdr, "X-Hub-Signature-256": good}).status_code == 200


def test_mobile_basics():
    html = client.get("/tools/acme-ts10").text
    assert "viewport-fit=cover" in html
    assert 'class="tabbar"' in html
    assert client.get("/more").status_code == 200
    assert client.get("/view/tools/acme-ts10/manual.pdf").status_code == 200
