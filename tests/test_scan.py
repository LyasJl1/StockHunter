"""Scanner de bout en bout sur une base SYNTHÉTIQUE : persistance, historique, reproductibilité, look-ahead."""

import json
from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import func, select

from stockhunter.data.ingestion import ensure_sources
from stockhunter.data.providers import parse_tiingo_prices
from stockhunter.db.models import Filing, FundamentalFact, Instrument, PriceBar, Score, StatusChange
from stockhunter.db.session import session_scope
from stockhunter.db.upsert import upsert
from stockhunter.scan import ScanError, build_inputs, run_scan
from stockhunter.scoring.config import DEFAULT_CONFIG, Thresholds
from stockhunter.scoring.engine import Context, StockInput, decide_status, market_regime
from tests.fixtures import FIXTURE_tiingo_bars

AS_OF = date(2026, 11, 11)


def seed(engine, end=AS_OF):
    src = ensure_sources(engine)
    specs = [("SPY", "etf", "benchmark", None, 400.0), ("ZZSEC", "etf", "sector_etf", None, 50.0),
             ("ZZTA", "stock", "candidate", "ZZSEC", 100.0), ("ZZTB", "stock", "candidate", "ZZSEC", 30.0),
             ("ZZTC", "stock", "candidate", "ZZSEC", 70.0)]
    with session_scope(engine) as s:
        for sym, at, role, sec, base in specs:
            inst = Instrument(symbol=sym, asset_type=at, role=role, sector_etf=sec)
            s.add(inst)
            s.flush()
            bars = parse_tiingo_prices(FIXTURE_tiingo_bars(date(2025, 6, 2), end, base=base))
            upsert(s, PriceBar, [{**b, "instrument_id": inst.id, "source_id": src["tiingo"]} for b in bars],
                   ["instrument_id", "bar_date", "source_id"])
        zzta = s.scalar(select(Instrument).where(Instrument.symbol == "ZZTA"))
        s.add(Filing(instrument_id=zzta.id, source_id=src["sec_edgar"], accession="FIXTURE-8K-1", form="8-K",
                     filed_date=date(2026, 10, 29), items="2.02,9.01", url="https://example.invalid/8k"))
        for i, (st, en, v, fd) in enumerate([("2025-07-01", "2025-09-30", 100, "2025-11-01"),
                                             ("2026-07-01", "2026-09-30", 130, "2026-11-01")]):
            s.add(FundamentalFact(instrument_id=zzta.id, source_id=src["sec_edgar"], fact_key=f"fx{i}",
                                  taxonomy="us-gaap", concept="Revenues", unit="USD",
                                  period_start=date.fromisoformat(st), period_end=date.fromisoformat(en),
                                  form="10-Q", filed_date=date.fromisoformat(fd), accession=f"FX-{i}", value=v))


def totals(engine, run_id):
    with session_scope(engine) as s:
        return {sym: (sc.total, sc.status, sc.subscores_json) for sc, sym in
                s.execute(select(Score, Instrument.symbol).join(Instrument).where(Score.scan_run_id == run_id))}


def test_scan_persists_and_tracks_status_changes(engine):
    seed(engine)
    r1 = run_scan(engine)
    r2 = run_scan(engine)
    t1, t2 = totals(engine, r1), totals(engine, r2)
    assert set(t1) == {"ZZTA", "ZZTB", "ZZTC"}          # les ETF ne sont pas classés
    assert t1 == t2                                      # reproductible
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(Score)) == 6   # 2 scans conservés
        assert s.scalar(select(func.count()).select_from(StatusChange)) == 3  # changements au 1er scan seulement
        sc = s.scalar(select(Score).join(Instrument).where(Instrument.symbol == "ZZTA", Score.scan_run_id == r1))
        assert sc.price_date == AS_OF and sc.close is not None
        sources = json.loads(sc.sources_json)
        assert any(x["type"] == "résultats" and x["url"] == "https://example.invalid/8k" for x in sources)


def test_no_look_ahead(engine, tmp_path):
    """Un scan au 30/10/2026 donne le même résultat, que la base contienne ou non les données postérieures."""
    seed(engine)
    with_future = totals(engine, run_scan(engine, date(2026, 10, 30)))

    from stockhunter.db.models import Base
    from stockhunter.db.session import get_engine
    e2 = get_engine(f"sqlite:///{tmp_path / 'past.db'}")
    Base.metadata.create_all(e2)
    seed(e2, end=date(2026, 10, 30))
    with session_scope(e2) as s:  # retire le 10-Q du 01/11/2026, inconnu au 30/10
        s.query(FundamentalFact).filter(FundamentalFact.filed_date > date(2026, 10, 30)).delete()
    without_future = totals(e2, run_scan(e2, date(2026, 10, 30)))
    assert with_future == without_future


