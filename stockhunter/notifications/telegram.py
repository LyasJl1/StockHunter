"""Client Telegram Bot API minimal. Le token est dans l'URL de l'API : il est masqué dans toute erreur et
n'est jamais journalisé (les appels ne passent pas par le journal api_calls, qui stocke les URL)."""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

MAX_LEN = 4000  # limite Telegram : 4096 caractères


class TelegramError(RuntimeError):
    pass


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    """Découpe aux sauts de ligne, sans couper une ligne (sauf ligne isolée trop longue)."""
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


class TelegramClient:
    def __init__(self, token: str, chat_id: str, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_retries: int = 3):
        self._token, self.chat_id, self.sleep, self.max_retries = token, str(chat_id), sleep, max_retries
        self._http = httpx.Client(base_url=f"https://api.telegram.org/bot{token}", timeout=30, transport=transport)

    def _redact(self, s: str) -> str:
        return s.replace(self._token, "***").replace(self.chat_id, "<chat>")

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        for attempt in range(self.max_retries + 1):
            try:
                r = self._http.post(f"/{method}", json=payload)
            except httpx.TransportError as exc:
                err = self._redact(f"erreur réseau : {exc!r}")
                if attempt >= self.max_retries:
                    raise TelegramError(err) from None
                self.sleep(2 ** attempt)
                continue
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and data.get("ok"):
                return data.get("result")
            if r.status_code == 429 or r.status_code >= 500:
                wait = (data.get("parameters") or {}).get("retry_after") or 2 ** attempt
                if attempt < self.max_retries:
                    self.sleep(min(float(wait), 60))
                    continue
            raise TelegramError(self._redact(f"{method} : HTTP {r.status_code} {data.get('description', '')}"))
        raise TelegramError(f"{method} : échec après {self.max_retries} tentatives")

    def send(self, text: str) -> None:
        for part in split_message(text):
            self._call("sendMessage", {"chat_id": self.chat_id, "text": part, "parse_mode": "HTML",
                                       "disable_web_page_preview": True})

    def updates(self, offset: int | None) -> list[dict]:
        return self._call("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message"]}) or []
