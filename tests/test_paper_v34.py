"""3.4 : scanner tenant compte du capital, refus répétitifs, quantités entières (scénarios SYNTHÉTIQUES ZZ*).

Les 13 cas exigés par la mission 3.4. Aucune donnée réelle, aucun ordre réel.
"""

import json
import math
from datetime import date

from sqlalchemy import func, select

from stockhunter.db.models import (Base, NotificationLog, PaperDecision, PaperTrade, PaperTradeEvent,
                                   PriceBar, ScanRun, Score)
from stockhunter.db.session import get_engine, session_scope
from stockhunter.notifications import engine as notif
from stockhunter.portfolio import audit, paper
from stockhunter.portfolio.paper import PaperConfig
from stockhunter.trading.rules import risk_per_share
from tests.test_paper import SIG, bars, kinds, setup, trade

SMALL = PaperConfig(capital=5000.0)                       # le capital de production
SMALL0 = PaperConfig(capital=5000.0, slippage_bps=0.0)


def new_scan(engine, src, ids, as_of, close=100.0, atr_pct=0.02):
    """Nouveau scan daté `as_of` reprenant les mêmes actions (signal READY répété)."""
    with session_scope(engine) as s:
        prev = s.scalars(select(ScanRun).order_by(ScanRun.id.desc())).first()
        run = ScanRun(strategy_version_id=prev.strategy_version_id, as_of=as_of, universe_size=1, scored=1,
                      regime_json="{}", sectors_json="{}")
        s.add(run)
        s.flush()
        for iid in ids.values():
            s.add(Score(scan_run_id=run.id, instrument_id=iid, as_of=as_of, close=close, total=75, confidence=0.9,
                        status="READY", status_reason="test", subscores_json="{}", reasons_pos_json="[]",
                        reasons_neg_json="[]", sources_json="[]", strategies_json=json.dumps({"B": {"active": True}}),
                        metrics_json=json.dumps({"atr14_pct": atr_pct})))
            if not s.scalar(select(PriceBar.id).where(PriceBar.instrument_id == iid, PriceBar.bar_date == as_of)):
                s.add(PriceBar(instrument_id=iid, source_id=src, bar_date=as_of, open=close, high=close, low=close,
                               close=close, volume=1e6))
        return run.id


def decs(engine, iid=None):
    with session_scope(engine) as s:
        q = select(PaperDecision).order_by(PaperDecision.id)
        if iid:
            q = q.where(PaperDecision.instrument_id == iid)
        out = list(s.scalars(q))
        for d in out:
            s.expunge(d)
        return out


def n_trades(engine):
    with session_scope(engine) as s:
        return s.scalar(select(func.count()).select_from(PaperTrade))


# --------------------------------------------------------------------------- 1. trop chère pour le risque
def test_1_too_expensive_for_risk_budget_creates_no_plan(engine):
    _src, ids, run = setup(engine, close=500.0, atr_pct=0.04)       # stop à 40 $ > budget 25 $ (0,5 % de 5 000)
    assert paper.plan_from_scan(engine, run, SMALL) == []
    (d,) = decs(engine)
    assert d.status == paper.NE_RISK and d.qty_risk == 0 and d.qty_exec == 0 and d.trade_id is None
    assert "budget de 25.00 $" in d.reason and d.risk_per_share > 40
    assert n_trades(engine) == 0                                     # un refus n'est jamais un trade


# --------------------------------------------------------------------------- 2. abordable et éligible
def test_2_affordable_is_eligible_and_never_exceeds_risk(engine):
    src, ids, run = setup(engine)                                    # stop 4 $ ; ≈ 4,2 $ tous frais compris
    (tid,) = paper.plan_from_scan(engine, run, SMALL)
    (d,) = decs(engine)
    rps = risk_per_share(100 * 1.0005, 4.0, 10, 5)
    assert d.status == paper.ELIGIBLE and d.qty_risk == math.floor(25 / rps) == 5 and d.qty_exec == 5
    assert d.max_loss <= 25 and d.trade_id == tid
    bars(engine, src, ids["ZZA"], [("2026-11-03", 100, 101, 99, 100)])
    paper.process(engine, SMALL)
    t = trade(engine, ids["ZZA"])
    assert t.status == "OPEN" and t.shares == 5
    assert t.shares * risk_per_share(t.entry_price, t.stop_initial_offset, 10, 5) <= 25 + 1e-9


