"""3.2.1 — portefeuille mondial : absence d'information future (look-ahead), horodatage, capital, frais.

Données SYNTHÉTIQUES. Ces tests perturbent des informations qui N'EXISTAIENT PAS au moment d'une décision
(clôture du jour, cours futurs, taux BCE publié plus tard) et exigent que la décision reste identique. Les tests
« non-régression » échouent sur le simulateur 3.2 (preuve dans docs/v3-2/rapport-v3-2-1.md).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from stockhunter.world import markets as mk
from stockhunter.world.fx import FxTable, ecb_known_date, ecb_published_at
from stockhunter.world.portfolio import GlobalConfig, SignalIn, session_times, simulate

UTC = timezone.utc


def sessions(market, start=date(2026, 9, 1), end=date(2026, 11, 13)):
    return [pd.Timestamp(s.session) for s in mk.sessions(mk.get(market), start, end)]


def bars(market, start=100.0, drift=0.001, seed=1, vol=0.01, start_day=date(2026, 9, 1), end=date(2026, 11, 13)):
    idx = sessions(market, start_day, end)
    rng = np.random.default_rng(seed)
    c = start * np.cumprod(1 + rng.normal(drift, vol, len(idx)))
    o = np.r_[start, c[:-1]] * (1 + rng.normal(0, 0.002, len(idx)))
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.004, "low": np.minimum(o, c) * 0.996, "close": c},
                        index=idx)


def fx(start=date(2026, 8, 1), end=date(2026, 11, 20), usd=1.10, inr=95.0, nok=11.5, overrides=None):
    rows = []
    for d in pd.bdate_range(start, end):
        for c, v in (("USD", usd), ("INR", inr), ("NOK", nok)):
            v = (overrides or {}).get((d.date(), c), v)
            rows.append({"date": d.date(), "currency": c, "rate": v})
    return FxTable.from_long(pd.DataFrame(rows))


def sig(market, key, df, i, ccy, country, atr=None, norm=1.0, cat="momentum"):
    d = df.index[i]
    c = float(df["close"].iloc[i])
    return SignalIn(market, key, key, d.date(), cat, norm, c, atr or c * 0.02, ccy, country)


def entry_decisions(res):
    return [(x["place"], x["ticker"], x["seance"], x["decision"], x["quantite"]) for x in res.decisions]


# ============================================================================ horodatage
def test_ecb_rate_known_only_after_16h_frankfurt_with_dst():
    assert ecb_published_at(date(2026, 11, 13)) == datetime(2026, 11, 13, 15, 0, tzinfo=UTC)      # hiver
    assert ecb_published_at(date(2026, 7, 13)) == datetime(2026, 7, 13, 14, 0, tzinfo=UTC)        # été
    assert ecb_known_date(datetime(2026, 11, 13, 3, 45, tzinfo=UTC)) == date(2026, 11, 12)        # ouverture Inde
    assert ecb_known_date(datetime(2026, 11, 13, 14, 30, tzinfo=UTC)) == date(2026, 11, 12)       # ouverture US
    assert ecb_known_date(datetime(2026, 11, 13, 21, 0, tzinfo=UTC)) == date(2026, 11, 13)        # clôture US


def test_session_times_follow_dst_per_market():
    w = session_times("XPAR", [date(2026, 3, 27), date(2026, 3, 30)])
    assert w[date(2026, 3, 27)][0].hour == 8 and w[date(2026, 3, 30)][0].hour == 7            # passage à l'heure d'été
    us = session_times("US", [date(2026, 3, 6), date(2026, 3, 9)])
    assert us[date(2026, 3, 6)][0].hour == 14 and us[date(2026, 3, 9)][0].hour == 13          # DST américain plus tôt
    assert session_times("XNSE", [date(2026, 3, 30)])[date(2026, 3, 30)][0] == datetime(2026, 3, 30, 3, 45, tzinfo=UTC)


# ============================================================================ non-régression look-ahead
def test_same_day_close_of_held_position_never_used_at_the_open():
    """Échoue sur 3.2 : l'exposition au pays était calculée avec la clôture du jour d'une position déjà ouverte."""
    a, b = bars("XNSE", 1000.0, seed=3), bars("XNSE", 500.0, seed=4)
    i_a = 10
    d_b = 30                                     # B entre à l'ouverture de la séance d_b + 1
    sigs = [sig("XNSE", "ZZA", a, i_a, "INR", "IN"), sig("XNSE", "ZZB", b, d_b, "INR", "IN", norm=0.9)]
    cfg = GlobalConfig(base="EUR", max_country=0.40, max_weight=0.15, max_corr=1.01)
    base = simulate(sigs, {("XNSE", "ZZA"): a, ("XNSE", "ZZB"): b}, fx(), cfg)
    held = [t for t in base.trades if t["ticker"] == "ZZA"]
    assert entry_decisions(base)[0][1] == "ZZA"
    a2 = a.copy()
    a2.iloc[d_b + 1, a2.columns.get_loc("close")] *= 8      # clôture du jour de l'entrée de B (connue à 10 h UTC)
    a2.iloc[d_b + 1, a2.columns.get_loc("high")] *= 8
    pert = simulate(sigs, {("XNSE", "ZZA"): a2, ("XNSE", "ZZB"): b}, fx(), cfg)
    dec_b = lambda r: [x for x in entry_decisions(r) if x[1] == "ZZB"]
    assert dec_b(base) == dec_b(pert) and dec_b(base)[0][3] == "entrée"
    assert not [x for x in pert.refused if x["ticker"] == "ZZB"]
    assert held or base.summary["positions_ouvertes"] >= 1


def test_ecb_rate_published_after_the_order_is_not_used():
    """Échoue sur 3.2 : un ordre passé à Mumbai à 3 h 45 UTC était converti au taux BCE publié à 15 h UTC."""
    b = bars("XNSE", 2000.0, seed=5)
    i = 20
    d_entry = b.index[i + 1].date()
    s = [sig("XNSE", "ZZI", b, i, "INR", "IN")]
    cfg = GlobalConfig(base="EUR", max_corr=1.01)
    r0 = simulate(s, {("XNSE", "ZZI"): b}, fx(), cfg)
    r1 = simulate(s, {("XNSE", "ZZI"): b}, fx(overrides={(d_entry, "INR"): 0.0001}), cfg)   # roupie « hors de prix »
    assert entry_decisions(r0) == entry_decisions(r1) and entry_decisions(r0)[0][3] == "entrée"
    assert r0.decisions[0]["taux_bce_du"] == str(b.index[i].date())
    assert [x["motif"] for x in r1.refused] == [x["motif"] for x in r0.refused]


def test_future_prices_do_not_change_past_decisions():
    p, n = bars("XPAR", 50.0, seed=7), bars("XNSE", 800.0, seed=8)
    sigs = [sig("XPAR", "ZZP", p, 12, "EUR", "FR"), sig("XNSE", "ZZN", n, 15, "INR", "IN", norm=0.8),
            sig("XPAR", "ZZQ", p, 25, "EUR", "FR", norm=0.7)]
    cut = pd.Timestamp(2026, 10, 15)
    p2, n2 = p.copy(), n.copy()
    for df in (p2, n2):
        df.loc[df.index > cut, ["open", "high", "low", "close"]] *= 1.7
    r0 = simulate(sigs, {("XPAR", "ZZP"): p, ("XNSE", "ZZN"): n, ("XPAR", "ZZQ"): p}, fx(), GlobalConfig(max_corr=1.01))
    r1 = simulate(sigs, {("XPAR", "ZZP"): p2, ("XNSE", "ZZN"): n2, ("XPAR", "ZZQ"): p2}, fx(), GlobalConfig(max_corr=1.01))
    before = lambda r: [x for x in r.decisions if x["seance"] <= str(cut.date())]
    assert before(r0) == before(r1) and before(r0)
    assert r0.equity.loc[:cut].equals(r1.equity.loc[:cut])


def test_correlation_uses_only_closed_returns():
    a, b = bars("XPAR", 100.0, seed=11), bars("XPAR", 100.0, seed=12)
    sigs = [sig("XPAR", "ZZA", a, 45, "EUR", "FR"), sig("XPAR", "ZZB", b, 50, "EUR", "FR", norm=0.9)]
    cfg = GlobalConfig(max_corr=0.5, corr_min_common=40, max_country=1.0)
    r0 = simulate(sigs, {("XPAR", "ZZA"): a, ("XPAR", "ZZB"): b}, fx(), cfg)
    b2 = b.copy()
    j = 51                                                     # séance d'entrée de B : rendement du jour inconnu
    b2.iloc[j, b2.columns.get_loc("close")] = b2["close"].iloc[j - 1] * (a["close"].iloc[j] / a["close"].iloc[j - 1])
    r1 = simulate(sigs, {("XPAR", "ZZA"): a, ("XPAR", "ZZB"): b2}, fx(), cfg)
    dec = lambda r: [x for x in entry_decisions(r) if x[1] == "ZZB"]
    assert dec(r0) == dec(r1)


# ============================================================================ plusieurs places le même jour
def test_india_europe_us_same_day_chronology_and_information():
    d0 = date(2026, 10, 6)                      # 02/10 : férié en Inde (Gandhi Jayanti) — vérifié par le moteur
    n, p, u = bars("XNSE", 900.0, seed=21), bars("XPAR", 40.0, seed=22), bars("US", 60.0, seed=23)
    # trois signaux de la même date : entrées le lendemain dans l'ordre Mumbai -> Paris -> New York
    i = lambda df: list(df.index).index(pd.Timestamp(d0))
    sigs = [sig("US", "ZZU", u, i(u), "USD", "US", norm=0.5), sig("XPAR", "ZZP", p, i(p), "EUR", "FR", norm=0.6),
            sig("XNSE", "ZZN", n, i(n), "INR", "IN", norm=0.7)]
    bs = {("XNSE", "ZZN"): n, ("XPAR", "ZZP"): p, ("US", "ZZU"): u}
    r = simulate(sigs, bs, fx(), GlobalConfig(max_corr=1.01))
    times = [x["instant"] for x in r.decisions]
    assert times == sorted(times) and [x["place"] for x in r.decisions] == ["XNSE", "XPAR", "US"]
    # la clôture indienne du jour (10 h UTC) est connue à l'ouverture US (14 h 30) mais pas à celle de Paris (8 h)
    n2 = n.copy()
    j = list(n.index).index(pd.Timestamp(r.decisions[0]["seance"]))
    n2.iloc[j, n2.columns.get_loc("close")] *= 1.3
    n2.iloc[j, n2.columns.get_loc("high")] *= 1.3
    r2 = simulate(sigs, {**bs, ("XNSE", "ZZN"): n2}, fx(), GlobalConfig(max_corr=1.01))
    cap = lambda r, m: [x["capital_connu"] for x in r.decisions if x["place"] == m]
    assert cap(r, "XPAR") == cap(r2, "XPAR")                      # Paris ouvre avant la clôture de Mumbai
    if r.decisions[0]["seance"] == r.decisions[2]["seance"]:
        assert cap(r, "US") != cap(r2, "US")                      # New York ouvre après : information connue
    assert r.decisions[0]["taux_bce_du"] < r.decisions[0]["seance"]


# ============================================================================ capital, frais, gaps, données manquantes
def test_cash_positions_and_fees_are_consistent():
    sigs, bs = [], {}
    for k in range(12):
        df = bars("XPAR", 20.0 + k, seed=40 + k)
        bs[("XPAR", f"ZZ{k}")] = df
        sigs.append(sig("XPAR", f"ZZ{k}", df, 10 + k, "EUR", "FR", norm=1 - k / 20))
    cfg = GlobalConfig(capital=20_000.0, max_country=1.0, max_weight=0.5, max_corr=1.01, risk_per_position=0.05)
    r = simulate(sigs, bs, fx(), cfg)
    s = r.summary
    assert s["tresorerie_finale"] >= 0                                    # jamais plus que la trésorerie
    tr_fees = sum(t["courtage_base"] + t["frais_change_base"] for t in r.trades)
    assert tr_fees <= s["frais"]["courtage"] + s["frais"]["change"] + 1e-6
    if s["positions_ouvertes"] == 0:
        assert s["valeur_finale"] == pytest.approx(s["tresorerie_finale"], abs=0.01)
        assert tr_fees == pytest.approx(s["frais"]["courtage"] + s["frais"]["change"], abs=0.05)
    assert all(x["capital_connu"] > 0 for x in r.decisions)
    assert any("trésorerie" in (x["motif"] or "") or "positions" in (x["motif"] or "") for x in r.refused) or len(r.trades) <= 12


def test_gap_outside_entry_zone_is_refused_at_the_open():
    df = bars("XPAR", 100.0, seed=60)
    i = 20
    df.iloc[i + 1, df.columns.get_loc("open")] = df["close"].iloc[i] * 1.10        # gap de +10 % > 3 %
    r = simulate([sig("XPAR", "ZZG", df, i, "EUR", "FR")], {("XPAR", "ZZG"): df}, fx(), GlobalConfig())
    assert not r.trades and "gap d'ouverture" in r.refused[0]["motif"] and r.decisions[0]["quantite"] == 0


def test_missing_bars_expire_plan_and_close_stale_position():
    df = bars("XNSE", 300.0, seed=70)
    i = 10
    gone = df.drop(df.index[i + 1:i + 7])                                          # 6 séances sans cotation
    r = simulate([sig("XNSE", "ZZM", gone, i, "INR", "IN")], {("XNSE", "ZZM"): gone,
                 ("XNSE", "ZZX"): df}, fx(), GlobalConfig())
    assert r.refused and "5 séances" in r.refused[0]["motif"]
    df2 = bars("XNSE", 300.0, seed=71, drift=0.0, vol=0.002)
    j = 10
    stale = df2.drop(df2.index[j + 3:j + 20])                                      # cotation interrompue après l'entrée
    r2 = simulate([sig("XNSE", "ZZS", stale, j, "INR", "IN", atr=30.0)], {("XNSE", "ZZS"): stale,
                  ("XNSE", "ZZX"): df2}, fx(), GlobalConfig())
    assert r2.trades and "10 séances" in r2.trades[0]["motif"]


def test_deterministic_and_order_independent():
    a, b = bars("XPAR", 30.0, seed=80), bars("XAMS", 30.0, seed=81)
    s1 = [sig("XPAR", "ZZA", a, 10, "EUR", "FR", norm=0.9), sig("XAMS", "ZZB", b, 10, "EUR", "NL", norm=0.9)]
    bs = {("XPAR", "ZZA"): a, ("XAMS", "ZZB"): b}
    r1, r2 = simulate(s1, bs, fx(), GlobalConfig()), simulate(list(reversed(s1)), dict(reversed(bs.items())), fx(), GlobalConfig())
    assert r1.decisions == r2.decisions and r1.equity.equals(r2.equity) and r1.trades == r2.trades


# ============================================================================ sensibilité des tests
@pytest.mark.parametrize("engine_name", ["v32", "v321"])
def test_perturbations_detect_the_legacy_lookahead(engine_name):
    """Les mêmes perturbations changent les décisions du simulateur 3.2 (look-ahead) et pas celles du 3.2.1."""
    from stockhunter.world import portfolio as new
    from tests.legacy import portfolio_v32 as old
    eng = old if engine_name == "v32" else new
    conv = lambda s: eng.SignalIn(**{k: getattr(s, k) for k in ("market", "key", "ticker", "as_of", "category",
                                                                "norm", "signal_close", "atr", "currency", "country")})
    a, b = bars("XNSE", 1000.0, seed=3), bars("XNSE", 500.0, seed=4)
    sigs = [conv(sig("XNSE", "ZZA", a, 10, "INR", "IN")), conv(sig("XNSE", "ZZB", b, 30, "INR", "IN", norm=0.9))]
    cfg = eng.GlobalConfig(base="EUR", max_country=0.40, max_weight=0.15, max_corr=1.01)
    a2 = a.copy()
    a2.iloc[31, a2.columns.get_loc("close")] *= 8
    a2.iloc[31, a2.columns.get_loc("high")] *= 8
    refused_b = lambda r: [x["motif"] for x in r.refused if x["ticker"] == "ZZB"]
    same_close = (refused_b(eng.simulate(sigs, {("XNSE", "ZZA"): a, ("XNSE", "ZZB"): b}, fx(), cfg))
                  == refused_b(eng.simulate(sigs, {("XNSE", "ZZA"): a2, ("XNSE", "ZZB"): b}, fx(), cfg)))
    bb = bars("XNSE", 2000.0, seed=5)
    s = [conv(sig("XNSE", "ZZI", bb, 20, "INR", "IN"))]
    q0 = eng.simulate(s, {("XNSE", "ZZI"): bb}, fx(), eng.GlobalConfig(max_corr=1.01))
    q1 = eng.simulate(s, {("XNSE", "ZZI"): bb}, fx(overrides={(bb.index[21].date(), "INR"): 0.0001}),
                      eng.GlobalConfig(max_corr=1.01))
    same_fx = len(q0.trades) == len(q1.trades) and q0.refused == q1.refused
    if engine_name == "v32":
        assert not same_close and not same_fx          # le look-ahead de 3.2 est bien détecté
    else:
        assert same_close and same_fx
