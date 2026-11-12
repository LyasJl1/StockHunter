"""Telegram Notification Engine : file d'attente persistante, déduplication, idempotence, anti-spam, retries.

Chaque message reçoit une clé (ex. « report:evening:2026-11-11 », « trade-event:42 ») : la contrainte d'unicité
en base garantit qu'un même événement n'est jamais mis deux fois en file, donc jamais envoyé deux fois,
même si un workflow est relancé. Un message non envoyé (panne, quota) reste en file pour le run suivant.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import Engine

from stockhunter.config import Settings
from stockhunter.db.models import (
    Instrument,
    JobRun,
    KeyValue,
    NotificationLog,
    PaperTrade,
    PaperTradeEvent,
    Score,
)
from stockhunter.db.session import session_scope
from stockhunter.db.upsert import upsert
from stockhunter.notifications.telegram import TelegramClient, TelegramError

log = logging.getLogger(__name__)
PARIS = ZoneInfo("Europe/Paris")
MAX_ATTEMPTS = 3
def e(x) -> str:
    return html.escape(str(x), quote=False)


def money(x: float) -> str:
    return f"{x:+,.0f}".replace(",", "\u202f")


def amount(x: float) -> str:
    return f"{x:,.0f}".replace(",", "\u202f")


def enqueue(engine: Engine, kind: str, key: str, text: str) -> bool:
    """Ajoute un message s'il n'existe pas déjà. Renvoie True s'il est nouveau."""
    with session_scope(engine) as s:
        if s.scalar(select(NotificationLog.id).where(NotificationLog.dedup_key == key)):
            return False
        upsert(s, NotificationLog, [{"dedup_key": key, "kind": kind, "text": text, "status": "PENDING",
                                     "attempts": 0}], ["dedup_key"], update_cols=[])
        return True


# --------------------------------------------------------------------------- alertes de trading (simulation)
def trade_alert_text(s, ev: PaperTradeEvent) -> str:
    t = s.get(PaperTrade, ev.trade_id)
    inst = s.get(Instrument, t.instrument_id)
    sc = s.scalar(select(Score).where(Score.scan_run_id == t.scan_run_id, Score.instrument_id == t.instrument_id))
    head = {"PLANNED": "OPPORTUNITÉ (plan créé)", "OPENED": "ENTRÉE SIMULÉE", "TP1": "OBJECTIF TP1 ATTEINT",
            "CLOSED": "SORTIE SIMULÉE", "CANCELLED": "PLAN ANNULÉ"}[ev.kind]
    L = [f"<b>STOCKHUNTER AI — {head}</b>", f"Action : <b>{e(inst.symbol)}</b> {e(inst.name or '')}",
         f"Stratégie(s) : {e(t.strategies or '-')} · score {sc.total:.0f}/100" if sc else "", ""]
    if ev.kind == "PLANNED":
        r = t.stop_initial_offset
        L += [f"Entrée conditionnelle : ouverture de demain entre {t.entry_min:.2f} et {t.entry_max:.2f} $",
              "Invalidation : ouverture hors de cette zone",
              f"Stop : entrée − {r:.2f} $ (2×ATR) · TP1 : entrée + {1.5 * r:.2f} $ · TP2 : entrée + {3 * r:.2f} $",
              "Risque : 0,5 % du capital simulé · ratio gain/risque TP2 : 3 R (avant frais)"]
        if sc:
            pos, neg = json.loads(sc.reasons_pos_json)[:3], json.loads(sc.reasons_neg_json)[:2]
            if pos:
                L += ["", "<b>Pourquoi maintenant ?</b>"] + [f"• {e(x)}" for x in pos]
            if neg:
                L += ["<b>Pourquoi ne pas acheter ?</b>"] + [f"• {e(x)}" for x in neg]
            src = json.loads(sc.sources_json)
            links = [f"<a href=\"{e(x['url'])}\">{e(x['type'])}</a>" for x in src if x.get("url")]
            L += ["", f"Données au {sc.as_of.strftime('%d/%m/%Y')} (clôture)" + (" · Sources : " + ", ".join(links) if links else "")]
    else:
        L += [e(ev.detail)]
        if ev.kind == "CLOSED":
            L += [f"Résultat simulé : {money(t.realized_pnl - t.costs)} $ (frais inclus)"]
    L += ["", "<i>Simulation (paper trading) d'une stratégie NON validée. Aucun ordre réel, pas un conseil.</i>"]
    return "\n".join(x for x in L if x is not None)


