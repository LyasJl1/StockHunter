"""Client Massive (ex-Polygon.io). Documentation vérifiée le 12/11/2026 :
- GET /v2/aggs/grouped/locale/us/market/stocks/{date} : toutes les actions US pour une séance (plan gratuit) ;
- GET /v3/reference/tickers : référentiel, `active=false` pour les titres retirés de la cote ;
- GET /stocks/v1/splits : splits avec ratio (split_from -> split_to).
Plan gratuit « Stocks Basic » : fin de journée, 2 ans d'historique, 5 appels/minute.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy.engine import Engine

from stockhunter.config import Settings
from stockhunter.data.http import ProviderClient, ProviderError, QuotaWindow

BASE_URL = "https://api.massive.com"


def parse_grouped(payload: dict[str, Any], session: date) -> list[dict[str, Any]]:
    out = []
    for r in payload.get("results") or []:
        t = r.get("T")
        if not t or r.get("c") is None:
            continue
        ts = r.get("t")
        if ts is not None:
            d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
            if abs((d - session).days) > 1:  # contrôle de cohérence de la date renvoyée
                raise ProviderError("massive", f"barre {t} datée du {d} dans la réponse du {session}")
        out.append({"ticker": t, "open": r.get("o"), "high": r.get("h"), "low": r.get("l"), "close": r.get("c"),
                    "volume": r.get("v"), "vwap": r.get("vw"), "trades": r.get("n"), "otc": bool(r.get("otc", False))})
    return out


def parse_tickers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in payload.get("results") or []:
        if not r.get("ticker"):
            continue
        delisted = r.get("delisted_utc")
        out.append({
            "ticker_key": f"{r['ticker']}|{r.get('composite_figi') or '-'}",
            "ticker": r["ticker"], "name": r.get("name"), "type": r.get("type"),
            "primary_exchange": r.get("primary_exchange"), "active": bool(r.get("active", True)),
            "cik": str(r["cik"]).zfill(10) if r.get("cik") else None,
            "composite_figi": r.get("composite_figi"),
            "delisted_utc": datetime.fromisoformat(delisted.replace("Z", "+00:00")) if delisted else None,
        })
    return out


def parse_splits(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in payload.get("results") or []:
        try:
            out.append({"ticker": r["ticker"], "execution_date": date.fromisoformat(r["execution_date"]),
                        "split_from": float(r["split_from"]), "split_to": float(r["split_to"]),
                        "adjustment_type": r.get("adjustment_type")})
        except (KeyError, TypeError, ValueError):
            continue
    return [s for s in out if s["split_from"] > 0 and s["split_to"] > 0]


class MassiveProvider:
    NAME = "massive"

    def __init__(self, settings: Settings, engine: Engine, **client_kw):
        if not settings.massive_api_key:
            raise ProviderError(self.NAME, "SH_MASSIVE_API_KEY non configurée")
        per_min = settings.limits.massive_per_minute
        self.client = ProviderClient(
            name=self.NAME, base_url=BASE_URL, engine=engine, cache_dir=Path(settings.cache_dir),
            auth_params={"apiKey": settings.massive_api_key.get_secret_value()},
            min_interval_s=60.0 / per_min + 0.5,
            quotas=[QuotaWindow(timedelta(minutes=1), per_min)],
            timeout_s=max(settings.http_timeout_s, 60.0), max_retries=settings.http_max_retries,
            backoff_base_s=15.0, **client_kw)

    def grouped_daily(self, session: date) -> list[dict[str, Any]]:
        payload = self.client.get_json(f"/v2/aggs/grouped/locale/us/market/stocks/{session.isoformat()}",
                                       {"adjusted": "false", "include_otc": "false"})
        return parse_grouped(payload, session)

    def _paged(self, path: str, params: dict[str, Any], max_pages: int) -> Iterator[dict[str, Any]]:
        url, p = path, params
        for _ in range(max_pages):
            payload = self.client.get_json(url, p)
            yield payload
            nxt = payload.get("next_url")
            if not nxt:
                return
            # httpx REMPLACE la chaîne de requête d'une URL quand on lui passe des paramètres : on la
            # décompose donc pour conserver le curseur, la clé étant ajoutée par le client.
            parts = urlsplit(nxt)
            url, p = parts.path, dict(parse_qsl(parts.query))

    def tickers(self, active: bool, max_pages: int = 100) -> list[dict[str, Any]]:
        out = []
        for payload in self._paged("/v3/reference/tickers",
                                   {"market": "stocks", "active": str(active).lower(), "limit": 1000}, max_pages):
            out.extend(parse_tickers(payload))
        return out

    def splits(self, since: date, max_pages: int = 20) -> list[dict[str, Any]]:
        out = []
        for payload in self._paged("/stocks/v1/splits", {"execution_date.gte": since.isoformat(), "limit": 5000},
                                   max_pages):
            out.extend(parse_splits(payload))
        return out
