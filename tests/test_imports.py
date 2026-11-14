"""3.3 — import d'historiques (mode recherche) et interface commune des sources. Fichiers d'EXEMPLE synthétiques
(symboles ZZ*), aux formats Yahoo Finance / yfinance / long générique ; aucun appel réseau, aucune connexion Yahoo."""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from stockhunter.world import imports as im
from stockhunter.world import markets as mk
from stockhunter.world import ranking as rk
from stockhunter.world import run as wr
from stockhunter.world import scan as ws
from stockhunter.world.store import WorldStore
from tests.world_fixtures import fx_table

UTC = timezone.utc
RIGHTS = "fichier d'exemple synthétique pour les tests, aucun droit tiers"


def yahoo_csv(path, market="XETR", n=40, start=50.0, seed=1, end=date(2026, 11, 13), extra_rows=""):
    """Export « Historical Data » Yahoo : Date,Open,High,Low,Close,Adj Close,Volume."""
    ss = [s.session for s in mk.sessions(mk.get(market), date(2024, 1, 1), end)][-n:]
    rng = np.random.default_rng(seed)
    c = start * np.cumprod(1 + rng.normal(0.001, 0.01, len(ss)))
    lines = ["Date,Open,High,Low,Close,Adj Close,Volume"]
    for d, x in zip(ss, c):
        lines.append(f"{d},{x * 0.998:.4f},{x * 1.01:.4f},{x * 0.99:.4f},{x:.4f},{x * 0.97:.4f},250000")
    path.write_text("\n".join(lines) + extra_rows + "\n")
    return ss, c


@pytest.fixture
def istore(tmp_path):
    return im.ImportStore(tmp_path / "imports")


def test_yahoo_symbols_map_to_registry_and_unknown_are_refused():
    assert im.parse_symbol("air.pa")[1:] == ("XPAR", "EUR", 1.0)
    assert im.parse_symbol("ZZX.L")[1:] == ("XLON", "GBp", 100.0)          # Londres : pence
    assert im.parse_symbol("RELIANCE.NS")[1] == "XNSE" and im.parse_symbol("7203.T")[1] == "XTKS"
    assert im.parse_symbol("BRK-B")[1] == "US" and im.parse_symbol("SAP.DE")[1] == "XETR"
    for bad in ("^GSPC", "EURUSD=X", "ZZ.QQ", ""):
        with pytest.raises(im.ImportError_):
            im.parse_symbol(bad)


def test_import_yahoo_export_with_quality_report(istore, tmp_path, engine):
    f = tmp_path / "ZZSAP.DE.csv"
    ss, c = yahoo_csv(f, extra_rows="\n2026-11-14,1,1,1,1,1,10\n2026-11-15,10,9,8,9,9,10\n2099-01-02,1,2,1,1,1,1")
    rep = im.import_files(istore, [str(f)], RIGHTS, "exemple", engine, today=date(2026, 11, 16))
    q = rep.quality.issues
    assert rep.markets == {"XETR": 40}
    assert q["dates futures écartées"] == 1 and q["OHLC incohérents ou non positifs écartés"] == 1
    assert q["lignes hors séance du calendrier de la place écartées"] == 1           # samedi 14/11
    bars = istore.load("XETR")
    assert len(bars) == 40 and (bars["currency"] == "EUR").all() and bars["isin"].iloc[0] == "YF:ZZSAP.DE"
    assert bars["close"].iloc[-1] == pytest.approx(round(c[-1], 4))                    # « Close » (ajusté des divisions)
    assert istore.files()[0]["droits"] == RIGHTS and istore.files()[0]["format"].startswith("export Yahoo")


def test_import_requires_rights_and_never_reimports(istore, tmp_path):
    f = tmp_path / "ZZA.PA.csv"
    yahoo_csv(f, "XPAR")
    with pytest.raises(im.ImportError_, match="droits"):
        im.import_files(istore, [str(f)], "", "x")
    im.import_files(istore, [str(f)], RIGHTS, "x")
    rep = im.import_files(istore, [str(f)], RIGHTS, "x")
    assert not rep.refused and "déjà importé" in rep.skipped[0] and rep.rows_added == 0   # reprise idempotente


