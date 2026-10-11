"""Keep a local copy of the repo up to date.

Two modes:
  * GIT_REPO_URL set: clone into REPO_PATH and keep it at the branch tip.
    Every page view asks GitHub for the branch's latest commit (`git
    ls-remote`, at most once per FRESHNESS_SECONDS) and pulls before rendering
    if it moved. A GitHub webhook and a background SYNC_INTERVAL loop pull
    too. GIT_TOKEN (a read-only GitHub token) is sent as an HTTP header so it
    never lands in .git/config.
  * GIT_REPO_URL unset: REPO_PATH is a folder you mounted yourself; it's
    re-read whenever a file in it changes.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("garage.sync")


class RepoSync:
    def __init__(self, path: Path, url: str = "", branch: str = "main", token: str = "",
                 interval: int = 300):
        self.path = path
        self.url = url
        self.branch = branch
        self.token = token
        self.interval = max(30, interval)
        self.last_ok: datetime | None = None
        self.last_error = ""
        self.head = ""
        self.last_checked: datetime | None = None
        self._remote_checked = 0.0
        self._lock = threading.Lock()
        self._fresh_lock = threading.Lock()
        self._wake = threading.Event()

    @property
    def git_mode(self) -> bool:
        return bool(self.url)

    def _git(self, *args: str, cwd: Path | None = None) -> str:
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
        res = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
        if res.returncode != 0:
            msg = (res.stderr or res.stdout).strip()
            if self.token:
                msg = msg.replace(self.token, "***")
            lines = msg.splitlines()
            fatal = next((l for l in lines if l.startswith(("fatal:", "error:"))), "")
            raise RuntimeError(fatal or (lines[-1] if lines else f"git {args[0]} failed"))
        return res.stdout.strip()

    def sync_once(self) -> bool:
        """Pull the latest commit. Returns True if HEAD changed."""
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
                head = self._git("rev-parse", "HEAD", cwd=self.path)
                changed = head != self.head
                self.head = head
                self.last_ok = self.last_checked = datetime.now(timezone.utc)
                self._remote_checked = time.monotonic()
                self.last_error = ""
                if changed:
                    log.info("repo at %s", head[:7])
                return changed
            except Exception as e:  # keep serving the last good copy
                self.last_error = str(e)
                log.warning("sync failed: %s", e)
                return False

    def ensure_fresh(self, max_age: float) -> bool:
        """Pull if the remote branch moved. Returns True if HEAD changed.

        Costs one `git ls-remote` round trip at most every `max_age` seconds;
        concurrent callers wait for the one check in flight. If GitHub can't
        be reached, the last good copy keeps being served.
        """
        if not self.git_mode or time.monotonic() - self._remote_checked < max_age:
            return False
        with self._fresh_lock:
            if time.monotonic() - self._remote_checked < max_age:
                return False
            try:
                out = self._git("ls-remote", self.url, f"refs/heads/{self.branch}")
                remote = out.split()[0] if out else ""
            except Exception as e:
                self._remote_checked = time.monotonic()
                self.last_error = str(e)
                log.warning("remote check failed: %s", e)
                return False
            self._remote_checked = time.monotonic()
            self.last_checked = datetime.now(timezone.utc)
            if not remote:
                self.last_error = f"branch {self.branch} not found on remote"
                return False
            if remote != self.head:
                return self.sync_once()
            self.last_error = ""
            return False

    def trigger(self) -> None:
        self._wake.set()

    def start(self, on_change) -> None:
        if not self.git_mode:
            return

        def loop():  # the first sync already ran at startup
            while True:
                self._wake.wait(self.interval)
                self._wake.clear()
                if self.sync_once():
                    on_change()

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
