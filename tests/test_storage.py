"""Where the data lives (1.1): this server first, GitHub source, or GitHub backup."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app import edit, storage
from app.sync import RepoSync, WriteError
from test_edit import git, other_push  # noqa: F401  (git helpers and the bare "GitHub" repo)
from test_edit import remote  # noqa: F401  pytest fixture

ROOT = Path(__file__).resolve().parent.parent


def log_mutate(slug, date, what):
    def mutate(root):
        p = root / "tools" / slug / "README.md"
        p.write_text(edit.add_log_entry(p.read_text(), date, what))
        return [f"tools/{slug}/README.md"]
    return mutate


def server(tmp_path, url=""):
    s = RepoSync(tmp_path / "data", url=url, mode="server")
    s.init_local(storage.STARTERS / "sample")
    return s


def test_server_mode_keeps_history_without_github(tmp_path):
    s = server(tmp_path)
    assert (tmp_path / "data/tools/example-ts10/README.md").exists() and s.head
    assert s.write(log_mutate("example-ts10", "2026-01-01", "Waxed"), "Log", author="me via MyGarage")
    assert git("log", "--format=%an|%s", cwd=tmp_path / "data").splitlines() == [
        "me via MyGarage|Log", "MyGarage|Start MyGarage"]
    # A failed edit leaves nothing behind.
    def bad(root):
        (root / "tools/example-ts10/README.md").write_text("broken")
        raise edit.Invalid("no")
    with pytest.raises(edit.Invalid):
        s.write(bad, "x")
    assert "Waxed" in (tmp_path / "data/tools/example-ts10/README.md").read_text()
    assert not git("status", "--porcelain", cwd=tmp_path / "data")


def test_existing_folder_is_adopted_not_replaced(tmp_path):
    (tmp_path / "data/tools/mine").mkdir(parents=True)
    (tmp_path / "data/README.md").write_text("# My own\n")
    s = RepoSync(tmp_path / "data", mode="server")
    s.init_local(storage.STARTERS / "sample")
    assert (tmp_path / "data/README.md").read_text() == "# My own\n"
    assert not (tmp_path / "data/tools/example-ts10").exists()
    assert git("log", "--format=%s", cwd=tmp_path / "data") == "Add existing files"


@pytest.fixture
def empty_remote(tmp_path):
    bare = tmp_path / "backup.git"
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    return bare


def test_backup_sends_everything_and_takes_github_edits(tmp_path, empty_remote):
    s = server(tmp_path, url=f"file://{empty_remote}")
    s.sync_backup()  # first connection: the whole history goes up
    assert git("rev-parse", "main", cwd=empty_remote) == s.head and not s.backup_error
    s.write(log_mutate("example-ts10", "2026-01-01", "Waxed"), "Log")
    s.wait_for_backup()  # the push runs after the save returns
    assert git("rev-parse", "main", cwd=empty_remote) == s.head  # each edit is pushed

    # An edit on GitHub comes in...
    other_push(empty_remote, tmp_path, "tools/example-dd20/README.md", lambda t: t + "\nFrom GitHub.\n")
    assert s.sync_backup()
    assert "From GitHub." in (tmp_path / "data/tools/example-dd20/README.md").read_text()

    # ...and edits on both sides to different files are combined.
    other_push(empty_remote, tmp_path / "b", "tools/example-dd20/README.md", lambda t: t + "\nAgain.\n") \
        if (tmp_path / "b").mkdir() is None else None
    s.url = "file:///nonexistent"  # GitHub unreachable: the edit still saves here
    assert s.write(log_mutate("example-ts10", "2026-02-02", "Belt"), "Log 2")
    s.wait_for_backup()  # the push runs after the save returns
    assert s.backup_error
    s.url = f"file://{empty_remote}"
    assert s.sync_backup() and not s.backup_error
    data = tmp_path / "data/tools"
    assert "Again." in (data / "example-dd20/README.md").read_text()
    assert "Belt" in git("show", "main:tools/example-ts10/README.md", cwd=empty_remote)


def test_backup_pauses_on_a_real_conflict_and_keeps_this_server(tmp_path, empty_remote):
    s = server(tmp_path, url=f"file://{empty_remote}")
    s.sync_backup()
    other_push(empty_remote, tmp_path, "tools/example-ts10/README.md",
               lambda t: t.replace("waxed the top.", "waxed the top (GitHub)."))
    s.url = "file:///nonexistent"
    def server_edit(root):
        p = root / "tools/example-ts10/README.md"
        p.write_text(p.read_text().replace("waxed the top.", "waxed (server)."))
        return ["tools/example-ts10/README.md"]
    s.write(server_edit, "Server edit")
    s.wait_for_backup()  # the push runs after the save returns
    s.url = f"file://{empty_remote}"
    assert not s.sync_backup()
    assert "both changed" in s.backup_error
    assert "waxed (server)." in (tmp_path / "data/tools/example-ts10/README.md").read_text()
    assert not (tmp_path / "data/.git/rebase-merge").exists()


def test_backup_refuses_unrelated_repo(tmp_path, remote):
    s = server(tmp_path, url=f"file://{remote}")
    assert not s.sync_backup()
    assert "different history" in s.backup_error


def test_storage_config(tmp_path):
    repo = tmp_path / "repo"
    cfg = storage.StorageConfig(tmp_path / "cfg/storage.json", repo)
    assert cfg.mode == ""                      # brand new: ask on the setup screen
    repo.mkdir()
    (repo / "README.md").write_text("# x")
    assert cfg.mode == "folder"                # 1.0 mounted folder keeps working
    cfg.set(mode="server", url="https://github.com/me/garage.git", token="t")
    assert len(cfg.webhook_secret) == 64
    assert oct((tmp_path / "cfg/storage.json").stat().st_mode & 0o777) == "0o600"
    again = storage.StorageConfig(tmp_path / "cfg/storage.json", repo)
    assert again.mode == "server" and again.token == "t"
    env = storage.StorageConfig(tmp_path / "cfg/storage.json", repo, env_url="https://x/y.git")
    assert env.mode == "github" and env.env_locked and env.url == "https://x/y.git"
    assert storage.normalize_repo_url("me/garage") == "https://github.com/me/garage.git"
    assert storage.normalize_repo_url("https://github.com/me/garage/") == "https://github.com/me/garage.git"
    assert storage.github_slug("https://github.com/me/garage.git") == "me/garage"


# --------------------------------------------------------------------------- first run, end to end

FIRST_RUN = textwrap.dedent("""
    import os, sys, time, json
    sys.path.insert(0, os.environ["APP_ROOT"])
    from starlette.testclient import TestClient
    from app import main
    out = {}
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "admin", "password": "correct horse battery"})
        r = c.get("/", follow_redirects=False, headers={"Accept": "text/html"})
        out["redirect"] = r.headers.get("location")
        out["health"] = c.get("/healthz").status_code
        form = json.loads(os.environ["FORM"])
        r = c.post("/setup/storage", data=form, follow_redirects=False)
        out["setup"] = [r.status_code, r.headers.get("location", ""), r.text[-1500:] if r.status_code != 303 else ""]
        if r.status_code != 303:
            print("RESULT" + json.dumps(out))
            raise SystemExit
        for _ in range(50):
            if main.sync.head:
                break
            time.sleep(0.1)
        main.inventory.invalidate()
        out["tools"] = c.get("/tools").text.count('data-tool ')
        r = c.post("/tools/new", data={"model_name": "Acme X1", "subtitle": "Sander"},
                   headers={"Accept": "application/json"})
        out["new"] = r.json()
        out["mode"] = main.sync.mode
        out["after"] = c.get("/", follow_redirects=False).status_code
        main.sync.wait_for_backup()  # the push runs after the save returns
    print("RESULT" + json.dumps(out))
