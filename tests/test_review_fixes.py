"""Regression tests for the problems found reviewing PR #2."""

import json
import os
import re
import subprocess
import time
import textwrap

import pytest

import fixture
from test_app import ROOT, client
from test_edit import git, other_push, remote  # noqa: F401  (remote is a fixture)
from test_storage import first_run, run_script, server, empty_remote, log_mutate  # noqa: F401

from app import edit, main, repo, storage, sync as sync_mod
from app.sync import RepoSync

JSON = {"Accept": "application/json"}
PW = "correct horse battery"


def test_a_git_checkout_is_never_taken_over(tmp_path):
    """A folder that's a git clone, with no storage chosen, is read as a plain folder."""
    repo_dir = fixture.build(tmp_path / "clone")
    git("init", "-q", cwd=repo_dir)
    cfg = storage.StorageConfig(tmp_path / "storage.json", repo_dir)
    assert cfg.mode == "folder"


def test_a_hung_git_becomes_a_network_error(tmp_path, monkeypatch):
    s = RepoSync(tmp_path, url="https://example.invalid/r.git", mode="server")

    def hang(cmd, cwd, env, timeout):
        raise subprocess.TimeoutExpired(cmd="git", timeout=timeout)

    monkeypatch.setattr(sync_mod, "_run", hang)
    with pytest.raises(RuntimeError) as e:
        s._git("ls-remote", "x", timeout=1)
    assert sync_mod.is_network_error(e.value.detail)
    # The backup check reports it instead of letting it escape (and kill the loop).
    assert s.sync_backup() is False and "reach GitHub" in s.backup_error


def test_failed_github_setup_can_be_retried(tmp_path, empty_remote):
    hook = empty_remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'remote: Permission to x denied' >&2\nexit 1\n")
    hook.chmod(0o755)
    script = textwrap.dedent("""
        import os, sys, json
        sys.path.insert(0, os.environ["APP_ROOT"])
        from starlette.testclient import TestClient
        from app import main
        out = {}
        with TestClient(main.app) as c:
            c.post("/login", data={"username": "admin", "password": "correct horse battery"})
            form = json.loads(os.environ["FORM"])
            out["setup"] = c.post("/setup/storage", data=form, follow_redirects=False).status_code
            out["configured"] = main.sync.configured
            out["again"] = c.get("/setup/storage", follow_redirects=False).status_code
            out["files"] = sorted(p.name for p in main.REPO_PATH.iterdir()) if main.REPO_PATH.exists() else []
        print("RESULT" + json.dumps(out))
    """)
    out = run_script(tmp_path, script, {"where": "github", "starter": "sample",
                                        "repo": f"file://{empty_remote}"})
    assert out["setup"] == 400 and not out["configured"] and out["again"] == 200, out
    assert out["files"] == []


def test_markdown_editor_only_deletes_reference_pages():
    for url in ("/edit/tools/acme-ts10", "/edit/projects/2027-01-bookshelf", "/edit/reference/index"):
        r = client.post(url, data={"action": "delete"}, headers=JSON)
        assert r.status_code == 400, url
    assert (ROOT / "tools/acme-ts10/README.md").exists()
    assert (ROOT / "reference/README.md").exists()


def test_a_tool_called_new_gets_its_own_page():
    r = client.post("/tools/new", data={"kind": "Sander", "model_no": "New"}, headers=JSON)
    assert r.json()["url"] == "/tools/new-2"
    assert "New Tool" not in client.get("/tools/new-2").text.split("<title>")[1].split("</title>")[0]
    client.post("/tools/new-2/delete", data={"confirm": "new-2"}, headers=JSON)


def test_repeated_detail_labels_survive_an_edit():
    base = [("Battery", "2.0Ah"), ("Battery", "5.0Ah")]
    assert edit.merge_specs(base, base, base) == base
    mine = [("Battery", "2.0Ah"), ("Battery", "6.0Ah")]
    assert edit.merge_specs(base, mine, base) == mine
    with pytest.raises(edit.Conflict):
        edit.merge_specs(base, mine, [("Battery", "4.0Ah")])


def test_disconnecting_the_backup_needs_the_password(monkeypatch):
    monkeypatch.setattr(main.store, "env_locked", False)
    monkeypatch.setattr(main.sync, "mode", "server")
    monkeypatch.setattr(main.sync, "url", "https://example.invalid/r.git")
    called = []
    monkeypatch.setattr(main.store, "set", lambda **kw: called.append(kw))
    r = client.post("/settings/storage", data={"action": "disconnect"})
    assert "incorrect" in r.text and not called
    monkeypatch.setattr(main, "apply_storage", lambda: None)
    r = client.post("/settings/storage", data={"action": "disconnect", "current": PW})
    assert called == [{"url": "", "token": ""}] and "disconnected" in r.text
    assert 'value="disconnect"' in client.get("/settings").text


