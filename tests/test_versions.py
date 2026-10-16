"""1.2: the installed version, What's New, update notices, and the overnight upgrade script."""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from datetime import timedelta
from pathlib import Path

import pytest

from app import releases
from app.releases import Release, Versions

ROOT = Path(__file__).resolve().parent.parent

CHANGELOG = """# Changelog

## 1.2.0 (unreleased)

### Added
- Overnight upgrades.

## 1.1.0 (2026-10-16)

- Phone editing.

## 1.0.0

- First release.
"""


def make(tmp_path, current="1.2.0", releases_=(), **kw):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    return Versions(tmp_path / "config", tmp_path / "cache", current=current,
                    fetch=lambda: list(releases_), changelog_path=tmp_path / "CHANGELOG.md", **kw)


def rel(version, notes=""):
    return Release(version, "2027-01-01", notes, releases.tag_url(version))


# ---------------------------------------------------------------- installed version

def test_new_install_has_nothing_to_announce(tmp_path):
    v = make(tmp_path)
    v.startup(existing_install=False)
    assert v.data["whats_new_seen"] is True and v.previous is None


def test_upgrade_from_1_1_is_announced_once(tmp_path):
    v = make(tmp_path)
    v.startup(existing_install=True)        # 1.1 kept no version.json
    assert v.data["whats_new_seen"] is False and v.installed_at
    v.mark_whats_new_seen()
    again = make(tmp_path)
    again.startup(existing_install=True)    # a restart on the same version
    assert again.data["whats_new_seen"] is True


def test_version_change_remembers_the_previous_one(tmp_path):
    make(tmp_path, current="1.2.0").startup(existing_install=True)
    v = make(tmp_path, current="1.3.0")
    v.startup(existing_install=True)
    assert v.previous == "1.2.0" and v.data["whats_new_seen"] is False


def test_installed_notes_come_from_the_bundled_changelog(tmp_path):
    v = make(tmp_path, current="1.1.0")
    assert [r.version for r in v.installed_notes()] == ["1.1.0", "1.0.0"]
    assert make(tmp_path).installed_notes()[0].notes.startswith("### Added")


# ---------------------------------------------------------------- update checks

def test_check_sorts_out_same_major_and_new_major(tmp_path):
    v = make(tmp_path, releases_=[rel("2.0.0", "### Upgrading from 1.x\n- Move the folder."),
                                  rel("1.3.0"), rel("1.2.0"), rel("1.1.0")])
    assert v.due()
    v.check_now()
    assert not v.due()
    assert [r.version for r in v.newer()] == ["2.0.0", "1.3.0"]
    assert v.same_major_update().version == "1.3.0"
    assert v.major_update().version == "2.0.0"
    assert v.major_popup().upgrading == "- Move the folder."
    v.dismiss_major("2.0.0")
    assert v.major_popup() is None and v.major_update()      # still listed in About


def test_a_later_major_version_brings_the_popup_back(tmp_path):
    v = make(tmp_path, releases_=[rel("2.0.0")])
    v.check_now()
    v.dismiss_major("2.0.0")
    v.fetch = lambda: [rel("3.0.0"), rel("2.0.0")]
    v.check_now()
    assert v.major_popup().version == "3.0.0"


def test_offline_check_keeps_the_last_list_and_retries_sooner(tmp_path):
    v = make(tmp_path, releases_=[rel("1.3.0")])
    v.check_now()

    def offline():
        raise OSError("no network")
    v.fetch = offline
    v.check_now()
    assert v.last_check["error"] and v.same_major_update().version == "1.3.0"
    cache = json.loads(v.cache.read_text())
    cache["checked_at"] = (releases.now() - timedelta(hours=2)).isoformat()
    v.cache.write_text(json.dumps(cache))
    assert v.due()                          # an hour after a failure, not a day


def test_checking_can_be_turned_off(tmp_path):
    assert not make(tmp_path, check=False).due()


def test_github_release_list_is_parsed(monkeypatch):
    items = [{"tag_name": "v1.3.0", "body": "x", "html_url": "u", "published_at": "2027-02-01T00:00:00Z"},
             {"tag_name": "v1.4.0-rc1", "body": "", "prerelease": True},
             {"tag_name": "v9.9.9", "draft": True},
             {"tag_name": "nightly"}]

    class Resp:
        def __enter__(self):
            import io
            return io.BytesIO(json.dumps(items).encode())

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(releases.urllib.request, "urlopen", lambda req, timeout: Resp())
    out = releases.fetch_github()
    assert [(r.version, r.date, r.url) for r in out] == [("1.3.0", "2027-02-01", "u")]


# ---------------------------------------------------------------- the Cron Job's reports