""")


def first_run(tmp_path, form):
    env = {**os.environ, "APP_ROOT": str(ROOT), "REPO_PATH": str(tmp_path / "data/repo"),
           "CACHE_PATH": str(tmp_path / "data/cache"), "CONFIG_PATH": str(tmp_path / "data/config/auth.json"),
           "AUTH_USERNAME": "admin", "AUTH_PASSWORD": "correct horse battery", "FORM": json.dumps(form)}
    for k in ("GIT_REPO_URL", "GIT_TOKEN", "WEBHOOK_SECRET", "GIT_BRANCH"):
        env.pop(k, None)
    res = subprocess.run([sys.executable, "-c", FIRST_RUN], env=env, capture_output=True, text=True,
                         timeout=120)
    line = next((l for l in res.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, res.stderr[-2000:]
    return json.loads(line[6:])


def test_first_run_on_this_server(tmp_path):
    out = first_run(tmp_path, {"where": "server", "starter": "sample"})
    assert out["redirect"] == "/setup/storage" and out["health"] == 200
    assert out["setup"][0] == 303, out
    assert out["tools"] == 2 and out["new"]["ok"] and out["mode"] == "server" and out["after"] == 200
    repo = tmp_path / "data/repo"
    assert git("log", "--format=%s", cwd=repo).splitlines() == ["Add Acme X1", "Start MyGarage"]
    saved = json.loads((tmp_path / "data/config/storage.json").read_text())
    assert saved["mode"] == "server" and not saved.get("url")


def test_first_run_with_an_empty_github_repo(tmp_path, empty_remote):
    out = first_run(tmp_path, {"where": "github", "starter": "empty", "repo": f"file://{empty_remote}",
                               "branch": "main"})
    assert out["setup"][0] == 303, out
    assert out["mode"] == "github" and out["tools"] == 0 and out["new"]["ok"]
    # The starter and the new tool both reached "GitHub".
    assert git("log", "--format=%s", "main", cwd=empty_remote).splitlines() == ["Add Acme X1", "Start MyGarage"]


def test_first_run_with_an_existing_github_garage(tmp_path, remote):
    out = first_run(tmp_path, {"where": "github", "repo": f"file://{remote}", "branch": "main"})
    assert out["setup"][0] == 303 and out["tools"] == 2 and out["new"]["ok"], out


def test_first_run_reports_a_bad_repo(tmp_path):
    out = first_run(tmp_path, {"where": "github", "repo": f"file://{tmp_path}/nope.git"})
    assert out["setup"][0] == 400 and "couldn" in out["setup"][2].lower()


# --------------------------------------------------------------------------- switching the primary copy

def test_switch_github_to_server_and_back(tmp_path, remote):
    s = RepoSync(tmp_path / "data", url=f"file://{remote}", mode="github")
    s.sync_once()
    assert (tmp_path / "data/.git/shallow").exists()  # GitHub mode keeps one commit
    s.write(lambda root: log_mutate("acme-ts10", "2027-03-03", "From GitHub mode")(root), "g1")

    s.make_server_primary()
    assert s.mode == "server" and not (tmp_path / "data/.git/shallow").exists()
    assert git("rev-list", "--count", "HEAD", cwd=tmp_path / "data") == "2"  # full history
    s.write(log_mutate("acme-ts10", "2027-04-04", "From server mode"), "s1")
    s.wait_for_backup()  # the push runs after the save returns
    assert git("rev-parse", "main", cwd=remote) == s.head  # still backed up

    # GitHub unreachable: switching back refuses rather than losing anything.
    s.url = "file:///nonexistent"
    s.write(log_mutate("acme-ts10", "2027-05-05", "Offline edit"), "s2")
    s.wait_for_backup()  # the push runs after the save returns
    with pytest.raises(WriteError):
        s.make_github_primary()
    assert s.mode == "server"
    s.url = f"file://{remote}"
    s.make_github_primary()
    assert s.mode == "github" and git("rev-parse", "main", cwd=remote) == s.head
    assert "Offline edit" in git("show", "main:tools/acme-ts10/README.md", cwd=remote)


def test_server_only_can_switch_to_github(tmp_path, empty_remote):
    s = server(tmp_path)
    s.write(log_mutate("example-ts10", "2026-01-01", "Waxed"), "Log")
    s.url = f"file://{empty_remote}"
    s.make_github_primary()
    assert s.mode == "github"
    assert git("rev-list", "--count", "main", cwd=empty_remote) == "2"


SWITCH = textwrap.dedent("""
    import os, sys, json, time
    sys.path.insert(0, os.environ["APP_ROOT"])
    from starlette.testclient import TestClient
    from app import main
    out = {}
    pw = "correct horse battery"
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "admin", "password": pw})
        form = json.loads(os.environ["FORM"])
        out["setup"] = c.post("/setup/storage", data=form, follow_redirects=False).status_code
        for _ in range(100):
            if main.sync.head:
                break
            time.sleep(0.1)
        main.inventory.invalidate()
        out["mode1"], out["url1"] = main.sync.mode, bool(main.sync.url)
        out["tools"] = c.get("/tools").text.count("data-tool ")
        out["page"] = "Make GitHub the Primary Copy" in c.get("/settings").text
        r = c.post("/settings/storage", data={"action": "primary_github", "current": "wrong"})
        out["badpw"] = "incorrect" in r.text
        c.post("/settings/storage", data={"action": "primary_github", "current": pw})
        out["mode2"] = main.sync.mode
        out["saved2"] = json.load(open(os.environ["CONFIG_PATH"].replace("auth.json", "storage.json")))["mode"]
        out["page2"] = "Make This Server the Primary Copy" in c.get("/settings").text
        c.post("/settings/storage", data={"action": "primary_server", "current": pw})
        out["mode3"] = main.sync.mode
        r = c.post("/tools/new", data={"model_name": "Acme Z9"}, headers={"Accept": "application/json"})
        out["new"] = r.json().get("ok")
        main.sync.wait_for_backup()  # the push runs after the save returns
    print("RESULT" + json.dumps(out))
""")


def run_script(tmp_path, script, form):
    global FIRST_RUN
    saved, FIRST_RUN = FIRST_RUN, script
    try:
        return first_run(tmp_path, form)
    finally:
        FIRST_RUN = saved


def test_setup_server_with_backup_of_an_existing_github_garage_then_switch(tmp_path, remote):
    out = run_script(tmp_path, SWITCH, {"where": "server_backup", "repo": f"file://{remote}"})
    assert out["setup"] == 303 and out["mode1"] == "server" and out["url1"] and out["tools"] == 2, out
    assert out["page"] and out["badpw"]
    assert out["mode2"] == "github" and out["saved2"] == "github" and out["page2"]
    assert out["mode3"] == "server" and out["new"]
    assert "Add Acme Z9" in git("log", "-1", "--format=%s", "main", cwd=remote)


def test_setup_server_with_backup_to_an_empty_repo(tmp_path, empty_remote):
    out = first_run(tmp_path, {"where": "server_backup", "starter": "sample", "repo": f"file://{empty_remote}"})
    assert out["setup"][0] == 303 and out["mode"] == "server" and out["tools"] == 2, out
    assert git("log", "--format=%s", "main", cwd=empty_remote).splitlines() == ["Add Acme X1", "Start MyGarage"]