def test_london_pence_converted_and_first_import_is_never_overwritten(istore, tmp_path):
    f = tmp_path / "ZZL.L.csv"
    ss, c = yahoo_csv(f, "XLON", start=500.0)                                         # 500 pence
    im.import_files(istore, [str(f)], RIGHTS, "x")
    bars = istore.load("XLON")
    assert (bars["currency"] == "GBP").all() and bars["close"].iloc[-1] == pytest.approx(c[-1] / 100, rel=1e-4)
    g = tmp_path / "ZZL.L-bis.csv"
    yahoo_csv(g, "XLON", start=900.0, seed=9)                                          # autre version des mêmes séances
    g2 = tmp_path / "x2" ; g2.mkdir()
    (g2 / "ZZL.L.csv").write_text(g.read_text())
    rep = im.import_files(istore, [str(g2 / "ZZL.L.csv")], RIGHTS, "x")
    assert rep.conflicts == 40 and rep.rows_added == 0
    assert istore.load("XLON")["close"].iloc[-1] == pytest.approx(c[-1] / 100, rel=1e-4)


def test_yfinance_multi_ticker_csv(istore, tmp_path):
    ss = [s.session for s in mk.sessions(mk.get("XNSE"), date(2026, 10, 1), date(2026, 10, 9))]
    lines = ["Price,Close,High,Low,Open,Volume,Close,High,Low,Open,Volume",
             "Ticker,ZZA.NS,ZZA.NS,ZZA.NS,ZZA.NS,ZZA.NS,ZZB.BO,ZZB.BO,ZZB.BO,ZZB.BO,ZZB.BO", "Date,,,,,,,,,,"]
    for k, d in enumerate(ss):
        lines.append(f"{d},{100 + k},{101 + k},{99 + k},{100 + k},1000,{50 + k},{51 + k},{49 + k},{50 + k},500")
    f = tmp_path / "multi.csv"
    f.write_text("\n".join(lines))
    rep = im.import_files(istore, [str(f)], RIGHTS, "yfinance-exemple")
    assert rep.files[0]["format"] == "yfinance (plusieurs symboles)"
    assert rep.markets == {"XNSE": len(ss), "XBOM": len(ss)}


def test_generic_long_parquet_with_isin_dedups_across_places(istore, tmp_path):
    rows = []
    for sym, mkt in (("ZZDUO.PA", "XPAR"), ("ZZDUO.DE", "XETR")):
        for d in [s.session for s in mk.sessions(mk.get(mkt), date(2026, 11, 2), date(2026, 11, 6))]:
            rows.append({"date": d, "symbol": sym, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100,
                         "isin": "ZZ0000000DUO"})
    p = tmp_path / "long.parquet"
    pd.DataFrame(rows).to_parquet(p)
    im.import_files(istore, [str(p)], RIGHTS, "x")
    assert set(istore.load("XPAR")["isin"]) == {"ZZ0000000DUO"} == set(istore.load("XETR")["isin"])
    r1 = rk.Row(key="ZZ0000000DUO", ticker="ZZDUO.PA", name=None, market="XPAR", exchange="", country="FR",
                region="Europe", continent="Europe", currency="EUR", as_of="2026-11-06", close=10.5, close_eur=10.5,
                close_usd=None, categories=["momentum"], norm=0.8, risk_pct=0.05, risk="modéré", confidence=0.9)
    r2 = rk.Row(**{**r1.__dict__, "market": "XETR", "ticker": "ZZDUO.DE", "categories": ["breakout"], "norm": 0.9})
    merged = rk.merge([r1, r2])
    assert len(merged) == 1 and set(merged[0].categories) == {"momentum", "breakout"}       # une société


