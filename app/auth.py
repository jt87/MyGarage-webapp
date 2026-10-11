"""Login and security, modeled on Sonarr/Radarr (Settings → General → Security).

Like the *arr apps:
  * No default credentials. On first run the app asks you to create a
    username and password ("Authentication Required").
  * Authentication Method: Forms (login page), Basic (browser prompt) or
    External (your reverse proxy does it, e.g. Authelia or Cloudflare Access).
  * Authentication Required: Enabled, or Disabled for Local Addresses.
  * An API key for scripts (X-Api-Key header or ?apikey=).
  * Settings live in a config file on the data volume, not in the image.

Hardened where the *arr apps have had trouble:
  * "Local address" is decided from the TCP connection itself. Any request
    that arrived through a proxy (X-Forwarded-For and friends) is treated as
    remote, so a forged header can't unlock it (cf. Sonarr CVE-2026-30975).
  * First-run setup is only allowed from a local address, so a site exposed
    to the internet before setup can't be claimed by a stranger.
  * Passwords are stored as salted PBKDF2-SHA256 (600,000 rounds, OWASP's
    2023 guidance), never in plain text. Failed logins are rate limited.
  * Sessions are signed cookies (HttpOnly, SameSite=Lax, Secure on HTTPS).
    "Log Out All Devices" invalidates every one of them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

log = logging.getLogger("garage.auth")

METHODS = {"forms": "Forms (Login Page)", "basic": "Basic (Browser Popup)",
           "external": "External (Reverse Proxy)"}
REQUIRED = {"enabled": "Enabled", "local": "Disabled for Local Addresses"}

PBKDF2_ROUNDS = 600_000
COOKIE = "mygarage_auth"
REMEMBER_SECONDS = 30 * 24 * 3600
SESSION_SECONDS = 12 * 3600           # without "Remember me"
MIN_PASSWORD = 8

# Headers a reverse proxy adds. Their presence means "not a direct LAN request".
PROXY_HEADERS = (b"x-forwarded-for", b"x-real-ip", b"forwarded", b"cf-connecting-ip",
                 b"true-client-ip", b"x-forwarded-host", b"via")
LOCAL_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10")]


# --------------------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(rounds))
        return hmac.compare_digest(dk, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- config file

class AuthConfig:
    """The security settings, kept in a JSON file on the data volume (mode 600)."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict = {}
        self.load()

    def load(self) -> None:
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        changed = False
        for key, default in (("method", "forms"), ("required", "enabled"),
                             ("api_key", secrets.token_hex(16)),
                             ("secret", secrets.token_hex(32)), ("session_version", 1)):
            if key not in self.data:
                self.data[key] = default
                changed = True
        if changed:
            self.save()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2)
            tmp.replace(self.path)

    # convenience
    @property
    def configured(self) -> bool:
        return bool(self.data.get("username") and self.data.get("password_hash"))

    @property
    def method(self) -> str:
        return self.data.get("method", "forms")

    @property
    def required(self) -> str:
        return self.data.get("required", "enabled")

    @property
    def username(self) -> str:
        return self.data.get("username", "")

    @property
    def api_key(self) -> str:
        return self.data["api_key"]

    def set_credentials(self, username: str, password: str) -> None:
        self.data["username"] = username
        self.data["password_hash"] = hash_password(password)
        self.data["session_version"] = self.data.get("session_version", 1) + 1
        self.save()

    def check(self, username: str, password: str) -> bool:
        if not self.configured:
            return False
        # Always run the slow hash so a wrong username takes as long as a wrong password.
        pw_ok = verify_password(password, self.data["password_hash"])
        return hmac.compare_digest(username.lower(), self.username.lower()) and pw_ok

    def regenerate_api_key(self) -> None:
        self.data["api_key"] = secrets.token_hex(16)
        self.save()

    def revoke_sessions(self) -> None:
        self.data["session_version"] = self.data.get("session_version", 1) + 1
        self.save()

    def reset(self) -> None:
        """Forget the username and password (recovery). Setup runs again."""
        self.data.pop("username", None)
        self.data.pop("password_hash", None)
        self.data["method"] = "forms"
        self.data["required"] = "enabled"
        self.data["session_version"] = self.data.get("session_version", 1) + 1
        self.save()


# --------------------------------------------------------------------------- requests

def is_local(scope) -> bool:
    """True only for a direct connection from a private address.

    Uses the socket peer (the server must not rewrite it from proxy headers)
    and refuses anything that passed through a proxy.
    """
    headers = dict(scope.get("headers") or [])
    if any(h in headers for h in PROXY_HEADERS):
        return False
    client = scope.get("client")
    if not client:
        return False
    try:
        ip = ipaddress.ip_address(client[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in LOCAL_NETS)


def client_key(scope) -> str:
    """Rate-limit key: peer address, plus the forwarded client when proxied."""
    headers = dict(scope.get("headers") or [])
    fwd = headers.get(b"x-forwarded-for", b"").split(b",")[0].strip().decode(errors="ignore")
    peer = (scope.get("client") or ("?",))[0]
    return f"{peer}|{fwd}"


class Sessions:
    def __init__(self, cfg: AuthConfig):
        self.cfg = cfg

    def _signer(self) -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(self.cfg.data["secret"], salt="mygarage-session")

    def issue(self, remember: bool) -> tuple[str, int | None]:
        token = self._signer().dumps({"u": self.cfg.username, "v": self.cfg.data["session_version"],
                                      "r": bool(remember)})
        return token, (REMEMBER_SECONDS if remember else None)

    def user(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            data = self._signer().loads(token, max_age=REMEMBER_SECONDS)
        except (BadSignature, SignatureExpired):
            return None
        if data.get("v") != self.cfg.data["session_version"] or data.get("u") != self.cfg.username:
            return None
        return data["u"]


class LoginThrottle:
    """10 failures per client per 15 minutes, 100 overall (stops header-rotating guessers)."""

    WINDOW = 900

    def __init__(self, per_client: int = 10, overall: int = 100):
        self.per_client, self.overall = per_client, overall
        self.fails: dict[str, list[float]] = {}
        self.all: list[float] = []

    def _trim(self, now: float) -> None:
        self.all = [t for t in self.all if now - t < self.WINDOW]
        for k in list(self.fails):
            self.fails[k] = [t for t in self.fails[k] if now - t < self.WINDOW]
            if not self.fails[k]:
                del self.fails[k]

    def blocked(self, key: str) -> bool:
        self._trim(time.time())
        return len(self.fails.get(key, [])) >= self.per_client or len(self.all) >= self.overall

    def fail(self, key: str) -> None:
        now = time.time()
        self.fails.setdefault(key, []).append(now)
        self.all.append(now)

    def succeed(self, key: str) -> None:
        self.fails.pop(key, None)


# --------------------------------------------------------------------------- recovery CLI

def main(argv: list[str]) -> int:
    """`python -m app.auth reset`  forget the login so setup runs again.
    `python -m app.auth status` show the current security settings."""
    path = Path(os.environ.get("CONFIG_PATH", "/data/config/auth.json"))
    cfg = AuthConfig(path)
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "reset":
        cfg.reset()
        print("Login reset. Open MyGarage from your home network to create a new username and password.")
    elif cmd == "status":
        print(f"Config file:   {path}")
        print(f"Username:      {cfg.username or '(not set up yet)'}")
        print(f"Method:        {METHODS[cfg.method]}")
        print(f"Required:      {REQUIRED[cfg.required]}")
    else:
        print(main.__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
