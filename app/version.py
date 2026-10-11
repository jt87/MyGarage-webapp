"""Tell the owner when a newer image of this app has been published.

The image is built with APP_COMMIT set to the commit it came from. Twice a
day this asks the container registry (GHCR) which commit the `latest` tag
was built from, using the `org.opencontainers.image.revision` label that the
GitHub Action adds. That's three or four small anonymous requests to
ghcr.io, not the GitHub API, and it needs no token because the image is
public. The app never updates itself; the owner does that in TrueNAS.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from datetime import datetime

log = logging.getLogger("garage.version")

CHECK_EVERY = 12 * 3600
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


class VersionCheck:
    def __init__(self, commit: str = "", image: str = "ghcr.io/jt87/mygarage", tag: str = "latest"):
        self.commit = commit.strip()
        registry, _, self.repo = image.partition("/")
        self.registry = registry
        self.tag = tag
        self.latest = ""                          # commit `latest` was built from
        self.latest_built: datetime | None = None
        self.checked: datetime | None = None
        self.error = ""

    @property
    def enabled(self) -> bool:
        """Only images built by the GitHub Action know their own commit."""
        return bool(self.commit and self.repo)

    @property
    def update_available(self) -> bool:
        return bool(self.enabled and self.latest and self.latest != self.commit)

    def _get(self, path: str, token: str, accept: str = "") -> dict:
        req = urllib.request.Request(f"https://{self.registry}{path}")
        # Unredirected, so the token isn't sent on to the blob storage CDN.
        req.add_unredirected_header("Authorization", f"Bearer {token}")
        if accept:
            req.add_header("Accept", accept)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)

    def fetch_labels(self) -> dict:
        """The `latest` image's labels, via the registry's v2 API."""
        url = (f"https://{self.registry}/token?service={self.registry}"
               f"&scope=repository:{self.repo}:pull")
        with urllib.request.urlopen(url, timeout=15) as r:
            token = json.load(r)["token"]
        m = self._get(f"/v2/{self.repo}/manifests/{self.tag}", token, ACCEPT)
        if "manifests" in m:  # a multi-platform index (buildx adds attestations)
            pick = next((d for d in m["manifests"]
                         if d.get("platform", {}).get("os") == "linux"
                         and d.get("platform", {}).get("architecture") == "amd64"), None)
            if not pick:
                raise RuntimeError("no linux/amd64 image in the index")
            m = self._get(f"/v2/{self.repo}/manifests/{pick['digest']}", token, ACCEPT)
        cfg = self._get(f"/v2/{self.repo}/blobs/{m['config']['digest']}", token)
        return cfg.get("config", {}).get("Labels") or {}

    def check(self) -> None:
        if not self.enabled:
            return
        try:
            labels = self.fetch_labels()
            self.latest = labels.get("org.opencontainers.image.revision", "")
            created = labels.get("org.opencontainers.image.created", "")
            try:
                self.latest_built = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                self.latest_built = None
            self.error = ""
        except Exception as e:  # offline or registry hiccup: try again next time
            self.error = str(e)
            log.warning("couldn't check for a newer app version: %s", e)
        self.checked = datetime.now().astimezone()

    def start(self) -> None:
        if not self.enabled:
            return

        def loop():
            while True:
                self.check()
                time.sleep(CHECK_EVERY)

        threading.Thread(target=loop, name="version-check", daemon=True).start()