def test_rejected_backup_checks_github_on_the_next_turn(tmp_path, empty_remote):
    s = server(tmp_path, url=f"file://{empty_remote}")
    s.sync_backup()
    other_push(empty_remote, tmp_path, "tools/example-dd20/README.md", lambda t: t + "\nGitHub edit.\n")
    s._checked_at = 10**12  # "just checked"
    s.write(log_mutate("example-ts10", "2026-03-03", "Server edit"), "Log")
    s.wait_for_backup()
    assert "Waiting" in s.backup_error
    # Due again in about a minute: not "just checked", and not a tight retry loop.
    assert s.ensure_fresh(s.interval) is False
    assert s.ensure_fresh(s.interval - 61) or not s.backup_error
    assert "GitHub edit." in (tmp_path / "data/tools/example-dd20/README.md").read_text()
    assert git("rev-parse", "main", cwd=empty_remote) == s.head


def test_saving_does_not_wait_for_the_backup(tmp_path, empty_remote):
    s = server(tmp_path, url=f"file://{empty_remote}")
    s.sync_backup()
    pushed = []
    real = s._push_backup
    s._push_backup = lambda **kw: pushed.append(1) or real(**kw)
    assert s.write(log_mutate("example-ts10", "2026-03-04", "Quick"), "Log")
    s.wait_for_backup()
    assert pushed == [1] and git("rev-parse", "main", cwd=empty_remote) == s.head


def test_history_is_fetched_once_and_kept(tmp_path, remote):
    for i in range(3):  # a few commits of history on "GitHub"
        other_push(remote, tmp_path / f"w{i}", "tools/acme-dd20/README.md", lambda t, i=i: t + f"\nEdit {i}\n") \
            if (tmp_path / f"w{i}").mkdir() is None else None
    s = RepoSync(tmp_path / "data", url=f"file://{remote}", mode="github")
    s.sync_once()
    assert git("rev-list", "--count", "HEAD", cwd=tmp_path / "data") == "1"
    before = s.fetches_today
    assert len(s.history([])) == 4
    assert len(s.history([])) == 4
    assert s.fetches_today - before == 1  # the second view didn't fetch again
    s.write(log_mutate("acme-ts10", "2027-06-06", "Kept history"), "Log")
    assert len(s.history([])) == 5  # writing didn't cut history back to one commit


@pytest.fixture
def restore_drill():
    """Put tools/acme-dd20 back as it was, so other tests don't see this one's files."""
    folder = ROOT / "tools/acme-dd20"
    saved = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    yield folder
    for p in folder.iterdir():
        if p.name not in saved:
            p.unlink()
    for name, data in saved.items():
        (folder / name).write_bytes(data)
    main.inventory.invalidate()


def test_cover_photo_links_follow_both_files(restore_drill):
    folder = restore_drill
    for name in ("photo.jpg", "side.jpg"):
        (folder / name).write_bytes(fixture._jpeg())
    readme = folder / "README.md"
    readme.write_text(readme.read_text() + "\n![old cover](photo.jpg) ![side](side.jpg)\n")
    main.inventory.invalidate()
    assert client.post("/tools/acme-dd20/files", data={"action": "cover", "name": "side.jpg"},
                       headers=JSON).json()["ok"]
    text = readme.read_text()
    moved = re.search(r"!\[old cover\]\((photo-[^)]+)\)", text).group(1)
    assert (folder / moved).read_bytes() == fixture._jpeg()  # the old cover, renamed
    assert "![side](photo.jpg)" in text and not (folder / "side.jpg").exists()


# --------------------------------------------------------------------------- second review

def test_git_subcommand_names_and_local_timeouts():
    assert sync_mod._subcommand(["-c", "user.name=x", "-c", "user.email=y", "commit", "-q"]) == "commit"
    assert sync_mod._subcommand(["push", "-q"]) == "push"


