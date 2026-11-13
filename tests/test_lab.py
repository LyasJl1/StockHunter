"""Laboratoire v3 : panel SYNTHÉTIQUE (tickers ZZ*), jamais de données réelles."""

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from stockhunter.lab import evaluate as ev
from stockhunter.lab import factors as fx
from stockhunter.lab import ml, registry
from stockhunter.lab.panel import build_panel, forward_returns, liquidity_tier, universe_mask
from stockhunter.lab.run import render, run_program
from stockhunter.lab.stats import benjamini_hochberg, block_bootstrap_ci, holm, t_sf

NO_SPLITS = pd.DataFrame(columns=["ticker", "execution_date", "split_from", "split_to"])


def synthetic_long(n_tickers=260, n_sessions=430, drift_scale=0.0, seed=1, extra=None):
    """Marché synthétique : r = marché + dérive propre persistante (momentum planté si drift_scale > 0) + bruit."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2024-11-12", periods=n_sessions)
    mkt = rng.normal(0.0004, 0.008, n_sessions)
    rows = []
    for i in range(n_tickers):
        drift = rng.normal(0, drift_scale)
        vol = rng.uniform(0.01, 0.03)
        r = mkt * rng.uniform(0.6, 1.4) + drift + rng.normal(0, vol, n_sessions)
        close = 50 * np.exp(np.cumsum(r))
        opn = np.concatenate([[50.0], close[:-1]]) * (1 + rng.normal(0, 0.002, n_sessions))
        hi, lo = np.maximum(opn, close) * 1.005, np.minimum(opn, close) * 0.995
        vol_sh = rng.uniform(2e5, 2e6) * (1 + rng.uniform(-0.3, 0.3, n_sessions))
        rows.append(pd.DataFrame({"ticker": f"ZZ{i:03d}", "date": days, "open": opn, "high": hi, "low": lo,
                                  "close": close, "volume": vol_sh, "otc": False}))
    spy = 400 * np.exp(np.cumsum(mkt))
    rows.append(pd.DataFrame({"ticker": "SPY", "date": days, "open": spy, "high": spy, "low": spy, "close": spy,
                              "volume": 1e8, "otc": False}))
    long = pd.concat(rows + (extra(days) if extra else []), ignore_index=True)
    return long, {f"ZZ{i:03d}" for i in range(n_tickers)}


@pytest.fixture(scope="module")
def planted():
    long, tickers = synthetic_long(drift_scale=0.0015)
    return build_panel(long, NO_SPLITS, tickers)


def test_no_look_ahead_in_factors_and_universe(planted):
    cut = planted.close.index[300]
    full_mask = universe_mask(planted)
    full = fx.compute(planted, full_mask)
    tr = planted.truncate(cut)
    tr_mask = universe_mask(tr)
    part = fx.compute(tr, tr_mask)
    pd.testing.assert_frame_equal(full_mask.loc[:cut], tr_mask)
    for k in list(fx.SPECS) + ["H1", "H4"]:
        a, b = full[k].loc[:cut].astype(float), part[k].astype(float)
        pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-9, obj=k)
    pd.testing.assert_frame_equal(liquidity_tier(planted, full_mask).loc[:cut], liquidity_tier(tr, tr_mask))


def test_universe_filters_point_in_time():
    def extra(days):
        n = len(days)
        base = dict(open=20.0, high=20.2, low=19.8, close=20.0, volume=1e6, otc=False)
        penny = pd.DataFrame({"ticker": "ZZPENNY", "date": days, **{**base, "open": 2.0, "high": 2.1, "low": 1.9, "close": 2.0}})
        illiq = pd.DataFrame({"ticker": "ZZILLIQ", "date": days, **{**base, "volume": 1e3}})
        halted = pd.DataFrame({"ticker": "ZZHALT", "date": days, **base}).drop(index=range(n - 12, n - 2))
        delisted = pd.DataFrame({"ticker": "ZZGONE", "date": days[:300], **base})
        jump = pd.DataFrame({"ticker": "ZZJUMP", "date": days, **base})
        jump.loc[n - 5:, ["open", "high", "low", "close"]] *= 3            # +200 % sans split : donnée suspecte
        return [penny, illiq, halted, delisted, jump]

    long, tickers = synthetic_long(n_tickers=20, n_sessions=330, extra=extra)
    p = build_panel(long, NO_SPLITS, tickers | {"ZZPENNY", "ZZILLIQ", "ZZHALT", "ZZGONE", "ZZJUMP"})
    m = universe_mask(p)
    last = m.iloc[-1]
    assert not last["ZZPENNY"] and not last["ZZILLIQ"] and not last["ZZHALT"] and not last["ZZJUMP"]
    assert m["ZZHALT"].iloc[-20] and m["ZZJUMP"].iloc[-10]                # admis AVANT l'anomalie
    assert m["ZZGONE"].iloc[290] and not m["ZZGONE"].iloc[-1]             # radié : présent tant qu'il cotait
    assert last["ZZ000"]


def test_forward_returns_definition(planted):
    fw = forward_returns(planted, 5)
    t, sym = 200, "ZZ010"
    assert fw.iloc[t][sym] == pytest.approx(planted.close.iloc[t + 5][sym] / planted.open.iloc[t + 1][sym] - 1)
    assert fw.iloc[-5:].isna().all().all()                                # pas encore observable


def test_planted_momentum_is_detected_and_noise_is_not(planted):
    res = run_program(planted, with_ml=False, with_portfolio=False, today=date(2026, 11, 12))
    v2 = res["facteurs"]["V2"]
    assert v2["developpement"][20]["t"] > 3 and v2["decision"]["decision"] in ("CANDIDAT", "CONFIRMÉ HORS ÉCHANTILLON",
                                                                              "NON CONFIRMÉ (période réservée)")
    assert "holdout" in v2                                                 # consultation unique seulement si candidat
    long, tickers = synthetic_long(drift_scale=0.0, seed=7)
    noise = run_program(build_panel(long, NO_SPLITS, tickers), with_ml=False, with_portfolio=False)
    for k, r in noise["facteurs"].items():
        assert r["decision"]["decision"] in ("NON RETENU", "NON TESTABLE"), k
        assert "holdout" not in r                                          # période réservée intacte
    assert noise["facteurs"]["V1"]["decision"]["decision"] == "NON TESTABLE"   # trop peu de dates (252 séances requises)
    assert "NON RETENU" in render(noise)


def test_decision_rule_requires_every_criterion():
    base = {"dates": 30, "t": 3.5, "top_vs_univers_net": {"25pb": 0.01}, "moitie_1": 0.02, "moitie_2": 0.01,
            "ic_boot95": (0.005, 0.04)}
    assert ev.decide({20: base})["decision"] == "CANDIDAT"
    for patch in ({"t": 2.9}, {"top_vs_univers_net": {"25pb": -0.001}}, {"moitie_2": -0.01}, {"ic_boot95": (-0.001, 0.04)}):
        assert ev.decide({20: {**base, **patch}})["decision"] == "NON RETENU", patch
    assert ev.decide({20: {**base, "dates": 19}})["decision"] == "NON TESTABLE"
    hold_bad = {20: {"t": 0.4, "top_vs_univers_net": {"25pb": 0.01}}}
    assert ev.decide({20: base}, hold_bad)["decision"].startswith("NON CONFIRMÉ")


def test_ml_walk_forward_is_purged(planted):
    sessions = planted.close.index
    pos = {d: i for i, d in enumerate(sessions)}
    dates = list(sessions[::5])
    d = dates[40]
    tr = ml.train_dates(dates, pos, d)
    assert tr and all(pos[t] + 20 + 5 < pos[d] for t in tr)               # rendement terminé + purge avant le test
    assert dates[39] not in tr and dates[36] not in tr
    mask = universe_mask(planted)
    facs = fx.compute(planted, mask)
    dev = sessions[::5][sessions[::5] <= pd.Timestamp("2026-02-28")]
    out = ml.walk_forward(ml.build_dataset(facs, mask, forward_returns(planted, 20), dev), sessions)
    assert out["dates_testees"] >= 5 and out["modeles"]["ridge"]["ic"] > 0
    assert set(out["ridge_coefficients"]) == set(ml.FEATURES)


def test_stats_helpers():
    assert t_sf(2.0, 30) == pytest.approx(0.02731, abs=1e-4) and t_sf(0.0, 10) == 0.5
    p = {"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.5}
    assert holm(p) == pytest.approx({"a": 0.04, "c": 0.09, "b": 0.09, "d": 0.5})
    assert benjamini_hochberg(p) == pytest.approx({"a": 0.04, "c": 0.0533333, "b": 0.0533333, "d": 0.5})
    x = pd.Series(np.random.default_rng(0).normal(0.05, 0.1, 60))
    lo, hi = block_bootstrap_ci(x, 3)
    assert lo < x.mean() < hi and lo > 0


def test_registry_records_every_test_once(tmp_path, planted):
    reg = registry.load()
    n0 = len(registry.tests(reg))
    assert n0 >= 8 and any(h["etat"] == "INTACTE" for h in reg["periodes_reservees"])
    run = json.loads(json.dumps(run_program(planted, with_ml=False, with_portfolio=False), default=str))
    reg = registry.record_run(registry.record_run(reg, run), run)          # idempotent
    assert len(registry.tests(reg)) == n0 + len(fx.SPECS) or len(registry.tests(reg)) == n0   # déjà enregistrés
    fam = registry.family_pvalues(reg)
    assert all(0 <= v["p"] <= v["holm"] <= 1 for v in fam.values())


def test_backtest_and_paper_trading_produce_identical_trades(engine):
    """Même plan, mêmes barres : le backtest (lab) et le paper trading (base) doivent donner les mêmes exécutions,
    car ils partagent `stockhunter.trading.rules`."""
    from stockhunter.lab.factors import atr
    from stockhunter.lab.portfolio_sim import PortfolioParams, simulate
    from stockhunter.db.models import PaperTradeEvent
    from stockhunter.db.session import session_scope
    from stockhunter.portfolio import paper
    from sqlalchemy import select
    from tests.test_paper import bars, setup

    rng = np.random.default_rng(3)
    days = pd.bdate_range("2026-09-01", periods=75)
    r = np.concatenate([rng.normal(0, 0.006, 45), np.full(30, 0.012) + rng.normal(0, 0.004, 30)])
    close = 100 * np.exp(np.cumsum(r))
    opn = np.concatenate([[100.0], close[:-1]]) * (1 + rng.normal(0, 0.001, 75))
    hi, lo = np.maximum(opn, close) * 1.004, np.minimum(opn, close) * 0.996
    long = pd.DataFrame({"ticker": "ZZA", "date": days, "open": opn, "high": hi, "low": lo, "close": close,
                         "volume": 1e6, "otc": False})
    p = build_panel(long, NO_SPLITS, {"ZZA"})
    k = 44                                                                 # signal à la 45e séance
    d0 = days[k]
    score = pd.DataFrame(1.0, index=days, columns=["ZZA"])
    mask = pd.DataFrame(True, index=days, columns=["ZZA"])
    sim = simulate(p, score, mask, pd.DatetimeIndex([d0]), days[-1], pp=PortfolioParams(top_n=1))
    a14 = float(atr(p).at[d0, "ZZA"])

    import tests.test_paper as tp
    old_sig = tp.SIG
    tp.SIG = d0.date()
    try:
        src, ids, run = setup(engine, ("ZZA",), close=float(close[k]), atr_pct=a14 / float(close[k]))
    finally:
        tp.SIG = old_sig
    paper.plan_from_scan(engine, run)
    bars(engine, src, ids["ZZA"], [(str(d.date()), float(opn[i]), float(hi[i]), float(lo[i]), float(close[i]))
                                   for i, d in enumerate(days) if i > k])
    paper.process(engine)
    with session_scope(engine) as s:
        evs = [(e.kind, round(e.price, 6), e.shares) for e in s.scalars(select(PaperTradeEvent).order_by(PaperTradeEvent.id))
               if e.kind in ("OPENED", "TP1", "CLOSED")]
    sm = paper.summary(engine)
    assert evs and evs[0][0] == "OPENED"
    assert sim["trades_clos"] == sm["closed"] and sim["positions_encore_ouvertes"] == sm["open"]
    assert sim["courbe"]["rendement_total"] == pytest.approx(sm["equity"] / 100_000 - 1, abs=1e-9)
