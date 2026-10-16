"""Smoke tests against a small made-up garage repo (see fixture.py)."""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fixture  # noqa: E402

_tmp = Path(tempfile.mkdtemp())
os.environ["REPO_PATH"] = str(fixture.build(_tmp / "repo"))
os.environ["CACHE_PATH"] = str(_tmp / "cache")
# The tests never ask GitHub for releases (test_versions.py fakes it).
os.environ["UPDATE_CHECK"] = "false"

os.environ["CONFIG_PATH"] = os.path.join(tempfile.mkdtemp(), "auth.json")

from starlette.testclient import TestClient  # noqa: E402

from app import auth, main, repo  # noqa: E402
from app.main import app  # noqa: E402

ROOT = Path(os.environ["REPO_PATH"])
USER, PASSWORD = "admin", "correct horse battery"
main.auth_cfg.set_credentials(USER, PASSWORD)
client = TestClient(app)  # an internet client: its address isn't local
client.post("/login", data={"username": USER, "password": PASSWORD, "remember": "on"})


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
    assert repo.rewrite_href("../acme-dd20/", "tools/acme-ts10", ROOT) == "/tools/acme-dd20"
    assert repo.rewrite_href("../../tools/acme-ts10/", "projects/x", ROOT) == "/tools/acme-ts10"
    # Viewable files go to the in-app viewer; others download.
    assert repo.rewrite_href("manual.pdf", "tools/acme-ts10", ROOT) == "/view/tools/acme-ts10/manual.pdf"
    assert repo.rewrite_href("design.f3d", "projects/2027-01-bookshelf", ROOT) == \
        "/files/projects/2027-01-bookshelf/design.f3d"
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

    # Pointing the app at a different repo replaces the old content.
    other = tmp_path / "other.git"
    git("init", "-q", "--bare", "-b", "main", str(other), cwd=tmp_path)
    git("push", "-q", str(other), "HEAD:main", cwd=work)
    (work / "README.md").write_text("mine")
    git("commit", "-qam", "3", cwd=work)
    git("push", "-q", str(other), "HEAD:main", cwd=work)
    s2 = RepoSync(tmp_path / "clone", url=str(other), branch="main")
    assert s2.ensure_fresh(0) is True
    assert (tmp_path / "clone" / "README.md").read_text() == "mine"


def test_webhook_requires_signature(monkeypatch):
    import hashlib
    import hmac as _hmac

    from app import main

    monkeypatch.setattr(main.store, "env_webhook_secret", "s3cret")
    monkeypatch.setattr(main.sync, "mode", "server")  # a GitHub repo is connected
    monkeypatch.setattr(main.sync, "url", "https://example.invalid/r.git")
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


def test_github_checks_are_capped_and_persisted(tmp_path, monkeypatch):
    """Page views can't make the server hammer GitHub, even across restarts."""
    from app import sync as sync_mod

    calls = []
    s = sync_mod.RepoSync(tmp_path / "clone", url="https://example.invalid/r.git",
                          state_file=tmp_path / "state.json")
    monkeypatch.setattr(s, "_git", lambda *a, **k: calls.append(a) or "")
    for _ in range(sync_mod.MAX_CHECKS_PER_HOUR + 20):
        s.ensure_fresh(0)
    assert len(calls) == sync_mod.MAX_CHECKS_PER_HOUR
    # Throttled by age: a fresh instance reads the saved check time.
    s2 = sync_mod.RepoSync(tmp_path / "clone", url="https://example.invalid/r.git",
                           state_file=tmp_path / "state.json")
    monkeypatch.setattr(s2, "_git", lambda *a, **k: calls.append(a) or "")
    s2.ensure_fresh(300)
    assert len(calls) == sync_mod.MAX_CHECKS_PER_HOUR
    assert s2.checks_today == sync_mod.MAX_CHECKS_PER_HOUR


