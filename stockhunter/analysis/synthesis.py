"""Synthèse écrite et sourcée d'une action (phase 6).

Texte produit par du code déterministe, sans LLM : chaque phrase vient d'une donnée datée de la base et
porte sa source (lien SEC, article, fournisseur de cours). Rien n'est extrapolé.

Point-in-time : la synthèse au jour J n'utilise que le scan de J (ou le dernier avant), les dépôts SEC
déposés au plus tard à J et les articles publiés avant la fin de J (heure de New York).

Famille E « Catalyst » : signal INFORMATIF, hors score, non validé. Il signale un événement important hors résultats
récent (8-K de forte importance ou article de forte importance) ; il ne change ni le score ni le statut.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from stockhunter.catalysts.classify import NEWS_LABELS, classify_8k
from stockhunter.db.models import (
    DataAnomaly,
    Filing,
    Instrument,
    MarketEvent,
    NewsItem,
    PaperTrade,
    ScanRun,
    Score,
)
from stockhunter.db.session import session_scope

NY = ZoneInfo("America/New_York")
CATALYST_DAYS = 30          # fenêtre des catalyseurs affichés
E_FILING_DAYS = 14          # famille E : 8-K important de moins de 14 jours
E_NEWS_DAYS = 7             # famille E : article important de moins de 7 jours
MAX_NEWS = 8
E_EXCLUDED_ITEMS = {"2.02"}           # résultats : couverts par la famille B
E_NEWS_CATEGORIES = {"legal", "regulatory", "mna", "guidance"}


@dataclass
class Source:
    label: str
    url: str | None = None
    when: str | None = None


@dataclass
class Line:
    text: str
    sources: list[Source] = field(default_factory=list)


@dataclass
class Synthesis:
    symbol: str
    name: str | None
    as_of: date | None
    header: list[Line]
    sections: list[tuple[str, list[Line]]]
    family_e: dict

    def all_sources(self) -> list[Source]:
        seen, out = set(), []
        for _t, lines in [("", self.header)] + self.sections:
            for ln in lines:
                for s in ln.sources:
                    k = (s.label, s.url)
                    if k not in seen:
                        seen.add(k)
                        out.append(s)
        return out


def _pct(x, d=1, signed=True) -> str:
    if x is None:
        return "N/D"
    return (f"{x * 100:+.{d}f}" if signed else f"{x * 100:.{d}f}").replace(".", ",") + " %"


def _fd(d) -> str:
    return d.strftime("%d/%m/%Y") if d else "N/D"


def _end_of_day_utc(d: date) -> datetime:
    return datetime.combine(d + timedelta(days=1), time(0), NY).astimezone(timezone.utc)


def build(engine: Engine, symbol: str, as_of: date | None = None) -> Synthesis | None:
    with session_scope(engine) as s:
        inst = s.scalar(select(Instrument).where(Instrument.symbol == symbol.upper()))
        if inst is None:
            return None
        q = select(Score, ScanRun).join(ScanRun).where(Score.instrument_id == inst.id)
        if as_of:
            q = q.where(ScanRun.as_of <= as_of)
        row = s.execute(q.order_by(ScanRun.as_of.desc(), ScanRun.id.desc()).limit(1)).first()
        sc, run = row if row else (None, None)
        day = as_of or (run.as_of if run else date.today())
        cutoff = _end_of_day_utc(day)

        filings = list(s.scalars(select(Filing).where(
            Filing.instrument_id == inst.id, Filing.form.in_(("8-K", "8-K/A")),
            Filing.filed_date <= day, Filing.filed_date > day - timedelta(days=CATALYST_DAYS))
            .order_by(Filing.filed_date.desc())))
        news = list(s.scalars(select(NewsItem).where(
            NewsItem.instrument_id == inst.id, NewsItem.published_at < cutoff,
            NewsItem.published_at >= cutoff - timedelta(days=CATALYST_DAYS)).order_by(NewsItem.published_at.desc())))
        upcoming = list(s.scalars(select(MarketEvent).where(
            MarketEvent.instrument_id == inst.id, MarketEvent.event_type == "earnings",
            MarketEvent.event_date > day).order_by(MarketEvent.event_date).limit(1)))
        trades = list(s.scalars(select(PaperTrade).where(
            PaperTrade.instrument_id == inst.id, PaperTrade.status.in_(("PLANNED", "OPEN")))))
        anomalies = list(s.scalars(select(DataAnomaly).where(
            DataAnomaly.instrument_id == inst.id, DataAnomaly.resolved_at.is_(None),
            DataAnomaly.severity.in_(("warning", "critical"))).order_by(DataAnomaly.id.desc()).limit(3)))
        n_total = s.scalar(select(func.count()).select_from(Score).where(Score.scan_run_id == run.id)) if run else 0
        return _compose(inst, sc, run, day, filings, news, upcoming, trades, anomalies, n_total)


def _compose(inst, sc, run, day, filings, news, upcoming, trades, anomalies, n_total) -> Synthesis:
    sym = inst.symbol
    metrics = json.loads(sc.metrics_json) if sc else {}
    srcs = json.loads(sc.sources_json) if sc else []
    price_src = Source("Tiingo EOD", None, f"clôture du {_fd(sc.price_date)}") if sc and sc.price_date else None
    fund_src = next((Source(f"SEC EDGAR — {x['detail']}", x.get("url")) for x in srcs if x["type"] == "fondamentaux"), None)
    res_src = next((Source(f"SEC EDGAR — 8-K résultats {x['detail']}", x.get("url")) for x in srcs
                    if x["type"] == "résultats"), None)
    scan_src = Source("Scan StockHunter", None, f"{_fd(run.as_of)} (version {run.strategy_version_id})") if run else None

    # ------------------------------------------------------------------ catalyseurs classés
    filing_lines, watch_lines, e_hits = [], [], []
    for f in filings:
        cats = [c for c in classify_8k(f.items) if c.importance >= 2] or classify_8k(f.items)[:1]
        if not cats:
            continue
        label = " ; ".join(c.label for c in cats)
        src = Source(f"SEC EDGAR — {f.form} déposé le {_fd(f.filed_date)}", f.url)
        filing_lines.append(Line(f"{_fd(f.filed_date)} · {f.form} : {label} (rubriques {f.items or 'N/D'}).", [src]))
        for c in cats:
            if c.watch:
                watch_lines.append(Line(f"Le {f.form} du {_fd(f.filed_date)} déclare : {c.label.lower()} "
                                        f"(rubrique {c.item}) — à lire avant toute décision.", [src]))
            # E = catalyseur hors résultats (les résultats relèvent de la famille B)
            if c.importance >= 3 and c.item not in E_EXCLUDED_ITEMS and (day - f.filed_date).days < E_FILING_DAYS:
                e_hits.append(f"{f.form} du {_fd(f.filed_date)} : {c.label.lower()}")

    ranked = sorted(news, key=lambda n: (-NEWS_LABELS.get(n.category, ("", 1))[1], -n.published_at.timestamp()))
    news_lines = []
    for n in ranked[:MAX_NEWS]:
        lab, imp = NEWS_LABELS.get(n.category, ("Autre", 1))
        pub = n.published_at if n.published_at.tzinfo else n.published_at.replace(tzinfo=timezone.utc)
        src = Source(f"{n.publisher or 'éditeur N/D'} via Finnhub", n.url, _fd(pub.astimezone(NY).date()))
        news_lines.append(Line(f"{_fd(pub.astimezone(NY).date())} · [{lab}] {n.headline}", [src]))
        if n.category in E_NEWS_CATEGORIES and (_end_of_day_utc(day) - pub) < timedelta(days=E_NEWS_DAYS):
            e_hits.append(f"article « {lab.lower()} » du {_fd(pub.astimezone(NY).date())}")
            if n.category in ("legal", "regulatory"):
                watch_lines.append(Line(f"Article {lab.lower()} récent : « {n.headline} » — vérifier la source.", [src]))
    family_e = {"label": "Catalyst (informatif)", "active": bool(e_hits), "hits": e_hits,
                "note": "hors score, non validé : signale un événement important récent, sans en juger l'effet"}

    # ------------------------------------------------------------------ en-tête
    header = []
    if sc:
        rank = f", rang {sc.rank}/{n_total}" if sc.rank else ""
        header.append(Line(f"{sym} est {sc.status} au scan du {_fd(run.as_of)} : score {sc.total:.0f}/100{rank}, "
                           f"confiance des données {round(sc.confidence * 100)} %. Le score classe les actions suivies "
                           f"entre elles ; ce n'est pas une probabilité de hausse.", [scan_src]))
        if sc.close is not None and price_src:
            header.append(Line(f"Dernière clôture connue : {_num(sc.close, 2)} $ ({_fd(sc.price_date)}, cours de clôture, "
                               f"pas un cours en direct).", [price_src]))
        header.append(Line(f"Statut expliqué : {sc.status_reason}", [scan_src]))
    else:
        header.append(Line(f"{sym} n'a pas encore été analysée par un scan : synthèse limitée aux événements.", []))
    header.append(Line("Validation historique : aucune stratégie validée à ce jour (docs/validation.md). "
                       "Cette synthèse décrit une situation, ce n'est pas une recommandation.", []))

    sections: list[tuple[str, list[Line]]] = []
    if sc:
        pos = [Line(r, [x for x in (scan_src,) if x]) for r in json.loads(sc.reasons_pos_json)]
        sections.append(("Points forts relevés par le scan", pos or [Line("Aucun point fort identifié par le scan.", [])]))
    neg = ([Line(r, [scan_src]) for r in json.loads(sc.reasons_neg_json)] if sc else []) + watch_lines
    neg += [Line(f"Qualité des données ({a.severity}) : {a.detail}", [Source("Contrôle qualité StockHunter", None,
                                                                          _fd(a.detected_at.date()))])
            for a in anomalies]
    sections.append(("Points de vigilance", neg or [Line("Aucun point de vigilance relevé.", [])]))

    cat = filing_lines + news_lines
    if not news:
        cat.append(Line("Aucun article collecté sur la période (collecte des actualités active depuis le 13/11/2026).", []))
    sections.append((f"Catalyseurs des {CATALYST_DAYS} derniers jours", cat or [Line("Aucun dépôt 8-K sur la période.", [])]))

    nxt = []
    for ev in upcoming:
        nxt.append(Line(f"{ev.title} prévus le {_fd(ev.event_date)} (calendrier prévisionnel, confirmé ensuite par le 8-K).",
                        [Source("Finnhub — calendrier des résultats", None, f"collecté le {_fd(ev.fetched_at.date())}")]))
    sections.append(("Prochain événement connu", nxt or [Line("Aucune date de résultats connue dans les 45 jours.", [])]))

    if metrics:
        f = [Line(f"Trimestre clos le {metrics.get('last_quarter_end') or 'N/D'} : chiffre d'affaires "
                  f"{_pct(metrics.get('revenue_yoy_q'))} sur un an, bénéfice par action {_pct(metrics.get('eps_yoy_q'))}, "
                  f"marge opérationnelle (12 mois) {_pct(metrics.get('op_margin_ttm'), signed=False)}.",
                  [x for x in (fund_src,) if x]),
             Line(f"Valorisation à la clôture : PER {_num(metrics.get('pe_ttm'))}, cours / ventes "
                  f"{_num(metrics.get('ps_ttm'))}, rendement du cash-flow libre {_pct(metrics.get('fcf_yield'), signed=False)}.",
                  [x for x in (fund_src, price_src) if x])]
        if res_src:
            f.append(Line("Derniers résultats publiés : voir le communiqué (8-K rubrique 2.02).", [res_src]))
        sections.append(("Fondamentaux clés", f))

    if sc:
        fam = json.loads(sc.strategies_json)
        lines = []
        for k, v in fam.items():
            if k == "E":
                continue
            lab = v.get("label", "")
            if v.get("active"):
                lines.append(Line(f"{k} {lab} : actif.", [scan_src]))
            elif v.get("implemented") and v.get("missing"):
                lines.append(Line(f"{k} {lab} : inactif (manque : {', '.join(v['missing'])}).", [scan_src]))
        lines.append(Line(f"E {family_e['label']} : {'actif — ' + '; '.join(e_hits) if e_hits else 'inactif'} "
                          f"({family_e['note']}).", []))
        sections.append(("Familles de signaux", lines))

    plan_lines = []
    for t in trades:
        if t.status == "OPEN":
            plan_lines.append(Line(f"Position simulée ouverte le {_fd(t.entry_date)} à {_num(t.entry_price, 2)} $ ; stop "
                                   f"{_num(t.stop, 2)} $, objectif 1 {_num(t.tp1, 2)} $, objectif 2 {_num(t.tp2, 2)} $.",
                                   [Source("Paper trading StockHunter (simulation)")]))
        else:
            plan_lines.append(Line(f"Plan simulé du {_fd(t.signal_date)} : entrée à l'ouverture suivante entre "
                                   f"{_num(t.entry_min, 2)} et {_num(t.entry_max, 2)} $, stop à {_num(t.stop_initial_offset, 2)} $ "
                                   f"sous l'entrée (2 × ATR).", [Source("Paper trading StockHunter (simulation)")]))
    if plan_lines:
        sections.append(("Plan simulé (paper trading, sans argent réel)", plan_lines))

    inval = []
    if metrics.get("sma50"):
        inval.append(Line(f"Clôture sous la moyenne 50 jours ({_num(metrics['sma50'], 2)} $) : la tendance de court terme "
                          f"serait rompue.", [x for x in (price_src,) if x]))
    if metrics.get("ext_sma50") is not None and metrics["ext_sma50"] > 0.08:
        inval.append(Line(f"Extension déjà de {_pct(metrics['ext_sma50'])} au-dessus de la MM50 : au-delà de +12 %, "
                          f"le statut READY n'est plus accordé.", [x for x in (price_src,) if x]))
    for ev in upcoming:
        if (ev.event_date - day).days <= 10:
            inval.append(Line(f"Résultats attendus le {_fd(ev.event_date)} : risque d'écart de cours important.", []))
    inval.append(Line("Passage du marché en RISK-OFF (règles de régime) : plus aucun READY n'est émis.", [scan_src]))
    sections.append(("Ce qui invaliderait la configuration", inval))

    return Synthesis(sym, inst.name, run.as_of if run else day, header, sections, family_e)


def _num(x, d=1):
    if x is None:
        return "N/D"
    return f"{x:.{d}f}".replace(".", ",")


# ====================================================================== rendus
def to_telegram(syn: Synthesis, max_lines_per_section: int = 5) -> str:
    """HTML Telegram : texte + liens courts. Les sources sans lien sont citées en clair."""
    import html

    def e(x):
        return html.escape(str(x), quote=False)

    def src(ln: Line) -> str:
        parts = []
        for s in ln.sources:
            lab = e(s.label + (f", {s.when}" if s.when else ""))
            parts.append(f'<a href="{e(s.url)}">{lab}</a>' if s.url else lab)
        return f" <i>[{' ; '.join(parts)}]</i>" if parts else ""

    out = [f"<b>Analyse {e(syn.symbol)}</b>" + (f" — {e(syn.name)}" if syn.name else "")]
    out += [e(ln.text) + src(ln) for ln in syn.header]
    for title, lines in syn.sections:
        out.append(f"\n<b>{e(title)}</b>")
        out += [f"• {e(ln.text)}{src(ln)}" for ln in lines[:max_lines_per_section]]
        if len(lines) > max_lines_per_section:
            out.append(f"• … {len(lines) - max_lines_per_section} autre(s) élément(s) sur le dashboard")
    return "\n".join(out)


def to_markdown(syn: Synthesis) -> str:
    def src(ln: Line) -> str:
        parts = [f"[{s.label}{', ' + s.when if s.when else ''}]({s.url})" if s.url else f"{s.label}"
                 + (f", {s.when}" if s.when else "") for s in ln.sources]
        return f" _({'; '.join(parts)})_" if parts else ""

    out = [f"# Analyse {syn.symbol}" + (f" — {syn.name}" if syn.name else ""), ""]
    out += [ln.text + src(ln) for ln in syn.header]
    for title, lines in syn.sections:
        out += ["", f"## {title}"] + [f"- {ln.text}{src(ln)}" for ln in lines]
    return "\n".join(out) + "\n"
