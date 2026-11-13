"""Lecture des données pour le dashboard (dictionnaires simples, aucun calcul de signal ici)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from stockhunter.db.models import (
    DataAnomaly,
    Filing,
    Instrument,
    JobRun,
    NotificationLog,
    PaperTrade,
    PaperTradeEvent,
    PriceBar,
    ScanRun,
    Score,
    StatusChange,
    StrategyVersion,
)
from stockhunter.db.session import session_scope


def latest_run(s) -> ScanRun | None:
    return s.scalars(select(ScanRun).order_by(ScanRun.as_of.desc(), ScanRun.id.desc()).limit(1)).first()


def score_rows(engine: Engine, statuses: tuple[str, ...] | None = None, strategy: str | None = None,
               min_score: float | None = None, q: str | None = None) -> tuple[dict | None, list[dict]]:
    from stockhunter.report_cli import validation_line
    with session_scope(engine) as s:
        run = latest_run(s)
        if run is None:
            return None, []
        sv = s.get(StrategyVersion, run.strategy_version_id)
        regime = {"label": "INCONNU", "reasons": [], **json.loads(run.regime_json)}
        meta = {"as_of": run.as_of, "id": run.id, "regime": regime,
                "sectors": json.loads(run.sectors_json), "version": f"{sv.name} {sv.version}", "stage": sv.stage,
                "validation": validation_line(s, run.strategy_version_id), "notes": run.notes}
        rows = []
        for sc, sym, name, sector in s.execute(select(Score, Instrument.symbol, Instrument.name, Instrument.sector)
                                               .join(Instrument).where(Score.scan_run_id == run.id)):
            strat = json.loads(sc.strategies_json)
            active = [k for k, v in strat.items() if v.get("active")]
            sub = json.loads(sc.subscores_json)
            m = json.loads(sc.metrics_json)
            if statuses and sc.status not in statuses:
                continue
            if strategy and strategy not in active:
                continue
            if min_score is not None and (sc.total or 0) < min_score:
                continue
            if q and q.upper() not in sym and q.lower() not in (name or "").lower():
                continue
            cat = sub.get("catalyst", {}).get("components", {}).get("réaction aux derniers résultats")
            rows.append({"symbol": sym, "name": name, "sector": sector, "close": sc.close, "price_date": sc.price_date,
                         "total": sc.total, "confidence": sc.confidence, "status": sc.status, "rank": sc.rank,
                         "reason": sc.status_reason, "active": active,
                         "sub": {k: v.get("score") for k, v in sub.items()},
                         "risk": sub.get("risk_quality", {}).get("score"), "atr": m.get("atr14_pct"),
                         "catalyst": cat, "pos": json.loads(sc.reasons_pos_json)[:1]})
        order = {"READY": 0, "DEVELOPING": 1, "WATCHLIST": 2, "REJECTED": 3}
        rows.sort(key=lambda r: (order[r["status"]], -(r["total"] or 0)))
        return meta, rows


def stock_detail(engine: Engine, symbol: str) -> dict[str, Any] | None:
    with session_scope(engine) as s:
        inst = s.scalar(select(Instrument).where(Instrument.symbol == symbol.upper()))
        if inst is None:
            return None
        row = s.execute(select(Score, ScanRun).join(ScanRun).where(Score.instrument_id == inst.id)
                        .order_by(ScanRun.as_of.desc(), ScanRun.id.desc()).limit(1)).first()
        hist = [{"as_of": h.as_of, "from": h.from_status, "to": h.to_status, "reason": h.reason}
                for h in s.scalars(select(StatusChange).where(StatusChange.instrument_id == inst.id)
                                   .order_by(StatusChange.as_of.desc(), StatusChange.id.desc()).limit(30))]
        filings = [{"form": f.form, "date": f.filed_date, "items": f.items, "url": f.url}
                   for f in s.scalars(select(Filing).where(Filing.instrument_id == inst.id,
                                                           Filing.form.in_(("8-K", "10-Q", "10-K", "8-K/A")))
                                      .order_by(Filing.filed_date.desc()).limit(12))]
        trades = []
        for t in s.scalars(select(PaperTrade).where(PaperTrade.instrument_id == inst.id).order_by(PaperTrade.id.desc())):
            evs = [{"date": e.event_date, "kind": e.kind, "detail": e.detail}
                   for e in s.scalars(select(PaperTradeEvent).where(PaperTradeEvent.trade_id == t.id)
                                      .order_by(PaperTradeEvent.id))]
            trades.append({"t": t, "plan": json.loads(t.plan_json), "events": evs})
        out = {"inst": inst, "history": hist, "filings": filings, "trades": trades, "score": None}
        if row:
            sc, run = row
            out["score"] = {"total": sc.total, "status": sc.status, "reason": sc.status_reason, "close": sc.close,
                            "price_date": sc.price_date, "confidence": sc.confidence, "rank": sc.rank,
                            "as_of": run.as_of, "sub": json.loads(sc.subscores_json),
                            "strategies": json.loads(sc.strategies_json), "pos": json.loads(sc.reasons_pos_json),
                            "neg": json.loads(sc.reasons_neg_json), "metrics": json.loads(sc.metrics_json),
                            "sources": json.loads(sc.sources_json)}
        for t in out["trades"]:
            s.expunge(t["t"])
        s.expunge(inst)
        return out


def price_series(engine: Engine, symbol: str, sessions: int = 260) -> dict[str, list]:
    with session_scope(engine) as s:
        iid = s.scalar(select(Instrument.id).where(Instrument.symbol == symbol.upper()))
        rows = s.execute(select(PriceBar.bar_date, PriceBar.close, PriceBar.adj_close, PriceBar.volume)
                         .where(PriceBar.instrument_id == iid).order_by(PriceBar.bar_date.desc()).limit(sessions + 200)).all()
    if not rows:
        return {"dates": [], "close": [], "sma50": [], "sma200": [], "volume": []}
    df = pd.DataFrame(rows, columns=["d", "close", "adj", "vol"]).iloc[::-1]
    df["sma50"] = df["adj"].rolling(50).mean() * df["close"] / df["adj"]
    df["sma200"] = df["adj"].rolling(200).mean() * df["close"] / df["adj"]
    df = df.iloc[-sessions:]
    clean = lambda ser: [None if pd.isna(x) else round(float(x), 2) for x in ser]
    return {"dates": [d.isoformat() for d in df["d"]], "close": clean(df["close"]), "sma50": clean(df["sma50"]),
            "sma200": clean(df["sma200"]), "volume": [float(x) for x in df["vol"]]}


def freshness(engine: Engine) -> dict[str, Any]:
    with session_scope(engine) as s:
        last_price = s.scalar(select(func.max(PriceBar.bar_date)))
        jobs = {}
        for name, status, started, finished in s.execute(
                select(JobRun.job_name, JobRun.status, JobRun.started_at, JobRun.finished_at).order_by(JobRun.id)):
            jobs.setdefault(name, {})["last"] = (status, started)
            if status in ("success", "partial"):
                jobs[name]["ok"] = finished or started
        alerts = [{"kind": n.kind, "status": n.status, "at": n.sent_at or n.created_at,
                   "title": n.text.split("\n")[0].replace("<b>", "").replace("</b>", "")}
                  for n in s.scalars(select(NotificationLog).order_by(NotificationLog.id.desc()).limit(8))]
        crit = s.scalar(select(func.count()).select_from(DataAnomaly).where(
            DataAnomaly.resolved_at.is_(None), DataAnomaly.severity == "critical")) or 0
    from stockhunter.data.calendar import staleness
    return {"last_price": last_price, "jobs": jobs, "alerts": alerts, "critical": crit,
            "now": datetime.now(timezone.utc), "stale": staleness(last_price)}