def test_webhook_ignores_other_branches(monkeypatch):
    import hashlib
    import hmac as _hmac

    from app import main

    monkeypatch.setattr(main.store, "env_webhook_secret", "s3cret")
    monkeypatch.setattr(main.sync, "mode", "server")  # a GitHub repo is connected
    monkeypatch.setattr(main.sync, "url", "https://example.invalid/r.git")
    fetched = []
    monkeypatch.setattr(main.sync, "ensure_fresh", lambda age: fetched.append(age) or False)

    def push(ref):
        body = f'{{"ref":"{ref}"}}'.encode()
        sig = "sha256=" + _hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        return client.post("/webhook/github", content=body,
                           headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": sig})

    assert push("refs/heads/some-feature").status_code == 200 and fetched == []
    # The served branch goes through the capped check, not a bare fetch.
    assert push(f"refs/heads/{main.sync.branch}").status_code == 200 and len(fetched) == 1


def test_placeholders_are_ignored(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "CHANGE_ME")
    monkeypatch.setenv("GIT_TOKEN", "real-token")
    assert main.setting("WEBHOOK_SECRET") == ""
    assert main.setting("GIT_TOKEN") == "real-token"


def test_svg_downloads_instead_of_rendering():
    svg = Path(main.REPO_PATH) / "tools" / "acme-ts10" / "diagram.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    try:
        r = client.get("/files/tools/acme-ts10/diagram.svg")
        assert r.status_code == 200
        assert r.headers["content-disposition"].startswith("attachment")
    finally:
        svg.unlink()



# --------------------------------------------------------------------------- security

def fresh(**kw):
    """A client with no cookies. client=(ip, port) sets the TCP peer address."""
    return TestClient(app, follow_redirects=False, **kw)


def test_password_is_hashed_not_stored():
    raw = Path(os.environ["CONFIG_PATH"]).read_text()
    assert PASSWORD not in raw and "pbkdf2_sha256$600000$" in raw
    assert oct(Path(os.environ["CONFIG_PATH"]).stat().st_mode & 0o777) == "0o600"


def test_pages_require_login():
    r = fresh().get("/tools", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=/tools")
    assert fresh().get("/files/tools/acme-ts10/manual.pdf").status_code == 401
    assert fresh().get("/static/style.css").status_code == 200
    assert fresh().get("/healthz").json() == {"ok": True}


def test_login_and_remember_me():
    c = fresh()
    bad = c.post("/login", data={"username": USER, "password": "nope"})
    assert bad.status_code == 401 and "Incorrect username or password" in bad.text
    ok = c.post("/login?next=/tools", data={"username": USER.upper(), "password": PASSWORD})
    assert ok.status_code == 303 and ok.headers["location"] == "/tools"
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age" not in cookie
    assert c.get("/tools").status_code == 200
    remembered = fresh().post("/login", data={"username": USER, "password": PASSWORD, "remember": "on"})
    assert "max-age=2592000" in remembered.headers["set-cookie"].lower()


def test_login_rate_limit():
    c = fresh(client=("203.0.113.9", 1234))
    for _ in range(10):
        c.post("/login", data={"username": USER, "password": "wrong"})
    r = c.post("/login", data={"username": USER, "password": PASSWORD})
    assert "Too many attempts" in r.text
    main.throttle.fails.clear(); main.throttle.all.clear()


def test_local_bypass_cannot_be_forged():
    main.auth_cfg.data["required"] = "local"
    try:
        assert fresh(client=("192.168.1.20", 5000)).get("/tools").status_code == 200
        # Through a reverse proxy: must log in, whatever the header claims.
        proxied = fresh(client=("192.168.1.2", 5000))
        r = proxied.get("/tools", headers={"X-Forwarded-For": "192.168.1.20", "accept": "text/html"})
        assert r.status_code == 303
        assert fresh(client=("8.8.8.8", 5000)).get("/tools").status_code == 401
    finally:
        main.auth_cfg.data["required"] = "enabled"


def test_api_key():
    key = main.auth_cfg.api_key
    c = fresh()
    assert c.get("/insurance.csv", headers={"X-Api-Key": key}).status_code == 200
    assert c.get(f"/insurance.csv?apikey={key}").status_code == 200
    assert c.get("/insurance.csv", headers={"X-Api-Key": "0" * 32}).status_code == 401
    assert c.post("/settings/apikey", headers={"X-Api-Key": key}).status_code == 403


def test_basic_auth():
    import base64
    main.auth_cfg.data["method"] = "basic"
    try:
        r = fresh().get("/tools")
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
        good = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        assert fresh().get("/tools", headers={"Authorization": f"Basic {good}"}).status_code == 200
    finally:
        main.auth_cfg.data["method"] = "forms"


def test_log_out_everywhere_and_cross_site_posts():
    c = fresh()
    c.post("/login", data={"username": USER, "password": PASSWORD})
    old = c.cookies.get(auth.COOKIE)
    assert c.post("/settings/sessions", headers={"Origin": "https://evil.example"}).status_code == 403
    r = c.post("/settings/sessions", headers={"Origin": "http://testserver"})
    assert r.status_code == 303
    stale = fresh(); stale.cookies.set(auth.COOKIE, old)
    assert stale.get("/tools", headers={"accept": "text/html"}).status_code == 303
    assert c.get("/tools").status_code == 200   # this device got a new session
    client.post("/login", data={"username": USER, "password": PASSWORD, "remember": "on"})


def test_settings_require_current_password():
    r = client.post("/settings/security", data={"method": "external", "required": "enabled",
                                                 "confirm_external": "on", "current": "wrong"})
    assert "incorrect" in r.headers.get("location", "") or "incorrect" in r.text
    assert main.auth_cfg.method == "forms"


def test_first_run_setup_only_from_home_network(tmp_path, monkeypatch):
    cfg = auth.AuthConfig(tmp_path / "auth.json")
    monkeypatch.setattr(main, "auth_cfg", cfg)
    monkeypatch.setattr(main.sessions, "cfg", cfg)
    form = {"username": "me", "password": "longenough", "confirm": "longenough",
            "method": "forms", "required": "enabled"}
    assert fresh().get("/tools").headers["location"] == "/setup"
    remote = fresh(client=("8.8.8.8", 1)).post("/setup", data=form)
    assert "from your home network" in remote.text and not cfg.configured
    proxied = fresh(client=("172.17.0.1", 1)).post("/setup", data=form, headers={"X-Forwarded-For": "10.0.0.5"})
    assert not cfg.configured and proxied.status_code == 200
    local = fresh(client=("192.168.1.10", 1)).post("/setup", data=form)
    assert local.status_code == 303 and cfg.configured and cfg.check("me", "longenough")


# --------------------------------------------------------------------------- regressions (bug sweep)

def test_check_for_updates_only_redirects_within_the_site():
    r = fresh_logged_in().post("/sync", headers={"Referer": "https://evil.example/x"})
    assert r.headers["location"] == "/"
    r = fresh_logged_in().post("/sync", headers={"Referer": "http://testserver/tools?x=1"})
    assert r.headers["location"] == "/tools"
    assert main.safe_next("/\\evil.example") == "/"


def test_posts_work_behind_a_proxy_that_rewrites_host():
    c = fresh_logged_in()
    hdrs = {"Host": "192.168.1.5:8088", "Origin": "https://garage.example.com"}
    assert c.post("/sync", headers={**hdrs, "X-Forwarded-Host": "garage.example.com"}).status_code == 303
    assert c.post("/sync", headers={**hdrs, "Sec-Fetch-Site": "same-origin"}).status_code == 303
    assert c.post("/sync", headers={**hdrs, "Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_browser_session_login_expires_after_12_hours(monkeypatch):
    import itsdangerous
    token, _ = main.sessions.issue(remember=False)
    remembered, _ = main.sessions.issue(remember=True)
    assert main.sessions.user(token)
    later = time.time() + auth.SESSION_SECONDS + 60
    monkeypatch.setattr(itsdangerous.timed.time, "time", lambda: later)
    monkeypatch.setattr(auth.time, "time", lambda: later)
    assert main.sessions.user(token) is None
    assert main.sessions.user(remembered)   # "Remember me" lasts 30 days


def test_non_ascii_file_names_download(tmp_path, monkeypatch):
    d = tmp_path / "tools" / "x-1"
    d.mkdir(parents=True)
    (d / "README.md").write_text("# X 1 — Thing\n")
    (d / "Anleitung – Gerät.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(main, "REPO_PATH", tmp_path)
    r = client.get("/files/tools/x-1/" + main.quote("Anleitung – Gerät.pdf"))
    assert r.status_code == 200 and "filename*=UTF-8''" in r.headers["content-disposition"]


def test_damaged_login_file_is_never_treated_as_new(tmp_path):
    p = tmp_path / "auth.json"
    auth.AuthConfig(p).set_credentials("me", "password123")
    p.write_text("{ truncated")
    import pytest
    with pytest.raises(SystemExit):
        auth.AuthConfig(p)


def test_webhook_rejects_huge_bodies(monkeypatch):
    monkeypatch.setattr(main.store, "env_webhook_secret", "s")
    monkeypatch.setattr(main.sync, "mode", "server")
    monkeypatch.setattr(main.sync, "url", "https://example.invalid/r.git")
    assert client.post("/webhook/github", content=b"x" * 3_000_000).status_code == 413


def test_csv_cells_cannot_become_formulas(monkeypatch):
    inv = main.inventory.get()
    monkeypatch.setattr(inv.tools[0], "title", "=HYPERLINK(\"http://x\")")
    import csv as _csv
    rows = list(_csv.reader(client.get("/insurance.csv").text.splitlines()))
    assert rows[1][0].startswith("'=HYPERLINK")


def fresh_logged_in():
    c = fresh()
    c.post("/login", data={"username": USER, "password": PASSWORD})
    return c