def test_scan_rejects_non_trading_day_and_changed_params(engine):
    seed(engine)
    with pytest.raises(ScanError, match="jour non ouvré"):
        run_scan(engine, date(2026, 11, 8))  # dimanche
    run_scan(engine)
    tweaked = replace(DEFAULT_CONFIG, thresholds=Thresholds(ready_at=65))
    with pytest.raises(ScanError, match="incrémenter SCORING_VERSION"):
        run_scan(engine, cfg=tweaked)


def test_stale_price_is_rejected(engine):
    seed(engine)
    with session_scope(engine) as s:
        zztb = s.scalar(select(Instrument).where(Instrument.symbol == "ZZTB"))
        s.query(PriceBar).filter(PriceBar.instrument_id == zztb.id, PriceBar.bar_date > date(2026, 11, 5)).delete()
    r = totals(engine, run_scan(engine))
    assert r["ZZTB"][1] == "REJECTED"


def _input(**tech):
    base = {"dollar_vol_20": 1e9, "price_date": "2026-11-11", "breakout_holding": True, "ext_sma50": 0.05,
            "gap_last": 0.0}
    base.update(tech)
    return StockInput(symbol="ZZ", instrument_id=1, asset_type="stock", tech=base)


def test_status_gates():
    th = DEFAULT_CONFIG.thresholds
    on = Context(as_of=AS_OF, regime={"label": "NEUTRE", "score": 50}, sectors={})
    off = Context(as_of=AS_OF, regime={"label": "RISK-OFF", "score": 20}, sectors={})
    strat = {"A": {"active": True}, "B": {"active": False}}
    assert decide_status(_input(), 75, 0.9, strat, on, th)[0] == "READY"
    st, why = decide_status(_input(ext_sma50=0.2), 75, 0.9, strat, on, th)
    assert st == "DEVELOPING" and "étendu" in why
    assert decide_status(_input(), 75, 0.9, strat, off, th)[0] == "DEVELOPING"
    imminent = _input()
    imminent.upcoming = {"date": "2026-11-12", "sessions_until": 1}
    assert "résultats imminents" in decide_status(imminent, 75, 0.9, strat, on, th)[1]
    assert decide_status(_input(dollar_vol_20=1e6), 90, 0.9, strat, on, th)[0] == "REJECTED"
    assert decide_status(_input(), 90, 0.3, strat, on, th)[0] == "REJECTED"
    assert decide_status(_input(), 75, 0.9, {"A": {"active": False}}, on, th)[0] == "WATCHLIST"


def test_market_regime_rules():
    up = market_regime({"above_sma200": True, "sma50_above_sma200": True, "ret_3m": 0.05}, (14.0, AS_OF), 0.7, None)
    down = market_regime({"above_sma200": False, "sma50_above_sma200": False, "ret_3m": -0.08}, (30.0, AS_OF), 0.3, None)
    assert up["label"] == "RISK-ON" and down["label"] == "RISK-OFF"
    assert market_regime({}, None, None, None)["label"] == "INCONNU"


def test_build_inputs_reports_calendar_not_used_in_past(engine):
    seed(engine)
    _, _, _, notes = build_inputs(engine, date(2026, 10, 30))
    assert notes and "calendrier" in notes[0]


def test_cli_scan_guards(engine, settings, monkeypatch, capsys):
    from stockhunter import cli
    from stockhunter.config import get_settings
    from stockhunter.db.models import Instrument as I
    seed(engine)
    with session_scope(engine) as s:
        for inst in s.scalars(select(I)):
            inst.last_price_date = AS_OF
        s.scalar(select(I).where(I.symbol == "ZZTC")).last_price_date = date(2026, 11, 10)
    monkeypatch.setenv("SH_DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    try:
        assert cli.main(["scan", "--require-fresh"]) == 2   # partiel attendu, visible dans le bilan de la chaîne
        assert "Scan reporté : 1 série(s)" in capsys.readouterr().out
        assert cli.main(["scan", "--if-new"]) == 0
        assert cli.main(["scan", "--if-new"]) == 0
        out = capsys.readouterr().out
        assert "Scan #1 enregistré" in out and "déjà réalisé" in out
        assert cli.main(["explain", "ZZTA"]) == 0
        assert "PAS une probabilité" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()
