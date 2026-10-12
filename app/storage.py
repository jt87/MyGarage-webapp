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
            log.warning("could not read %s: %s", path, e)
        if self.env_locked:
            self.data.update(mode="github", url=env_url, token=env_token,
                             branch=env_branch or "main")
        elif env_branch and not self.data.get("branch"):
            self.data["branch"] = env_branch

    @property
    def mode(self) -> str:
        if self.data.get("mode"):
            return self.data["mode"]
        # Installs from before the setup screen: a folder that's already there.
        if (self.repo_path / ".git").exists():
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
        self.data.update(values)
        if self.data.get("url") and not self.data.get("webhook_secret"):
            self.data["webhook_secret"] = secrets.token_hex(32)
        self.save()

    def save(self) -> None:
        if self.env_locked:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({k: v for k, v in self.data.items()}, f, indent=2)
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