def test_a_push_that_timed_out_but_landed_is_not_saved_twice(tmp_path, remote):
    s = RepoSync(tmp_path / "data", url=f"file://{remote}", mode="github")
    s.sync_once()
    real = s._git

    def slow_push(*args, **kw):
        out = real(*args, **kw)
        if args and args[0] == "push":  # it went through, then the connection hung
            err = RuntimeError("git push timed out")
            err.timed_out, err.detail = True, "git push: operation timed out after 300s"
            raise err
        return out

    s._git = slow_push
    assert s.write(log_mutate("acme-ts10", "2027-07-07", "Once"), "Log")
    shown = git("show", "main:tools/acme-ts10/README.md", cwd=remote)
    assert shown.count("Once") == 1 and s.head == git("rev-parse", "main", cwd=remote)


def test_github_setup_refuses_a_folder_with_a_checkout(tmp_path, empty_remote):
    script = textwrap.dedent("""
        import os, sys, json, subprocess
        sys.path.insert(0, os.environ["APP_ROOT"])
        repo = os.environ["REPO_PATH"]
        os.makedirs(repo); subprocess.run(["git", "init", "-q", repo], check=True)
        from starlette.testclient import TestClient
        from app import main
        out = {}
        with TestClient(main.app) as c:
            c.post("/login", data={"username": "admin", "password": "correct horse battery"})
            r = c.post("/setup/storage", data=json.loads(os.environ["FORM"]), follow_redirects=False)
            out["setup"], out["text"] = r.status_code, r.text[-600:]
            out["files"] = sorted(os.listdir(repo))
        print("RESULT" + json.dumps(out))
    """)
    out = run_script(tmp_path, script, {"where": "github", "starter": "sample",
                                        "repo": f"file://{empty_remote}"})
    assert out["setup"] == 400 and "already has files" in out["text"] and out["files"] == [".git"], out


def test_any_failure_during_github_setup_is_undone(tmp_path, empty_remote):
    script = textwrap.dedent("""
        import os, sys, json
        sys.path.insert(0, os.environ["APP_ROOT"])
        from starlette.testclient import TestClient
        from app import main
        real = main.sync.init_local
        def broken(starter=None):
            real(starter)
            raise OSError("disk full")
        main.sync.init_local = broken
        out = {}
        with TestClient(main.app) as c:
            c.post("/login", data={"username": "admin", "password": "correct horse battery"})
            r = c.post("/setup/storage", data=json.loads(os.environ["FORM"]), follow_redirects=False)
            out["setup"] = r.status_code
            out["mode"], out["store"] = main.sync.mode, main.store.mode
            out["files"] = os.listdir(main.REPO_PATH)
            out["again"] = c.get("/setup/storage", follow_redirects=False).status_code
        print("RESULT" + json.dumps(out))
    """)
    out = run_script(tmp_path, script, {"where": "github", "starter": "sample",
                                        "repo": f"file://{empty_remote}"})
    assert out == {"setup": 400, "mode": "", "store": "", "files": [], "again": 200}, out


def test_a_rejected_push_during_backup_sync_is_rechecked(tmp_path, empty_remote):
    s = server(tmp_path, url=f"file://{empty_remote}")
    s.sync_backup()
    s.write(log_mutate("example-ts10", "2026-04-04", "Local"), "Log")
    s.wait_for_backup()
    real = s._git

    (tmp_path / "racer").mkdir()

    def race(*args, **kw):  # someone pushes to GitHub just before our push
        if args and args[0] == "push" and not (tmp_path / "racer" / "other").exists():
            other_push(empty_remote, tmp_path / "racer", "tools/example-dd20/README.md",
                       lambda t: t + "Racer.")
        return real(*args, **kw)

    s._git = race
    real("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-qm", "unsent",
         cwd=tmp_path / "data")
    s.head = real("rev-parse", "HEAD", cwd=tmp_path / "data")
    s.sync_backup()
    assert "Waiting" in s.backup_error
    assert time.time() - s._checked_at >= s.interval - 61  # due again within a minute


def test_fetching_keeps_the_history_depth_with_merge_commits(tmp_path, remote):
    work = tmp_path / "work"
    git("clone", "-q", str(remote), str(work), cwd=tmp_path)
    cfg = ["-c", "user.name=o", "-c", "user.email=o@o"]
    for i in range(4):  # a few merged side branches, like merged pull requests
        git("checkout", "-qb", f"b{i}", cwd=work)
        (work / f"note{i}.md").write_text("x")
        git("add", "-A", cwd=work)
        git(*cfg, "commit", "-qm", f"side {i}", cwd=work)
        git("checkout", "-q", "main", cwd=work)
        (work / f"main{i}.md").write_text("y")
        git("add", "-A", cwd=work)
        git(*cfg, "commit", "-qm", f"main {i}", cwd=work)
        git(*cfg, "merge", "-q", "--no-ff", "-m", f"merge {i}", f"b{i}", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)

    s = RepoSync(tmp_path / "data", url=f"file://{remote}", mode="github")
    s.sync_once()
    s.write(lambda root: [], "nothing", depth=3)  # deepen a little, as Undo does
    count = lambda: int(git("rev-list", "--count", "HEAD", cwd=tmp_path / "data"))
    before = count()
    for _ in range(3):
        s._checked_at = 0
        s.sync_once()
    assert count() == before  # no creeping toward the whole history


