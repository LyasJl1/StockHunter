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
NOSLIP = PaperConfig(slippage_bps=0.0)   # chiffres ronds pour les tests ; le glissement a ses propres tests


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
    paper.process(engine, NOSLIP)
    t = trade(engine, ids["ZZA"])
    # risque 0,5 % de 100 000 = 500 $ / 4 $ = 125 actions
    assert t.status == "CLOSED" and t.entry_price == 100 and t.tp1_done
    assert t.realized_pnl == pytest.approx(62 * 6.0)             # 62 actions vendues +6 $, 63 au prix d'entrée
    assert kinds(engine, t.id) == ["PLANNED", "OPENED", "TP1", "STOP_MOVED", "STOP_MOVED", "CLOSED"]
    paper.process(engine, NOSLIP)                                 # idempotent : rien de nouveau
    assert kinds(engine, t.id) == ["PLANNED", "OPENED", "TP1", "STOP_MOVED", "STOP_MOVED", "CLOSED"]
    sm = paper.summary(engine)
    assert sm["closed"] == 1 and sm["equity"] == pytest.approx(100_000 + 372 - t.costs)


def test_gap_below_stop_fills_at_open(engine):
    src, ids, run = setup(engine)
    paper.plan_from_scan(engine, run)
    bars(engine, src, ids["ZZA"], [("2026-11-03", 100, 101, 99, 100), ("2026-11-04", 90, 92, 89, 91)])
    paper.process(engine, NOSLIP)
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
    cfg = PaperConfig(max_per_sector=2, slippage_bps=0.0)
    paper.process(engine, cfg)
    a, c = trade(engine, ids["ZZA"]), trade(engine, ids["ZZC"])
    assert c.status == "CANCELLED" and "secteur" in c.close_reason
    assert a.status == "OPEN" and a.shares == 250 and a.entry_price == 50 and a.stop == 48   # split 2:1
    assert a.atr_at_signal == 1.0 and a.initial_risk == pytest.approx(500)   # ATR ajusté, risque en $ inchangé
    # non-régression : la courbe de capital ne doit PAS voir de gain/perte fictif le jour du split
    spy = Instrument(symbol="SPY", asset_type="etf")
    with session_scope(engine) as s:
        s.add(spy)
        s.flush()
        for d in ("2026-11-03", "2026-11-04"):
            s.add(PriceBar(instrument_id=spy.id, source_id=src, bar_date=date.fromisoformat(d), open=1, high=1, low=1,
                           close=1, volume=1))
    curve = paper.equity_curve(engine, cfg)
    costs = (125 + 124) * 100 * 10 / 1e4                  # deux positions ouvertes (125 et 124 actions), frais d'entrée
    assert [round(x["equity"], 2) for x in curve] == [100_000 - costs, 100_000 - costs]


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


# ----------------------------------------------------------------------------- règles partagées (v3)
def test_rules_same_day_tp1_does_not_assume_intraday_order():
    """TP1 touché et plus bas sous le prix d'entrée la même séance : on ne suppose PAS que le stop remonté a été
    touché (ordre intrajournalier inconnu) ; le stop ne passe à l'entrée qu'à la séance suivante."""
    from stockhunter.trading.rules import Bar, Rules, plan, step
    r = Rules(slippage_bps=0)
    p = plan(100.0, 2.0, r)
    size = lambda fill, off: (100, None)
    step(p, Bar(date(2026, 11, 3), 100, 101, 99, 100), size, r)
    step(p, Bar(date(2026, 11, 4), 101, 106.5, 99.0, 105), size, r)      # TP1 (106) et plus bas 99 < entrée
    assert p.status == "OPEN" and p.tp1_done and p.shares == 50 and p.stop == 99   # stop suiveur seul (105 − 3×ATR)
    step(p, Bar(date(2026, 11, 5), 104, 105, 101, 104), size, r)
    assert p.stop == 100                                                  # effectif le lendemain


def test_rules_slippage_and_costs_are_adverse():
    from stockhunter.trading.rules import Bar, Rules, plan, step
    r = Rules(slippage_bps=10, cost_bps=10)
    p = plan(100.0, 2.0, r)
    step(p, Bar(date(2026, 11, 3), 100, 101, 99, 100), lambda f, o: (100, None), r)
    assert p.entry_price == pytest.approx(100.10) and p.stop == pytest.approx(96.10)
    step(p, Bar(date(2026, 11, 4), 99, 99, 95, 96), lambda f, o: (100, None), r)
    assert p.status == "CLOSED" and p.realized == pytest.approx((96.10 * 0.999 - 100.10) * 100)
    assert p.costs == pytest.approx((100.10 + 96.10 * 0.999) * 100 * 0.001)


def test_rules_split_while_planned_and_open():
    from stockhunter.trading.rules import Bar, Rules, plan, step
    r = Rules(slippage_bps=0)
    p = plan(100.0, 2.0, r)
    step(p, Bar(date(2026, 11, 3), 50, 50.5, 49.5, 50, split=2.0), lambda f, o: (200, None), r)   # split avant l'entrée
    assert p.status == "OPEN" and p.entry_price == 50 and p.stop == 48 and p.atr == 1.0
    step(p, Bar(date(2026, 11, 4), 25, 25.2, 24.8, 25, split=2.0), lambda f, o: (0, None), r)     # split en position
    assert p.shares == 400 and p.entry_price == 25 and p.stop == 24 and p.tp1 == 26.5 and p.initial_risk == 400


