"""Moteur de découverte v3 : panel SYNTHÉTIQUE (tickers ZZ*)."""

import json
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import func, select

from stockhunter.db.models import DiscoveryCandidate, DiscoveryDeep, DiscoveryOutcome, DiscoveryRun
from stockhunter.db.session import session_scope
from stockhunter.discovery import engine as disc
from stockhunter.discovery import store as dstore
from stockhunter.lab.panel import build_panel
from tests.test_lab import NO_SPLITS, synthetic_long


@pytest.fixture(scope="module")
def panel():
    long, tickers = synthetic_long(drift_scale=0.0015, n_sessions=430)
    return build_panel(long, NO_SPLITS, tickers)


def test_ranking_respects_rules_and_never_forces_a_count(panel):
    res = disc.run(panel, {"ZZ001": "ZZ Un"}, validation={"v3-V2": "NON RETENU"})
    assert res["as_of"] == panel.close.index[-1].date() and res["univers"] > 100
    mom = res["categories"]["momentum"]
    assert 0 < len(mom["candidats"]) <= disc.MAX_PER_CATEGORY <= max(mom["admis"], disc.MAX_PER_CATEGORY)
    assert "v3-V2 : NON RETENU" in mom["validation"]                       # niveau de validation affiché
    for c in mom["candidats"]:
        m = c["metrics"]
        assert m["rank_trend"] >= 0.90 and m["rank_resid_mom"] >= 0.80 and m["ext_sma50"] <= 0.20
        pl = c["plan"]
        assert pl["entry_min"] < c["close"] < pl["entry_max"] and pl["stop_si_entree_au_cours"] < pl["entry_min"] <= c["close"]
        assert 0 < c["confidence"] <= 1 and c["pos"]
    assert [c["rank"] for c in mom["candidats"]] == list(range(1, len(mom["candidats"]) + 1))
    assert res["categories"]["shock"]["candidats"] == []                   # aucune règle remplie -> catégorie vide
    assert "pas des recommandations" in res["conclusion"]
    text = json.dumps(res, default=str, ensure_ascii=False).lower()
    assert "probabilité de" not in text and "% de chances" not in text


def test_hostile_market_suspends_trend_categories():
    """Marché baissier (SPY sous sa MM200, peu de titres au-dessus de leur MM50) : pas de sélection forcée."""
    long, tickers = synthetic_long(n_tickers=150, n_sessions=330, drift_scale=0.001, seed=5)
    days = sorted(long["date"].unique())
    fall = pd.Series(np.linspace(0, -0.5, 80), index=days[-80:])           # −40 % sur les 80 dernières séances
    k = long["date"].map(fall).fillna(0.0)
    for col in ("open", "high", "low", "close"):
        long[col] = long[col] * np.exp(k)
    res = disc.run(build_panel(long, NO_SPLITS, tickers))
    assert res["marche"]["regime"] == "DÉFAVORABLE"
    for key in ("momentum", "breakout", "steady"):
        assert res["categories"][key]["suspendue"] and res["categories"][key]["candidats"] == []
    assert res["conclusion"].startswith("Marché défavorable")


def test_no_look_ahead_ranking_ignores_future_sessions(panel):
    cut = panel.close.index[-40]
    a = disc.run(panel.truncate(cut))
    long, tickers = synthetic_long(drift_scale=0.0015, n_sessions=430)
    long.loc[long["date"] > cut, ["open", "high", "low", "close"]] *= 3    # futur modifié : ne doit rien changer
    b = disc.run(build_panel(long, NO_SPLITS, tickers).truncate(cut))
    assert json.dumps(a, default=str) == json.dumps(b, default=str)


def test_save_is_immutable_and_idempotent(engine, panel):
    res = disc.run(panel)
    rid, created = dstore.save(engine, res)
    assert created
    res2 = json.loads(json.dumps(res, default=str))
    res2["as_of"], res2["conclusion"] = res["as_of"], "MODIFIÉ"
    rid2, created2 = dstore.save(engine, res2)
    assert rid2 == rid and not created2
    with session_scope(engine) as s:
        assert s.get(DiscoveryRun, rid).conclusion == res["conclusion"]     # jamais réécrit
        n = s.scalar(select(func.count()).select_from(DiscoveryCandidate))
    assert n == sum(len(v["candidats"]) for v in res["categories"].values())


