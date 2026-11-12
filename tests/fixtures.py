"""!!! FIXTURES DE TEST — DONNÉES SYNTHÉTIQUES !!!

Aucune de ces valeurs ne représente un cours, un résultat ou un dépôt réel.
Les symboles ZZTA / ZZTB / ZZBM et les CIK 9999999xx sont fictifs.
Ces données ne doivent JAMAIS être chargées dans une base de production.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from stockhunter.data.calendar import trading_days

FIXTURE_NOTICE = "SYNTHETIC TEST FIXTURE - NOT REAL MARKET DATA"


def FIXTURE_tiingo_bars(start: date, end: date, base: float = 100.0, split_on: date | None = None,
                        div_on: date | None = None) -> list[dict]:
    rows = []
    for i, ts in enumerate(trading_days(start, end)):
        px = base + i * 0.1
        rows.append({
            "date": ts.strftime("%Y-%m-%dT00:00:00.000Z"),
            "open": px, "high": px + 1, "low": px - 1, "close": px + 0.5, "volume": 1_000_000 + i,
            "adjOpen": px, "adjHigh": px + 1, "adjLow": px - 1, "adjClose": px + 0.5, "adjVolume": 1_000_000 + i,
            "divCash": 0.5 if div_on and ts.date() == div_on else 0.0,
            "splitFactor": 2.0 if split_on and ts.date() == split_on else 1.0,
        })
    return rows


FIXTURE_SEC_TICKERS = {
    "_fixture_notice": FIXTURE_NOTICE,
    "fields": ["cik", "name", "ticker", "exchange"],
    "data": [
        [999999901, "Test Alpha Corp (FICTIF)", "ZZTA", "Nasdaq"],
        [999999902, "Test Beta Inc (FICTIF)", "ZZTB", "NYSE"],
    ],
}


def FIXTURE_companyfacts(cik: int) -> dict:
    return {
        "_fixture_notice": FIXTURE_NOTICE,
        "cik": cik,
        "entityName": "FICTIF",
        "facts": {
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                {"end": "2026-07-15", "val": 1000000, "accn": "0009999999-26-000010", "fy": 2026, "fp": "Q2",
                 "form": "10-Q", "filed": "2026-08-01"}]}}},
            "us-gaap": {
                "Revenues": {"units": {"USD": [
                    {"start": "2026-04-01", "end": "2026-06-30", "val": 5000000, "accn": "0009999999-26-000010",
                     "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-08-01"},
                    # doublon exact (fréquent dans companyfacts) -> doit être dédupliqué
                    {"start": "2026-04-01", "end": "2026-06-30", "val": 5000000, "accn": "0009999999-26-000010",
                     "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-08-01", "frame": "CY2026Q2"},
                ]}},
                # poste de bilan : pas de "start" -> period_start NULL
                "Assets": {"units": {"USD": [
                    {"end": "2026-06-30", "val": 9000000, "accn": "0009999999-26-000010", "fy": 2026, "fp": "Q2",
                     "form": "10-Q", "filed": "2026-08-01"}]}},
                "SomethingIgnored": {"units": {"USD": [
                    {"end": "2026-06-30", "val": 1, "accn": "0009999999-26-000010", "filed": "2026-08-01"}]}},
            },
        },
    }


def FIXTURE_submissions(cik: int) -> dict:
    return {
        "_fixture_notice": FIXTURE_NOTICE,
        "cik": str(cik),
        "name": "FICTIF",
        "sic": "7372",
        "sicDescription": "Services-Prepackaged Software",
        "filings": {"recent": {
            "accessionNumber": [f"{cik:010d}-26-000011", f"{cik:010d}-26-000010", f"{cik:010d}-26-000009"],
            "filingDate": ["2026-10-30", "2026-08-01", "2026-07-01"],
            "reportDate": ["2026-10-30", "2026-06-30", ""],
            "form": ["8-K", "10-Q", "SD"],
            "primaryDocument": ["ev8k.htm", "q2.htm", "sd.htm"],
            "items": ["2.02,9.01", "", ""],
        },
            "files": [{"name": f"CIK{cik:010d}-submissions-001.json", "filingCount": 2,
                       "filingFrom": "2018-01-01", "filingTo": "2019-12-31"},
                      {"name": f"CIK{cik:010d}-submissions-002.json", "filingCount": 1,
                       "filingFrom": "2001-01-01", "filingTo": "2005-12-31"}]},
    }


def FIXTURE_submissions_archive(cik: int) -> dict:
    """Page d'archive SEC (format colonnaire, sans enveloppe)."""
    return {
        "accessionNumber": [f"{cik:010d}-19-000001", f"{cik:010d}-19-000002"],
        "filingDate": ["2019-01-30", "2019-02-01"],
        "reportDate": ["2019-01-30", ""],
        "form": ["8-K", "4"],
        "primaryDocument": ["old8k.htm", "f4.xml"],
        "items": ["2.02", ""],
    }


FIXTURE_FRED_SERIES = {"seriess": [{"id": "DGS10", "title": "FICTIF 10Y", "frequency": "Daily", "units": "Percent"}]}


def FIXTURE_fred_obs(start: date, end: date) -> dict:
    obs = []
    for ts in pd.bdate_range(start, end):
        obs.append({"date": ts.strftime("%Y-%m-%d"), "value": "." if ts.day == 1 else "4.10",
                    "realtime_start": end.isoformat()})
    return {"_fixture_notice": FIXTURE_NOTICE, "observations": obs}


FIXTURE_EARNINGS = {
    "_fixture_notice": FIXTURE_NOTICE,
    "earningsCalendar": [
        {"date": "2026-11-20", "symbol": "ZZTA", "hour": "amc", "quarter": 3, "year": 2026, "epsEstimate": 1.0},
        {"date": "2026-11-21", "symbol": "NOTTRACKED", "hour": "bmo", "quarter": 3, "year": 2026},
    ],
}