def test_correlated_position_is_refused(engine):
    src, ids, run = setup(engine, symbols=("ZZA", "ZZB"), sector=None)
    with session_scope(engine) as s:   # 40 séances d'historique identiques -> corrélation 1
        for k in range(40):
            d = date(2026, 9, 1 + k) if k < 30 else date(2026, 10, k - 29)
            px = 100 + (k % 7) - 0.3 * (k % 3)
            for sym in ids:
                s.add(PriceBar(instrument_id=ids[sym], source_id=src, bar_date=d, open=px, high=px, low=px, close=px,
                               adj_close=px, volume=1e6))
    paper.plan_from_scan(engine, run)
    for sym in ids:
        bars(engine, src, ids[sym], [("2026-11-03", 100, 101, 99, 100)])
    paper.process(engine, NOSLIP)
    a, b = trade(engine, ids["ZZA"]), trade(engine, ids["ZZB"])
    assert a.status == "OPEN" and b.status == "CANCELLED" and "corrélée" in b.close_reason


def _two_trade_scenario(engine):
    """A entre le 3/11 et est stoppée le 4/11 ; B (une seule place au portefeuille) ne doit pas en profiter."""
    src, ids, run = setup(engine, symbols=("ZZA", "ZZB"), sector=None)
    paper.plan_from_scan(engine, run)
    return src, ids, {"ZZA": [("2026-11-03", 100, 101, 99, 100), ("2026-11-04", 99, 99.5, 94, 95), ("2026-11-05", 95, 96, 94, 95)],
                      "ZZB": [("2026-11-03", 100, 101, 99, 100), ("2026-11-04", 100, 101, 99, 100), ("2026-11-05", 100, 101, 99, 100)]}


def test_catch_up_run_equals_daily_runs(engine, tmp_path):
    """Revue indépendante (C1) : traiter 3 séances d'un coup doit donner exactement le même résultat que 3 soirs."""
    from stockhunter.db.models import Base
    from stockhunter.db.session import get_engine
    cfg = PaperConfig(max_positions=1, slippage_bps=0.0)
    src, ids, series = _two_trade_scenario(engine)
    for k in range(3):                                           # un appel par soir
        for sym in ids:
            bars(engine, src, ids[sym], [series[sym][k]])
        paper.process(engine, cfg)
    daily = {sym: (trade(engine, ids[sym]).status, trade(engine, ids[sym]).close_reason) for sym in ids}

    other = get_engine(f"sqlite:///{tmp_path / 'catchup.db'}")
    Base.metadata.create_all(other)
    src2, ids2, series2 = _two_trade_scenario(other)
    for sym in ids2:
        bars(other, src2, ids2[sym], series2[sym])
    paper.process(other, cfg)                                    # rattrapage : 3 séances en un appel
    catchup = {sym: (trade(other, ids2[sym]).status, trade(other, ids2[sym]).close_reason) for sym in ids2}
    assert daily == catchup
    assert daily["ZZB"][0] == "CANCELLED" and "limite de 1 positions" in daily["ZZB"][1]   # pas d'entrée grâce au futur


def test_slot_freed_intraday_is_not_reused_at_same_day_open(engine):
    """Revue indépendante (C2) : A sort en séance le 4/11 ; un plan B dont l'entrée est à l'OUVERTURE du 4/11 ne
    peut pas utiliser la place libérée plus tard dans la journée."""
    cfg = PaperConfig(max_positions=1, slippage_bps=0.0)
    src, ids, run = setup(engine, symbols=("ZZA",), sector=None)
    paper.plan_from_scan(engine, run)
    bars(engine, src, ids["ZZA"], [("2026-11-03", 100, 101, 99, 100)])
    paper.process(engine, cfg)
    with session_scope(engine) as s:                             # second signal, daté du 3/11 : entrée prévue le 4/11
        b = Instrument(symbol="ZZB", asset_type="stock")
        s.add(b)
        s.flush()
        t = s.scalars(select(PaperTrade)).first()
        s.add(PaperTrade(instrument_id=b.id, scan_run_id=t.scan_run_id, strategy_version_id=t.strategy_version_id,
                         signal_date=date(2026, 11, 3), status="PLANNED", signal_close=100, atr_at_signal=2,
                         entry_min=98, entry_max=103, stop_initial_offset=4, risk_fraction=0.005, strategies="A",
                         plan_json="{}"))
        bid = b.id
    bars(engine, src, ids["ZZA"], [("2026-11-04", 99, 99.5, 94, 95)])          # A stoppée en séance
    bars(engine, src, bid, [("2026-11-04", 100, 101, 99, 100)])
    paper.process(engine, cfg)
    assert trade(engine, ids["ZZA"]).status == "CLOSED"
    assert trade(engine, bid).status == "CANCELLED" and "limite de 1 positions" in trade(engine, bid).close_reason