# --------------------------------------------------------------------------- 3. écart entre plan et exécution
def test_3_gap_between_plan_and_execution_resizes_with_same_formula(engine):
    src, ids, run = setup(engine)
    paper.plan_from_scan(engine, run, SMALL0)
    bars(engine, src, ids["ZZA"], [("2026-11-03", 102.5, 103, 102, 102.5)])   # ouverture +2,5 % (dans la zone)
    paper.process(engine, SMALL0)
    t = trade(engine, ids["ZZA"])
    assert t.status == "OPEN" and t.entry_price == 102.5
    assert t.shares == math.floor(25 / risk_per_share(102.5, 4.0, 10, 0))          # recalcul à l'ouverture réelle
    assert t.shares * risk_per_share(102.5, 4.0, 10, 0) <= 25


# --------------------------------------------------------------------------- 4. stop déplacé
def test_4_trailing_stop_is_never_lowered_and_traced(engine):
    src, ids, run = setup(engine)
    paper.plan_from_scan(engine, run, SMALL0)
    bars(engine, src, ids["ZZA"], [("2026-11-03", 100, 101, 99, 100), ("2026-11-04", 101, 103, 100.5, 103),
                                   ("2026-11-05", 103, 105.5, 102.5, 105), ("2026-11-06", 104, 104.5, 101, 101.5),
                                   ("2026-11-09", 101.5, 102.5, 100.5, 102)])
    paper.process(engine, SMALL0)
    with session_scope(engine) as s:
        moves = list(s.scalars(select(PaperTradeEvent).where(PaperTradeEvent.kind == "STOP_MOVED")
                               .order_by(PaperTradeEvent.event_date, PaperTradeEvent.id)))
        prices = [m.price for m in moves]
        assert prices and prices == sorted(prices)                   # jamais abaissé
        assert all(m.detail for m in moves)                          # chaque changement est motivé
    t = trade(engine, ids["ZZA"])
    assert t.stop >= 96.0 and (not prices or t.stop == prices[-1])


# --------------------------------------------------------------------------- 5. TP partiel avec 1 à 3 actions
def test_5_partial_take_profit_with_one_to_three_shares(tmp_path):
    expected = {1: (1, "CLOSED"), 2: (1, "OPEN"), 3: (1, "OPEN")}   # floor(n/2), ou tout si floor = 0
    for n, (sold, status) in expected.items():
        eng = get_engine(f"sqlite:///{tmp_path / f'tp{n}.db'}")
        Base.metadata.create_all(eng)
        cfg = PaperConfig(capital=1000.0 * n, slippage_bps=0.0)
        src, ids, run = setup(eng)
        paper.plan_from_scan(eng, run, cfg)
        (d,) = decs(eng)
        assert d.qty_exec == n
        plan = json.loads(trade(eng, ids["ZZA"]).plan_json)
        assert ("vente totale" in plan["eligibilite"]["tp1"]) == (n == 1)       # règle annoncée dès le plan
        bars(eng, src, ids["ZZA"], [("2026-11-03", 100, 101, 99, 100), ("2026-11-04", 101, 106.5, 100.5, 105)])
        paper.process(eng, cfg)
        with session_scope(eng) as s:
            tp = s.scalar(select(PaperTradeEvent).where(PaperTradeEvent.kind == "TP1"))
            assert tp.shares == sold and float(tp.shares).is_integer()          # quantités entières, jamais 0,5
            assert ("TOTALE" in tp.detail) == (n == 1)
        t = trade(eng, ids["ZZA"])
        assert t.status == status and t.shares == n - sold and float(t.shares).is_integer()