def enqueue_trade_events(engine: Engine) -> int:
    n = 0
    with session_scope(engine) as s:
        evs = list(s.scalars(select(PaperTradeEvent).where(
            PaperTradeEvent.kind.in_(("PLANNED", "OPENED", "TP1", "CLOSED", "CANCELLED"))).order_by(PaperTradeEvent.id)))
        texts = [(ev.id, trade_alert_text(s, ev)) for ev in evs]
    for ev_id, text in texts:
        n += enqueue(engine, "trade", f"trade-event:{ev_id}", text)
    return n


def enqueue_catalyst_alerts(engine: Engine, lookback_days: int = 2) -> int:
    """Alerte sur un catalyseur important (hors simple publication de résultats) touchant une action READY,
    DEVELOPING ou en position simulée. Une seule alerte par événement (clé de déduplication)."""
    from stockhunter.catalysts.recent import recent_catalysts
    from stockhunter.db.models import PaperTrade, ScanRun

    with session_scope(engine) as s:
        run = s.scalars(select(ScanRun).order_by(ScanRun.as_of.desc(), ScanRun.id.desc()).limit(1)).first()
        if run is None:
            return 0
        watched = dict(s.execute(select(Instrument.symbol, Score.status).join(Score)
                                 .where(Score.scan_run_id == run.id, Score.status.in_(("READY", "DEVELOPING")))).all())
        for sym, st in s.execute(select(Instrument.symbol, PaperTrade.status).join(PaperTrade, PaperTrade.instrument_id == Instrument.id)
                                 .where(PaperTrade.status.in_(("PLANNED", "OPEN")))):
            watched[sym] = f"position simulée {'ouverte' if st == 'OPEN' else 'planifiée'}"
        cats = [c for c in recent_catalysts(s, run.as_of - timedelta(days=lookback_days), run.as_of + timedelta(days=1),
                                            set(watched))
                if not c["earnings_only"] and (c["importance"] >= 3 or c["watch"])]
    n = 0
    for c in cats:
        text = (f"<b>STOCKHUNTER AI — Catalyseur {e(c['symbol'])}</b> ({e(watched[c['symbol']])})\n"
                f"{'⚠️ ' if c['watch'] else ''}{c['date'].strftime('%d/%m/%Y')} · {e(c['kind'])} — {e(c['label'][:300])}\n"
                f"Source : <a href=\"{e(c['url'])}\">{e(c['source'])}</a>\n"
                f"<i>Événement classé par type, pas par effet attendu. Lire la source ; /analyse {e(c['symbol'])} "
                f"pour la synthèse.</i>")
        n += enqueue(engine, "alert", f"catalyst:{c['key']}", text)
    return n


def unresolved_failures(s, now: datetime, hours: int = 24) -> list[JobRun]:
    """Échecs des dernières heures qui n'ont PAS été suivis d'une exécution réussie du même job :
    une panne déjà réparée ne déclenche pas d'alerte."""
    since = now - timedelta(hours=hours)
    out = []
    for j in s.scalars(select(JobRun).where(JobRun.status == "failed", JobRun.started_at >= since)
                       .order_by(JobRun.id.desc())):
        later_ok = s.scalar(select(JobRun.id).where(JobRun.job_name == j.job_name, JobRun.id > j.id,
                                                    JobRun.status.in_(("success", "partial"))).limit(1))
        if not later_ok and all(x.job_name != j.job_name for x in out):
            out.append(j)
    return out


def enqueue_system_alerts(engine: Engine, now: datetime | None = None) -> int:
    """Alerte en cas de job en échec complet non résolu (panne critique)."""
    n = 0
    now = now or datetime.now(timezone.utc)
    with session_scope(engine) as s:
        failed = [(j.id, j.job_name, j.detail) for j in unresolved_failures(s, now)]
    for jid, name, detail in failed:
        n += enqueue(engine, "system", f"job-failed:{jid}",
                     f"<b>STOCKHUNTER AI — ALERTE SYSTÈME</b>\nJob <b>{e(name)}</b> en échec.\n<pre>{e((detail or '')[:800])}</pre>")
    return n


