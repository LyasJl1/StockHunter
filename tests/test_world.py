"""3.2 — marchés mondiaux. Données SYNTHÉTIQUES (ISIN « ZZ… », symboles ZZ*), aucun appel réseau."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import func, select

from stockhunter.db.models import WorldListing, WorldOutcome, WorldScan, WorldSignal, WorldTickerChange
from stockhunter.db.session import session_scope
from stockhunter.world import markets as mk
from stockhunter.world import providers as pv
from stockhunter.world import ranking as rk
from stockhunter.world import run as wr
from stockhunter.world import scan as ws
from stockhunter.world import sync
from stockhunter.world.fx import FxTable, FxUnavailable, conversion_cost, decompose
from stockhunter.world.portfolio import GlobalConfig, SignalIn, benchmarks, simulate
from stockhunter.world.store import StorageBudgetExceeded, WorldStore
from tests.world_fixtures import bhavcopy, euronext_csv, fx_table, synth_market

UTC = timezone.utc
D = date(2026, 11, 13)


@pytest.fixture
def store(tmp_path):
    return WorldStore(tmp_path / "world")


# ============================================================================ 1. places boursières
def test_registry_covers_all_target_markets_honestly():
    wanted = {"US", "XPAR", "XETR", "XLON", "XAMS", "XBRU", "XMIL", "XMAD", "XSWX", "XSTO", "XTSE", "XTKS", "XSHG",
              "XSHE", "XHKG", "XNSE", "XBOM", "XKRX", "XASX", "BVMF", "XMEX", "XJSE", "XCAS", "XSAU", "XDFM"}
    assert wanted <= set(mk.MARKETS)
    for m in mk.MARKETS.values():
        assert m.region in mk.REGIONS and len(m.currency) == 3 and len(m.country) == 2
        datetime(2026, 1, 1, tzinfo=__import__("zoneinfo").ZoneInfo(m.tz))       # fuseau valide
    priced = {m.code for m in mk.with_prices()}
    assert priced == {"US", "XPAR", "XAMS", "XBRU", "XLIS", "XDUB", "XMIL", "XOSL", "XNSE", "XBOM"}
    assert mk.get("XLON").data == mk.NONE and mk.get("XSHG").data == mk.QUOTA      # pas de couverture prétendue


# ============================================================================ 2. symboles identiques
def test_identical_tickers_on_two_exchanges_are_distinct_listings(engine):
    nse = pd.DataFrame([{**_row("XNSE", "ZZABC", "ZZE000000001"), "date": D}])
    bse = pd.DataFrame([{**_row("XBOM", "ZZABC", "ZZE000000002"), "date": D}])
    sync.update_master(engine, nse, "nse")
    sync.update_master(engine, bse, "bse")
    with session_scope(engine) as s:
        rows = s.execute(select(WorldListing.market, WorldListing.isin).where(WorldListing.ticker == "ZZABC")).all()
    assert sorted(rows) == [("XBOM", "ZZE000000002"), ("XNSE", "ZZE000000001")]
    a = _rk_row("ZZE000000001", "XNSE", "ZZABC")
    b = _rk_row("ZZE000000002", "XBOM", "ZZABC")
    assert len(rk.merge([a, b])) == 2                                # même symbole, sociétés différentes


def test_same_isin_on_two_exchanges_is_one_company(engine):
    a, b = _rk_row("ZZE000000009", "XNSE", "ZZDUO", norm=0.6), _rk_row("ZZE000000009", "XBOM", "ZZDUO", norm=0.9)
    out = rk.merge([a, b])
    assert len(out) == 1 and out[0].market == "XNSE" and out[0].norm == 0.9 and out[0].also_listed == ["ZZDUO (XBOM)"]


# ============================================================================ 3. ADR
def test_adr_never_merged_by_name_only_by_explicit_link():
    adr = _rk_row("US:ZZADR", "US", "ZZADR", name="ZZ Holding")
    adr.instrument = "ADR"
    local = _rk_row("ZZFR00000001", "XPAR", "ZZH", name="ZZ Holding")
    assert len(rk.merge([adr, local])) == 2
    merged = rk.merge([adr, local], links={"US:ZZADR": "ZZFR00000001"})
    assert len(merged) == 1 and merged[0].market == "US"            # priorité US (place la plus liquide)
    assert merged[0].also_listed == ["ZZH (XPAR)"]


# ============================================================================ 4. et 5. devises, change historique
def test_currency_conversion_cross_rates_and_missing_currency():
    fx = fx_table(date(2026, 11, 2), D, usd=1.10, inr=95.0)
    assert fx.rate("USD", "EUR", D) == pytest.approx(1 / 1.10)
    assert fx.rate("INR", "USD", D) == pytest.approx(1.10 / 95.0)
    assert fx.rate("EUR", "EUR", D) == 1.0
    with pytest.raises(FxUnavailable):
        fx.rate("SAR", "EUR", D)                                     # jamais de taux inventé
    d = decompose(100, 110, 1.0, 0.95)
    assert d["local"] == pytest.approx(0.10) and d["converti"] == pytest.approx(1.1 * 0.95 - 1)
    assert d["effet_change"] == pytest.approx(d["converti"] - d["local"])
    assert conversion_cost(10_000, 25) == 25.0


def test_historical_fx_is_point_in_time():
    fx = FxTable.from_long(pd.DataFrame([{"date": date(2026, 11, 2), "currency": "USD", "rate": 1.10},
                                         {"date": date(2026, 11, 6), "currency": "USD", "rate": 1.20}]))
    assert fx.per_eur("USD", date(2026, 11, 5)) == 1.10               # dernier taux publié ≤ date, jamais le futur
    assert fx.per_eur("USD", date(2026, 11, 8)) == 1.20               # week-end : dernier taux connu
    with pytest.raises(FxUnavailable):
        fx.per_eur("USD", date(2026, 11, 1))                          # avant la première publication
    s = fx.series("USD", "EUR", pd.DatetimeIndex(["2026-11-01", "2026-11-03", "2026-11-06"]))
    assert np.isnan(s.iloc[0]) and s.iloc[1] == pytest.approx(1 / 1.10) and s.iloc[2] == pytest.approx(1 / 1.20)


# ============================================================================ 6. fuseaux horaires
def test_tokyo_close_never_mixed_with_future_us_close():
    noon = datetime(2026, 11, 13, 13, 30, tzinfo=UTC)                 # 22 h 30 à Tokyo, 8 h 30 à New York
    assert mk.last_completed_session(mk.get("XTKS"), noon) == D       # clôture de Tokyo du jour terminée
    assert mk.last_completed_session(mk.get("US"), noon) == date(2026, 11, 12)   # séance US du 13 pas commencée
    exp = mk.expected_sessions(noon, ("XTKS", "US", "XNSE", "XPAR"))
    assert exp == {"XTKS": "2026-11-13", "US": "2026-11-12", "XNSE": "2026-11-13", "XPAR": "2026-11-12"}


def test_us_session_counted_once_published_same_evening():
    """Correctif 3.2 : la séance US du jour est attendue dès sa publication (clôture + 3 h), plus le lendemain."""
    assert mk.last_completed_session(mk.get("US"), datetime(2026, 11, 13, 23, 30, tzinfo=UTC)) == date(2026, 11, 12)
    assert mk.last_completed_session(mk.get("US"), datetime(2026, 11, 14, 0, 37, tzinfo=UTC)) == D
    from stockhunter.pipeline import expected_session
    assert expected_session(datetime(2026, 11, 14, 0, 37, tzinfo=UTC)) == D


# ============================================================================ 7. et 8. fériés locaux, places fermées
def test_local_holidays_and_closed_markets():
    assert not mk.is_session(mk.get("XPAR"), date(2026, 12, 25))
    assert not mk.is_session(mk.get("XNSE"), date(2026, 1, 26))      # fête de la République (Inde)
    assert mk.is_session(mk.get("US"), date(2026, 1, 26))
    assert not mk.is_session(mk.get("US"), date(2026, 11, 26))       # Thanksgiving : US fermé…
    assert mk.is_session(mk.get("XPAR"), date(2026, 11, 26))         # … Paris ouvert
    sat = datetime(2026, 11, 14, 12, tzinfo=UTC)
    assert not mk.session_in_progress(mk.get("XPAR"), sat)
    assert mk.session_in_progress(mk.get("XPAR"), datetime(2026, 11, 13, 14, tzinfo=UTC))


def test_calendar_fallback_is_flagged_beyond_coverage():
    ss = mk.sessions(mk.get("XCAS"), date(2026, 11, 9), date(2026, 11, 13))
    assert len(ss) == 5 and all("non disponible" in s.source for s in ss)


def test_euronext_refuses_snapshot_during_session(store, engine):
    calls = []
    fetch = pv.Fetcher(get=lambda u: calls.append(u) or (200, b"", ""), min_interval_s=0, sleep=lambda s: None)
    reps = sync.sync_euronext(store, engine, fetch, datetime(2026, 11, 13, 14, tzinfo=UTC))
    assert not calls and all("non définitif" in r.detail for r in reps)


# ============================================================================ analyse des sources
def test_parse_euronext_markets_segments_and_untraded_rows():
    body = euronext_csv("14 Nov 2026", [
        ("ZZ Alpha", "ZZFR00000001", "ZZA", "Euronext Paris", "EUR", 10, 11, 9.5, 10.5, 1000, 10500),
        ("ZZ Beta", "ZZNL00000002", "ZZB", "Euronext Growth Amsterdam", "EUR", 5, 5.2, 4.9, 5.1, 200, 1020),
        ("ZZ Oslo", "ZZNO00000003", "ZZO", "Oslo Børs", "NOK", 50, 51, 49, 50.5, 10, 505),
        ("ZZ Dormant", "ZZFR00000004", "ZZD", "Euronext Paris", "EUR", "-", "-", "-", 7.0, "-", "-"),
        ("ZZ US", "ZZUS00000005", "ZZU", "Euronext Global Equity Market", "EUR", 1, 1, 1, 1, 5, 5),
    ])
    df, meta = pv.parse_euronext(body, D)
    assert dict(zip(df["ticker"], df["market"])) == {"ZZA": "XPAR", "ZZB": "XAMS", "ZZO": "XOSL", "ZZD": "XPAR"}
    dormant = df[df["ticker"] == "ZZD"].iloc[0]
    assert not dormant["traded"] and pd.isna(dormant["close"])        # cours de la veille jamais présenté comme clôture
    assert df[df["ticker"] == "ZZB"]["segment"].iloc[0] == "Growth" and meta["ecartees"]["lieu secondaire"] == 1
    with pytest.raises(pv.SourceUnavailable):
        pv.parse_euronext(euronext_csv("12 Nov 2026", []), D)         # instantané antérieur à la séance : périmé


def test_parse_bhavcopy_keeps_equities_only():
    body = bhavcopy(D, [("ZZE000000001", "ZZA", "EQ", "ZZ A LTD", 10, 11, 9, 10.5, 10, 1000, 10500, 1),
                        ("IN0020200104", "SGB28", "GB", "GOLD BOND", 1, 1, 1, 1, 1, 5, 5, 1),
                        ("ZZF000000001", "ZZETF", "EQ", "ZZ ETF", 1, 1, 1, 1, 1, 5, 5, 1)])
    body = bhavcopy(D, [("INE000000001", "ZZA", "EQ", "ZZ A LTD", 10, 11, 9, 10.5, 10, 1000, 10500, 1),
                        ("IN0020200104", "SGB28", "GB", "GOLD BOND", 1, 1, 1, 1, 1, 5, 5, 1),
                        ("INF000000001", "ZZETF", "EQ", "ZZ ETF", 1, 1, 1, 1, 1, 5, 5, 1)], zipped=False)
    df, meta = pv.parse_bhavcopy(body, D, "XNSE")
    assert list(df["ticker"]) == ["ZZA"] and meta["autres_instruments"] == 2
    assert df["prev_close_adj"].iloc[0] == 10 and df["currency"].iloc[0] == "INR"
    with pytest.raises(pv.SourceUnavailable):
        pv.parse_bhavcopy(bhavcopy(D - timedelta(days=1), [("INE000000001", "ZZA", "EQ", "A", 1, 1, 1, 1, 1, 1, 1, 1)]),
                          D, "XNSE")


def test_parse_ecb():
    body = b"KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n" \
           b"EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-11-13,1.1193\nEXR.D.JPY.EUR.SP00.A,D,JPY,EUR,SP00,A,2026-11-13,\n"
    df = pv.parse_ecb(body)
    assert len(df) == 1 and df.iloc[0]["rate"] == 1.1193


# ============================================================================ 9. et 10. données manquantes, périmées
def test_missing_file_old_vs_recent(store, engine):
    def get(url):
        return (404, b"", "") if "20261112" in url or "20261113" in url else \
            (200, bhavcopy(_url_date(url), [
                ("INE000000001", "ZZA", "EQ", "ZZ A", 10, 11, 9, 10, 10, 100, 1000, 1)]), "application/zip")
    fetch = pv.Fetcher(get=get, min_interval_s=0, sleep=lambda s: None)
    rep = sync.sync_india(store, engine, fetch, "XNSE", datetime(2026, 11, 13, 14, tzinfo=UTC), backfill_sessions=4)
    assert "2026-11-12" in rep.missing                                # ancien : consigné absent, jamais inventé
    assert any("pas encore publié" in e for e in rep.errors)          # séance du jour : réessayée plus tard
    assert not store.is_missing("XNSE", D) and store.has("XNSE", date(2026, 11, 11))


def test_freshness_counts_sessions_not_days():
    m = mk.get("XPAR")
    f = mk.freshness(m, date(2026, 11, 12), datetime(2026, 11, 16, 20, tzinfo=UTC))
    assert f.expected == date(2026, 11, 16) and f.lag == 2 and f.stale
    assert not mk.freshness(m, date(2026, 11, 13), datetime(2026, 11, 15, 12, tzinfo=UTC)).stale


# ============================================================================ 11. divisions et dividendes
def test_split_adjustment_from_exchange_reference_price():
    rows = []
    closes = [1000, 1010, 505, 510]                  # division 2:1 le 3e jour : référence ajustée publiée = 505
    prev = [None, 1000, 505, 505]
    for k, (c, p) in enumerate(zip(closes, prev)):
        rows.append({**_row("XNSE", "ZZS", "INE00000000S"), "date": pd.Timestamp(2026, 11, 9 + k), "close": c,
                     "open": c, "high": c, "low": c, "prev_close_adj": p})
    rows.append({**_row("XNSE", "ZZD", "INE00000000D"), "date": pd.Timestamp(2026, 11, 10), "close": 99.0,
                 "prev_close_adj": 99.5})                         # dividende : écart < 2 % ignoré
    long = pd.DataFrame(rows)
    f = ws._adjust_factors(long)
    adj = (long["close"] * f).tolist()
    assert adj[:4] == pytest.approx([500, 505, 505, 510]) and f.iloc[4] == 1.0


def test_unexplained_jump_truncates_history(store):
    fx = fx_table(date(2025, 1, 1), D)
    synth_market(store, "XPAR", 150, 10)
    last = store.stored_sessions("XPAR")[-5]
    df = pd.read_parquet(store.path("XPAR", last))
    df.loc[df["ticker"] == "ZZ01", ["open", "high", "low", "close"]] *= 3   # division non corrigée par la source
    df.to_parquet(store.path("XPAR", last), index=False)
    p, info = ws.build_market_panel(store, "XPAR", fx)
    col = [c for c in p.close.columns if c.endswith("00000001")][0]
    assert info["titres_tronques"] == 1 and p.close[col].notna().sum() == 3    # après le retour à la normale


# ============================================================================ scanner, 12. reproductible, 13. sans look-ahead
def test_market_scan_insufficient_history_is_declared(store, engine):
    synth_market(store, "XPAR", 20)
    res = ws.scan_market(engine, store, "XPAR", fx_table(date(2026, 1, 1), D))
    assert res["statut"] == "HISTORIQUE_INSUFFISANT" and "20 séance(s)" in res["conclusion"]
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(WorldSignal)) == 0
        assert s.scalar(select(WorldScan.status)) == "HISTORIQUE_INSUFFISANT"


def test_market_scan_runs_saves_and_is_reproducible(store, engine):
    fx = fx_table(date(2025, 1, 1), D)
    synth_market(store, "XPAR", 260, 40)
    a = ws.scan_market(engine, store, "XPAR", fx, save=False)
    b = ws.scan_market(engine, store, "XPAR", fx, save=False)
    pick = lambda r: [(k, c["isin"], c["rank"]) for k, v in r["categories"].items() for c in v["candidats"]]
    assert a["statut"] in ("OK", "AUCUNE_OPPORTUNITE") and pick(a) == pick(b) and a["univers"] == 40
    assert "€" in json.dumps(a, ensure_ascii=False) or not pick(a)
    ws.scan_market(engine, store, "XPAR", fx)
    ws.scan_market(engine, store, "XPAR", fx)                          # jamais réécrit
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(WorldScan)) == 1
        n = s.scalar(select(func.count()).select_from(WorldSignal))
    assert n == len(pick(a))


def test_world_scan_has_no_look_ahead(store, engine):
    fx = fx_table(date(2025, 1, 1), date(2026, 12, 31))
    ss = synth_market(store, "XAMS", 240, 30, end=D)
    cut = ss[-20]
    early = ws.scan_market(engine, store, "XAMS", fx, save=False, end=cut)
    assert early["as_of"] == str(cut)
    full = WorldStore(store.root)                                     # mêmes fichiers ; la fin est ignorée
    again = ws.scan_market(engine, full, "XAMS", fx, save=False, end=cut)
    pick = lambda r: [(k, c["isin"], c["score"]) for k, v in r["categories"].items() for c in v["candidats"]]
    assert pick(early) == pick(again)
    # ajouter des séances futures ne change pas le scan daté de `cut`
    p_cut, _ = ws.build_market_panel(store, "XAMS", fx, end=cut)
    assert p_cut.close.index[-1].date() == cut


def test_ranking_deterministic_and_filters(engine):
    rows = [_rk_row("ZZFR1", "XPAR", "ZZB", norm=0.9), _rk_row("ZZFR2", "XPAR", "ZZA", norm=0.9),
            _rk_row("ZZIN1", "XNSE", "ZZC", norm=1.0), _rk_row("US:ZZU", "US", "ZZU", norm=0.5)]
    o1 = [r.ticker for r in rk.order(rk.merge(rows))]
    o2 = [r.ticker for r in rk.order(rk.merge(list(reversed(rows))))]
    assert o1 == o2 == ["ZZC", "ZZA", "ZZB", "ZZU"]
    ordered = rk.order(rk.merge(rows))
    assert [r.ticker for r in rk.apply_filters(ordered, region="Europe")] == ["ZZA", "ZZB"]
    assert [r.ticker for r in rk.apply_filters(ordered, country="in")] == ["ZZC"]
    assert rk.global_ranking(engine, top=5)["lignes"] == []           # base vide : aucune ligne inventée


def test_world_outcomes_tracked_in_local_and_eur(store, engine):
    fx = fx_table(date(2025, 1, 1), date(2026, 12, 31))
    ss = synth_market(store, "XPAR", 300, 40, end=D)
    ws.scan_market(engine, store, "XPAR", fx, end=ss[-40])
    out = ws.update_outcomes(engine, store, "XPAR", fx)
    with session_scope(engine) as s:
        n_sig = s.scalar(select(func.count()).select_from(WorldSignal))
        rows = list(s.scalars(select(WorldOutcome)))
    assert out["mis_a_jour"] == n_sig and len(rows) == n_sig
    for r in rows:
        if r.ret_20_local is not None and r.ret_20_eur is not None:
            assert r.fx_effect_eur == pytest.approx(0.0, abs=1e-12)       # EUR -> EUR : aucun effet de change


# ============================================================================ 14 à 16. portefeuille international
def _bars(n=80, start=100.0, drift=0.002, market="XPAR", seed=1):
    m = mk.get(market)
    ss = [pd.Timestamp(s.session) for s in mk.sessions(m, date(2026, 6, 1), D)][-n:]
    rng = np.random.default_rng(seed)
    c = start * np.cumprod(1 + rng.normal(drift, 0.01, len(ss)))
    return pd.DataFrame({"open": c * 0.999, "high": c * 1.01, "low": c * 0.99, "close": c}, index=ss)


def test_international_portfolio_values_cash_and_fx(store):
    fx = fx_table(date(2026, 1, 1), D, inr=95.0, drift_inr=0.0005)
    bp, bi = _bars(market="XPAR"), _bars(market="XNSE", start=2000.0, seed=2)
    sigs = [SignalIn("XPAR", "ZZFR1", "ZZP", bp.index[10].date(), "momentum", 1.0, float(bp["close"].iloc[10]),
                     2.0, "EUR", "FR"),
            SignalIn("XNSE", "ZZIN1", "ZZI", bi.index[10].date(), "momentum", 0.9, float(bi["close"].iloc[10]),
                     40.0, "INR", "IN")]
    res = simulate(sigs, {("XPAR", "ZZFR1"): bp, ("XNSE", "ZZIN1"): bi}, fx, GlobalConfig(base="EUR"))
    assert len(res.equity) and res.equity.iloc[0] == pytest.approx(100_000.0)
    assert res.summary["nature"].startswith("SIMULATION") and res.summary["frais"]["change"] > 0
    usd = simulate(sigs, {("XPAR", "ZZFR1"): bp, ("XNSE", "ZZIN1"): bi}, fx, GlobalConfig(base="USD"))
    assert usd.summary["devise_base"] == "USD" and usd.equity.iloc[-1] != res.equity.iloc[-1]


def test_risk_per_position_and_country_cap():
    fx = fx_table(date(2026, 1, 1), D)
    sigs, bars = [], {}
    for k in range(5):
        b = _bars(market="XPAR", seed=10 + k, drift=0.0)
        bars[("XPAR", f"ZZFR{k}")] = b
        sigs.append(SignalIn("XPAR", f"ZZFR{k}", f"ZZ{k}", b.index[10].date(), "steady", 1 - k / 10,
                             float(b["close"].iloc[10]), 1.5, "EUR", "FR"))
    cfg = GlobalConfig(base="EUR", max_country=0.40, max_weight=0.15, max_corr=1.01)
    res = simulate(sigs, bars, fx, cfg)
    assert any("pays FR" in r["motif"] for r in res.refused)           # 3 lignes de 15 % dépasseraient 40 %
    # risque initial de chaque position ≤ 0,5 % du capital (arrondi à l'action inférieure)
    for t in res.trades:
        assert t["r"] is None or t["r"] > -1.5


def test_fx_fees_only_when_currency_differs():
    fx = fx_table(date(2026, 1, 1), D)
    b = _bars(market="XPAR")
    s = [SignalIn("XPAR", "ZZFR1", "ZZP", b.index[5].date(), "steady", 1.0, float(b["close"].iloc[5]), 2.0, "EUR", "FR")]
    eur = simulate(s, {("XPAR", "ZZFR1"): b}, fx, GlobalConfig(base="EUR"))
    usd = simulate(s, {("XPAR", "ZZFR1"): b}, fx, GlobalConfig(base="USD"))
    assert eur.summary["frais"]["change"] == 0 and usd.summary["frais"]["change"] > 0
    assert eur.summary["frais"]["courtage"] > 0


def test_benchmarks_same_period_same_currency():
    fx = fx_table(date(2026, 1, 1), D, usd=1.10)
    s = pd.Series([100.0, 110.0], index=pd.DatetimeIndex(["2026-11-02", "2026-11-13"]))
    out = benchmarks({"ref": (s, "USD"), "vide": (pd.Series(dtype=float), "USD"), "SAR": (s, "SAR")}, fx, "EUR",
                     "2026-11-01", "2026-11-13")
    assert out["ref"] == pytest.approx(0.10) and out["vide"] is None and out["SAR"] is None


# ============================================================================ 17. reprise sur erreur fournisseur
def test_fetcher_retries_then_recovers_and_never_bypasses_antibot():
    seq = [(503, b"", ""), (200, b"a,b\n1,2", "text/csv")]
    f = pv.Fetcher(get=lambda u: seq.pop(0), min_interval_s=0, sleep=lambda s: None)
    assert f("https://x.test/f.csv", "csv") == b"a,b\n1,2" and f.calls == 2
    html = pv.Fetcher(get=lambda u: (200, b"<!DOCTYPE html><script>challenge</script>", "text/html"),
                      min_interval_s=0, sleep=lambda s: None)
    with pytest.raises(pv.SourceUnavailable, match="anti-robot"):
        html("https://x.test/f.csv", "csv")
    assert html.calls == 1                                            # aucune tentative de contournement


def test_blocked_source_stops_after_three_failures(store, engine):
    f = pv.Fetcher(get=lambda u: (403, b"", ""), min_interval_s=0, sleep=lambda s: None)
    rep = sync.sync_india(store, engine, f, "XNSE", datetime(2026, 11, 14, 12, tzinfo=UTC), backfill_sessions=30)
    assert f.calls == 3 and rep.status == "échec" and "interrompue" in rep.errors[-1]


# ============================================================================ 18. isolation des pannes par région
def test_region_failure_isolation(store, engine):
    def get(url):
        if "euronext" in url:
            raise ConnectionError("réseau")
        if "ecb" in url:
            return 200, b"KEY,CURRENCY,TIME_PERIOD,OBS_VALUE\nX,USD,2026-11-13,1.1\n", "text/csv"
        d = _url_date(url)
        return 200, bhavcopy(d, [("INE000000001", "ZZA", "EQ", "ZZ A", 10, 11, 9, 10, 10, 100, 1000, 1)]), "zip"
    fetch = pv.Fetcher(get=get, min_interval_s=0, sleep=lambda s: None, retries=0)
    code = wr.collect(engine, store, fetch, datetime(2026, 11, 14, 12, tzinfo=UTC), backfill_sessions=2,
                      markets=("XPAR", "XNSE"))
    assert code == 2                                                   # alerte, pas panne
    assert store.has("XNSE", D) and not store.has("XPAR", D)
    state = wr.kv_get(engine, wr.KV_STATE)["collecte"]["places"]
    assert state["XPAR"]["statut"] == "échec" and state["XNSE"]["statut"] == "ok" and state["FX-BCE"]["statut"] == "ok"


# ============================================================================ 19. quotas et stockage
def test_storage_immutable_budget_and_file_cap(tmp_path, engine):
    st = WorldStore(tmp_path / "w", budget_mb=1000)
    df = pd.DataFrame([{**_row("XPAR", "ZZA", "ZZFR00000001"), "date": D}])
    assert st.write_session("XPAR", D, df, "t") and not st.write_session("XPAR", D, df.assign(close=1.0), "t")
    assert pd.read_parquet(st.path("XPAR", D))["close"].iloc[0] == 10.0     # jamais réécrit
    tiny = WorldStore(tmp_path / "w", budget_mb=0.0001)
    with pytest.raises(StorageBudgetExceeded):
        tiny.write_session("XPAR", D - timedelta(days=1), df, "t")
    calls = []

    def get(url):
        calls.append(url)
        d = _url_date(url)
        return 200, bhavcopy(d, [("INE000000001", "ZZA", "EQ", "ZZ A", 10, 11, 9, 10, 10, 100, 1000, 1)]), "zip"
    rep = sync.sync_india(st, engine, pv.Fetcher(get=get, min_interval_s=0, sleep=lambda s: None), "XNSE",
                          datetime(2026, 11, 14, 12, tzinfo=UTC), backfill_sessions=50, max_files=7)
    assert len(calls) == 7 and "restante" in rep.detail                # plafond de fichiers par passage respecté
    rep2 = sync.sync_india(st, engine, pv.Fetcher(get=get, min_interval_s=0, sleep=lambda s: None), "XNSE",
                           datetime(2026, 11, 14, 12, tzinfo=UTC), backfill_sessions=50, max_files=7)
    assert len(calls) == 14 and not set(rep.written) & set(rep2.written)  # reprise sans retéléchargement


def test_master_ticker_change_and_inactive_listings(engine):
    sync.update_master(engine, pd.DataFrame([{**_row("XPAR", "ZZOLD", "ZZFR00000007"), "date": date(2026, 10, 1)},
                                             {**_row("XPAR", "ZZGONE", "ZZFR00000008"), "date": date(2026, 10, 1)}]),
                       "euronext")
    sync.update_master(engine, pd.DataFrame([{**_row("XPAR", "ZZNEW", "ZZFR00000007"), "date": D}]), "euronext")
    with session_scope(engine) as s:
        ch = s.scalar(select(WorldTickerChange))
        gone = s.scalar(select(WorldListing).where(WorldListing.ticker == "ZZGONE"))
        assert (ch.old_ticker, ch.new_ticker) == ("ZZOLD", "ZZNEW")
        assert gone.status == "INACTIF" and gone.last_seen == date(2026, 10, 1)     # conservé, jamais supprimé


# ============================================================================ outils
def _url_date(url: str) -> date:
    import re
    return datetime.strptime(re.search(r"_(\d{8})_F_", url).group(1), "%Y%m%d").date()


def _row(market, ticker, isin):
    return {"market": market, "ticker": ticker, "isin": isin, "name": f"{ticker} SA", "currency": mk.get(market).currency,
            "segment": "EQ", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": 100.0, "turnover": 1000.0,
            "prev_close_adj": None, "lot_size": 1, "traded": True}


def _rk_row(key, market, ticker, norm=0.8, name=None):
    m = mk.get(market)
    return rk.Row(key=key, ticker=ticker, name=name, market=market, exchange=m.name, country=m.country, region=m.region,
                  continent=m.continent, currency=m.currency, as_of=str(D), close=10.0, close_eur=10.0, close_usd=11.0,
                  categories=["momentum"], norm=norm, risk_pct=0.05, risk="modéré", confidence=0.9)