# --------------------------------------------------------------------------- 6. refus répétitifs
def test_6_repeated_refusals_are_traced_without_new_plan_or_alert(engine):
    src, ids, run = setup(engine, close=500.0, atr_pct=0.04)
    paper.plan_from_scan(engine, run, SMALL)
    for k, d in enumerate([date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]):
        paper.plan_from_scan(engine, new_scan(engine, src, ids, d, close=500.0 + k, atr_pct=0.04), SMALL)
    ds = decs(engine)
    assert len(ds) == 4 and all(d.status == paper.NE_RISK for d in ds)
    assert ds[0].repeat_of is None and all(d.repeat_of == ds[0].id for d in ds[1:])   # journal complet, immuable
    assert n_trades(engine) == 0
    assert notif.enqueue_decision_alerts(engine) == 1 and notif.enqueue_decision_alerts(engine) == 0
    s_ = paper.summary(engine, SMALL)
    assert s_["decisions"]["repetitions_sans_nouveau_plan"] == 3


# --------------------------------------------------------------------------- 7. redevient éligible
def test_7_opportunity_becomes_eligible_again_when_conditions_change(engine):
    src, ids, run = setup(engine, symbols=("ZZA", "ZZQ"), sector=None, close=500.0, atr_pct=0.04)
    paper.plan_from_scan(engine, run, SMALL)
    # ZZA : le cours et la volatilité baissent nettement -> nouvelle évaluation, pas une répétition
    created = paper.plan_from_scan(engine, new_scan(engine, src, {"ZZA": ids["ZZA"]}, date(2026, 11, 3),
                                                    close=100.0, atr_pct=0.02), SMALL)
    a, b = decs(engine, ids["ZZA"])
    assert a.status == paper.NE_RISK and b.status == paper.ELIGIBLE and b.repeat_of is None and len(created) == 1
    # ZZQ : même cours, mais le capital change -> réévaluée (et éligible), jamais bloquée par l'ancien refus
    paper.plan_from_scan(engine, new_scan(engine, src, {"ZZQ": ids["ZZQ"]}, date(2026, 11, 4), close=500.0,
                                          atr_pct=0.04), PaperConfig(capital=50_000.0))
    q1, q2 = decs(engine, ids["ZZQ"])
    assert q1.status == paper.NE_RISK and q2.status == paper.ELIGIBLE and q2.repeat_of is None


# --------------------------------------------------------------------------- 8. données historiques manquantes
def test_8_missing_data_is_reported_not_guessed(engine):
    _src, ids, run = setup(engine, atr_pct=None)
    assert paper.plan_from_scan(engine, run, SMALL) == []
    (d,) = decs(engine)
    assert d.status == paper.NO_DATA and d.qty_exec == 0 and d.risk_per_share is None and d.max_loss is None


# --------------------------------------------------------------------------- 9. continuité des positions existantes
def test_9_existing_positions_and_legacy_plans_continue_unchanged(engine):
    """Une position ouverte avant 3.4 (plan sans bloc « eligibilite ») continue d'être suivie sans réécriture ;
    un nouveau signal sur la même action est EN ATTENTE, jamais un second plan."""
    src, ids, run = setup(engine)
    with session_scope(engine) as s:
        sv = s.scalars(select(ScanRun)).first().strategy_version_id
        t = PaperTrade(instrument_id=ids["ZZA"], scan_run_id=run, strategy_version_id=sv, signal_date=SIG,
                       status="PLANNED", signal_close=100, atr_at_signal=2, entry_min=98, entry_max=103,
                       stop_initial_offset=4, risk_fraction=0.005, strategies="B", plan_json="{}", last_processed=SIG)
        s.add(t)
    bars(engine, src, ids["ZZA"], [("2026-11-03", 100, 101, 99, 100)])
    paper.process(engine, SMALL0)
    before = trade(engine, ids["ZZA"])
    assert before.status == "OPEN" and kinds(engine, before.id) == ["OPENED"]
    paper.plan_from_scan(engine, new_scan(engine, src, ids, date(2026, 11, 3)), SMALL0)
    (d,) = decs(engine)
    assert d.status == paper.WAITING and n_trades(engine) == 1
    after = trade(engine, ids["ZZA"])
    assert (after.entry_price, after.shares, after.stop) == (before.entry_price, before.shares, before.stop)