def test_imported_market_is_scanned_in_research_mode(tmp_path, engine):
    from tests.world_fixtures import synth_market
    primary = WorldStore(tmp_path / "w")
    fx = fx_table(date(2025, 1, 1), date(2026, 11, 13))
    primary.save_fx(fx.wide.drop(columns="EUR").stack().rename("rate").reset_index().assign(date=lambda x: x["date"].dt.date))
    istore = im.ImportStore(tmp_path / "w" / "imports")
    synth_market(istore, "XETR", 260, 40, trend_first=12)                       # historique importé (Xetra)
    store = im.CombinedStore(primary, istore)
    assert "XETR" in wr.research_markets(store) and "XETR" in wr.scannable(store)
    code = wr.scan_all(engine, store, ("XETR",))
    st = wr.kv_get(engine, wr.KV_STATE)["scanners"]
    assert code in (0, 2) and st["etats"]["XETR"][0] in (wr.OK, wr.NO_OPP) and "mode recherche" in st["etats"]["XETR"][1]
    late = datetime(2026, 12, 15, 12, tzinfo=UTC)
    surv = rk.global_ranking(engine, now=late)
    rech = rk.global_ranking(engine, now=late, mode="recherche")
    n = st["places"]["XETR"]["selections"]
    assert surv["total"] == 0                                                    # données non à jour : exclues
    assert rech["total"] > 0 if n else rech["total"] == 0
    assert all(r.perime for r in rech["lignes"])                                 # retard affiché


def test_combined_store_prefers_collected_sessions(tmp_path):
    from tests.world_fixtures import synth_market
    primary, istore = WorldStore(tmp_path / "p"), im.ImportStore(tmp_path / "i")
    synth_market(primary, "XNSE", 5, 3, seed=1)
    synth_market(istore, "XNSE", 8, 3, seed=2)                                   # mêmes ISIN, autres cours
    c = im.CombinedStore(primary, istore)
    df = c.load("XNSE")
    assert len(c.stored_sessions("XNSE")) == 8 and not df.duplicated(["date", "isin"]).any()
    last = primary.stored_sessions("XNSE")[-1]
    a = primary.load("XNSE")
    got = df[df["date"] == pd.Timestamp(last)].sort_values("isin")["close"].to_numpy()
    assert np.allclose(got, a[a["date"] == pd.Timestamp(last)].sort_values("isin")["close"].to_numpy())


def test_sources_status_disables_unauthorized_sources_and_no_yahoo_client():
    st = {s.name: s for s in im.sources_status()}
    assert not st["yahoo"].enabled and "2(d)(ix)" in st["yahoo"].reason
    assert not st["euronext"].enabled and st["import"].enabled and st["massive"].enabled
    assert "yfinance" not in sys.modules                                         # aucun client Yahoo chargé


def test_world_telegram_opportunity_and_trade_messages():
    from stockhunter.world.telegram import opportunity_message, trade_message
    card = {"ticker": "ZZA", "isin": "ZZ1", "name": "ZZ Alpha", "category": "momentum", "norm_score": 0.9, "close": 101.5,
            "pos": ["hausse de 12 % sur 3 mois"], "plan": {"entry_min": 99.0, "entry_max": 104.5,
                                                            "stop_si_entree_au_cours": 95.0, "tp1": 111.0, "tp2": 121.0}}
    msg = opportunity_message("XNSE", {"as_of": "2026-11-13", "categories": {"momentum": {"candidats": [card, card]}}})
    assert "clôture du 2026-11-13" in msg and "pas un cours en direct" in msg and "INR" in msg and msg.count("ZZA") == 1
    assert "TP1 111.00" in msg and "hausse de 12 %" in msg and "probabilité" in msg
    assert opportunity_message("XNSE", {"as_of": "x", "categories": {}}) is None
    t = {"ticker": "ZZA", "place": "XNSE", "devise": "INR", "categorie": "momentum", "entree": "2026-11-02",
         "sortie": "2026-11-10", "motif": "stop touché", "rendement": -0.031, "pnl_base": -312.4, "r": -1.02}
    tm = trade_message(t, "EUR")
    assert "−" in tm or "-3.1 %" in tm and "-1.02 R" in tm and "Simulation" in tm


def test_status_text_and_market_table_work_on_combined_store(tmp_path, engine):
    from tests.world_fixtures import synth_market
    primary, istore = WorldStore(tmp_path / "w"), im.ImportStore(tmp_path / "w" / "imports")
    synth_market(istore, "XETR", 5, 3)
    c = im.CombinedStore(primary, istore)
    txt = wr.status_text(engine, c)
    assert "Stockage mondial" in txt
    row = {r["code"]: r for r in wr.market_table(engine, c)}["XETR"]
    assert row["seances"] == 5 and c.status()["imports"]["XETR"]["seances"] == 5
