"""Moteur de validation (données SYNTHÉTIQUES)."""

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from stockhunter.scan import DataStore, ScanError
from stockhunter.validation.backtest import (
    BacktestParams,
    PriceBook,
    add_forward_returns,
    newey_west_tstat,
    simulate_portfolio,
    simulate_trades,
    trade_metrics,
)
from stockhunter.validation.run import render_markdown, run_validation
from tests.test_scan import seed


class FakeBook(PriceBook):
    def __init__(self, series: dict[str, dict[str, list[float]]]):
        idx = pd.bdate_range("2026-01-05", periods=len(next(iter(series.values()))["c"]))
        self.idx = {k: idx for k in series}
        self.o = {k: np.array(v["o"], float) for k, v in series.items()}
        self.c = {k: np.array(v["c"], float) for k, v in series.items()}
        self.lo = {k: np.array(v["lo"], float) for k, v in series.items()}


def flat(n, px=100.0):
    return {"o": [px] * n, "c": [px] * n, "lo": [px] * n}


def sig_row(sym, d, atr=0.02):
    return pd.DataFrame([{"symbol": sym, "date": pd.Timestamp(d), "atr_pct": atr}])


P = BacktestParams(stop_atr=2.0, trail_atr=3.0, max_hold_sessions=5)


def test_entry_next_open_and_stop_and_gap():
    # signal le lundi 05/01 -> entrée à l'ouverture du mardi (100), stop = 100 - 2×2 = 96
    s = flat(10)
    s["lo"][3] = 95.0                      # le 3e jour touche le stop intrajournalier
    book = FakeBook({"SPY": flat(10), "X": s})
    t = simulate_trades(sig_row("X", "2026-01-05"), pd.Series([True]), book, P)[0]
    assert t.entry_date == pd.Timestamp("2026-01-06") and t.entry == 100
    assert t.reason == "stop" and t.exit == 96.0
    g = flat(10)
    g["o"][2], g["lo"][2], g["c"][2] = 90.0, 89.0, 91.0   # ouverture sous le stop : exécuté à 90, pas à 96
    book = FakeBook({"SPY": flat(10), "X": g})
    t = simulate_trades(sig_row("X", "2026-01-05"), pd.Series([True]), book, P)[0]
    assert t.reason == "stop (gap)" and t.exit == 90.0


def test_trailing_stop_and_time_exit_and_unfinished_excluded():
    up = {"o": [100, 100, 104, 108, 112, 116, 120, 120, 120, 120], "c": [100, 104, 108, 112, 116, 120, 120, 120, 120, 120],
          "lo": [100, 100, 103, 107, 111, 115, 119, 119, 119, 119]}
    book = FakeBook({"SPY": flat(10), "X": up})
    t = simulate_trades(sig_row("X", "2026-01-05"), pd.Series([True]), book, P)[0]
    assert t.reason == "durée max" and t.sessions == 5 and t.exit == 120
    # signal trop proche de la fin des données : le trade n'est pas terminé -> exclu (jamais supposé gagnant)
    assert simulate_trades(sig_row("X", "2026-01-14"), pd.Series([True]), book, P) == []


def test_one_position_per_symbol_and_costs():
    book = FakeBook({"SPY": flat(20), "X": flat(20)})
    sig = pd.concat([sig_row("X", "2026-01-05"), sig_row("X", "2026-01-06"), sig_row("X", "2026-01-20")])
    trades = simulate_trades(sig.reset_index(drop=True), pd.Series([True] * 3), book, P)
    assert len(trades) == 2                       # le 2e signal tombe pendant la 1re position
    m = trade_metrics(trades, cost_bps=10)
    assert m["moy_net"] == -0.002                 # prix plat : on perd exactement les frais aller-retour


