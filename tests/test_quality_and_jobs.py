from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from stockhunter.data.ingestion import ensure_sources
from stockhunter.data.providers import parse_tiingo_prices
from stockhunter.data.quality import run_quality_checks
from stockhunter.db.models import DataAnomaly, Instrument, JobLock, JobRun, PriceBar
from stockhunter.db.session import session_scope
from stockhunter.db.upsert import upsert
from stockhunter.jobs.runner import JobLocked, job_lock, run_job
from tests.fixtures import FIXTURE_tiingo_bars

TODAY = date(2026, 11, 12)


def add_instrument(engine, symbol, bars, asset_type="stock"):
    src = ensure_sources(engine)["tiingo"]
    with session_scope(engine) as s:
        inst = Instrument(symbol=symbol, asset_type=asset_type, cik="0999999901")
        s.add(inst)
        s.flush()
        upsert(s, PriceBar, [{**b, "instrument_id": inst.id, "source_id": src} for b in bars],
               ["instrument_id", "bar_date", "source_id"])
        return inst.id


def findings_for(findings, check):
    return [f for f in findings if f.check == check]


def test_quality_detects_problems(engine):
    clean = parse_tiingo_prices(FIXTURE_tiingo_bars(date(2026, 6, 1), date(2026, 11, 11)))
    add_instrument(engine, "CLEAN", clean)

    stale = parse_tiingo_prices(FIXTURE_tiingo_bars(date(2026, 6, 1), date(2026, 10, 30)))
    add_instrument(engine, "STALE", stale)

    broken = parse_tiingo_prices(FIXTURE_tiingo_bars(date(2026, 6, 1), date(2026, 11, 11)))
    broken[10]["high"] = broken[10]["low"] - 5           # OHLC incohérent
    broken[20]["adj_close"] *= 3                         # saut >50 % sans split
    del broken[30:40]                                    # trou de 10 séances
    add_instrument(engine, "BROKEN", broken)

    with session_scope(engine) as s:
        s.add(Instrument(symbol="EMPTY", asset_type="stock"))

    f = run_quality_checks(engine, today=TODAY)
    syms = lambda check: {x.detail.split(" ")[0] for x in findings_for(f, check)}
    assert syms("price_stale") == {"STALE"}
    assert syms("price_ohlc_incoherent") == {"BROKEN"}
    assert syms("price_extreme_move") == {"BROKEN"}
    assert syms("price_gaps") == {"BROKEN"}
    assert syms("price_missing") == {"EMPTY"}
    assert not [x for x in f if x.detail.startswith("CLEAN") and x.check.startswith("price")]
    assert next(x for x in f if x.check == "price_stale").severity == "critical"  # 8 séances de retard

    # 2e exécution : les anciennes anomalies sont closes, l'historique conservé
    n_first = len(f)
    run_quality_checks(engine, today=TODAY)
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(DataAnomaly).where(DataAnomaly.resolved_at.is_(None))) == n_first
        assert s.scalar(select(func.count()).select_from(DataAnomaly)) == 2 * n_first


def test_lock_prevents_concurrency_and_expires(engine):
    with job_lock(engine, "j"):
        with pytest.raises(JobLocked):
            with job_lock(engine, "j"):
                pass
        run = run_job(engine, "j", lambda ctx: ctx.success())
        assert run.status == "skipped"
    # verrou orphelin expiré (crash) -> repris
    with session_scope(engine) as s:
        s.add(JobLock(job_name="k", owner="dead", expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    assert run_job(engine, "k", lambda ctx: ctx.success()).status == "success"


def test_job_status_partial_and_failed(engine):
    def partial(ctx):
        ctx.success()
        ctx.failure("X", RuntimeError("boom"))

    r = run_job(engine, "p", partial)
    assert r.status == "partial" and "X: boom" in r.detail
    r = run_job(engine, "f", lambda ctx: 1 / 0)
    assert r.status == "failed" and "ZeroDivisionError" in r.detail
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(JobRun)) == 2
