"""Client HTTP commun à tous les fournisseurs.

- Limitation de débit (intervalle minimal entre requêtes)
- Contrôle des quotas glissants, persistés en base (survit aux redémarrages)
- Cache disque avec TTL (les clés de cache n'incluent jamais les secrets)
- Retries avec backoff exponentiel sur 429 / 5xx / erreurs réseau, respect de Retry-After
- Journalisation de chaque appel (table api_calls) pour l'audit
- Les secrets sont masqués dans tous les messages d'erreur
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from stockhunter.db.models import ApiCall
from stockhunter.db.session import session_scope

log = logging.getLogger(__name__)


SHORT_WINDOW = timedelta(minutes=5)


class ProviderError(RuntimeError):
    def __init__(self, provider: str, message: str, status_code: int | None = None):
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.status_code = status_code


class QuotaExceeded(ProviderError):
    pass


class NotFound(ProviderError):
    pass


@dataclass
class QuotaWindow:
    span: timedelta
    max_calls: int


@dataclass
class ProviderClient:
    name: str
    base_url: str
    engine: Engine
    cache_dir: Path
    headers: dict[str, str] = field(default_factory=dict)
    auth_params: dict[str, str] = field(default_factory=dict)  # ex. {"token": "..."} — jamais journalisé
    min_interval_s: float = 0.0
    quotas: list[QuotaWindow] = field(default_factory=list)
    timeout_s: float = 30.0
    max_retries: int = 4
    # Codes HTTP qui signifient « quota de la fenêtre épuisé » : pas de retry (inutile et coûteux)
    quota_status_codes: tuple[int, ...] = ()
    backoff_base_s: float = 1.0
    sleep: Callable[[float], None] = time.sleep
    transport: httpx.BaseTransport | None = None

    _last_call: float = field(default=0.0, init=False)
    _client: httpx.Client | None = field(default=None, init=False)

    # ---------------------------------------------------------------- internals
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=self.headers,
                timeout=self.timeout_s,
                transport=self.transport,
                follow_redirects=True,
            )
        return self._client

    def _redact(self, text: str) -> str:
        for secret in self.auth_params.values():
            if secret:
                text = text.replace(secret, "***")
        return text

    def _cache_path(self, path: str, params: dict[str, Any]) -> Path:
        key = json.dumps([self.name, self.base_url, path, sorted(params.items())], default=str)
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.cache_dir / self.name / digest[:2] / f"{digest}.json"

    def _record(self, endpoint: str, symbol: str | None, status: int | None, from_cache: bool, ms: int | None):
        with session_scope(self.engine) as s:
            s.add(ApiCall(provider=self.name, endpoint=endpoint[:255], symbol=symbol,
                          status_code=status, from_cache=from_cache, duration_ms=ms))

    def calls_in_window(self, span: timedelta) -> int:
        since = datetime.now(timezone.utc) - span
        with session_scope(self.engine) as s:
            return s.scalar(
                select(func.count()).select_from(ApiCall).where(
                    ApiCall.provider == self.name,
                    ApiCall.from_cache.is_(False),
                    ApiCall.called_at >= since,
                )
            ) or 0

    def remaining_quota(self) -> dict[str, int]:
        return {str(q.span): q.max_calls - self.calls_in_window(q.span) for q in self.quotas}

    def _oldest_in_window(self, span: timedelta) -> datetime | None:
        since = datetime.now(timezone.utc) - span
        with session_scope(self.engine) as s:
            return s.scalar(select(func.min(ApiCall.called_at)).where(
                ApiCall.provider == self.name, ApiCall.from_cache.is_(False), ApiCall.called_at >= since))

    def _check_quota(self):
        """Fenêtre courte (≤ 5 min, ex. 5 appels/minute) : on ATTEND qu'une place se libère.
        Fenêtre longue (heure, jour) : on lève QuotaExceeded pour que le job s'arrête proprement."""
        for q in self.quotas:
            for _attempt in range(4):
                used = self.calls_in_window(q.span)
                if used < q.max_calls:
                    break
                if q.span > SHORT_WINDOW:
                    raise QuotaExceeded(self.name, f"quota atteint : {used}/{q.max_calls} sur {q.span}")
                oldest = self._oldest_in_window(q.span)
                if oldest is not None and oldest.tzinfo is None:
                    oldest = oldest.replace(tzinfo=timezone.utc)
                wait = ((oldest + q.span) - datetime.now(timezone.utc)).total_seconds() + 0.5 if oldest else 1.0
                self.sleep(max(0.5, min(wait, q.span.total_seconds() + 1)))
            else:
                raise QuotaExceeded(self.name, f"quota toujours atteint après attente : {used}/{q.max_calls} sur {q.span}")

    def _throttle(self):
        if self.min_interval_s:
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                self.sleep(wait)
        self._last_call = time.monotonic()

    # -------------------------------------------------------------------- public
    def get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        symbol: str | None = None,
        cache_ttl: timedelta | None = None,
    ) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        endpoint = f"{path}?{'&'.join(f'{k}={v}' for k, v in sorted(params.items()))}" if params else path

        cache_file = self._cache_path(path, params)
        if cache_ttl and cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < cache_ttl.total_seconds():
                self._record(endpoint, symbol, 200, True, 0)
                return json.loads(cache_file.read_text())

        attempt = 0
        while True:
            self._check_quota()
            self._throttle()
            t0 = time.monotonic()
            status: int | None = None
            try:
                resp = self.client.get(path, params={**params, **self.auth_params})
                status = resp.status_code
            except httpx.TransportError as exc:
                err: Exception = ProviderError(self.name, self._redact(f"erreur réseau : {exc!r}"))
            else:
                err = None  # type: ignore[assignment]
            finally:
                self._record(endpoint, symbol, status, False, int((time.monotonic() - t0) * 1000))

            if err is None and status is not None and status < 400:
                data = resp.json()
                if cache_ttl:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(json.dumps(data))
                return data

            if err is None and status in self.quota_status_codes:
                raise QuotaExceeded(self.name, f"HTTP {status} : quota du fournisseur épuisé sur {endpoint}", status)
            retryable = err is not None or status in (429, 500, 502, 503, 504)
            if not retryable:
                body = self._redact(resp.text[:300])
                if status == 404:
                    raise NotFound(self.name, f"404 sur {endpoint}", status)
                raise ProviderError(self.name, f"HTTP {status} sur {endpoint} : {body}", status)
            if attempt >= self.max_retries:
                raise err or ProviderError(self.name, f"HTTP {status} après {attempt} retries sur {endpoint}", status)

            delay = self.backoff_base_s * (2**attempt)
            if err is None and status == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            log.warning("%s : retry %d dans %.1fs (status=%s)", self.name, attempt + 1, delay, status)
            self.sleep(min(delay, 300))
            attempt += 1

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None
