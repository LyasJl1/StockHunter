"""Audit EN LECTURE SEULE du paper trading (3.4) : journal, positions, plans, capital, P&L, frais, signaux, alertes.

Recalcule chaque grandeur à partir du journal d'événements (source de vérité immuable) et la compare aux valeurs
enregistrées dans `paper_trades` et à celles du tableau de bord (`paper.summary`). N'écrit rien. N'affiche ni
secret ni chaîne de connexion : symboles, dates, prix simulés et compteurs uniquement.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from stockhunter.db.models import Instrument, NotificationLog, PaperTrade, PaperTradeEvent, PriceBar, ScanRun, Score
from stockhunter.db.session import session_scope
from stockhunter.portfolio import paper


def _r(x, n=2):
    return None if x is None else round(float(x), n)


def audit(engine: Engine) -> dict[str, Any]:
    cfg = paper.load_config(engine)
    out: dict[str, Any] = {"config": {k: getattr(cfg, k) for k in cfg.__dataclass_fields__}}
    anomalies: list[str] = []
    with session_scope(engine) as s:
        trades = list(s.scalars(select(PaperTrade).order_by(PaperTrade.id)))
        syms = {i.id: i.symbol for i in s.scalars(select(Instrument).where(
            Instrument.id.in_({t.instrument_id for t in trades} or {-1})))}
        sectors = {i.id: i.sector_etf for i in s.scalars(select(Instrument).where(
            Instrument.id.in_({t.instrument_id for t in trades} or {-1})))}
        evs = list(s.scalars(select(PaperTradeEvent).order_by(PaperTradeEvent.trade_id, PaperTradeEvent.event_date,
                                                                PaperTradeEvent.id)))
        last_px = {}
        for t in trades:
            row = s.execute(select(PriceBar.close, PriceBar.bar_date).where(PriceBar.instrument_id == t.instrument_id)
                            .order_by(PriceBar.bar_date.desc()).limit(1)).first()
            last_px[t.id] = (row.close, str(row.bar_date)) if row else (None, None)
        runs = list(s.execute(select(ScanRun.id, ScanRun.as_of).order_by(ScanRun.as_of.desc()).limit(10)))
        ready = {str(a): s.scalar(select(func.count()).select_from(Score).where(Score.scan_run_id == rid,
                                                                                 Score.status == "READY"))
                 for rid, a in runs}
        notes = list(s.execute(select(NotificationLog.kind, NotificationLog.status, NotificationLog.dedup_key,
                                      NotificationLog.text).where(NotificationLog.kind.in_(("trade", "alert")))
                               .order_by(NotificationLog.id.desc()).limit(200)))
    by_trade: dict[int, list] = {}
    for e in evs:
        by_trade.setdefault(e.trade_id, []).append(e)
    rows, cash = [], cfg.capital
    fees_events = 0.0
    for t in trades:
        ev = by_trade.get(t.id, [])
        held, entry, realized, fees = 0.0, None, 0.0, 0.0
        buys = sells = stops = 0
        for e in ev:
            if e.kind == "OPENED" and e.shares:
                held, entry = e.shares, e.price
                fees += e.price * e.shares * cfg.cost_bps / 1e4
                cash -= e.price * e.shares
                buys += 1
            elif e.kind in ("TP1", "CLOSED") and e.shares and entry is not None:
                realized += (e.price - entry) * e.shares
                fees += e.price * e.shares * cfg.cost_bps / 1e4
                cash += e.price * e.shares
                held -= e.shares
                sells += 1
            elif e.kind == "STOP_MOVED":
                stops += 1
            elif e.kind == "SPLIT" and entry is not None and e.price:
                held, entry = held * e.price, entry / e.price
        cash -= fees
        fees_events += fees
        last, last_d = last_px[t.id]
        latent = (last - entry) * held if held > 1e-9 and last is not None and entry is not None else 0.0
        risk_per_share = t.stop_initial_offset
        risk_budget = cfg.capital * t.risk_fraction
        rec = {"id": t.id, "symbole": syms.get(t.instrument_id), "secteur": sectors.get(t.instrument_id),
               "statut": t.status, "signal": str(t.signal_date), "zone": [_r(t.entry_min), _r(t.entry_max)],
               "risque_par_action": _r(risk_per_share), "budget_risque": _r(risk_budget),
               "quantite_max_risque": int(risk_budget // risk_per_share) if risk_per_share else None,
               "entree": [str(t.entry_date) if t.entry_date else None, _r(t.entry_price), t.shares],
               "stop": _r(t.stop), "tp1": _r(t.tp1), "tp2": _r(t.tp2), "tp1_fait": t.tp1_done,
               "dernier_cours": [_r(last), last_d], "realise_table": _r((t.realized_pnl or 0) - (t.costs or 0)),
               "realise_journal": _r(realized - fees), "frais_table": _r(t.costs), "frais_journal": _r(fees),
               "latent": _r(latent), "achats": buys, "ventes": sells, "stops_deplaces": stops,
               "motif": t.close_reason, "evenements": [(str(e.event_date), e.kind, _r(e.price), e.shares) for e in ev]}
        if abs((rec["realise_table"] or 0) - (rec["realise_journal"] or 0)) > 0.01:
            anomalies.append(f"{rec['symbole']} #{t.id} : réalisé table {rec['realise_table']} ≠ journal {rec['realise_journal']}")
        if abs((t.costs or 0) - fees) > 0.01:
            anomalies.append(f"{rec['symbole']} #{t.id} : frais table {_r(t.costs)} ≠ journal {_r(fees)}")
        if t.status == "OPEN" and abs((t.shares or 0) - held) > 1e-9:
            anomalies.append(f"{rec['symbole']} #{t.id} : quantité table {t.shares} ≠ journal {held}")
        if t.status == "CANCELLED" and buys:
            anomalies.append(f"{rec['symbole']} #{t.id} : annulé mais un achat figure au journal")
        rows.append(rec)
    sm = paper.summary(engine, cfg)
    refusals = Counter((r["symbole"], r["motif"]) for r in rows if r["statut"] == "CANCELLED")
    repeated = {f"{k[0]} — {k[1]}": v for k, v in refusals.items() if v > 1}
    realized_j = sum(r["realise_journal"] or 0 for r in rows)
    latent_j = sum(r["latent"] or 0 for r in rows)
    invested = sum((r["entree"][1] or 0) * (r["entree"][2] or 0) for r in rows if r["statut"] == "OPEN")
    out.update({
        "comptes": {"lignes_paper_trades": len(trades), "plans_actifs": sum(t.status == "PLANNED" for t in trades),
                    "positions_ouvertes": sum(t.status == "OPEN" for t in trades),
                    "positions_cloturees": sum(t.status == "CLOSED" for t in trades),
                    "plans_annules": sum(t.status == "CANCELLED" for t in trades),
                    "achats_simules": sum(r["achats"] for r in rows), "ventes_simulees": sum(r["ventes"] for r in rows),
                    "stops_deplaces": sum(r["stops_deplaces"] for r in rows)},
        "dashboard": {k: (_r(v) if isinstance(v, float) else v) for k, v in sm.items() if k != "positions"},
        "recalcul": {"tresorerie_journal": _r(cash), "investi": _r(invested), "realise_net_journal": _r(realized_j),
                     "latent_journal": _r(latent_j), "valeur_journal": _r(cfg.capital + realized_j + latent_j),
                     "frais_journal": _r(fees_events)},
        "refus_repetes": repeated, "signaux_ready": ready, "trades": rows,
        "alertes": {"par_statut": dict(Counter((k, st) for k, st, _d, _t in notes)),
                    "doublons_de_texte": sum(v - 1 for v in Counter(t for _k, _s, _d, t in notes).values() if v > 1),
                    "trade": [(d, st) for k, st, d, _t in notes if k == "trade"][:30]},
    })
    if _r(sm["equity"]) != out["recalcul"]["valeur_journal"]:
        anomalies.append(f"valeur dashboard {_r(sm['equity'])} ≠ recalcul journal {out['recalcul']['valeur_journal']}")
    if sm["trades_total"] != sm["open"] + sm["closed"]:
        anomalies.append(f"le dashboard compte {sm['trades_total']} « trades » dont {sm['planned']} plans et "
                         f"{sm['cancelled']} annulés : un plan n'est pas un trade")
    out["anomalies"] = anomalies
    return out


def main(engine: Engine) -> str:
    return json.dumps(audit(engine), ensure_ascii=False, indent=1, default=str)