# --------------------------------------------------------------------------- envoi
def client_from(settings: Settings, **kw) -> TelegramClient | None:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return None
    return TelegramClient(settings.telegram_bot_token.get_secret_value(),
                          settings.telegram_chat_id.get_secret_value(), **kw)


def flush(engine: Engine, client: TelegramClient | None, max_messages: int = 15) -> dict[str, int]:
    out = {"envoyes": 0, "echecs": 0, "en_attente": 0, "ignores": 0}
    with session_scope(engine) as s:
        pending = list(s.scalars(select(NotificationLog).where(NotificationLog.status == "PENDING")
                                 .order_by(NotificationLog.id)))
        for n in pending:
            s.expunge(n)
    for i, n in enumerate(pending):
        if client is None:
            status, err = "SKIPPED", "Telegram non configuré"
            out["ignores"] += 1
        elif i >= max_messages:
            out["en_attente"] += 1
            continue  # anti-spam : le reste part au run suivant
        else:
            try:
                client.send(n.text)
                status, err = "SENT", None
                out["envoyes"] += 1
            except TelegramError as exc:
                err = str(exc)
                status = "FAILED" if n.attempts + 1 >= MAX_ATTEMPTS else "PENDING"
                out["echecs"] += 1
        with session_scope(engine) as s:
            row = s.get(NotificationLog, n.id)
            row.status, row.error = status, err
            row.attempts += 0 if status == "SKIPPED" else 1
            if status == "SENT":
                row.sent_at = datetime.now(timezone.utc)
    return out


# --------------------------------------------------------------------------- commandes
HELP = ("<b>Commandes StockHunter</b>\n/today — dernier rapport\n/top — classement du jour\n"
        "/watchlist — actions en veille\n/portfolio — portefeuille simulé\n/analyse SYMBOLE — synthèse sourcée "
        "d'une action (ex. /analyse MA)\n/status — état du système\n\n"
        "<i>Réponse en quelques secondes quand le serveur Railway est actif ; sinon au prochain envoi planifié.</i>")


def answer(engine: Engine, cmd: str) -> str:
    from stockhunter.portfolio import paper
    from stockhunter.reports.daily import build_report

    parts = cmd.strip().split()
    cmd = parts[0].split("@")[0].lower() if parts else ""
    if cmd in ("/analyse", "/analyze", "/a"):
        from stockhunter.analysis.synthesis import build, to_telegram
        if len(parts) < 2:
            return "Usage : /analyse SYMBOLE (ex. /analyse MA)"
        sym = parts[1].upper()[:12]
        syn = build(engine, sym)
        return to_telegram(syn) if syn else f"{e(sym)} ne fait pas partie de l'univers suivi."
    if cmd in ("/today", "/start"):
        return build_report(engine, "evening")[0]
    if cmd in ("/top", "/watchlist"):
        from stockhunter.db.models import ScanRun
        with session_scope(engine) as s:
            run = s.scalars(select(ScanRun).order_by(ScanRun.as_of.desc(), ScanRun.id.desc()).limit(1)).first()
            if run is None:
                return "Aucun scan."
            wanted = ("READY", "DEVELOPING") if cmd == "/top" else ("WATCHLIST",)
            rows = s.execute(select(Score.total, Score.status, Score.close, Instrument.symbol).join(Instrument)
                             .where(Score.scan_run_id == run.id, Score.status.in_(wanted))
                             .order_by(Score.total.desc()).limit(15)).all()
        title = "Classement (READY / DEVELOPING)" if cmd == "/top" else "Veille (WATCHLIST)"
        body = "\n".join(f"• <b>{e(sym)}</b> {tot:.0f}/100 {st} · {c:.2f} $" for tot, st, c, sym in rows) or "Aucune action."
        return f"<b>{title}</b> — clôture du {run.as_of.strftime('%d/%m/%Y')}\n{body}\n\n<i>Informatif : aucune stratégie validée.</i>"
    if cmd == "/portfolio":
        pf = paper.summary(engine)
        lines = [f"• {e(p['symbol'])} {p['shares']:g} @ {p['entry']:.2f} → {p['last'] or 0:.2f} ({p['unrealized']:+.0f} $)"
                 for p in pf["positions"]] or ["Aucune position ouverte."]
        wr = "N/D" if pf["win_rate"] is None else f"{pf['win_rate']:.0%}"
        return (f"<b>Portefeuille simulé</b>\nValeur {amount(pf['equity'])} $ · réalisé {money(pf['realized'])} $ · "
                f"latent {money(pf['unrealized'])} $\nTrades clos : {pf['closed']} · gagnants {wr}\n"
                + "\n".join(lines) + "\n\n<i>Simulation, distincte de tout portefeuille réel.</i>")
    if cmd == "/status":
        with session_scope(engine) as s:
            jobs = s.execute(select(JobRun.job_name, JobRun.status, JobRun.started_at)
                             .order_by(JobRun.id.desc()).limit(8)).all()
        return "<b>État du système</b>\n" + "\n".join(
            f"• {j} : {st} ({(t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(PARIS):%d/%m %H:%M} Paris)"
            for j, st, t in jobs)
    return HELP


