"""Versions: what's installed, what changed, and what's available.

Three small JSON files, each with one writer:

  config/version.json       this app: the installed version, when it changed,
                            and which notices have been dismissed
  config/auto-upgrade.json  deploy/auto-upgrade.sh, through `python -m
                            app.releases record` (the nightly TrueNAS Cron Job)
  cache/releases.json       this app: GitHub's release list, checked once a day

Release notes for installed versions come from the CHANGELOG.md bundled in
the image, so they work offline. Newer releases come from GitHub.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__

log = logging.getLogger("garage.releases")

HERE = Path(__file__).parent
# /srv/CHANGELOG.md in the image, the repo root in a checkout.
CHANGELOG = HERE.parent / "CHANGELOG.md"
RELEASES_PAGE = "https://github.com/jt87/MyGarage-webapp/releases"
RELEASES_API = "https://api.github.com/repos/jt87/MyGarage-webapp/releases?per_page=30"
CHECK_EVERY = 24 * 3600
RETRY_AFTER = 3600
# The Cron Job runs nightly; after this long without a word it's reported as not running.
AUTO_STALE = timedelta(days=3)


def parse(version: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (version or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


@dataclass
class Release:
    version: str
    date: str          # "2026-10-16", or "" when unknown
    notes: str         # Markdown
    url: str

    @property
    def key(self) -> tuple[int, int, int]:
        return parse(self.version) or (0, 0, 0)

    @property
    def upgrading(self) -> str:
        """The release's "Upgrading from …" section: what someone has to do."""
        m = re.search(r"^#{2,4} Upgrading[^\n]*\n(.*?)(?=^#{1,4} |\Z)", self.notes, re.M | re.S)
        return m.group(1).strip() if m else ""


def tag_url(version: str) -> str:
    return f"{RELEASES_PAGE}/tag/v{version}"