def test_outcomes_follow_each_signal_to_its_end(engine, panel):
    past = panel.truncate(panel.close.index[-70])
    res = disc.run(past)
    dstore.save(engine, res)
    c1 = dstore.update_outcomes(engine, past)                               # aucune séance postérieure
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(DiscoveryOutcome).where(DiscoveryOutcome.final.is_(True))) == 0
    c2 = dstore.update_outcomes(engine, panel)                              # 70 séances plus tard : tout est joué
    assert c1["finalises"] == 0 and c2["finalises"] == c2["mis_a_jour"] > 0
    with session_scope(engine) as s:
        rows = list(s.scalars(select(DiscoveryOutcome)))
        assert all(r.final and r.status in ("CLOSED", "CANCELLED") and r.ret_20 is not None for r in rows)
        closed = [r for r in rows if r.status == "CLOSED"]
        assert closed and all(r.r_multiple is not None and r.entry_date and r.exit_date for r in closed)
        before = {r.candidate_id: (r.r_multiple, r.exit_date) for r in rows}
    assert dstore.update_outcomes(engine, panel)["mis_a_jour"] == 0         # résultat final : plus jamais recalculé
    with session_scope(engine) as s:
        assert {r.candidate_id: (r.r_multiple, r.exit_date) for r in s.scalars(select(DiscoveryOutcome))} == before
    perf = dstore.performance(engine)
    assert perf["momentum"]["finalises"] > 0 and "insuffisant" in perf["momentum"]["lecture"]


class FakeSec:
    def __init__(self, filed):
        self.calls, self.filed = [], filed

    def fetch_ticker_map(self):
        return {}

    def fetch_companyfacts(self, cik, symbol=None):
        self.calls.append(symbol)
        return []

    def fetch_submissions(self, cik, symbol=None, history_since=None):
        from stockhunter.data.providers import SecSubmissions
        d = self.filed
        return SecSubmissions("ZZ Corp", "1", "Test", [
            {"form": "8-K", "filed_date": d, "items": "2.06,9.01", "url": "https://www.sec.gov/x", "accession": "A"},
            {"form": "8-K", "filed_date": date(2030, 1, 1), "items": "1.01", "url": "https://www.sec.gov/futur", "accession": "B"}])


class FakeNews:
    def fetch_company_news(self, symbol, start, end):
        return [{"external_id": "1", "published_at": datetime(2026, 2, 9, 15, tzinfo=timezone.utc),
                 "headline": f"{symbol} faces antitrust probe", "summary": None, "publisher": "ZZ Wire", "url": "https://ex.test/1"}]


def test_deep_dive_only_for_top_candidates_and_point_in_time(engine, panel):
    from stockhunter.db.models import MarketTicker
    res = disc.run(panel)
    top = res["categories"]["momentum"]["candidats"][0]["ticker"]
    low = res["categories"]["momentum"]["candidats"][-1]["ticker"]
    with session_scope(engine) as s:
        for t in (top, low):
            s.add(MarketTicker(ticker_key=t, ticker=t, name=t, type="CS", primary_exchange="XNYS", active=True, cik="0000000042"))
    from datetime import timedelta
    sec = FakeSec(res["as_of"] - timedelta(days=10))
    stats = dstore.deepen(engine, res, panel, sec, FakeNews(), today=res["as_of"])
    assert stats["series"] >= stats["approfondis"] > 0 and top in sec.calls and low not in sec.calls
    with session_scope(engine) as s:
        deep = json.loads(s.get(DiscoveryDeep, top).payload)
        shallow = json.loads(s.get(DiscoveryDeep, low).payload)
    assert len(deep["series"]["close"]) == 260 and deep["series"]["close"][-1] == pytest.approx(
        res["categories"]["momentum"]["candidats"][0]["close"], abs=0.01)
    assert [d["url"] for d in deep["depots"]] == ["https://www.sec.gov/x"]          # dépôt futur exclu
    assert deep["depots"][0]["vigilance"] and "Dépréciation" in deep["depots"][0]["label"]
    assert deep["actualites"][0]["category"] == "legal" and deep["fondamentaux"] == {"disponible": False}
    assert "depots" not in shallow and shallow["series"]["close"]


def test_telegram_scanner_and_signal_trace_origin_and_result(engine, panel):
    from stockhunter.notifications.engine import answer
    from stockhunter.reports.daily import build_report
    assert "Aucun classement" in answer(engine, "/scanner")
    past = panel.truncate(panel.close.index[-70])
    res = disc.run(past, {t: f"{t} <b>Corp</b>" for t in panel.close.columns})
    dstore.save(engine, res)
    dstore.update_outcomes(engine, panel)
    top = res["categories"]["momentum"]["candidats"][0]["ticker"]
    msg = answer(engine, "/scanner")
    assert top in msg and "exploratoires" in msg
    sig = answer(engine, f"/signal {top.lower()}")
    assert "Pourquoi" in sig and "Plan simulé" in sig and "Historique et résultats" in sig
    assert (" R" in sig or "annulé" in sig) and "<b>Corp</b>" not in sig          # résultat tracé ; HTML échappé
    assert "jamais été relevée" in answer(engine, "/signal NOPE") and "Usage" in answer(engine, "/signal")
    assert "/scanner" in answer(engine, "/help")
