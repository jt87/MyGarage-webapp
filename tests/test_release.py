"""Release plumbing: version, changelog notes, and the tag check."""

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("release_notes", ROOT / "scripts/release_notes.py")
rn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rn)


def test_version_is_semver_and_in_changelog():
    from app import __version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    heading, body = rn.section(__version__)
    assert body.strip(), "the current version needs notes in CHANGELOG.md"


def test_tag_check(capsys):
    from app import __version__
    assert rn.main(["--check", "0.0.1"]) == 1
    assert "tag says" in capsys.readouterr().out
    assert rn.main(["--check", "v1"]) == 2


def test_version_is_shown_when_logged_in():
    from test_app import client
    from app import __version__
    status = client.post("/sync", headers={"Accept": "application/json"}).json()
    assert status["version"] == __version__
    assert __version__ in client.get("/settings").text


def test_catalog_app_tracks_the_release():
    """The TrueNAS catalog package must ship the current version's image."""
    import yaml
    from app import __version__
    base = ROOT / "deploy/truenas-catalog/mygarage"
    app = yaml.safe_load((base / "app.yaml").read_text())
    values = yaml.safe_load((base / "ix_values.yaml").read_text())
    assert app["app_version"] == __version__
    assert values["images"]["image"]["tag"].split("@")[0] == __version__
    assert values["images"]["image"]["repository"] == "ghcr.io/jt87/mygarage"
