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

Edits made in the app are committed and pushed by `write`, under the same
lock, so a pull can never wipe a commit that hasn't reached GitHub yet.

GIT_TOKEN (a GitHub token; Contents read-only, or read and write for editing)
is sent as an HTTP header so it never lands in .git/config. With GIT_REPO_URL unset, REPO_PATH is a folder you
mounted yourself and is re-read whenever a file in it changes.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("garage.sync")

MAX_CHECKS_PER_HOUR = 60
COMMIT_EMAIL = os.environ.get("GIT_AUTHOR_EMAIL", "mygarage@users.noreply.github.com")


class WriteError(Exception):
    """A change couldn't be saved. `retry` is True when trying later may work."""

    def __init__(self, message: str, retry: bool = False):
        super().__init__(message)
        self.retry = retry


def is_network_error(msg: str) -> bool:
    msg = msg.lower()
    return any(s in msg for s in ("could not resolve", "timed out", "failed to connect",
                                  "connection reset", "network is unreachable",
                                  "operation timed out", "unable to access", "early eof",
                                  "502", "503", "504")) and not any(
        s in msg for s in ("403", "401", "permission", "denied"))


def friendly_git_error(msg: str) -> str:
    low = msg.lower()
    if any(s in low for s in ("403", "permission", "denied", "write access")):
        return ("GitHub refused the change. Give the GIT_TOKEN fine-grained token "
                "Contents: Read and write access to your data repo.")
    if "protected branch" in low or "gh006" in low:
        return "GitHub refused the change because the branch is protected."
    if "large" in low and ("file" in low or "gh001" in low):
        return "GitHub refused the change because a file is too large (100 MB limit)."
    if is_network_error(msg):
        return "Can't reach GitHub right now."
    return f"Couldn't save to GitHub: {msg}"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class RepoSync:
    def __init__(self, path: Path, url: str = "", branch: str = "main", token: str = "",
                 interval: int = 1800, state_file: Path | None = None, mode: str | None = None):
        self.path = path
        # github: GitHub is the source and this is a cache of it.
        # server: this server's own git repo is the source; `url`, if set, is a
        #         GitHub repo it backs up to (and takes edits made there from).
        # folder: a plain folder you manage yourself; no history.
        # "":     not set up yet.
        self.mode = mode if mode is not None else ("github" if url else "folder")
        self.url = url
        self.branch = branch
        self.token = token
        self.interval = max(60, interval)
        self.state_file = state_file
        self.head = ""
        self.last_error = ""
        self.last_checked: datetime | None = None   # last time GitHub was asked
        self.last_changed: datetime | None = None   # last time new content arrived
        self.backup_error = ""                      # server mode: why the last backup failed
        self.last_backup: datetime | None = None    # server mode: last time GitHub matched
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
        """GitHub is the source of truth (the server keeps a cache)."""
        return self.mode == "github"

    @property
    def remote(self) -> bool:
        """There's a GitHub repo to check: the source, or a backup."""
        return bool(self.url) and self.mode in ("github", "server")

    @property
    def versioned(self) -> bool:
        return self.mode in ("github", "server")

    @property
    def configured(self) -> bool:
        return bool(self.mode)

    def configure(self, mode: str, url: str = "", token: str = "", branch: str = "") -> None:
        with self._lock:
            self.mode, self.url, self.token = mode, url, token
            self.branch = branch or self.branch
            self.last_error = self.backup_error = ""
            self._checked_at = 0.0
            self.head = self._head_quietly()
        self.trigger()

    def _head_quietly(self) -> str:
        if not (self.path / ".git").exists():
            return ""
        try:
            return self._git("rev-parse", "HEAD", cwd=self.path)
        except Exception:
            return ""

    # ------------------------------------------------------------------ state

    def _load_state(self) -> None:
        if self.versioned:
            self.head = self._head_quietly()
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

    def _git(self, *args: str, cwd: Path | None = None, timeout: int = 600,
             ok: tuple[int, ...] = (0,), want_code: bool = False, token: str | None = None):
        token = self.token if token is None else token
        cmd = ["git", "-c", "advice.detachedHead=false"]
        if token:
            cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            cmd += ["-c", f"http.extraHeader=Authorization: Basic {cred}"]
        cmd += list(args)
        # safe.directory via env so git's own subprocesses see it too: the
        # data volume is often owned by a different uid than the container user.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "HOME": os.environ.get("HOME", "/tmp"),
               "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
               "GIT_CONFIG_VALUE_0": "*"}
        res = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                             timeout=timeout)
        if want_code and (res.returncode == 0 or res.returncode in ok):
            return res.returncode
        if res.returncode in ok and res.returncode != 0:
            return ""
        if res.returncode != 0:
            msg = (res.stderr or res.stdout).strip()
            if token:
                msg = msg.replace(token, "***")
            lines = msg.splitlines()
            fatal = next((l for l in lines if l.startswith(("fatal:", "error:"))), "")
            err = RuntimeError(fatal or (lines[-1] if lines else f"git {args[0]} failed"))
            err.detail = msg  # all of git's output, for telling rejections apart
            raise err
        return res.stdout.strip()

    def sync_once(self) -> bool:
        """Clone or fetch the branch tip. Returns True if HEAD changed."""
        if not self.git_mode:
            return self.sync_backup() if self.remote else False
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
                    # Fetch from GIT_REPO_URL, not the clone's "origin", so
                    # pointing the app at a different repo takes effect.
                    self._git("fetch", "--depth", "1", self.url, self.branch, cwd=self.path)
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
        if not self.remote or time.time() - self._checked_at < max_age:
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
                if self.git_mode:
                    self.last_error = "" if remote else f"branch {self.branch} not found on GitHub"
                else:
                    self.backup_error = ""
            except Exception as e:
                if self.git_mode:
                    self.last_error = str(e)
                else:
                    self.backup_error = friendly_git_error(getattr(e, "detail", str(e)))
                log.warning("remote check failed: %s", e)
                return False
            finally:
                self._save_state()
        if self.git_mode:
            return self.sync_once() if remote and remote != self.head else False
        if remote == self.head:
            self.last_backup = datetime.now(timezone.utc)
            return False
        return self.sync_backup()  # GitHub has edits, or this server has unsent ones

    # ------------------------------------------------------------------ writes

    def write(self, mutate, message: str, author: str = "MyGarage") -> bool:
        """Apply `mutate(root) -> [changed paths]` to the latest copy and publish it.

        In git mode this fetches the branch tip, applies the change, commits
        and pushes, all under the sync lock so a pull can't wipe the commit
        before it reaches GitHub. If someone pushed in between, it starts over
        from their commit (the edit is re-applied, not merged by git). Any
        failure resets the clone to GitHub's copy. Returns True if anything
        changed. Raises WriteError, or whatever `mutate` raises.
        """
        with self._lock:
            if self.mode == "folder":
                return bool(mutate(self.path))
            if self.mode == "server":
                return self._write_local(mutate, message, author)
            for attempt in range(3):
                try:
                    self._git("fetch", "--depth", "1", self.url, self.branch, cwd=self.path)
                    self._count("fetch")
                    self._git("reset", "--hard", "FETCH_HEAD", cwd=self.path)
                    self._git("clean", "-fdq", cwd=self.path)
                    self.head = self._git("rev-parse", "HEAD", cwd=self.path)
                except RuntimeError as e:
                    self.last_error = str(e)
                    detail = getattr(e, "detail", str(e))
                    raise WriteError(friendly_git_error(detail),
                                     retry="403" not in detail and "401" not in detail) from e
                try:
                    paths = mutate(self.path)
                    if not paths:
                        return False
                    self._git("add", "-A", "--", *paths, cwd=self.path)
                    if not self._git("status", "--porcelain", "--", *paths, cwd=self.path):
                        return False
                    self._git("-c", f"user.name={author}", "-c", f"user.email={COMMIT_EMAIL}",
                              "commit", "-q", "-m", message, cwd=self.path)
                    self._git("push", "-q", self.url, f"HEAD:refs/heads/{self.branch}",
                              cwd=self.path, timeout=300)
                except BaseException as e:
                    self._reset_quietly()
                    if isinstance(e, RuntimeError):
                        detail = getattr(e, "detail", str(e))
                        rejected = any(s in detail for s in ("[rejected]", "fetch first",
                                                             "non-fast-forward", "stale info"))
                        if rejected and attempt < 2:
                            continue  # someone else pushed first: redo on top of theirs
                        raise WriteError(friendly_git_error(detail),
                                         retry=is_network_error(detail)) from e
                    raise
                self.head = self._git("rev-parse", "HEAD", cwd=self.path)
                now = datetime.now(timezone.utc)
                self.last_checked = self.last_changed = now
                self._checked_at = time.time()
                self.last_error = ""
                self._save_state()
                log.info("pushed %s: %s", self.head[:7], message.splitlines()[0])
                return True
            raise WriteError("GitHub kept changing while saving. Try again.", retry=True)

    def _commit(self, paths: list[str], message: str, author: str) -> bool:
        self._git("add", "-A", "--", *paths, cwd=self.path)
        if not self._git("status", "--porcelain", "--", *paths, cwd=self.path):
            return False
        self._git("-c", f"user.name={author}", "-c", f"user.email={COMMIT_EMAIL}",
                  "commit", "-q", "-m", message, cwd=self.path)
        return True

    def _write_local(self, mutate, message: str, author: str) -> bool:
        """Server mode: commit here; the GitHub backup, if any, follows."""
        try:
            paths = mutate(self.path)
            if not paths or not self._commit(paths, message, author):
                self._reset_quietly("HEAD")
                return False
        except BaseException:
            self._reset_quietly("HEAD")
            raise
        self.head = self._git("rev-parse", "HEAD", cwd=self.path)
        self.last_changed = datetime.now(timezone.utc)
        self._save_state()
        log.info("committed %s: %s", self.head[:7], message.splitlines()[0])
        if self.url:
            self._push_backup()
        return True

    # ------------------------------------------------------------------ server mode

    def init_local(self, starter: Path | None = None) -> None:
        """Start (or adopt) this server's own repo, optionally from a starter folder."""
        with self._lock:
            self.path.mkdir(parents=True, exist_ok=True)
            fresh = not (self.path / ".git").exists()
            if fresh:
                self._git("init", "-q", "-b", self.branch, cwd=self.path)
            seeded = bool(starter) and not (self.path / "README.md").exists()
            if seeded:
                shutil.copytree(starter, self.path, dirs_exist_ok=True)
            if not self._git("status", "--porcelain", cwd=self.path) and not fresh:
                self.head = self._head_quietly()
                return
            self._git("add", "-A", cwd=self.path)
            msg = "Start MyGarage" if seeded else "Add existing files"
            self._git("-c", "user.name=MyGarage", "-c", f"user.email={COMMIT_EMAIL}",
                      "commit", "-q", "--allow-empty", "-m", msg, cwd=self.path)
            self.head = self._git("rev-parse", "HEAD", cwd=self.path)
            self.last_changed = datetime.now(timezone.utc)
            self._save_state()

    def seed_remote(self, starter: Path | None) -> None:
        """Fill an empty GitHub repo from a starter, so GitHub mode can begin there."""
        self.init_local(starter)
        with self._lock:
            try:
                self._git("push", "-q", self.url, f"HEAD:refs/heads/{self.branch}",
                          cwd=self.path, timeout=300)
            except RuntimeError as e:
                raise WriteError(friendly_git_error(getattr(e, "detail", str(e)))) from e

    def _push_backup(self) -> bool:
        """Best effort: a failed backup never loses or blocks an edit."""
        try:
            self._git("push", "-q", self.url, f"HEAD:refs/heads/{self.branch}",
                      cwd=self.path, timeout=300)
        except RuntimeError as e:
            detail = getattr(e, "detail", str(e))
            if any(s in detail for s in ("[rejected]", "fetch first", "non-fast-forward")):
                self.backup_error = "Waiting to bring in changes made on GitHub."
                self.trigger()
            else:
                self.backup_error = friendly_git_error(detail)
            log.warning("backup push failed: %s", e)
            return False
        self.backup_error = ""
        self.last_backup = datetime.now(timezone.utc)
        return True

    def sync_backup(self) -> bool:
        """Server mode: take edits made on GitHub and send this server's.

        GitHub's commits are fast-forwarded in, or this server's unsent
        commits are replayed on top of them. If both changed the same lines,
        nothing here is touched and the backup pauses with a message.
        Returns True if this server's files changed.
        """
        if self.mode != "server" or not self.url:
            return False
        with self._lock:
            try:
                remote = self._git("ls-remote", self.url, f"refs/heads/{self.branch}", timeout=30)
                if not remote:  # an empty GitHub repo: send everything
                    self._push_backup()
                    return False
                self._git("fetch", "-q", self.url, self.branch, cwd=self.path)
                self._count("fetch")
                theirs = self._git("rev-parse", "FETCH_HEAD", cwd=self.path)
                head = self._git("rev-parse", "HEAD", cwd=self.path)
                changed = False
                if theirs == head:
                    pass
                elif self._is_ancestor(theirs, head):
                    pass                     # only this server has new commits
                elif self._is_ancestor(head, theirs):
                    self._git("merge", "-q", "--ff-only", "FETCH_HEAD", cwd=self.path)
                    changed = True
                elif not self._git("merge-base", "HEAD", "FETCH_HEAD", cwd=self.path, ok=(1,)):
                    self.backup_error = ("That GitHub repo has different history. Connect an "
                                         "empty repo (don't add a README when creating it).")
                    return False
                else:
                    try:
                        self._git("-c", "user.name=MyGarage", "-c", f"user.email={COMMIT_EMAIL}",
                                  "rebase", "-q", "FETCH_HEAD", cwd=self.path)
                        changed = True
                    except RuntimeError:
                        self._git("rebase", "--abort", cwd=self.path, ok=(0, 128))
                        self.backup_error = ("This server and GitHub both changed the same "
                                             "part of a file. Backup is paused; this server's "
                                             "copy is kept. Undo the GitHub edit to resume.")
                        return False
                self.head = self._git("rev-parse", "HEAD", cwd=self.path)
                if self.head != theirs:
                    self._push_backup()
                else:
                    self.backup_error = ""
                    self.last_backup = datetime.now(timezone.utc)
                if changed:
                    self.last_changed = datetime.now(timezone.utc)
                    log.info("took GitHub edits; now at %s", self.head[:7])
                return changed
            except RuntimeError as e:
                self.backup_error = friendly_git_error(getattr(e, "detail", str(e)))
                log.warning("backup sync failed: %s", e)
                return False
            finally:
                self.last_checked = datetime.now(timezone.utc)
                self._checked_at = time.time()
                self._save_state()

    def _is_ancestor(self, a: str, b: str) -> bool:
        return self._git("merge-base", "--is-ancestor", a, b, cwd=self.path, ok=(1,),
                         want_code=True) == 0

    def check_remote(self, url: str, token: str, branch: str) -> str:
        """Can this repo be reached with this token? Returns its branch tip ("" if
        the branch doesn't exist yet). Raises WriteError with a readable reason."""
        try:
            out = self._git("ls-remote", url, f"refs/heads/{branch}", timeout=30, token=token)
            return out.split()[0] if out else ""
        except RuntimeError as e:
            detail = getattr(e, "detail", str(e)).lower()
            if any(s in detail for s in ("not found", "403", "401", "authentication",
                                         "could not read username")):
                raise WriteError("GitHub couldn't find that repo with this token. Check the "
                                 "address, and that the token can access the repo.") from e
            raise WriteError(friendly_git_error(getattr(e, "detail", str(e))),
                             retry=True) from e

    def _reset_quietly(self, to: str = "FETCH_HEAD") -> None:
        for args in (("reset", "--hard", to), ("clean", "-fdq")):
            try:
                self._git(*args, cwd=self.path)
            except RuntimeError as e:
                log.warning("could not reset after a failed write: %s", e)
            if to == "HEAD" and args[0] == "clean":
                break

    # ------------------------------------------------------------------ loop

    def trigger(self) -> None:
        self._wake.set()

    def start(self, on_change) -> None:
        """Background checks. Runs in every mode, so storage chosen later in
        the app (first-run setup, Settings) takes effect without a restart."""

        def loop():
            backoff = 60
            while True:
                if not self.remote:
                    changed, wait = False, 60
                elif self.git_mode and not (self.path / ".git").exists():
                    # No clone yet (bad token, GitHub down): retry, backing off to
                    # SYNC_INTERVAL so a misconfiguration can't hammer GitHub.
                    changed = self.sync_once()
                    wait = 60 if changed else backoff
                    backoff = 60 if changed else min(backoff * 2, self.interval)
                else:
                    changed, wait = self.ensure_fresh(self.interval), 60
                if changed:
                    on_change()
                self._wake.wait(wait)
                self._wake.clear()

        threading.Thread(target=loop, name="repo-sync", daemon=True).start()

    def fingerprint(self) -> str:
        """Cheap change detector: HEAD, or file times for a plain folder."""
        if self.versioned:
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
