"""Where the garage data lives, chosen on the first-run screen (or in the YAML).

  server  This server keeps the data in its own git repo (history included).
          A GitHub repo can be connected later as an off-site backup.
  github  A GitHub repo is the source; the server keeps a copy of it.
  folder  A folder you manage yourself (the 1.0 "mounted folder" setup).

GIT_REPO_URL in the environment still wins and locks the choice to GitHub,
so existing installs keep working unchanged. Saved next to the login
settings in /data/config, readable only by the app (it can hold a token).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from pathlib import Path

log = logging.getLogger("garage.storage")

STARTERS = Path(__file__).parent / "starter"


class StorageConfig:
    def __init__(self, path: Path, repo_path: Path, env_url: str = "", env_token: str = "",
                 env_branch: str = "", env_webhook_secret: str = ""):
        self.path = path
        self.repo_path = repo_path
        self.env_locked = bool(env_url)
        self.env_webhook_secret = env_webhook_secret
        self.data: dict = {}
        try:
            self.data = json.loads(path.read_text())
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            # Guessing would run the garage the wrong way (no history, no backup);
            # stopping says exactly what to fix.
            raise RuntimeError(f"Can't read MyGarage's storage settings in {path} ({e}). "
                               "Fix or remove that file and restart.") from e
        if self.env_locked:
            if not self.data.get("from_env_github"):
                # Remember that the folder is this app's own copy of a GitHub repo,
                # so if GIT_REPO_URL is removed later it carries on as this
                # server's garage instead of a plain folder. (Only the marker is
                # saved; the YAML's URL and token stay out of the file.)
                self.data["from_env_github"] = True
                try:
                    self._write(self.data)
                except OSError as e:
                    log.warning("could not save %s: %s", self.path, e)
            self.data.update(mode="github", url=env_url, token=env_token,
                             branch=env_branch or "main")
        elif env_branch and not self.data.get("branch"):
            self.data["branch"] = env_branch

    @property
    def mode(self) -> str:
        if self.data.get("mode"):
            return self.data["mode"]
        if self.data.get("needs_setup"):
            return ""  # a first-run setup failed partway: show the setup screen again
        # Installs from before the setup screen (or a setup that didn't finish):
        # a folder that's already there is read as it is, never taken over.
        # Even a git checkout stays a plain folder until someone turns on
        # history in Settings, so the app never commits into someone's clone
        # uninvited, and changes made to it outside the app keep showing up.
        # This app's own copy of a GitHub repo (set up by GIT_REPO_URL, since
        # removed) carries on as this server's garage.
        if (self.repo_path / ".git").exists() and self.data.get("from_env_github"):
            return "server"
        if (self.repo_path / "README.md").exists():
            return "folder"
        return ""

    url = property(lambda self: self.data.get("url", "") if self.mode in ("github", "server") else "")
    token = property(lambda self: self.data.get("token", ""))
    branch = property(lambda self: self.data.get("branch") or "main")

    @property
    def webhook_secret(self) -> str:
        """From the environment, else made up the first time a GitHub repo is connected."""
        return self.env_webhook_secret or (self.data.get("webhook_secret", "") if self.url else "")

    def set(self, **values) -> None:
        """Save the new values, then use them (so a failed save changes nothing)."""
        data = {**self.data, **values}
        if data.get("mode"):
            data.pop("needs_setup", None)
        if data.get("url") and not data.get("webhook_secret"):
            data["webhook_secret"] = secrets.token_hex(32)
        if not self.env_locked:
            self._write(data)
        self.data = data

    def _write(self, data: dict) -> None:
        """Raises OSError, so a setting that couldn't be saved isn't reported as saved."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self.path)


def github_slug(url: str) -> str:
    """"owner/name" for a github.com URL, for "Open on GitHub" links."""
    m = re.match(r"(?:https://|git@)github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url or "")
    return m.group(1) if m else ""


def normalize_repo_url(text: str) -> str:
    """Accept "owner/name", a github.com page URL, or any git URL."""
    text = text.strip()
    if re.fullmatch(r"[\w.-]+/[\w.-]+", text):
        return f"https://github.com/{text.removesuffix('.git')}.git"
    m = re.match(r"https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?$", text)
    if m:
        return f"https://github.com/{m.group(1)}.git"
    return text
