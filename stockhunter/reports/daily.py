"""Rapport quotidien (matin / soir), construit uniquement à partir des données en base : chaque chiffre est
daté, chaque affirmation vient d'une source enregistrée. Aucun LLM ici (synthèse rédigée : phase 6)."""

from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from stockhunter.db.models import (
    DataAnomaly,
    Instrument,
    MacroObservation,
    MacroSeries,
    MarketEvent,
    PaperTrade,
    PaperTradeEvent,
    ScanRun,
    Score,
    StatusChange,
)
from stockhunter.db.session import session_scope
from stockhunter.portfolio import paper
from stockhunter.scoring.engine import fmt_pct

DISCLAIMER = ("<i>Analyse automatisée, pas un conseil en investissement. Aucune stratégie n'est validée : "
              "les signaux sont suivis en simulation (paper trading) uniquement. Score ≠ probabilité.</i>")
def e(x) -> str:
    return html.escape(str(x), quote=False)


def money(x: float) -> str:
    return f"{x:+,.0f}".replace(",", "\u202f")


def amount(x: float) -> str:
    return f"{x:,.0f}".replace(",", "\u202f")


def _macro(s, code: str, as_of: date):
    rows = s.execute(select(MacroObservation.obs_date, MacroObservation.value).join(MacroSeries)
                     .where(MacroSeries.series_code == code, MacroObservation.value.is_not(None),
                            MacroObservation.obs_date <= as_of)
                     .order_by(MacroObservation.obs_date.desc()).limit(400)).all()
    return rows


def _num(x, d=2):
    return f"{x:.{d}f}".replace(".", ",")


