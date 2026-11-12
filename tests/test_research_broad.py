"""Étude univers large sur un marché SYNTHÉTIQUE avec un effet momentum planté."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stockhunter.data.calendar import trading_days
from stockhunter.db.models import MarketTicker
from stockhunter.db.session import session_scope
from stockhunter.market.store import MarketStore
from stockhunter.research import broad


def synthetic_market(n_tickers=300, n_days=420, seed=7, momentum=True):
    rng = np.random.default_rng(seed)
    days = trading_days(date(2025, 1, 2), date(2026, 12, 31))[:n_days]
    drift = rng.normal(0, 0.0015, n_tickers)          # tendance propre à chaque titre -> momentum persistant
    rets = rng.normal(0, 0.015, (n_days, n_tickers)) + (drift if momentum else 0)
    close = 20 * np.cumprod(1 + rets, axis=0)
    rows = []
    for j in range(n_tickers):
        for i, d in enumerate(days):
            if j == 0 and i > 300:            # ZZ0000 radié après la séance 300
                continue
            c = close[i, j]
            rows.append({"ticker": f"ZZ{j:04d}", "open": c * (1 - rets[i, j] / 2), "high": c * 1.01, "low": c * 0.99,
                         "close": c, "volume": 1e6, "vwap": c, "trades": 100.0, "otc": False, "date": d})
    return pd.DataFrame(rows), days


@pytest.fixture
def market(engine, tmp_path):
    long, days = synthetic_market()
    st = MarketStore(tmp_path / "m")
    for d, g in long.groupby("date"):
        st.write(d.date(), g.drop(columns="date").to_dict("records"))
    with session_scope(engine) as s:
        for t in long["ticker"].unique():
            s.add(MarketTicker(ticker_key=f"{t}|-", ticker=t, type="CS", primary_exchange="XNAS", active=t != "ZZ0000"))
        s.add(MarketTicker(ticker_key="ZZETF|-", ticker="ZZETF", type="ETF", primary_exchange="XNAS", active=True))
    return st, long, days


def test_planted_momentum_is_detected_and_no_lookahead(engine, market, monkeypatch):
    st, long, days = market
    p = broad.build_panel(long, pd.DataFrame(columns=["ticker", "execution_date", "split_from", "split_to"]),
                          set(long["ticker"]))
    f = broad.compute_factors(p)
    # point-in-time : modifier le futur ne change pas les facteurs à une date passée
    t0 = p.close.index[250]
    p2 = broad.Panel(*(x.copy() for x in (p.open, p.high, p.close, p.volume, p.raw_close, p.dollar_vol)))
    p2.close.loc[p2.close.index > t0] *= 3
    p2.open.loc[p2.open.index > t0] *= 0.5
    f2 = broad.compute_factors(p2)
    for k in f:
        pd.testing.assert_series_equal(f[k].loc[t0], f2[k].loc[t0])
    # radiation : rendement à terme = dernière clôture disponible
    fwd = broad.forward_returns(p, 20)
    d = p.close.index[295]
    assert fwd.loc[d, "ZZ0000"] == pytest.approx(p.close["ZZ0000"].iloc[300] / p.open["ZZ0000"].iloc[296] - 1)
    # l'étude complète détecte le momentum planté (dates de dév. élargies pour ce marché synthétique)
    monkeypatch.setattr(broad, "DEV_END", date(2026, 12, 31))
    res = broad.run_study(engine, st)
    h1 = res.factors["H1"]["developpement"][20]
    assert h1["ic"] > 0.1 and h1["t_ic"] > broad.T_THRESHOLD
    assert res.verdicts["H1"]["decision"].startswith("PROMETTEUR")
    assert res.meta["titres_eligibles"] == 300          # l'ETF est exclu
    assert "Étude pré-enregistrée" in broad.render(res)


def test_split_does_not_create_fake_crash(engine):
    long = pd.DataFrame([{"ticker": "ZZA", "date": pd.Timestamp(d), "open": o, "high": o, "low": o, "close": o,
                          "volume": 100.0, "vwap": o, "trades": 1.0, "otc": False}
                         for d, o in ((date(2026, 5, 28), 400.0), (date(2026, 5, 29), 404.0), (date(2026, 6, 1), 101.0))])
    splits = pd.DataFrame([{"ticker": "ZZA", "execution_date": date(2026, 6, 1), "split_from": 1.0, "split_to": 4.0}])
    p = broad.build_panel(long, splits, {"ZZA"})
    assert list(p.close["ZZA"]) == [100.0, 101.0, 101.0]
    assert list(p.raw_close["ZZA"]) == [400.0, 404.0, 101.0]   # le filtre de prix utilise le cours réel
    assert list(p.volume["ZZA"]) == [400.0, 400.0, 100.0]


def test_no_effect_market_is_not_retained(engine, tmp_path, monkeypatch):
    long, _ = synthetic_market(momentum=False, seed=11)
    st = MarketStore(tmp_path / "m2")
    for d, g in long.groupby("date"):
        st.write(d.date(), g.drop(columns="date").to_dict("records"))
    with session_scope(engine) as s:
        for t in long["ticker"].unique():
            s.add(MarketTicker(ticker_key=f"{t}|-", ticker=t, type="CS", primary_exchange="XNYS", active=True))
    monkeypatch.setattr(broad, "DEV_END", date(2026, 12, 31))
    res = broad.run_study(engine, st)
    retained = [k for k, v in res.verdicts.items() if v["decision"].startswith("PROMETTEUR")]
    assert len(retained) <= 1   # sans effet planté, au plus un faux positif toléré au seuil corrigé
