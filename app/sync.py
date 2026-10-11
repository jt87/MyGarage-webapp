"""Keep a local copy of the repo up to date without leaning on GitHub.

The clone on disk (REPO_PATH) is the server's cache: every page is rendered
from it, never from GitHub directly. Updates arrive three ways, cheapest first:

  1. GitHub push webhook  -> pull right away (one fetch per real commit).
  2. Page views           -> if the last check is older than FRESHNESS_SECONDS
                             (default 5 min), one `git ls-remote` first.
  3. Background loop      -> the same check every SYNC_INTERVAL (default 30 min).

`ls-remote` is a single small HTTPS request that returns the branch tip; a
`fetch` only happens when that tip has moved. Worst case is ~340 checks a
day, far inside GitHub's free limits. A hard cap (MAX_CHECKS_PER_HOUR) backs
this up, and the last check time is saved to disk so restarts don't cause a
burst of checks.

GIT_TOKEN (a read-only GitHub token) is sent as an HTTP header so it never
lands in .git/config. With GIT_REPO_URL unset, REPO_PATH is a folder you
mounted yourself and is re-read whenever a file in it changes.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("garage.sync")

MAX_CHECKS_PER_HOUR = 60


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class RepoSync:
    def __init__(self, path: Path, url: str = "", branch: str = "main", token: str = "",
                 interval: int = 1800, state_file: Path | None = None):
        self.path = path
        self.url = url
        self.branch = branch
        self.token = token
        self.interval = max(60, interval)
        self.state_file = state_file
        self.head = ""
        self.last_error = ""
        self.last_checked: datetime | None = None   # last time GitHub was asked
        self.last_changed: datetime | None = None   # last time new content arrived
        self._checked_at = 0.0                      # wall clock, persisted
        self._recent: deque[float] = deque()        # check times in the last hour
        self.checks_today = 0
        self.fetches_today = 0
        self._day = ""
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._load_state()

    @property
    def git_mode(self) -> bool:
        return bool(self.url)

    # ------------------------------------------------------------------ state

    def _load_state(self) -> None:
        if self.git_mode and (self.path / ".git").exists():
            try:
                self.head = self._git("rev-parse", "HEAD", cwd=self.path)
            except Exception:
                pass
        if not self.state_file or not self.state_file.exists():
            return
        try:
            s = json.loads(self.state_file.read_text())
            self._checked_at = float(s.get("checked_at") or 0)
            if self._checked_at:
                self.last_checked = datetime.fromtimestamp(self._checked_at, timezone.utc)
            if s.get("changed_at"):
                self.last_changed = datetime.fromisoformat(s["changed_at"])
            if s.get("day") == _today():
                self._day = s["day"]
                self.checks_today = int(s.get("checks", 0))
                self.fetches_today = int(s.get("fetches", 0))
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "head": self.head, "checked_at": self._checked_at,
                "changed_at": self.last_changed.isoformat() if self.last_changed else None,
                "day": self._day, "checks": self.checks_today, "fetches": self.fetches_today,
            }))
            tmp.replace(self.state_file)
        except OSError as e:
            log.warning("could not save sync state: %s", e)

    def _count(self, kind: str) -> None:
        if self._day != _today():
            self._day, self.checks_today, self.fetches_today = _today(), 0, 0
        if kind == "check":
            self.checks_today += 1
        else:
            self.fetches_today += 1

    # ------------------------------------------------------------------ git

    def _git(self, *args: str, cwd: Path | None = None, timeout: int = 600) -> str:
        cmd = ["git", "-c", "advice.detachedHead=false"]
        if self.token:
            cred = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            cmd += ["-c", f"http.extraHeader=Authorization: Basic {cred}"]
        cmd += list(args)
        # safe.directory via env so git's own subprocesses see it too: the
        # data volume is often owned by a different uid than the container user.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "HOME": os.environ.get("HOME", "/tmp"),
               "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
               "GIT_CONFIG_VALUE_0": "*"}
        res = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                             timeout=timeout)
        if res.returncode != 0:
            msg = (res.stderr or res.stdout).strip()
            if self.token:
                msg = msg.replace(self.token, "***")
            lines = msg.splitlines()
            fatal = next((l for l in lines if l.startswith(("fatal:", "error:"))), "")
            raise RuntimeError(fatal or (lines[-1] if lines else f"git {args[0]} failed"))
        return res.stdout.strip()

    def sync_once(self) -> bool:
        """Clone or fetch the branch tip. Returns True if HEAD changed."""
        if not self.git_mode:
            return False
        with self._lock:
            try:
                if not (self.path / ".git").exists():
                    self.path.mkdir(parents=True, exist_ok=True)
                    if any(self.path.iterdir()):
                        raise RuntimeError(f"{self.path} is not empty and not a git clone")
                    log.info("cloning %s (%s)", self.url, self.branch)
                    self._git("clone", "--depth", "1", "--branch", self.branch, self.url,
                              str(self.path))
                else:
                    self._git("fetch", "--depth", "1", "origin", self.branch, cwd=self.path)
                    self._git("reset", "--hard", "FETCH_HEAD", cwd=self.path)
                    self._git("clean", "-fdq", cwd=self.path)
                self._count("fetch")
                head = self._git("rev-parse", "HEAD", cwd=self.path)
                changed = head != self.head
                self.head = head
                now = datetime.now(timezone.utc)
                self.last_checked = now
                self._checked_at = time.time()
                if changed:
                    self.last_changed = now
                    log.info("repo at %s", head[:7])
                self.last_error = ""
                return changed
            except Exception as e:  # keep serving the cached copy
                self.last_error = str(e)
                log.warning("sync failed: %s", e)
                return False
            finally:
                self._save_state()

    def ensure_fresh(self, max_age: float) -> bool:
        """Pull if the last check is older than `max_age` seconds and the
        remote branch moved. Returns True if HEAD changed.

        Concurrent callers wait for the one check in flight. If GitHub can't
        be reached, the cached copy keeps being served.
        """
        if not self.git_mode or time.time() - self._checked_at < max_age:
            return False
        with self._lock:
            now = time.time()
            if now - self._checked_at < max_age:
                return False
            while self._recent and now - self._recent[0] > 3600:
                self._recent.popleft()
            if len(self._recent) >= MAX_CHECKS_PER_HOUR:
                return False
            self._recent.append(now)
            self._checked_at = now
            self._count("check")
            remote = ""
            try:
                out = self._git("ls-remote", self.url, f"refs/heads/{self.branch}", timeout=10)
                remote = out.split()[0] if out else ""
                self.last_checked = datetime.now(timezone.utc)
                self.last_error = "" if remote else f"branch {self.branch} not found on GitHub"
            except Exception as e:
                self.last_error = str(e)
                log.warning("remote check failed: %s", e)
            finally:
                self._save_state()
        if remote and remote != self.head:
            return self.sync_once()
        return False

    # ------------------------------------------------------------------ loop

    def trigger(self) -> None:
        self._wake.set()

    def start(self, on_change) -> None:
        if not self.git_mode:
            return

        def loop():
            while True:
                if not (self.path / ".git").exists():
                    changed = self.sync_once()
                else:
                    changed = self.ensure_fresh(self.interval)
                if changed:
                    on_change()
                self._wake.wait(60)
                self._wake.clear()

        threading.Thread(target=loop, name="repo-sync", daemon=True).start()

    def fingerprint(self) -> str:
        """Cheap change detector for mounted (non-git) mode."""
        if self.git_mode:
            return self.head
        latest, count = 0.0, 0
        for d in ("", "tools", "projects", "reference"):
            base = self.path / d
            if not base.is_dir():
                continue
            for p in (base.iterdir() if d == "" else base.rglob("*")):
                try:
                    latest = max(latest, p.stat().st_mtime)
                    count += 1
                except OSError:
                    pass
        return f"{latest}:{count}"


class Watcher:
    """Re-run `loader` only when the repo's fingerprint changes."""

    def __init__(self, sync: RepoSync, loader, min_check: float = 2.0):
        self.sync = sync
        self.loader = loader
        self.min_check = min_check
        self._value = None
        self._fp = None
        self._checked = 0.0
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        self._checked = 0.0

    def get(self):
        now = time.monotonic()
        # In git mode the fingerprint is just HEAD, so check it every time.
        if (self._value is not None and not self.sync.git_mode
                and now - self._checked < self.min_check):
            return self._value
        with self._lock:
            fp = self.sync.fingerprint()
            self._checked = now
            if self._value is None or fp != self._fp:
                self._value = self.loader()
                self._fp = fp
            return self._value