def test_forward_returns_use_next_open():
    x = {"o": [100, 110, 120, 130], "c": [105, 115, 125, 135], "lo": [100, 110, 120, 130]}
    book = FakeBook({"SPY": flat(4), "X": x})
    sig = pd.DataFrame([{"symbol": "X", "date": pd.Timestamp("2026-01-05")},
                        {"symbol": "SPY", "date": pd.Timestamp("2026-01-05")}])
    out = add_forward_returns(sig, book, (2,))
    # entrée ouverture J+1 = 110, sortie clôture J+2 = 125
    assert abs(out.loc[0, "fwd_2"] - (125 / 110 - 1)) < 1e-12
    assert abs(out.loc[0, "xs_spy_2"] - (125 / 110 - 1)) < 1e-12


def test_portfolio_respects_capacity():
    book = FakeBook({"SPY": flat(10), **{f"S{i}": flat(10) for i in range(3)}})
    sig = pd.concat([sig_row(f"S{i}", "2026-01-05") for i in range(3)]).reset_index(drop=True)
    trades = simulate_trades(sig, pd.Series([True] * 3), book, P)
    p = BacktestParams(max_positions=2, position_weight=0.5, max_hold_sessions=5)
    eq = simulate_portfolio(trades, book, p, 0.0, pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-16"))
    assert abs(eq.iloc[-1] - 1.0) < 1e-12        # prix plats, sans frais : equity inchangée


def test_newey_west():
    rng = np.random.default_rng(0)
    x = pd.Series(rng.normal(0.1, 1, 2000))
    plain = x.mean() / (x.std() / math.sqrt(len(x)))
    assert abs(newey_west_tstat(x, 0) - plain) < 0.05 * abs(plain)
    # série autocorrélée (moyenne mobile) : le t de Newey-West est plus faible que le t naïf
    y = pd.Series(np.convolve(rng.normal(0.05, 1, 3000), np.ones(4) / 4, "valid"))
    naive = y.mean() / (y.std() / math.sqrt(len(y)))
    assert abs(newey_west_tstat(y, 4)) < abs(naive)
    assert newey_west_tstat(pd.Series([0.0] * 50), 2) is None


def test_run_validation_end_to_end_and_holdout_guard(engine, tmp_path):
    seed(engine)
    p = BacktestParams(warmup_sessions=200, every_n_sessions=10, horizons=(5, 20),
                       holdout_start=date(2026, 9, 1), embargo_days=30, min_trades=1)
    run_id, rep, prior = run_validation(engine, include_holdout=False, params=p,
                                        signals_cache=tmp_path / "sig.pkl")
    assert prior == 0 and rep.period["holdout_evalue"] is False and "holdout" not in rep.event_study
    assert (tmp_path / "sig.pkl").exists()
    md = render_markdown(run_id, rep, __import__("stockhunter.scoring.config", fromlist=["x"]).DEFAULT_CONFIG, prior)
    assert "Biais du survivant" in md and "NON évaluée" in md
    run_validation(engine, include_holdout=True, params=p, signals_cache=tmp_path / "sig.pkl")
    with pytest.raises(ScanError, match="déjà été évaluée 1 fois"):
        run_validation(engine, include_holdout=True, params=p, signals_cache=tmp_path / "sig.pkl")
    _, rep3, prior3 = run_validation(engine, include_holdout=True, allow_holdout_again=True, params=p,
                                     signals_cache=tmp_path / "sig.pkl")
    assert prior3 == 1 and "holdout" in rep3.event_study


def test_replay_matches_live_scan(engine):
    """Le rejeu utilise exactement le moteur du scan quotidien : mêmes scores à une même date."""
    from stockhunter.scan import run_scan
    from stockhunter.validation.backtest import replay
    seed(engine)
    run_scan(engine, date(2026, 11, 11))
    sig = replay(DataStore(engine), params=BacktestParams(warmup_sessions=0, every_n_sessions=1),
                 start=date(2026, 11, 11), end=date(2026, 11, 11))
    from sqlalchemy import select
    from stockhunter.db.models import Instrument, Score
    from stockhunter.db.session import session_scope
    with session_scope(engine) as s:
        live = dict(s.execute(select(Instrument.symbol, Score.total).join(Instrument)).all())
    assert dict(zip(sig["symbol"], sig["total"])) == live