def changelog(path: Path = CHANGELOG) -> list[Release]:
    """Every version in the bundled CHANGELOG.md, newest first."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for m in re.finditer(r"^## (\d+\.\d+\.\d+)([^\n]*)\n(.*?)(?=^## |\Z)", text, re.M | re.S):
        date = re.search(r"\d{4}-\d{2}-\d{2}", m.group(2))
        out.append(Release(m.group(1), date.group(0) if date else "", m.group(3).strip(),
                           tag_url(m.group(1))))
    return sorted(out, key=lambda r: r.key, reverse=True)


def fetch_github(url: str = RELEASES_API) -> list[Release]:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"MyGarage/{__version__}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        items = json.load(resp)
    out = []
    for r in items:
        version = (r.get("tag_name") or "").lstrip("v")
        if r.get("draft") or r.get("prerelease") or not parse(version):
            continue
        out.append(Release(version, (r.get("published_at") or "")[:10], r.get("body") or "",
                           r.get("html_url") or tag_url(version)))
    return out


def auto_path(config_dir: Path) -> Path:
    return config_dir / "auto-upgrade.json"


class Versions:
    def __init__(self, config_dir: Path, cache_dir: Path, current: str = __version__,
                 fetch=fetch_github, check: bool = True, changelog_path: Path = CHANGELOG):
        self.path = config_dir / "version.json"
        self.auto_file = auto_path(config_dir)
        self.cache = cache_dir / "releases.json"
        self.current = current
        self.fetch = fetch
        self.check_enabled = check
        self.changelog_path = changelog_path
        self.data = _read(self.path)
        self._lock = threading.Lock()
        self._checking = False

    # ------------------------------------------------------------------ installed version

    def startup(self, existing_install: bool) -> None:
        """Notice a new version (an upgrade, overnight or by hand) and remember it."""
        before = self.data.get("current")
        if before == self.current:
            return
        if before is None and not existing_install:
            # A brand-new install has nothing to announce.
            self.data.update(current=self.current, previous=None,
                             installed_at=now().isoformat(), whats_new_seen=True)
        else:
            # `before` is None for an upgrade from 1.1, which didn't keep this file.
            self.data.update(current=self.current, previous=before,
                             installed_at=now().isoformat(), whats_new_seen=False)
        self._save()

    def _save(self) -> None:
        try:
            _write(self.path, self.data)
        except OSError as e:
            log.warning("could not save %s: %s", self.path, e)

    @property
    def installed_at(self) -> datetime | None:
        return _time(self.data.get("installed_at"))

    @property
    def previous(self) -> str | None:
        return self.data.get("previous")

    def mark_whats_new_seen(self) -> None:
        if not self.data.get("whats_new_seen"):
            self.data["whats_new_seen"] = True
            self._save()

    def dismiss_major(self, version: str) -> None:
        if parse(version):
            self.data["dismissed_major"] = version
            self._save()

    # ------------------------------------------------------------------ automatic upgrades

    def auto(self) -> dict:
        """The Cron Job's last report, with `state`: off, stale, ok, failed, pinned or blocked."""
        rec = _read(self.auto_file)
        at = _time(rec.get("at"))
        if not at:
            return {"state": "off"}
        rec["at"] = at
        if now() - at > AUTO_STALE:
            rec["state"] = "stale"
        else:
            rec["state"] = {"rolled_back": "failed", "pinned": "pinned",
                            "major": "blocked"}.get(rec.get("result"), "ok")
        return rec

    @property
    def upgraded_overnight(self) -> bool:
        rec = _read(self.auto_file)
        return rec.get("result") == "upgraded" and rec.get("to") == self.current

    # ------------------------------------------------------------------ release notes

    def installed_notes(self) -> list[Release]:
        """Notes for this version and every earlier one."""
        return [r for r in changelog(self.changelog_path) if r.key <= (parse(self.current) or (0, 0, 0))]

    def newer(self) -> list[Release]:
        """Releases on GitHub newer than this one, newest first."""
        mine = parse(self.current)
        if not mine:
            return []
        out = [Release(**r) for r in _read(self.cache).get("releases", [])
               if isinstance(r, dict) and (parse(r.get("version", "")) or (0, 0, 0)) > mine]
        return sorted(out, key=lambda r: r.key, reverse=True)

    def same_major_update(self) -> Release | None:
        mine = parse(self.current)
        return next((r for r in self.newer() if mine and r.key[0] == mine[0]), None)

    def major_update(self) -> Release | None:
        mine = parse(self.current)
        return next((r for r in self.newer() if mine and r.key[0] > mine[0]), None)

    def major_popup(self) -> Release | None:
        """A newer major version not yet dismissed: shown once, on every device."""
        r = self.major_update()
        return r if r and r.version != self.data.get("dismissed_major") else None

    @property
    def last_check(self) -> dict:
        c = _read(self.cache)
        return {"at": _time(c.get("checked_at")), "error": c.get("error")}

    # ------------------------------------------------------------------ checking GitHub

    def due(self) -> bool:
        if not self.check_enabled:
            return False
        c = _read(self.cache)
        at = _time(c.get("checked_at"))
        wait = RETRY_AFTER if c.get("error") else CHECK_EVERY
        return not at or (now() - at).total_seconds() > wait

    def maybe_check(self) -> None:
        """Check GitHub in the background when a day has passed. Never blocks a page."""
        if not self.due():
            return
        with self._lock:
            if self._checking:
                return
            self._checking = True
        threading.Thread(target=self.check_now, daemon=True, name="release-check").start()

    def check_now(self) -> None:
        old = _read(self.cache)
        try:
            releases = self.fetch()
            data = {"checked_at": now().isoformat(),
                    "releases": [r.__dict__ for r in releases]}
        except Exception as e:  # offline, rate limited, GitHub down: try again later
            log.info("could not check for new releases: %s", e)
            data = {**old, "checked_at": now().isoformat(), "error": str(e)[:200]}
        try:
            _write(self.cache, data)
        except OSError as e:
            log.warning("could not save %s: %s", self.cache, e)
        finally:
            with self._lock:
                self._checking = False


# ---------------------------------------------------------------------- CLI (for the Cron Job)

def record(config_dir: Path, result: str, frm: str = "", to: str = "", detail: str = "") -> None:
    _write(auto_path(config_dir), {"at": now().isoformat(), "result": result,
                                   "from": frm, "to": to, "detail": detail[:300]})


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m app.releases")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version", help="print the installed version")
    r = sub.add_parser("record", help="record the result of an automatic upgrade check")
    r.add_argument("--result", required=True,
                   choices=["current", "upgraded", "rolled_back", "pinned", "major"])
    r.add_argument("--from", dest="frm", default="")
    r.add_argument("--to", default="")
    r.add_argument("--detail", default="")
    args = p.parse_args(argv)
    if args.cmd == "version":
        print(__version__)
        return 0
    config_dir = Path(os.environ.get("CONFIG_PATH", "/data/config/auth.json")).parent
    record(config_dir, args.result, args.frm, args.to, args.detail)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
