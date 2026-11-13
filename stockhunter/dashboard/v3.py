"""Pages StockHunter 3.0 : scanner d'opportunités (univers large), fiche approfondie, laboratoire, santé du système.

Lecture seule de la base : tout ce qui demande les données de marché complètes (classement, test prospectif) est
calculé par la chaîne nocturne et enregistré ; le serveur web ne fait qu'afficher.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine

from stockhunter.config import Settings
from stockhunter.db.models import (
    ApiCall,
    DataAnomaly,
    DiscoveryCandidate,
    DiscoveryDeep,
    DiscoveryOutcome,
    DiscoveryRun,
    Instrument,
    JobRun,
    KeyValue,
    NotificationLog,
    PaperTrade,
    PaperTradeEvent,
)
from stockhunter.db.session import session_scope

CATEGORY_ORDER = ("momentum", "breakout", "steady", "shock", "watch")


def kv_json(s, key: str) -> Any:
    kv = s.get(KeyValue, key)
    return json.loads(kv.value) if kv else None


def discovery_view(engine: Engine, as_of: date | None = None, category: str = "", tier: str = "", q: str = "",
                   min_conf: float | None = None) -> dict[str, Any] | None:
    with session_scope(engine) as s:
        dates = list(s.scalars(select(DiscoveryRun.as_of).order_by(DiscoveryRun.as_of.desc()).limit(60)))
        if not dates:
            return None
        run = s.scalar(select(DiscoveryRun).where(DiscoveryRun.as_of == (as_of or dates[0]))
                       .order_by(DiscoveryRun.id.desc()))
        if run is None:
            return None
        ov = json.loads(run.overview_json)
        prev_date = next((d for d in dates if d < run.as_of), None)
        prev = set()
        if prev_date:
            prev = set(s.execute(select(DiscoveryCandidate.ticker, DiscoveryCandidate.category)
                                 .where(DiscoveryCandidate.as_of == prev_date)).all())
        rows = []
        for c, o in s.execute(select(DiscoveryCandidate, DiscoveryOutcome)
                              .join(DiscoveryOutcome, DiscoveryOutcome.candidate_id == DiscoveryCandidate.id, isouter=True)
                              .where(DiscoveryCandidate.run_id == run.id)
                              .order_by(DiscoveryCandidate.category, DiscoveryCandidate.rank)):
            card = json.loads(c.card_json)
            if category and c.category != category:
                continue
            if tier and not card["tier"].startswith(tier):
                continue
            if q and q.lower() not in (c.ticker + " " + (c.name or "")).lower():
                continue
            if min_conf is not None and c.confidence < min_conf:
                continue
            card["new"] = bool(prev_date) and (c.ticker, c.category) not in prev
            card["outcome"] = None if o is None else {"status": o.status, "r": o.r_multiple, "final": o.final}
            rows.append(card)
        cats = [{"key": k, **ov["categories"][k], "rows": [r for r in rows if r["category"] == k]}
                for k in CATEGORY_ORDER if k in ov["categories"]]
        return {"as_of": run.as_of, "dates": dates, "universe": run.universe_size, "market": ov["marche"],
                "conclusion": run.conclusion, "categories": cats, "version": run.engine_version,
                "n": len(rows), "prev_date": prev_date}


def ticker_view(engine: Engine, ticker: str) -> dict[str, Any] | None:
    t = ticker.upper()
    with session_scope(engine) as s:
        hist = s.execute(select(DiscoveryCandidate, DiscoveryOutcome)
                         .join(DiscoveryOutcome, DiscoveryOutcome.candidate_id == DiscoveryCandidate.id, isouter=True)
                         .where(DiscoveryCandidate.ticker == t)
                         .order_by(DiscoveryCandidate.as_of.desc(), DiscoveryCandidate.category).limit(120)).all()
        if not hist:
            return None
        deep = s.get(DiscoveryDeep, t)
        tracked = s.scalar(select(Instrument.symbol).where(Instrument.symbol == t, Instrument.is_active.is_(True)))
        latest_date = hist[0][0].as_of
        cards = [json.loads(c.card_json) for c, _o in hist if c.as_of == latest_date]
        history = [{"as_of": c.as_of, "category": c.category, "rank": c.rank, "score": c.score, "close": c.close,
                    "status": o.status if o else None, "r": o.r_multiple if o else None,
                    "excess_20": o.excess_20 if o else None, "final": bool(o and o.final),
                    "detail": o.detail if o else None, "entry": o.entry_date if o else None, "exit": o.exit_date if o else None}
                   for c, o in hist]
        return {"ticker": t, "name": hist[0][0].name, "as_of": latest_date, "cards": cards, "history": history,
                "deep": json.loads(deep.payload) if deep else None, "deep_as_of": deep.as_of if deep else None,
                "tracked": tracked}


def lab_view(engine: Engine) -> dict[str, Any]:
    from stockhunter.discovery.store import performance
    from stockhunter.lab import registry
    reg = registry.load()
    fam = registry.family_pvalues(reg)
    with session_scope(engine) as s:
        fwd = kv_json(s, "forward_report")
    exps = [{**e, **({"p": fam[e["id"]]["p"], "holm": fam[e["id"]]["holm"], "bh": fam[e["id"]]["bh"]} if e["id"] in fam else {})}
            for e in reg["experiences"]]
    return {"experiments": exps, "holdouts": reg["periodes_reservees"], "runs": reg.get("executions", []),
            "n_tests": len(registry.tests(reg)), "forward": fwd, "discovery": performance(engine),
            "validated": [e for e in exps if e["decision"].startswith(("VALIDÉ", "CONFIRMÉ"))]}


def decision_journal(engine: Engine, limit: int = 60) -> list[dict]:
    with session_scope(engine) as s:
        rows = s.execute(select(PaperTradeEvent, Instrument.symbol).select_from(PaperTradeEvent)
                         .join(PaperTrade, PaperTrade.id == PaperTradeEvent.trade_id)
                         .join(Instrument, Instrument.id == PaperTrade.instrument_id)
                         .order_by(PaperTradeEvent.id.desc()).limit(limit)).all()
        return [{"date": e.event_date, "symbol": sym, "kind": e.kind, "price": e.price, "shares": e.shares,
                 "detail": e.detail} for e, sym in rows]


def system_view(engine: Engine, settings: Settings) -> dict[str, Any]:
    from stockhunter.data.calendar import staleness
    from stockhunter.db.models import PriceBar
    now = datetime.now(timezone.utc)
    lim = settings.limits
    quota_def = {"tiingo": [("1 h", 1, lim.tiingo_per_hour), ("24 h", 24, lim.tiingo_per_day)],
                 "massive": [("1 min", 1 / 60, lim.massive_per_minute)],
                 "finnhub": [("1 min", 1 / 60, lim.finnhub_per_minute)],
                 "sec_edgar": [("24 h", 24, None)], "fred": [("24 h", 24, None)]}
    with session_scope(engine) as s:
        pipe = kv_json(s, "pipeline_last")
        jobs: dict[str, dict] = {}
        for name, status, started, finished, detail in s.execute(
                select(JobRun.job_name, JobRun.status, JobRun.started_at, JobRun.finished_at, JobRun.detail)
                .where(JobRun.started_at >= now - timedelta(days=30)).order_by(JobRun.id)):
            j = jobs.setdefault(name, {"runs": 0, "failed": 0})
            j["runs"] += 1
            j["failed"] += status == "failed"
            j["last"], j["last_at"], j["detail"] = status, started, detail if status in ("failed", "partial") else None
            if status in ("success", "partial"):
                j["ok_at"] = finished or started
        quotas = []
        for prov, windows in quota_def.items():
            for label, hours, cap in windows:
                used = s.scalar(select(func.count()).select_from(ApiCall).where(
                    ApiCall.provider == prov, ApiCall.from_cache.is_(False),
                    ApiCall.called_at >= now - timedelta(hours=max(hours, 1 / 60)))) or 0
                day = s.scalar(select(func.count()).select_from(ApiCall).where(
                    ApiCall.provider == prov, ApiCall.from_cache.is_(False), ApiCall.called_at >= now - timedelta(hours=24))) or 0
                errs = s.scalar(select(func.count()).select_from(ApiCall).where(
                    ApiCall.provider == prov, ApiCall.called_at >= now - timedelta(hours=24),
                    ApiCall.status_code.is_not(None), ApiCall.status_code >= 400)) or 0
                quotas.append({"provider": prov, "window": label, "used": used, "cap": cap, "day": day, "errors": errs})
        last_price = s.scalar(select(func.max(PriceBar.bar_date)))
        anomalies = dict(s.execute(select(DataAnomaly.severity, func.count()).where(DataAnomaly.resolved_at.is_(None))
                                   .group_by(DataAnomaly.severity)).all())
        notif = dict(s.execute(select(NotificationLog.status, func.count())
                               .where(NotificationLog.created_at >= now - timedelta(days=7))
                               .group_by(NotificationLog.status)).all())
        last_sent = s.scalar(select(func.max(NotificationLog.sent_at)))
        failed_notif = [{"at": n.created_at, "kind": n.kind, "error": n.error}
                        for n in s.scalars(select(NotificationLog).where(NotificationLog.status == "FAILED")
                                           .order_by(NotificationLog.id.desc()).limit(5))]
        reader = kv_json(s, "telegram_reader")
        db: dict[str, Any] = {"dialect": engine.dialect.name}
        try:
            if engine.dialect.name == "postgresql":
                db["bytes"] = s.execute(text("SELECT pg_database_size(current_database())")).scalar()
                db["tables"] = [{"name": n, "bytes": b, "rows": r} for n, b, r in s.execute(text(
                    "SELECT relname, pg_total_relation_size(relid), n_live_tup FROM pg_stat_user_tables "
                    "ORDER BY pg_total_relation_size(relid) DESC LIMIT 8"))]
                db["version"] = s.execute(text("SHOW server_version")).scalar()
            else:
                pc, ps = s.execute(text("PRAGMA page_count")).scalar(), s.execute(text("PRAGMA page_size")).scalar()
                db["bytes"] = (pc or 0) * (ps or 0)
            db["ok"] = True
        except Exception as exc:                                    # la page de santé ne doit jamais planter
            db.update(ok=False, error=str(exc)[:200])
        last_disc = s.scalar(select(func.max(DiscoveryRun.as_of)))
        last_disc_at = s.scalar(select(func.max(DiscoveryRun.created_at)))
        last_signal = s.execute(select(DiscoveryCandidate.as_of, DiscoveryCandidate.ticker)
                                .order_by(DiscoveryCandidate.id.desc()).limit(1)).first()
        last_ingest_ok = s.scalar(select(func.max(JobRun.finished_at)).where(
            JobRun.job_name == "ingest_prices", JobRun.status.in_(("success", "partial"))))
        open_failures = s.scalar(select(func.count()).select_from(NotificationLog).where(
            NotificationLog.status == "FAILED", NotificationLog.created_at >= now - timedelta(days=2))) or 0
    reader_live = None
    if reader:
        until = datetime.fromisoformat(reader["until"])
        reader_live = {"owner": "serveur web" if reader["owner"].startswith("worker") else "tâche planifiée",
                       "active": until > now, "until": until}
    limit_bytes = int(settings.db_volume_mb * 1024 * 1024)
    try:
        from stockhunter.db.copy import storage
        disk = storage(engine, settings.db_volume_mb)
    except Exception as exc:                                        # la page de santé ne doit jamais planter
        disk = {"niveau": "inconnu", "erreur": str(exc)[:160]}
    stale = staleness(last_price)
    age = lambda t: None if t is None else (now - (t if t.tzinfo else t.replace(tzinfo=timezone.utc)))
    pipe_ok = pipe and pipe.get("final") and pipe.get("status") == "success"
    checks = [
        ("Service web en ligne", True, "cette page répond"),
        ("Base de données disponible", bool(db.get("ok")), db.get("error") or f"{db['dialect']} {db.get('version') or ''}"),
        ("Espace disque", disk.get("niveau") in ("ok", "attention"),
         f"{disk.get('utilise_mo', '?')} Mo / {disk.get('volume_mo', '?')} Mo — niveau {disk.get('niveau')}"
         if "utilise_mo" in disk else disk.get("erreur") or "SQLite : sans objet"),
        ("Données fraîches", not stale["stale"],
         f"dernière clôture {last_price or 'aucune'} ; séance attendue {stale['expected']}"),
        ("Dernière collecte des prix réussie", age(last_ingest_ok) is not None and age(last_ingest_ok) < timedelta(hours=36),
         str(last_ingest_ok)[:16] if last_ingest_ok else "jamais"),
        ("Chaîne nocturne complète", bool(pipe_ok),
         "jamais exécutée" if not pipe else f"{pipe.get('status')} ({'terminée' if pipe.get('final') else 'en cours'}), "
         f"séance {pipe.get('seance', '?')}"),
        ("Dernier scanner exécuté", last_disc is not None and (stale["expected"] - last_disc).days <= 4,
         f"séance {last_disc}" if last_disc else "jamais"),
        ("Dernier signal enregistré", last_signal is not None,
         f"{last_signal[1]} le {last_signal[0]}" if last_signal else "aucun"),
        ("Dernière notification envoyée", age(last_sent) is not None and age(last_sent) < timedelta(hours=36),
         str(last_sent)[:16] if last_sent else "jamais"),
        ("Erreurs non résolues", open_failures == 0 and not anomalies.get("critical"),
         f"{open_failures} envoi(s) Telegram en échec (48 h), {anomalies.get('critical', 0)} anomalie(s) critique(s)"),
    ]
    return {"checks": [{"label": l, "ok": ok, "detail": d} for l, ok, d in checks], "disk": disk,
            "last_disc_at": last_disc_at,
            "pipeline": pipe, "jobs": jobs, "quotas": quotas, "stale": stale, "last_price": last_price,
            "anomalies": anomalies, "notif": notif, "last_sent": last_sent, "failed_notif": failed_notif,
            "reader": reader_live, "db": db, "db_limit": limit_bytes,
            "db_share": disk.get("part"),
            "last_discovery": last_disc, "now": now}


def register(app, engine: Engine, settings: Settings, guard, render) -> None:
    @app.get("/scanner", response_class=HTMLResponse)
    def scanner(request: Request, d: str = "", category: str = "", tier: str = "", q: str = "", min_conf: str = ""):
        sess = guard(request)
        try:
            as_of = date.fromisoformat(d) if d else None
        except ValueError:
            as_of = None
        mc = float(min_conf) if min_conf.replace(".", "", 1).isdigit() else None
        view = discovery_view(engine, as_of, category, tier, q.strip()[:20], mc)
        f = {"d": d, "category": category, "tier": tier, "q": q, "min_conf": min_conf}
        name = "_scanner_table.html" if request.headers.get("HX-Request") else "scanner.html"
        return render(request, name, sess, v=view, f=f)

    @app.get("/discover/{ticker}", response_class=HTMLResponse)
    def discover(request: Request, ticker: str):
        sess = guard(request)
        view = ticker_view(engine, ticker[:12])
        if view is None:
            raise HTTPException(404, "titre jamais relevé par le moteur de découverte")
        return render(request, "discover.html", sess, v=view, series=json.dumps((view["deep"] or {}).get("series")))

    @app.get("/lab", response_class=HTMLResponse)
    def lab(request: Request):
        return render(request, "lab.html", guard(request), v=lab_view(engine))

    @app.get("/system", response_class=HTMLResponse)
    def system(request: Request):
        return render(request, "system.html", guard(request), v=system_view(engine, settings))