# --------------------------------------------------------------------------- third review

def test_removing_git_repo_url_keeps_the_garage_versioned(tmp_path):
    repo_dir = fixture.build(tmp_path / "repo")
    git("init", "-q", cwd=repo_dir)
    cfg_path = tmp_path / "config" / "storage.json"
    env = storage.StorageConfig(cfg_path, repo_dir, env_url="https://github.com/me/g.git",
                                env_token="secret")
    assert env.mode == "github"
    saved = json.loads(cfg_path.read_text())
    assert saved == {"from_env_github": True}  # the token never lands in the file
    later = storage.StorageConfig(cfg_path, repo_dir)  # GIT_REPO_URL removed
    assert later.mode == "server"


def test_a_failed_server_setup_can_be_retried(tmp_path):
    script = textwrap.dedent("""
        import os, sys, json
        sys.path.insert(0, os.environ["APP_ROOT"])
        from starlette.testclient import TestClient
        from app import main
        real = main.sync.init_local
        def broken(starter=None):
            real(starter)
            raise OSError("disk full")
        main.sync.init_local = broken
        out = {}
        with TestClient(main.app) as c:
            c.post("/login", data={"username": "admin", "password": "correct horse battery"})
            r = c.post("/setup/storage", data=json.loads(os.environ["FORM"]), follow_redirects=False)
            out["setup"], out["mode"], out["store"] = r.status_code, main.sync.mode, main.store.mode
            out["files"] = os.listdir(main.REPO_PATH)
            main.sync.init_local = real
            out["retry"] = c.post("/setup/storage", data=json.loads(os.environ["FORM"]),
                                  follow_redirects=False).status_code
            out["after"] = main.sync.mode
        print("RESULT" + json.dumps(out))
    """)
    out = run_script(tmp_path, script, {"where": "server", "starter": "sample"})
    assert out == {"setup": 400, "mode": "", "store": "", "files": [], "retry": 303,
                   "after": "server"}, out


def test_repeated_labels_merge_with_unrelated_edits():
    base = [("Model #", "X1"), ("Battery", "2.0Ah")]
    mine = [("Model #", "X2"), ("Battery", "2.0Ah")]
    theirs = [("Model #", "X1"), ("Battery", "2.0Ah"), ("Battery", "5.0Ah")]
    assert edit.merge_specs(base, mine, theirs) == [("Model #", "X2"), ("Battery", "2.0Ah"),
                                                    ("Battery", "5.0Ah")]


def test_a_push_that_timed_out_and_did_not_land_is_not_resent(tmp_path, remote):
    s = RepoSync(tmp_path / "data", url=f"file://{remote}", mode="github")
    s.sync_once()
    real = s._git

    def lost_push(*args, **kw):
        if args and args[0] == "push":
            err = RuntimeError("git push timed out")
            err.timed_out, err.detail = True, "git push: operation timed out after 300s"
            raise err
        return real(*args, **kw)

    s._git = lost_push
    with pytest.raises(sync_mod.WriteError) as e:
        s.write(log_mutate("acme-ts10", "2027-08-08", "Maybe"), "Log")
    assert not e.value.retry and "Reload" in str(e.value)


def test_pointing_at_another_repo_fetches_only_the_kept_history(tmp_path, remote):
    s = RepoSync(tmp_path / "data", url=f"file://{remote}", mode="github")
    s.sync_once()
    other = tmp_path / "other.git"
    seed = fixture.build(tmp_path / "seed2")
    git("init", "-q", "-b", "main", cwd=seed)
    for i in range(5):
        (seed / f"f{i}.md").write_text(str(i))
        git("add", "-A", cwd=seed)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"c{i}", cwd=seed)
    git("clone", "-q", "--bare", str(seed), str(other), cwd=tmp_path)
    s.url = f"file://{other}"
    s._checked_at = 0
    s.sync_once()
    assert git("rev-list", "--count", "HEAD", cwd=tmp_path / "data") == "1"