# --------------------------------------------------------------------------- 10. frais et capital disponible
def test_10_fees_and_available_cash_including_active_plans(engine):
    cfg = PaperConfig(capital=1000.0, risk_fraction=0.05, slippage_bps=0.0)   # budget 50 $ : le capital limite
    src, ids, run = setup(engine, symbols=("ZZA", "ZZB"), sector=None)
    paper.plan_from_scan(engine, run, cfg)
    da, db = decs(engine)
    assert da.status == paper.ELIGIBLE and da.qty_risk == 11 and da.qty_cash == 9 and da.qty_exec == 9
    assert db.status == paper.NE_CAPITAL and db.cash < 100          # capital réservé par le plan actif de ZZA
    bars(engine, src, ids["ZZA"], [("2026-11-03", 100, 101, 99, 100)])
    paper.process(engine, cfg)
    t = trade(engine, ids["ZZA"])
    assert t.shares == 9 and abs(t.costs - 9 * 100 * 10 / 1e4) < 1e-9   # frais d'entrée 10 pb
    with session_scope(engine) as s:
        st = paper.portfolio_state(s, cfg)
    assert abs(st["cash"] - (1000 - t.costs - 900)) < 1e-9 and st["cash"] >= 0   # pas de levier


# --------------------------------------------------------------------------- 11. cohérence journal / tableau / Telegram
def test_11_journal_dashboard_and_telegram_agree(engine):
    src, ids, run = setup(engine)
    paper.plan_from_scan(engine, run, SMALL0)
    bars(engine, src, ids["ZZA"], [("2026-11-03", 100, 101, 99, 100), ("2026-11-04", 101, 106.5, 100.5, 105)])
    paper.process(engine, SMALL0)
    a = audit.audit(engine)
    sm = paper.summary(engine, SMALL0)
    assert not [x for x in a["anomalies"] if "≠" in x]
    assert abs(sm["performance"]["frais"] - a["recalcul"]["frais_journal"]) < 0.01
    assert sm["plans"]["crees"] == 1 and sm["positions_resume"]["ouvertes"] + sm["positions_resume"]["cloturees"] == 1
    notif.enqueue_trade_events(engine)
    (d,) = decs(engine)
    with session_scope(engine) as s:
        texts = [n.text for n in s.scalars(select(NotificationLog).where(NotificationLog.kind == "trade"))]
    planned = next(x for x in texts if "plan créé" in x)
    assert f"quantité {d.qty_exec}" in planned and f"{d.max_loss:.2f} $" in planned and "pas un cours en direct" in planned


# --------------------------------------------------------------------------- 12. identique après redémarrage
def test_12_restart_and_replay_give_identical_results(engine, tmp_path):
    def scenario(eng):
        src, ids, run = setup(eng, symbols=("ZZA", "ZZB"), sector=None, close=100.0)
        paper.plan_from_scan(eng, run, SMALL0)
        for sym in ids:
            bars(eng, src, ids[sym], [("2026-11-03", 100, 101, 99, 100), ("2026-11-04", 101, 106.5, 100.5, 105)])
        return src, ids, run

    src, ids, run = scenario(engine)
    paper.process(engine, SMALL0)
    snap = lambda eng: ([(d.symbol, d.status, d.qty_exec, d.fingerprint) for d in decs(eng)],
                        paper.summary(eng, SMALL0)["equity"], n_trades(eng))
    first = snap(engine)
    paper.plan_from_scan(engine, run, SMALL0)          # « redémarrage » : le même scan et les mêmes séances rejoués
    paper.process(engine, SMALL0)
    assert snap(engine) == first
    other = get_engine(f"sqlite:///{tmp_path / 'replay.db'}")
    Base.metadata.create_all(other)
    scenario(other)
    paper.process(other, SMALL0)
    assert snap(other) == first


