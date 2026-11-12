"""Tests des calculs (données SYNTHÉTIQUES — voir tests/fixtures.py)."""

from datetime import date

import numpy as np
import pandas as pd

from stockhunter.analytics.fundamentals import fundamental_snapshot, valuation
from stockhunter.analytics.technical import pct_rank, technical_snapshot
from stockhunter.data.calendar import trading_days


def fact(concept, start, end, value, filed, form="10-Q"):
    return {"concept": concept, "period_start": date.fromisoformat(start) if start else None,
            "period_end": date.fromisoformat(end), "form": form, "filed_date": date.fromisoformat(filed),
            "value": value}


def fiscal_year(year: int, quarterly: list[float], concept="Revenues", filed_shift=30):
    """Publie comme une vraie société : T1 sur 3 mois, puis cumuls 6 m / 9 m, et l'exercice dans le 10-K."""
    ends = [f"{year}-03-31", f"{year}-06-30", f"{year}-09-30", f"{year}-12-31"]
    files = [f"{year}-05-0{1}", f"{year}-08-01", f"{year}-11-01", f"{year + 1}-02-15"]
    start = f"{year}-01-01"
    out, cum = [], 0.0
    for i, (e, v) in enumerate(zip(ends, quarterly)):
        cum += v
        form = "10-K" if i == 3 else "10-Q"
        out.append(fact(concept, start, e, cum, files[i], form))
    return out


def test_quarters_rebuilt_from_ytd_and_ttm():
    facts = fiscal_year(2025, [100, 110, 120, 130]) + fiscal_year(2026, [150, 160, 170, 0])[:3]
    s = fundamental_snapshot(facts, date(2026, 11, 15))
    assert s["last_quarter_end"] == "2026-09-30"
    # TTM = T4 2025 (FY - 9M = 130) + 150 + 160 + 170
    assert s["revenue_ttm"] == 610
    assert abs(s["revenue_yoy_q"] - (170 / 120 - 1)) < 1e-9
    assert abs(s["revenue_accel"] - ((170 / 120 - 1) - (160 / 110 - 1))) < 1e-9


def test_point_in_time_ignores_later_filings_and_uses_known_restatement():
    facts = fiscal_year(2025, [100, 110, 120, 130]) + fiscal_year(2026, [150, 160, 170, 0])[:3]
    s = fundamental_snapshot(facts, date(2026, 8, 15))  # le 10-Q du 01/11/2026 n'existe pas encore
    assert s["last_quarter_end"] == "2026-06-30"
    # Retraitement du T3 2026 (publié sur 3 mois le 01/12) : invisible avant sa date de dépôt, utilisé après
    facts.append(fact("Revenues", "2026-07-01", "2026-09-30", 180, "2026-12-01"))
    assert fundamental_snapshot(facts, date(2026, 11, 15))["revenue_ttm"] == 610
    assert fundamental_snapshot(facts, date(2026, 12, 2))["revenue_ttm"] == 620


def test_concept_switch_and_stale_instant_and_diluted_shares():
    facts = fiscal_year(2025, [100, 100, 100, 100], "Revenues")
    facts += fiscal_year(2026, [120, 120, 120, 0], "RevenueFromContractWithCustomerExcludingAssessedTax")[:3]
    facts += [fact("EntityCommonStockSharesOutstanding", None, "2010-01-27", 1e6, "2010-02-03"),  # périmé
              fact("WeightedAverageNumberOfDilutedSharesOutstanding", "2026-07-01", "2026-09-30", 2e6, "2026-11-01"),
              fact("NetIncomeLoss", "2026-07-01", "2026-09-30", 50, "2026-11-01")]
    s = fundamental_snapshot(facts, date(2026, 11, 15))
    assert abs(s["revenue_yoy_q"] - 0.2) < 1e-9          # concepts fusionnés par période
    assert s["shares"] == 2e6 and s["shares_source"] == "diluées pondérées"
    assert "shares_stale_date" in s
    assert valuation(s, 10.0)["market_cap"] == 2e7
    assert valuation(s, None) == {}


def test_growth_undefined_on_negative_base():
    facts = fiscal_year(2025, [-10, -10, -10, -10], "NetIncomeLoss") + \
        fiscal_year(2026, [5, 5, 5, 0], "NetIncomeLoss")[:3]
    s = fundamental_snapshot(facts, date(2026, 11, 15))
    assert s["net_income_yoy_q"] is None


def synthetic_bars(n=300, drift=0.001, seed=1, breakout=False):
    rng = np.random.default_rng(seed)
    idx = trading_days(date(2025, 6, 2), date(2026, 11, 11))[-n:]
    rets = drift + rng.normal(0, 0.01, n)
    if breakout:
        rets[:-1] = rng.normal(0, 0.004, n - 1)   # range étroit
        rets[-1] = 0.06
    c = 100 * np.cumprod(1 + rets)
    vol = np.full(n, 1e6)
    if breakout:
        vol[-1] = 3e6
    df = pd.DataFrame({"open": c / (1 + rets / 2), "close": c, "volume": vol, "adj_open": c / (1 + rets / 2),
                       "adj_high": c * 1.002, "adj_low": c * 0.998, "adj_close": c, "adj_volume": vol}, index=idx)
    return df


def test_technical_snapshot_breakout_and_rs():
    spy = synthetic_bars(drift=0.0002, seed=2)
    stock = synthetic_bars(drift=0.0, seed=3, breakout=True)
    t = technical_snapshot(stock, spy)
    assert t["breakout_today"] and t["breakout_holding"] and t["breakout_date"] == "2026-11-11"
    assert t["rs_spy_3m"] is not None and "squeeze_pct" in t
    short = technical_snapshot(stock.iloc[-30:], spy)
    assert short["insufficient_history"]


def test_pct_rank():
    r = pct_rank({"a": 1.0, "b": 2.0, "c": None, "d": 3.0})
    assert r == {"a": 0.0, "b": 50.0, "c": None, "d": 100.0}
    assert pct_rank({"a": 1.0, "b": 2.0}, higher_is_better=False) == {"a": 100.0, "b": 0.0}