def process_commands(engine: Engine, client: TelegramClient | None) -> int:
    if client is None:
        return 0
    with session_scope(engine) as s:
        kv = s.get(KeyValue, "telegram_offset")
        offset = int(kv.value) if kv else None
    try:
        updates = client.updates(offset)
    except TelegramError as exc:
        log.warning("lecture des commandes impossible : %s", exc)
        return 0
    n = 0
    for u in updates:
        msg = u.get("message") or {}
        # sécurité : seules les commandes venant du chat configuré sont traitées
        if str((msg.get("chat") or {}).get("id")) == client.chat_id and (msg.get("text") or "").startswith("/"):
            n += enqueue(engine, "command", f"cmd:{u['update_id']}", answer(engine, msg["text"]))
        with session_scope(engine) as s:
            kv = s.get(KeyValue, "telegram_offset")
            if kv is None:
                s.add(KeyValue(key="telegram_offset", value=str(u["update_id"] + 1)))
            else:
                kv.value = str(u["update_id"] + 1)
    return n


# --------------------------------------------------------------------------- orchestration
def run_notifications(engine: Engine, settings: Settings, kind: str, now: datetime | None = None,
                      client: TelegramClient | None = None, force: bool = False) -> dict:
    """kind = 'evening' (après l'ingestion du soir) ou 'morning' (planifié à 6 h et 7 h UTC : n'agit qu'à
    l'heure voulue à Paris, ce qui gère automatiquement l'heure d'été et d'hiver)."""
    from stockhunter.reports.daily import build_report

    now = now or datetime.now(timezone.utc)
    paris = now.astimezone(PARIS)
    client = client or client_from(settings)
    out: dict = {"rapport": None}
    if kind == "morning" and not force and paris.hour != settings.report_morning_hour_paris:
        out["rapport"] = f"ignoré : il est {paris:%H:%M} à Paris (rapport prévu à {settings.report_morning_hour_paris} h)"
    elif kind == "morning" and paris.weekday() >= 5 and not force:
        out["rapport"] = "ignoré : week-end"
    else:
        text, as_of = build_report(engine, kind, now)
        key = f"report:{kind}:{as_of if kind == 'evening' else paris.date()}"
        out["rapport"] = "mis en file" if enqueue(engine, f"report_{kind}", key, text) else "déjà envoyé (dédupliqué)"
    out["alertes_trading"] = enqueue_trade_events(engine)
    out["alertes_catalyseurs"] = enqueue_catalyst_alerts(engine)
    out["alertes_systeme"] = enqueue_system_alerts(engine, now)
    out["commandes"] = process_commands(engine, client)
    out.update(flush(engine, client, settings.telegram_max_per_run))
    return out


__all__ = ["run_notifications", "enqueue", "flush", "answer", "date"]