# --------------------------------------------------------------------------- 13. pas de look-ahead
def test_13_eligibility_uses_only_information_known_at_signal(engine, tmp_path):
    """Des séances POSTÉRIEURES au signal (prix futurs corrélés à 1) ne changent pas l'évaluation du signal."""
    def run_case(eng, with_future):
        src, ids, run = setup(eng, symbols=("ZZA", "ZZB"), sector=None)
        with session_scope(eng) as s:            # passé décorrélé
            for k in range(40):
                d = date(2026, 9, 1 + k) if k < 30 else date(2026, 10, k - 29)
                for j, sym in enumerate(ids):
                    px = 100 + ((k * (3 + 4 * j)) % 11)
                    s.add(PriceBar(instrument_id=ids[sym], source_id=src, bar_date=d, open=px, high=px, low=px,
                                   close=px, adj_close=px, volume=1e6))
            if with_future:                      # futur parfaitement corrélé (inconnu au signal)
                for k in range(1, 70):
                    d = date.fromordinal(SIG.toordinal() + k)
                    for sym in ids:
                        px = 100 + (k % 9)
                        s.add(PriceBar(instrument_id=ids[sym], source_id=src, bar_date=d, open=px, high=px, low=px,
                                       close=px, adj_close=px, volume=1e6))
        paper.plan_from_scan(eng, run, SMALL0)
        return [(d.symbol, d.status, d.qty_exec, None if d.max_corr is None else round(d.max_corr, 9)) for d in decs(eng)]

    other = get_engine(f"sqlite:///{tmp_path / 'future.db'}")
    Base.metadata.create_all(other)
    assert run_case(engine, False) == run_case(other, True)


# --------------------------------------------------------------------------- rejeu avant / après (base jetable)
def test_replay_refuses_non_local_database_and_replays_chronologically(engine):
    import pytest
    from sqlalchemy import create_engine

    from stockhunter.portfolio import replay
    with pytest.raises(SystemExit):
        replay.replay(create_engine("postgresql+psycopg://u@db.example.com:5432/prod"))
    src, ids, run = setup(engine, close=500.0, atr_pct=0.04)       # refusée à chaque scan
    with session_scope(engine) as s:                                 # état « avant » de l'ancien code : un plan annulé
        sv = s.scalars(select(ScanRun)).first().strategy_version_id
        s.add(PaperTrade(instrument_id=ids["ZZA"], scan_run_id=run, strategy_version_id=sv, signal_date=SIG,
                         status="CANCELLED", signal_close=500, atr_at_signal=20, entry_min=480, entry_max=515,
                         stop_initial_offset=40, risk_fraction=0.005, strategies="B", plan_json="{}",
                         close_reason="risque par position trop faible pour une action"))
        from stockhunter.db.models import KeyValue
        s.add(KeyValue(key="paper_config", value=json.dumps({"capital": 5000.0})))
    new_scan(engine, src, ids, date(2026, 11, 3), close=500.0, atr_pct=0.04)
    out = replay.replay(engine)
    assert out["avant"]["plans_generes"] == 1 and out["apres"]["plans_generes"] == 0
    assert out["apres"]["evaluations"] == 2 and out["apres"]["repetitions_sans_nouveau_plan"] == 1
    assert out["scans_rejoues"] == ["2026-11-02", "2026-11-03"]


def test_process_until_ignores_later_sessions(engine):
    src, ids, run = setup(engine)
    paper.plan_from_scan(engine, run, SMALL0)
    bars(engine, src, ids["ZZA"], [("2026-11-03", 100, 101, 99, 100), ("2026-11-04", 99, 99.5, 90, 91)])
    paper.process(engine, SMALL0, until=date(2026, 11, 3))
    assert trade(engine, ids["ZZA"]).status == "OPEN"
    paper.process(engine, SMALL0)
    assert trade(engine, ids["ZZA"]).status == "CLOSED"