# --------------------------------------------------------------------------- fourth review

def test_a_persons_own_shallow_checkout_is_not_taken_over(tmp_path):
    seed = fixture.build(tmp_path / "seed")
    git("init", "-q", "-b", "main", cwd=seed)
    git("add", "-A", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "s", cwd=seed)
    clone = tmp_path / "repo"
    git("clone", "-q", "--depth", "1", f"file://{seed}", str(clone), cwd=tmp_path)
    assert storage.StorageConfig(tmp_path / "s.json", clone).mode == "folder"


def test_settings_that_cannot_be_saved_say_so(tmp_path):
    cfg = storage.StorageConfig(tmp_path / "ro" / "storage.json", tmp_path / "repo")
    (tmp_path / "ro").mkdir()
    (tmp_path / "ro").chmod(0o500)
    try:
        if os.access(tmp_path / "ro", os.W_OK):
            pytest.skip("running as a user that can write anywhere")
        with pytest.raises(OSError):
            cfg.set(mode="server")
    finally:
        (tmp_path / "ro").chmod(0o700)


def test_an_unreadable_storage_file_stops_the_app_and_is_left_alone(tmp_path):
    p = tmp_path / "storage.json"
    p.write_text("{not json")
    for env in ("", "https://github.com/me/g.git"):
        with pytest.raises(RuntimeError, match="Fix or remove"):
            storage.StorageConfig(p, tmp_path / "repo", env_url=env)
    assert p.read_text() == "{not json"


def test_a_timeout_stops_gits_helper_processes_too(tmp_path):
    marker = tmp_path / "still-running"
    script = f"sleep 3; touch {marker}"
    with pytest.raises(subprocess.TimeoutExpired):
        sync_mod._run(["sh", "-c", f"({script}) & wait"], cwd=tmp_path, env=None, timeout=1)
    time.sleep(3)
    assert not marker.exists()  # the background child was stopped with its parent


def test_saving_the_form_unchanged_elsewhere_keeps_its_order():
    b = [("Battery", "2Ah"), ("Charger", "C1"), ("Battery", "4Ah")]
    assert edit.merge_specs(b, [("Charger", "C1"), ("Battery", "4Ah")], b) == \
        [("Charger", "C1"), ("Battery", "4Ah")]


def test_a_new_copy_fetches_history_again(tmp_path, remote):
    s = RepoSync(tmp_path / "data", url=f"file://{remote}", mode="github")
    s.depth = 100  # remembered from before the folder was lost
    s.sync_once()
    assert s.depth == 1


def test_setup_refuses_a_folder_with_files_but_no_garage(tmp_path):
    script = textwrap.dedent("""
        import os, sys, json
        sys.path.insert(0, os.environ["APP_ROOT"])
        repo = os.environ["REPO_PATH"]
        os.makedirs(repo); open(os.path.join(repo, "notes.txt"), "w").write("mine")
        from starlette.testclient import TestClient
        from app import main
        with TestClient(main.app) as c:
            c.post("/login", data={"username": "admin", "password": "correct horse battery"})
            r = c.post("/setup/storage", data=json.loads(os.environ["FORM"]), follow_redirects=False)
            out = {"setup": r.status_code, "files": sorted(os.listdir(repo)), "mode": main.sync.mode}
        print("RESULT" + json.dumps(out))
    """)
    out = run_script(tmp_path, script, {"where": "server", "starter": "sample"})
    assert out == {"setup": 400, "files": ["notes.txt"], "mode": ""}, out


def test_repeated_labels_keep_their_rows_in_place():
    b = [("Battery", "2Ah"), ("Charger", "C1"), ("Battery", "4Ah")]
    assert edit.merge_specs(b, b, b) == b
    m = [("Battery", "2Ah"), ("Charger", "C2"), ("Battery", "4Ah")]
    assert edit.merge_specs(b, m, b) == m


def test_a_failed_settings_save_changes_nothing(tmp_path, monkeypatch):
    cfg = storage.StorageConfig(tmp_path / "storage.json", tmp_path / "repo")
    cfg.set(mode="server", url="https://github.com/me/g.git", token="t")

    def fail(data):
        raise OSError("read-only")

    monkeypatch.setattr(cfg, "_write", fail)
    with pytest.raises(OSError):
        cfg.set(url="", token="")
    assert cfg.url == "https://github.com/me/g.git" and cfg.token == "t"


