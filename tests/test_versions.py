"""1.2: the installed version, What's New, update notices, and the overnight upgrade script."""

import json
from datetime import timedelta

import pytest

from app import releases
from app.releases import Release, Versions


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


def test_update_banner_until_dismissed_and_back_for_the_next_release(tmp_path):
    v = make(tmp_path, releases_=[rel("1.3.0")])
    v.check_now()
    assert v.update_banner().version == "1.3.0"
    v.dismiss_update("1.3.0")
    assert v.update_banner() is None and v.same_major_update()
    v.fetch = lambda: [rel("1.4.0"), rel("1.3.0")]
    v.check_now()
    assert v.update_banner().version == "1.4.0"


# ---------------------------------------------------------------- in the app

@pytest.fixture
def app_versions():
    from test_app import client
    from app import main
    v = main.versions
    saved = dict(v.data)
    v.cache.unlink(missing_ok=True)
    yield client, v
    v.data = saved
    v.cache.unlink(missing_ok=True)


def seed(v, *versions_):
    releases._write(v.cache, {"checked_at": releases.now().isoformat(),
                              "releases": [rel(x, "### Upgrading from 1.x\n- Do **this**.").__dict__
                                           for x in versions_]})


def test_settings_about_section(app_versions):
    client, v = app_versions
    page = client.get("/settings").text
    assert 'id="about"' in page and "choose <strong>Update</strong>" in page
    seed(v, "1.99.0")
    assert "1.99.0 is available" in client.get("/settings").text


def test_updated_banner_until_dismissed(app_versions):
    client, v = app_versions
    v.data["whats_new_seen"] = False
    assert f"Updated to {v.current}." in client.get("/tools").text
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


def test_available_banner_says_how_to_update(app_versions):
    client, v = app_versions
    seed(v, "1.99.0")
    page = client.get("/tools").text
    assert "1.99.0 is available. To update, open it in TrueNAS Apps and choose Update." in page
    client.post("/settings/updates", data={"action": "dismiss_update", "version": "1.99.0", "next": "/tools"})
    assert "1.99.0 is available" not in client.get("/tools").text
    assert "1.99.0 is available" in client.get("/settings").text   # About still says so


def test_major_version_popup(app_versions):
    client, v = app_versions
    seed(v, "9.0.0")
    page = client.get("/").text
    assert "data-major-update" in page and "9.0.0 Is Available" in page
    assert "<strong>this</strong>" in page                  # its upgrade steps, as Markdown
    assert "needs a step from you first" in client.get("/settings").text
    client.post("/settings/updates", data={"action": "dismiss_major", "version": "9.0.0", "next": "//evil"})
    assert "data-major-update" not in client.get("/").text
    assert "9.0.0 is available" in client.get("/settings").text   # About still says so


def test_dismiss_redirect_stays_on_this_site(app_versions):
    client, _ = app_versions
    r = client.post("/settings/updates", data={"action": "seen", "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"