def test_auto_upgrade_states(tmp_path):
    v = make(tmp_path)
    assert v.auto()["state"] == "off"
    releases.record(tmp_path / "config", "current", "1.2.0")
    assert v.auto()["state"] == "ok"
    releases.record(tmp_path / "config", "upgraded", "1.1.0", "1.2.0")
    assert v.upgraded_overnight
    releases.record(tmp_path / "config", "rolled_back", "1.2.0", "1.3.0")
    assert v.auto()["state"] == "failed"
    releases.record(tmp_path / "config", "pinned", "1.2.0", detail="1.2.0")
    assert v.auto()["state"] == "pinned"
    rec = json.loads(v.auto_file.read_text())
    rec["at"] = (releases.now() - timedelta(days=4)).isoformat()
    v.auto_file.write_text(json.dumps(rec))
    assert v.auto()["state"] == "stale"


def test_record_cli_uses_the_app_config_folder(tmp_path):
    env = {**os.environ, "CONFIG_PATH": str(tmp_path / "auth.json")}
    subprocess.run([sys.executable, "-m", "app.releases", "record", "--result", "upgraded",
                    "--from", "1.1.0", "--to", "1.2.0"], cwd=ROOT, env=env, check=True)
    assert json.loads((tmp_path / "auto-upgrade.json").read_text())["to"] == "1.2.0"


# ---------------------------------------------------------------- in the app

@pytest.fixture
def app_versions():
    from test_app import client
    from app import main
    v = main.versions
    saved = dict(v.data)
    for f in (v.cache, v.auto_file):
        f.unlink(missing_ok=True)
    yield client, v
    v.data = saved
    for f in (v.cache, v.auto_file):
        f.unlink(missing_ok=True)


def seed(v, *versions_):
    releases._write(v.cache, {"checked_at": releases.now().isoformat(),
                              "releases": [rel(x, "### Upgrading from 1.x\n- Do **this**.").__dict__
                                           for x in versions_]})


def test_settings_about_section(app_versions):
    client, v = app_versions
    page = client.get("/settings").text
    assert 'id="about"' in page and "Not set up" in page and "How to set it up" in page
    releases.record(v.auto_file.parent, "upgraded", "1.1.0", v.current)
    seed(v, "1.99.0")
    page = client.get("/settings").text
    assert "1.99.0 installs tonight" in page and f"upgraded to {v.current}" in page


