"""Fournisseurs de données. Chaque fournisseur expose :
- des fonctions `parse_*` pures (testables sans réseau),
- des méthodes `fetch_*` qui appellent l'API via ProviderClient.

Sources retenues (plans gratuits, vérifiés le 2026-11-12 — à revérifier) :
- Tiingo        : OHLCV EOD ajustés + dividendes + splits (Free : 50 req/h, 1000 req/j, ~500 symboles/mois)
- SEC EDGAR     : fondamentaux XBRL point-in-time + dépôts 8-K/10-K/10-Q/Form 4 (gratuit, 10 req/s max)
- FRED          : macro (taux, inflation, VIX, courbe) (gratuit avec clé)
- Finnhub       : calendrier des résultats (Free ; usage personnel uniquement, chandeliers payants)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from stockhunter.config import Settings
from stockhunter.data.http import ProviderClient, ProviderError, QuotaWindow


def _d(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.fromisoformat(value[:10]).date()


def _f(value: Any) -> float | None:
    if value is None or value == "" or value == ".":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# =========================================================================== Tiingo
def parse_tiingo_prices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        out.append(
            {
                "bar_date": _d(r.get("date")),
                "open": _f(r.get("open")),
                "high": _f(r.get("high")),
                "low": _f(r.get("low")),
                "close": _f(r.get("close")),
                "volume": _f(r.get("volume")),
                "adj_open": _f(r.get("adjOpen")),
                "adj_high": _f(r.get("adjHigh")),
                "adj_low": _f(r.get("adjLow")),
                "adj_close": _f(r.get("adjClose")),
                "adj_volume": _f(r.get("adjVolume")),
                "div_cash": _f(r.get("divCash")) or 0.0,
                "split_factor": _f(r.get("splitFactor")) or 1.0,
            }
        )
    return [r for r in out if r["bar_date"] is not None]


class TiingoProvider:
    NAME = "tiingo"
    BASE_URL = "https://api.tiingo.com"

    def __init__(self, settings: Settings, engine: Engine, **client_kw):
        if not settings.tiingo_api_key:
            raise ProviderError(self.NAME, "SH_TIINGO_API_KEY non configurée")
        lim = settings.limits
        self.client = ProviderClient(
            name=self.NAME,
            base_url=self.BASE_URL,
            engine=engine,
            cache_dir=Path(settings.cache_dir),
            headers={"Content-Type": "application/json"},
            auth_params={"token": settings.tiingo_api_key.get_secret_value()},
            min_interval_s=1.0,
            quota_status_codes=(429,),  # chez Tiingo, 429 = quota horaire/journalier épuisé
            quotas=[QuotaWindow(timedelta(hours=1), lim.tiingo_per_hour),
                    QuotaWindow(timedelta(days=1), lim.tiingo_per_day)],
            timeout_s=settings.http_timeout_s,
            max_retries=settings.http_max_retries,
            **client_kw,
        )

    def fetch_metadata(self, symbol: str) -> dict[str, Any]:
        return self.client.get_json(f"/tiingo/daily/{symbol.lower()}", symbol=symbol,
                                    cache_ttl=timedelta(days=7))

    def fetch_prices(self, symbol: str, start: date, end: date | None = None) -> list[dict[str, Any]]:
        raw = self.client.get_json(
            f"/tiingo/daily/{symbol.lower()}/prices",
            {"startDate": start.isoformat(), "endDate": end.isoformat() if end else None, "format": "json"},
            symbol=symbol,
        )
        return parse_tiingo_prices(raw or [])


# =========================================================================== SEC EDGAR
# Concepts XBRL conservés (le reste est ignoré pour limiter le volume)
SEC_CONCEPTS: dict[str, set[str]] = {
    "us-gaap": {
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "GrossProfit",
        "OperatingIncomeLoss",
        "NetIncomeLoss",
        "EarningsPerShareBasic",
        "EarningsPerShareDiluted",
        "NetCashProvidedByUsedInOperatingActivities",
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "CashAndCashEquivalentsAtCarryingValue",
        "LongTermDebt",
        "LongTermDebtNoncurrent",
        "LongTermDebtCurrent",
        "Assets",
        "Liabilities",
        "StockholdersEquity",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
    },
    "dei": {"EntityCommonStockSharesOutstanding"},
}

SEC_FORMS = {"8-K", "8-K/A", "10-K", "10-K/A", "10-Q", "10-Q/A", "4", "S-1", "S-3", "424B5",
             "SC 13D", "SC 13D/A", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G", "6-K", "20-F"}


def cik10(cik: int | str) -> str:
    return str(int(cik)).zfill(10)


def parse_sec_ticker_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """company_tickers_exchange.json -> {TICKER: {cik, name, exchange}}"""
    fields = payload["fields"]
    out = {}
    for row in payload["data"]:
        rec = dict(zip(fields, row))
        out[str(rec["ticker"]).upper()] = {
            "cik": cik10(rec["cik"]),
            "name": rec.get("name"),
            "exchange": rec.get("exchange"),
        }
    return out


def parse_sec_companyfacts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    facts = payload.get("facts", {})
    for taxonomy, wanted in SEC_CONCEPTS.items():
        for concept, body in facts.get(taxonomy, {}).items():
            if concept not in wanted:
                continue
            for unit, items in body.get("units", {}).items():
                for it in items:
                    end, filed, val = _d(it.get("end")), _d(it.get("filed")), _f(it.get("val"))
                    if end is None or filed is None or val is None or not it.get("accn"):
                        continue
                    start = _d(it.get("start"))
                    out.append(
                        {
                            "fact_key": f"{taxonomy}|{concept}|{unit}|{start or '-'}|{end}|{it['accn']}",
                            "taxonomy": taxonomy,
                            "concept": concept,
                            "unit": unit,
                            "period_start": start,
                            "period_end": end,
                            "fiscal_year": it.get("fy"),
                            "fiscal_period": it.get("fp"),
                            "form": it.get("form"),
                            "filed_date": filed,
                            "accession": it["accn"],
                            "value": val,
                        }
                    )
    return out


@dataclass
class SecSubmissions:
    name: str | None
    sic: str | None
    sic_description: str | None
    filings: list[dict[str, Any]]


def parse_sec_submissions(payload: dict[str, Any], forms: set[str] = SEC_FORMS) -> SecSubmissions:
    cik_int = int(payload["cik"])
    recent = payload.get("filings", {}).get("recent", {})
    n = len(recent.get("accessionNumber", []))
    filings = []
    for i in range(n):
        form = recent["form"][i]
        if form not in forms:
            continue
        accn = recent["accessionNumber"][i]
        doc = (recent.get("primaryDocument") or [None] * n)[i]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn.replace('-', '')}/" + (doc or "")
        filings.append(
            {
                "accession": accn,
                "form": form,
                "filed_date": _d(recent["filingDate"][i]),
                "report_date": _d((recent.get("reportDate") or [None] * n)[i]),
                "items": ((recent.get("items") or [None] * n)[i] or None),
                "primary_doc": doc,
                "url": url,
            }
        )
    return SecSubmissions(payload.get("name"), payload.get("sic") or None,
                          payload.get("sicDescription") or None, filings)


def sic_to_sector(sic: str | None) -> str | None:
    """Divisions SIC officielles (classification grossière, affinée plus tard)."""
    if not sic or not str(sic).isdigit():
        return None
    s = int(sic)
    if s < 1000:
        return "Agriculture"
    if s < 1500:
        return "Mining & Energy"
    if s < 1800:
        return "Construction"
    if s < 4000:
        if 2830 <= s <= 2836 or 3840 <= s <= 3851:
            return "Healthcare"
        if 3570 <= s <= 3579 or 3660 <= s <= 3679:
            return "Technology"
        return "Manufacturing"
    if s < 5000:
        return "Transportation, Communications & Utilities"
    if s < 5200:
        return "Wholesale Trade"
    if s < 6000:
        return "Retail Trade"
    if s < 6800:
        return "Finance & Real Estate"
    if s < 9000:
        if 7370 <= s <= 7379:
            return "Technology"
        if 8000 <= s <= 8099:
            return "Healthcare"
        return "Services"
    return "Public Administration / Other"


class SecEdgarProvider:
    NAME = "sec_edgar"

    def __init__(self, settings: Settings, engine: Engine, **client_kw):
        if not settings.sec_user_agent or "@" not in settings.sec_user_agent:
            raise ProviderError(self.NAME, "SH_SEC_USER_AGENT doit contenir un nom d'application et un e-mail de contact")
        common = dict(
            engine=engine,
            cache_dir=Path(settings.cache_dir),
            headers={"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"},
            min_interval_s=1.0 / settings.limits.sec_per_second,
            timeout_s=settings.http_timeout_s,
            max_retries=settings.http_max_retries,
            **client_kw,
        )
        self.www = ProviderClient(name=self.NAME, base_url="https://www.sec.gov", **common)
        self.data = ProviderClient(name=self.NAME, base_url="https://data.sec.gov", **common)

    def fetch_ticker_map(self) -> dict[str, dict[str, Any]]:
        return parse_sec_ticker_map(self.www.get_json("/files/company_tickers_exchange.json",
                                                      cache_ttl=timedelta(days=1)))

    def fetch_companyfacts(self, cik: str, symbol: str | None = None) -> list[dict[str, Any]]:
        payload = self.data.get_json(f"/api/xbrl/companyfacts/CIK{cik10(cik)}.json", symbol=symbol,
                                     cache_ttl=timedelta(hours=20))
        return parse_sec_companyfacts(payload)

    def fetch_submissions(self, cik: str, symbol: str | None = None) -> SecSubmissions:
        payload = self.data.get_json(f"/submissions/CIK{cik10(cik)}.json", symbol=symbol,
                                     cache_ttl=timedelta(hours=6))
        return parse_sec_submissions(payload)


# =========================================================================== FRED
FRED_SERIES: dict[str, str] = {
    "VIXCLS": "CBOE Volatility Index (VIX), clôture",
    "DGS10": "Rendement Treasury 10 ans",
    "DGS2": "Rendement Treasury 2 ans",
    "T10Y2Y": "Écart 10 ans - 2 ans",
    "DFF": "Taux effectif des Fed Funds",
    "CPIAUCSL": "Indice des prix à la consommation (CPI-U)",
    "UNRATE": "Taux de chômage US",
}


def parse_fred_observations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for o in payload.get("observations", []):
        d = _d(o.get("date"))
        if d is None:
            continue
        out.append({"obs_date": d, "value": _f(o.get("value")), "realtime_start": _d(o.get("realtime_start"))})
    return out


class FredProvider:
    NAME = "fred"

    def __init__(self, settings: Settings, engine: Engine, **client_kw):
        if not settings.fred_api_key:
            raise ProviderError(self.NAME, "SH_FRED_API_KEY non configurée")
        self.client = ProviderClient(
            name=self.NAME,
            base_url="https://api.stlouisfed.org",
            engine=engine,
            cache_dir=Path(settings.cache_dir),
            auth_params={"api_key": settings.fred_api_key.get_secret_value()},
            min_interval_s=0.6,
            quotas=[QuotaWindow(timedelta(minutes=1), settings.limits.fred_per_minute)],
            timeout_s=settings.http_timeout_s,
            max_retries=settings.http_max_retries,
            **client_kw,
        )

    def fetch_series_info(self, series_id: str) -> dict[str, Any]:
        payload = self.client.get_json("/fred/series", {"series_id": series_id, "file_type": "json"},
                                       cache_ttl=timedelta(days=7))
        s = payload["seriess"][0]
        return {"name": s.get("title"), "frequency": s.get("frequency"), "units": s.get("units")}

    def fetch_observations(self, series_id: str, start: date) -> list[dict[str, Any]]:
        payload = self.client.get_json(
            "/fred/series/observations",
            {"series_id": series_id, "file_type": "json", "observation_start": start.isoformat()},
        )
        return parse_fred_observations(payload)


# =========================================================================== Finnhub
def parse_finnhub_earnings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for e in payload.get("earningsCalendar", []) or []:
        d = _d(e.get("date"))
        sym = (e.get("symbol") or "").upper()
        if d is None or not sym:
            continue
        out.append({"symbol": sym, "event_date": d, "raw": e})
    return out


class FinnhubProvider:
    NAME = "finnhub"

    def __init__(self, settings: Settings, engine: Engine, **client_kw):
        if not settings.finnhub_api_key:
            raise ProviderError(self.NAME, "SH_FINNHUB_API_KEY non configurée")
        self.client = ProviderClient(
            name=self.NAME,
            base_url="https://finnhub.io/api/v1",
            engine=engine,
            cache_dir=Path(settings.cache_dir),
            auth_params={"token": settings.finnhub_api_key.get_secret_value()},
            min_interval_s=60.0 / settings.limits.finnhub_per_minute,
            quotas=[QuotaWindow(timedelta(minutes=1), settings.limits.finnhub_per_minute)],
            timeout_s=settings.http_timeout_s,
            max_retries=settings.http_max_retries,
            **client_kw,
        )

    def fetch_earnings_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        payload = self.client.get_json("/calendar/earnings", {"from": start.isoformat(), "to": end.isoformat()},
                                       cache_ttl=timedelta(hours=6))
        return parse_finnhub_earnings(payload)


SOURCE_REGISTRY: dict[str, dict[str, str]] = {
    TiingoProvider.NAME: {"base_url": TiingoProvider.BASE_URL, "plan": "free",
                          "license_note": "Usage personnel ; redistribution des données interdite."},
    SecEdgarProvider.NAME: {"base_url": "https://data.sec.gov", "plan": "free",
                            "license_note": "Données publiques SEC ; User-Agent identifié, <10 req/s."},
    FredProvider.NAME: {"base_url": "https://api.stlouisfed.org", "plan": "free",
                        "license_note": "Conditions FRED ; certaines séries tierces ont leurs propres droits."},
    FinnhubProvider.NAME: {"base_url": "https://finnhub.io", "plan": "free",
                           "license_note": "Plan gratuit = usage personnel uniquement."},
}