def test_deleting_different_copies_of_a_repeated_label_conflicts():
    base = [("Battery", "2.0Ah"), ("Battery", "5.0Ah")]
    with pytest.raises(edit.Conflict):
        edit.merge_specs(base, [("Battery", "2.0Ah")], [("Battery", "5.0Ah")])


# --------------------------------------------------------------------------- fifth review

def test_extra_and_new_rows_keep_form_order():
    A, B = (lambda v: ("A", v)), (lambda v: ("B", v))
    assert edit.merge_specs([A(1), B(1)], [A(1), A(2), B(1), B(2)], [A(1), B(1)]) == \
        [A(1), A(2), B(1), B(2)]
    assert edit.merge_specs([A(1), B(1)], [A(1), A(2), B(1), ("C", "x")], [A(1), B(1)]) == \
        [A(1), A(2), B(1), ("C", "x")]


def test_backups_never_overlap(tmp_path, empty_remote):
    s = server(tmp_path, url=f"file://{empty_remote}")
    s.sync_backup()
    for i in range(3):  # quick saves while the first push may still be running
        s.write(log_mutate("example-ts10", f"2026-05-0{i + 1}", f"Save {i}"), f"Log {i}")
    s.wait_for_backup()
    assert not s.backup_error and git("rev-parse", "main", cwd=empty_remote) == s.head



def test_a_half_undone_setup_still_shows_the_setup_screen(tmp_path):
    repo_dir = fixture.build(tmp_path / "repo")  # files left behind by a failed setup
    cfg = storage.StorageConfig(tmp_path / "storage.json", repo_dir)
    cfg.set(mode="", url="", token="", needs_setup=True)
    again = storage.StorageConfig(tmp_path / "storage.json", repo_dir)
    assert again.mode == ""
    again.set(mode="server")
    assert storage.StorageConfig(tmp_path / "storage.json", repo_dir).mode == "server"


def test_a_failed_background_clone_returns_to_setup(tmp_path):
    script = textwrap.dedent("""
        import os, sys, json, time
        sys.path.insert(0, os.environ["APP_ROOT"])
        from starlette.testclient import TestClient
        from app import main
        out = {}
        with TestClient(main.app) as c:
            c.post("/login", data={"username": "admin", "password": "correct horse battery"})
            main.sync.check_remote = lambda *a, **k: "0" * 40  # "a garage exists there"
            r = c.post("/setup/storage", data=json.loads(os.environ["FORM"]), follow_redirects=False)
            out["setup"] = r.status_code
            for _ in range(100):
                if not main.sync.configured:
                    break
                time.sleep(0.1)
            out["configured"] = main.sync.configured
            page = c.get("/setup/storage", follow_redirects=False)
            out["again"], out["says"] = page.status_code, "bring the garage over" in page.text
        print("RESULT" + json.dumps(out))
    """)
    out = run_script(tmp_path, script, {"where": "server_backup",
                                        "repo": f"file://{tmp_path}/missing.git"})
    assert out == {"setup": 303, "configured": False, "again": 200, "says": True}, out


# --------------------------------------------------------------------------- sixth review

def test_a_refused_backup_is_not_retried_every_minute(tmp_path, monkeypatch):
    s = RepoSync(tmp_path, url="https://example.invalid/r.git", mode="server")
    s._checked_at = before = time.time()

    def refuse(*a, **k):
        err = RuntimeError("push refused")
        err.detail = "remote: error: GH001: Large files detected. File is 150 MB"
        raise err

    monkeypatch.setattr(s, "_git", refuse)
    assert not s._push_backup()
    assert "too large" in s.backup_error and s._checked_at == before


def test_hidden_files_alone_do_not_block_setup(tmp_path):
    script = textwrap.dedent("""
        import os, sys, json
        sys.path.insert(0, os.environ["APP_ROOT"])
        repo = os.environ["REPO_PATH"]
        os.makedirs(repo); open(os.path.join(repo, ".DS_Store"), "w").write("x")
        from starlette.testclient import TestClient
        from app import main
        with TestClient(main.app) as c:
            c.post("/login", data={"username": "admin", "password": "correct horse battery"})
            r = c.post("/setup/storage", data=json.loads(os.environ["FORM"]), follow_redirects=False)
            out = {"setup": r.status_code, "mode": main.sync.mode}
        print("RESULT" + json.dumps(out))
    """)
    assert run_script(tmp_path, script, {"where": "server", "starter": "empty"}) == \
        {"setup": 303, "mode": "server"}