def test_updated_banner_until_dismissed(app_versions):
    client, v = app_versions
    v.data["whats_new_seen"] = False
    releases.record(v.auto_file.parent, "upgraded", "1.1.0", v.current)
    assert f"Updated to {v.current} overnight" in client.get("/tools").text
    r = client.post("/settings/updates", data={"action": "seen", "next": "/tools"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/tools"
    assert "Updated to" not in client.get("/tools").text


def test_whats_new_page_marks_the_banner_seen(app_versions):
    client, v = app_versions
    v.data["whats_new_seen"] = False
    seed(v, "1.99.0")
    page = client.get("/settings/about").text
    assert "Not Installed Yet" in page and f">{v.current}" in page
    assert v.data["whats_new_seen"] is True


def test_major_version_popup(app_versions):
    client, v = app_versions
    seed(v, "9.0.0")
    page = client.get("/").text
    assert "data-major-update" in page and "9.0.0 Is Available" in page
    assert "<strong>this</strong>" in page                  # its upgrade steps, as Markdown
    assert "won't install on its own" in client.get("/settings").text
    client.post("/settings/updates", data={"action": "dismiss_major", "version": "9.0.0", "next": "//evil"})
    assert "data-major-update" not in client.get("/").text
    assert "9.0.0 is available" in client.get("/settings").text   # About still says so


def test_dismiss_redirect_stays_on_this_site(app_versions):
    client, _ = app_versions
    r = client.post("/settings/updates", data={"action": "seen", "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"


# ---------------------------------------------------------------- deploy/auto-upgrade.sh

SCRIPT = ROOT / "deploy/auto-upgrade.sh"
REF = "ghcr.io/jt87/mygarage:1"

# A pretend `docker` and `midclt`, driven by state.json, logging every call.
DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
p = os.environ["STUB_STATE"]; s = json.load(open(p)); a = sys.argv[1:]
s["log"].append(" ".join(a)); out = ""; code = 0
c = s["container"]
def fmt(i): return a[a.index("-f") + 1]
if a[0] == "ps":
    out = f'{c["name"]} {c["ref"]}' if c else ""
elif a[0] == "inspect":
    f = fmt(a)
    if "Health" in f: out = s["health"].get(c["image"], "starting")
    elif "Running" in f: out = "true"
    elif "Config.Image" in f: out = c["ref"]
    else: out = c["image"]
elif a[:2] == ["image", "inspect"]:
    f, target = fmt(a), a[-1]
    image = s["refs"].get(target, target)
    out = s["labels"].get(image, "") if "Labels" in f else image
elif a[0] == "pull":
    if s.get("offline"): code = 1
    else: s["refs"][a[-1]] = s["remote"][a[-1]]
elif a[0] == "tag":
    s["refs"][a[2]] = a[1]
elif a[0] == "exec":
    s["reports"].append(a[a.index("record") + 1:])
json.dump(s, open(p, "w"))
print(out); sys.exit(code)
'''
MIDCLT = r'''#!/usr/bin/env python3
import json, os, sys
p = os.environ["STUB_STATE"]; s = json.load(open(p))
s["log"].append("midclt " + " ".join(sys.argv[1:]))
s["container"]["image"] = s["refs"][s["container"]["ref"]]
json.dump(s, open(p, "w"))
'''


def run_script(tmp_path, *, ref=REF, remote="sha256:new", health=None, labels=None, offline=False):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    for name, body in (("docker", DOCKER), ("midclt", MIDCLT)):
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    state = tmp_path / "state.json"
    if not state.exists():
        state.write_text(json.dumps({
            "container": {"name": "ix-mygarage-mygarage-1", "ref": ref, "image": "sha256:old"},
            "refs": {ref: "sha256:old"}, "remote": {ref: remote},
            "labels": labels or {"sha256:old": "1.2.0", "sha256:new": "1.3.0"},
            "health": health if health is not None else {"sha256:old": "healthy", "sha256:new": "healthy"},
            "offline": offline, "log": [], "reports": []}))
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "STUB_STATE": str(state),
           "MYGARAGE_UPGRADE_POLL": "0", "MYGARAGE_UPGRADE_WAIT": "1",
           "MYGARAGE_UPGRADE_STATE": str(tmp_path)}
    r = subprocess.run(["sh", str(SCRIPT), "mygarage"], env=env, capture_output=True, text=True)
    s = json.loads(state.read_text())
    s["state_file"] = state
    return r, s


needs_sh = pytest.mark.skipif(not shutil.which("sh"), reason="needs a POSIX shell")


@needs_sh
def test_script_nothing_new(tmp_path):
    r, s = run_script(tmp_path, remote="sha256:old")
    assert r.returncode == 0 and "is current" in r.stdout
    assert not any(l.startswith("midclt") for l in s["log"])
    assert s["reports"] == [["--result", "current", "--from", "1.2.0"]]


@needs_sh
def test_script_upgrades_and_reports(tmp_path):
    r, s = run_script(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "midclt call -j app.redeploy mygarage" in s["log"]
    assert s["container"]["image"] == "sha256:new"
    assert s["reports"][-1] == ["--result", "upgraded", "--from", "1.2.0", "--to", "1.3.0"]
    assert "rmi sha256:old" in s["log"]


@needs_sh
def test_script_rolls_back_an_unhealthy_upgrade_and_skips_it_next_night(tmp_path):
    r, s = run_script(tmp_path, health={"sha256:old": "healthy", "sha256:new": "unhealthy"})
    assert r.returncode == 1 and "rolling back" in r.stderr
    assert s["container"]["image"] == "sha256:old" and s["refs"][REF] == "sha256:old"
    assert s["reports"][-1][:2] == ["--result", "rolled_back"]
    assert (tmp_path / ".mygarage-auto-upgrade-mygarage.failed").read_text().strip() == "sha256:new"
    redeploys = sum(l.startswith("midclt") for l in s["log"])
    r, s = run_script(tmp_path)                       # the next night: same broken image
    assert r.returncode == 0 and "failed before" in r.stdout
    assert sum(l.startswith("midclt") for l in s["log"]) == redeploys
    assert s["refs"][REF] == "sha256:old"


@needs_sh
def test_script_never_crosses_a_major_version(tmp_path):
    r, s = run_script(tmp_path, ref="ghcr.io/jt87/mygarage:latest",
                      labels={"sha256:old": "1.4.0", "sha256:new": "2.0.0"})
    assert r.returncode == 0 and "needs you to update" in r.stdout
    assert not any(l.startswith("midclt") for l in s["log"])
    assert s["refs"]["ghcr.io/jt87/mygarage:latest"] == "sha256:old"
    assert s["reports"][-1][:2] == ["--result", "major"]


@needs_sh
@pytest.mark.parametrize("tag", ["1.2.0", "sha-9c82cf5"])
def test_script_leaves_a_pinned_image_alone(tmp_path, tag):
    r, s = run_script(tmp_path, ref=f"ghcr.io/jt87/mygarage:{tag}")
    assert r.returncode == 0 and "pinned" in r.stdout
    assert not any(l.startswith(("pull", "midclt")) for l in s["log"])
    assert s["reports"][-1][:2] == ["--result", "pinned"]


@needs_sh
def test_script_offline_is_an_error_for_the_cron_email(tmp_path):
    r, s = run_script(tmp_path, offline=True)
    assert r.returncode == 1 and "could not pull" in r.stderr