def build_report(engine: Engine, kind: str = "evening", now: datetime | None = None) -> tuple[str, date | None]:
    """Renvoie (texte HTML Telegram, date des données). kind : 'morning' ou 'evening'."""
    from stockhunter.report_cli import validation_line

    now = now or datetime.now(timezone.utc)
    with session_scope(engine) as s:
        run = s.scalars(select(ScanRun).order_by(ScanRun.as_of.desc(), ScanRun.id.desc()).limit(1)).first()
        if run is None:
            return "StockHunter : aucun scan disponible pour l'instant.", None
        as_of = run.as_of
        regime, sectors = json.loads(run.regime_json), json.loads(run.sectors_json)
        regime.setdefault("label", "INCONNU")
        vline = validation_line(s, run.strategy_version_id)
        rows = s.execute(select(Score, Instrument.symbol, Instrument.name).join(Instrument)
                         .where(Score.scan_run_id == run.id)).all()
        changes = s.execute(select(StatusChange, Instrument.symbol).join(Instrument)
                            .where(StatusChange.scan_run_id == run.id, StatusChange.from_status.is_not(None))).all()
        macro = {c: _macro(s, c, as_of) for c in ("DGS10", "DGS2", "T10Y2Y", "DFF", "CPIAUCSL", "UNRATE")}
        upcoming = s.execute(select(MarketEvent.event_date, MarketEvent.title, Instrument.symbol).join(Instrument)
                             .where(MarketEvent.event_type == "earnings", MarketEvent.event_date > as_of,
                                    MarketEvent.event_date <= as_of + timedelta(days=10))
                             .order_by(MarketEvent.event_date)).all()
        from stockhunter.catalysts.recent import recent_catalysts
        catalysts = recent_catalysts(s, as_of - timedelta(days=4), as_of + timedelta(days=1))
        events = s.execute(select(PaperTradeEvent, Instrument.symbol).select_from(PaperTradeEvent)
                           .join(PaperTrade, PaperTrade.id == PaperTradeEvent.trade_id)
                           .join(Instrument, Instrument.id == PaperTrade.instrument_id)
                           .where(PaperTradeEvent.event_date >= as_of - timedelta(days=3),
                                  PaperTradeEvent.kind.in_(("OPENED", "TP1", "CLOSED", "CANCELLED", "PLANNED")))
                           .order_by(PaperTradeEvent.id)).all()
        planned_syms = list(s.scalars(select(Instrument.symbol).join(PaperTrade, PaperTrade.instrument_id == Instrument.id)
                                      .where(PaperTrade.scan_run_id == run.id)))
        crit = s.scalar(select(func.count()).select_from(DataAnomaly).where(
            DataAnomaly.resolved_at.is_(None), DataAnomaly.severity == "critical")) or 0
        from stockhunter.notifications.engine import unresolved_failures
        failed = [(j.job_name, j.started_at) for j in unresolved_failures(s, now)]

    pf = paper.summary(engine)
    scores = {sym: (sc, name) for sc, sym, name in rows}
    ranked = sorted((r for r in scores.items() if r[1][0].status in ("READY", "DEVELOPING")),
                    key=lambda r: -(r[1][0].total or 0))
    ready = [k for k, (sc, _) in scores.items() if sc.status == "READY"]
    title = "Rapport du matin" if kind == "morning" else "Rapport après clôture US"
    L = [f"<b>STOCKHUNTER AI — {title}</b>",
         f"Données de clôture du <b>{as_of.strftime('%d/%m/%Y')}</b> (pas de cours en temps réel) · généré "
         f"{now.astimezone(ZoneInfo('Europe/Paris')).strftime('%d/%m %H:%M')} (heure de Paris)", ""]
    from stockhunter.data.calendar import staleness
    st = staleness(as_of, now)
    if st["stale"]:
        L[-1:-1] = [f"⚠️ <b>Données en retard</b> : séance attendue {st['expected'].strftime('%d/%m/%Y')}, "
                    f"dernier scan sur la clôture du {as_of.strftime('%d/%m/%Y')}. Ce rapport n'est pas à jour."]

    # 1. Marché
    L += ["<b>1. État du marché</b>",
          f"Régime : <b>{e(regime['label'])}</b> — " + e(" ; ".join(regime.get("reasons", [])))]
    # 2. Macro
    m = []
    for code, label in (("DFF", "Fed funds"), ("DGS2", "2 ans"), ("DGS10", "10 ans"), ("T10Y2Y", "10a−2a")):
        r = macro[code]
        if r:
            m.append(f"{label} {_num(r[0][1])} % ({r[0][0].strftime('%d/%m')})")
    cpi = macro["CPIAUCSL"]
    if len(cpi) >= 13:
        m.append(f"inflation CPI {_num((cpi[0][1] / cpi[12][1] - 1) * 100, 1)} % sur 1 an ({cpi[0][0].strftime('%m/%Y')})")
    if macro["UNRATE"]:
        m.append(f"chômage {_num(macro['UNRATE'][0][1], 1)} % ({macro['UNRATE'][0][0].strftime('%m/%Y')})")
    L += ["", "<b>2. Macro</b>", e(" · ".join(m)) or "N/D"]
    # 3. Nouvelles documentées
    L += ["", "<b>3. Catalyseurs documentés (SEC 8-K et presse, univers suivi, 4 jours)</b>"]
    L += [f"• {'⚠️ ' if c['watch'] else ''}<b>{e(c['symbol'])}</b> {c['date'].strftime('%d/%m')} · {e(c['kind'])} — "
          f"{e(c['label'][:160])} (<a href=\"{e(c['url'])}\">{e(c['source'])}</a>)" for c in catalysts[:10]] or \
         ["Aucun dépôt ni article important sur les derniers jours."]
    if len(catalysts) > 10:
        L.append(f"… {len(catalysts) - 10} autre(s) sur le dashboard.")
    L.append("<i>Classement par type d'événement (rubriques SEC, mots-clés des titres) : il ne dit pas si "
             "l'effet est positif ou négatif.</i>")
    # 4. Secteurs
    srt = sorted(sectors.items(), key=lambda x: x[1]["rank"])
    if srt:
        L += ["", "<b>4. Secteurs</b> (force relative 1-3 mois vs SPY)",
              "Forts : " + ", ".join(f"{k} {fmt_pct(v['rs_blend'])}" for k, v in srt[:3]),
              "Faibles : " + ", ".join(f"{k} {fmt_pct(v['rs_blend'])}" for k, v in srt[-3:])]
    # 5-7. Opportunités
    L += ["", "<b>5. Classement</b>", f"<i>{e(vline)}</i>"]
    for sym, (sc, name) in ranked[:6]:
        pos = json.loads(sc.reasons_pos_json)[:1]
        neg = json.loads(sc.reasons_neg_json)[:1]
        L.append(f"• <b>{e(sym)}</b> {sc.total:.0f}/100 {sc.status} · clôture {_num(sc.close)} $"
                 + (f"\n   + {e(pos[0])}" if pos else "") + (f"\n   − {e(neg[0])}" if neg else ""))
    if not ranked:
        L.append("Aucune action en DEVELOPING ou READY : pas d'achat justifié selon les règles actuelles.")
    if changes:
        L += ["", "<b>6. Changements depuis le dernier scan</b>"]
        L += [f"• {e(sym)} : {ch.from_status} → {ch.to_status}" for ch, sym in sorted(changes, key=lambda x: x[1])[:10]]
    if not ready:
        sig = "Aucun signal READY."
    elif planned_syms:
        sig = (f"READY : {', '.join(ready)}. Plans de paper trading enregistrés : {', '.join(sorted(planned_syms))} "
               "(entrée conditionnelle à l'ouverture de la séance suivante).")
    else:
        sig = f"READY : {', '.join(ready)}. Plans de paper trading pas encore créés (création après l'ingestion du soir)."
    L += ["", "<b>7. Signaux (simulation)</b>", sig]
    # 8-9. Portefeuille papier
    L += ["", "<b>8. Portefeuille simulé</b>",
          f"Valeur {amount(pf['equity'])} $ (capital {amount(pf['capital'])} $) · réalisé {money(pf['realized'])} $ · "
          f"latent {money(pf['unrealized'])} $ · {pf['open']} position(s), {pf['planned']} plan(s)"]
    for p in pf["positions"]:
        L.append(f"• {e(p['symbol'])} {p['shares']:g} @ {_num(p['entry'])} → {_num(p['last'] or 0)} "
                 f"(stop {_num(p['stop'])}, TP1 {_num(p['tp1'])}{' ✓' if p['tp1_done'] else ''}, TP2 {_num(p['tp2'])})")
    if events:
        L += ["", "<b>9. Événements du portefeuille simulé</b>"]
        L += [f"• {ev.event_date.strftime('%d/%m')} {e(sym)} {ev.kind} : {e(ev.detail)}" for ev, sym in events[-8:]]
    # 10-11. Risques et agenda
    risks = []
    if regime["label"] == "RISK-OFF":
        risks.append("marché en RISK-OFF : aucun nouveau signal READY possible")
    if crit:
        risks.append(f"{crit} anomalie(s) critique(s) dans les données")
    if failed:
        risks.append("job(s) en échec sur 24 h : " + ", ".join(j for j, _ in failed))
    soon = [f"{sym} ({d.strftime('%d/%m')})" for d, _, sym in upcoming if (d - as_of).days <= 5]
    if soon:
        risks.append("résultats imminents : " + ", ".join(soon))
    L += ["", "<b>10. Risques à surveiller</b>", e(" · ".join(risks)) if risks else "Rien de particulier détecté."]
    L += ["", "<b>11. Agenda (résultats annoncés, 10 jours)</b>"]
    L += [f"• {d.strftime('%d/%m')} {e(sym)}" for d, _, sym in upcoming[:10]] or ["Aucun dans l'univers suivi."]
    # 12. Synthèse
    synth = (f"Marché {regime['label'].lower()}, {len(ranked)} action(s) en DEVELOPING/READY sur {len(scores)} suivies. "
             + ("Des signaux READY existent mais restent des tests en simulation : aucune règle n'a d'avantage démontré. "
                if ready else "Aucun signal d'entrée aujourd'hui. ")
             + "Priorité : préserver le capital, observer les résultats du paper trading.")
    L += ["", "<b>12. Synthèse</b>", e(synth), "", DISCLAIMER]
    return "\n".join(L), as_of
