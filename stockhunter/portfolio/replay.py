"""Avant / après 3.4 sur les MÊMES données (base JETABLE uniquement).

Usage prévu : le workflow « Comparaison avant/après 3.4 » restaure une copie de la production dans un conteneur
PostgreSQL éphémère, mesure l'état « avant » (journal produit par l'ancien code), vide les tables du paper
trading DE LA COPIE, puis rejoue chaque scan dans l'ordre chronologique avec le code actuel : à chaque scan,
seules les séances antérieures ou égales à sa date sont traitées (`process(until=...)`), puis les plans sont
évalués. La production n'est jamais modifiée : la commande refuse toute base qui n'est pas locale.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from stockhunter.db.models import NotificationLog, PaperDecision, PaperTrade, PaperTradeEvent, PriceBar, ScanRun
from stockhunter.db.session import session_scope
from stockhunter.notifications import engine as notif
from stockhunter.portfolio import audit, paper

LOCAL_HOSTS = {"localhost", "127.0.0.1", "postgres", "::1"}


def _is_throwaway(engine: Engine) -> bool:
    url = engine.url
    if url.get_backend_name() == "sqlite":
        return True
    return (url.host or "") in LOCAL_HOSTS


def metrics(engine: Engine, cfg: paper.PaperConfig) -> dict[str, Any]:
    a = audit.audit(engine)
    sm = paper.summary(engine, cfg)
    with session_scope(engine) as s:
        trades = list(s.scalars(select(PaperTrade)))
        risks = [t.initial_risk for t in trades if t.initial_risk]
        n_dec = s.scalar(select(func.count()).select_from(PaperDecision)) or 0
        alerts = s.scalar(select(func.count()).select_from(NotificationLog).where(NotificationLog.kind == "trade")) or 0
    refus = sum(v for v in a["refus_repetes"].values())
    return {"plans_generes": len(trades), "plans_refuses_ou_annules": sm["cancelled"],
            "refus_repetes_de_plans": refus, "evaluations": n_dec,
            "repetitions_sans_nouveau_plan": sm["decisions"]["repetitions_sans_nouveau_plan"],
            "decisions_par_statut": sm["decisions"]["par_statut"],
            "positions_ouvertes": sm["open"], "trades_clotures": sm["closed"],
            "capital_utilise": round(a["recalcul"]["investi"], 2),
            "risque_moyen_par_position": round(sum(risks) / len(risks), 2) if risks else None,
            "frais": round(sm["performance"]["frais"], 2), "realise_net": round(sm["realized"], 2),
            "latent": round(sm["unrealized"], 2), "valeur": round(sm["equity"], 2),
            "alertes_telegram_trade": alerts, "anomalies": a["anomalies"]}


def replay(engine: Engine, since: date | None = None) -> dict[str, Any]:
    if not _is_throwaway(engine):
        raise SystemExit("rejeu refusé : la base n'est pas une copie locale jetable (production protégée)")
    cfg = paper.load_config(engine)
    before = metrics(engine, cfg)
    with session_scope(engine) as s:
        first = s.scalar(select(func.min(PaperTrade.signal_date)))
        since = since or first
        runs = [(r.id, r.as_of) for r in s.scalars(select(ScanRun).where(ScanRun.as_of >= since)
                                                    .order_by(ScanRun.as_of, ScanRun.id))] if since else []
        s.execute(delete(PaperTradeEvent))                # COPIE jetable uniquement (vérifié ci-dessus)
        s.execute(delete(PaperDecision))
        s.execute(delete(PaperTrade))
        s.execute(delete(NotificationLog).where(NotificationLog.kind == "trade"))
        last_bar = s.scalar(select(func.max(PriceBar.bar_date)))
    for rid, as_of in runs:
        paper.process(engine, cfg, until=as_of)           # séances jusqu'au scan inclus, puis décision
        paper.plan_from_scan(engine, rid, cfg)
    paper.process(engine, cfg, until=last_bar)
    notif.enqueue_trade_events(engine)
    notif.enqueue_decision_alerts(engine)
    after = metrics(engine, cfg)
    return {"scans_rejoues": [str(d) for _r, d in runs], "derniere_seance": str(last_bar), "avant": before,
            "apres": after}


def main(engine: Engine) -> str:
    return json.dumps(replay(engine), ensure_ascii=False, indent=1, default=str)
