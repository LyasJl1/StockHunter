"""Authentification mono-utilisateur.

- Mot de passe stocké uniquement sous forme de hachage scrypt (SH_DASHBOARD_PASSWORD_HASH), jamais en clair.
- Session dans un cookie signé (SH_DASHBOARD_SECRET), HttpOnly, SameSite=Lax, Secure derrière HTTPS.
- Jeton CSRF par session, exigé sur tout formulaire POST.
- Limitation des tentatives : 5 échecs par IP sur 15 minutes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE = "sh_session"
MAX_AGE = 7 * 24 * 3600
N, R, P = 2**14, 8, 1


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        salt, dk = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
    except ValueError:
        return False
    cand = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=len(dk))
    return hmac.compare_digest(cand, dk)


class Sessions:
    def __init__(self, secret: str):
        self.s = URLSafeTimedSerializer(secret, salt="stockhunter-session")

    def issue(self) -> str:
        return self.s.dumps({"u": "owner", "csrf": secrets.token_urlsafe(24)})

    def read(self, cookie: str | None) -> dict | None:
        if not cookie:
            return None
        try:
            return self.s.loads(cookie, max_age=MAX_AGE)
        except (BadSignature, SignatureExpired):
            return None


class RateLimiter:
    def __init__(self, max_failures: int = 5, window_s: int = 900):
        self.max, self.window = max_failures, window_s
        self.fails: dict[str, deque] = defaultdict(deque)

    def blocked(self, key: str) -> bool:
        q = self.fails[key]
        while q and time.monotonic() - q[0] > self.window:
            q.popleft()
        return len(q) >= self.max

    def fail(self, key: str) -> None:
        self.fails[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        self.fails.pop(key, None)
