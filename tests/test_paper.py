"""Paper trading (scénarios SYNTHÉTIQUES)."""

import json
from datetime import date

import pytest
from sqlalchemy import func, select

from stockhunter.data.ingestion import ensure_sources
from stockhunter.db.models import Instrument, PaperTrade, PaperTradeEvent, PriceBar, ScanRun, Score, StrategyVersion
from stockhunter.db.session import session_scope
from stockhunter.portfolio import paper
from stockhunter.portfolio.paper import PaperConfig

SIG = date(2026, 11, 2)  # lundi


def setup(engine, symbols=("ZZA",), sector="ZZSEC", close=100.0, atr_pct=0.02):
    src = ensure_sources(engine)["tiingo"]
    with session_scope(engine) as s:
        sv = StrategyVersion(name="t", version="1", params_json="{}", params_hash="x")
        s.add(sv)
        s.flush()
        run = ScanRun(strategy_version_id=sv.id, as_of=SIG, universe_size=1, scored=1, regime_json="{}", sectors_json="{}")
        s.add(run)
        s.flush()
        ids = {}
        for sym in symbols:
            inst = Instrument(symbol=sym, asset_type="stock", sector_etf=sector)
            s.add(inst)
            s.flush()
            ids[sym] = inst.id
            s.add(Score(scan_run_id=run.id, instrument_id=inst.id, as_of=SIG, close=close, total=75, confidence=0.9,
                        status="READY", status_reason="test", subscores_json="{}", reasons_pos_json="[]",
                        reasons_neg_json="[]", sources_json="[]",
                        strategies_json=json.dumps({"B": {"active": True}}),
                        metrics_json=json.dumps({"atr14_pct": atr_pct})))
            s.add(PriceBar(instrument_id=inst.id, source_id=src, bar_date=SIG, open=close, high=close, low=close,
                           close=close, volume=1e6))
        return src, ids, run.id


def bars(engine, src, iid, rows):
    with session_scope(engine) as s:
        for d, o, h, lo, c, *f in rows:
            s.add(PriceBar(instrument_id=iid, source_id=src, bar_date=date.fromisoformat(d), open=o, high=h, low=lo,
                           close=c, volume=1e6, split_factor=f[0] if f else 1.0))


def trade(engine, iid):
    with session_scope(engine) as s:
        t = s.scalar(select(PaperTrade).where(PaperTrade.instrument_id == iid))
        s.expunge(t)
        return t


def kinds(engine, tid):
    with session_scope(engine) as s:
        return [e.kind for e in s.scalars(select(PaperTradeEvent).where(PaperTradeEvent.trade_id == tid)
                                          .order_by(PaperTradeEvent.id))]


def test_gap_above_zone_is_cancelled(engine):
    src, ids, run = setup(engine)
    paper.plan_from_scan(engine, run)
    bars(engine, src, ids["ZZA"], [("2026-11-03", 104, 105, 103, 104)])  # > 100 × 1,03
    paper.process(engine)
    t = trade(engine, ids["ZZA"])
    assert t.status == "CANCELLED" and "gap" in t.close_reason


def test_full_lifecycle_tp1_then_breakeven_stop(engine):
    src, ids, run = setup(engine)   # ATR = 2 $, R = 4 $
    paper.plan_from_scan(engine, run)
    bars(engine, src, ids["ZZA"], [
        ("2026-11-03", 100, 101, 99, 100.5),     # entrée à 100 ; stop 96 ; TP1 106 ; TP2 112
        ("2026-11-04", 101, 106.5, 100.5, 105),  # TP1 : moitié vendue à 106, stop -> 100
        ("2026-11-05", 104, 104.5, 99.5, 100),   # stop à l'entrée touché
    ])
    paper.process(engine)
    t = trade(engine, ids["ZZA"])
    # risque 0,5 % de 100 000 = 500 $ / 4 $ = 125 actions
    assert t.status == "CLOSED" and t.entry_price == 100 and t.tp1_done
    assert t.realized_pnl == pytest.approx(62 * 6.0)             # 62 actions vendues +6 $, 63 au prix d'entrée
    assert kinds(engine, t.id) == ["PLANNED", "OPENED", "TP1", "CLOSED"]
    paper.process(engine)                                         # idempotent : rien de nouveau
    assert kinds(engine, t.id) == ["PLANNED", "OPENED", "TP1", "CLOSED"]
    sm = paper.summary(engine)
    assert sm["closed"] == 1 and sm["equity"] == pytest.approx(100_000 + 372 - t.costs)


def test_gap_below_stop_fills_at_open(engine):
    src, ids, run = setup(engine)
    paper.plan_from_scan(engine, run)
    bars(engine, src, ids["ZZA"], [("2026-11-03", 100, 101, 99, 100), ("2026-11-04", 90, 92, 89, 91)])
    paper.process(engine)
    t = trade(engine, ids["ZZA"])
    assert t.status == "CLOSED" and "gap" in t.close_reason and t.realized_pnl == pytest.approx(-10 * 125)


def test_ambiguous_bar_assumes_stop_first(engine):
    src, ids, run = setup(engine)
    paper.plan_from_scan(engine, run)
    bars(engine, src, ids["ZZA"], [("2026-11-03", 100, 101, 99, 100), ("2026-11-04", 100, 113, 95, 101)])
    paper.process(engine)
    t = trade(engine, ids["ZZA"])
    assert t.status == "CLOSED" and t.close_reason.startswith("stop") and not t.tp1_done


def test_sector_limit_and_split(engine):
    src, ids, run = setup(engine, symbols=("ZZA", "ZZB", "ZZC"))
    paper.plan_from_scan(engine, run, PaperConfig(max_per_sector=2))
    for sym in ids:
        bars(engine, src, ids[sym], [("2026-11-03", 100, 101, 99, 100), ("2026-11-04", 50, 50.5, 49.5, 50, 2.0)])
    paper.process(engine, PaperConfig(max_per_sector=2))
    a, c = trade(engine, ids["ZZA"]), trade(engine, ids["ZZC"])
    assert c.status == "CANCELLED" and "secteur" in c.close_reason
    assert a.status == "OPEN" and a.shares == 250 and a.entry_price == 50 and a.stop == 48   # split 2:1


def test_one_plan_per_instrument(engine):
    src, ids, run = setup(engine)
    assert len(paper.plan_from_scan(engine, run)) == 1
    assert paper.plan_from_scan(engine, run) == []
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(PaperTrade)) == 1


def test_cancelled_signal_is_not_replanned_from_same_scan(engine):
    src, ids, run = setup(engine)
    paper.plan_from_scan(engine, run)
    bars(engine, src, ids["ZZA"], [("2026-11-03", 110, 111, 109, 110)])
    paper.process(engine)
    assert paper.plan_from_scan(engine, run) == []   # relance du workflow : pas de nouveau plan
