"""Classement mondial (3.2) : sélections de toutes les places, dédupliquées, normalisées, filtrables.

- Source : dernier scanner de chaque place (États-Unis : moteur de découverte existant ; autres places : scanner
  mondial). Chaque ligne garde la date de SA séance : une clôture de Tokyo n'est pas présentée comme contemporaine
  d'une clôture de New York.
- Normalisation intra-place : `norm` = 1 − (rang − 1) ÷ admis, position relative du titre parmi les titres admis par
  la même catégorie sur la même place ce jour-là (1 = meilleur). Les scores bruts des places ne sont pas comparés.
- Multi-catégories : un titre présent dans plusieurs catégories n'apparaît qu'une fois (liste des catégories).
- Déduplication : même ISIN sur plusieurs places = une société ; on garde le listing principal (priorité de place).
  Un ADR n'est rapproché de l'action locale que par un lien explicite fourni (`links`) : jamais par le nom.
- Classement déterministe : norm décroissant, puis nombre de catégories, confiance, place, symbole.
- Le score n'est PAS une probabilité de gain.
"""

from __future__ import annotations

import json
from datetime import date
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from stockhunter.db.models import (DiscoveryCandidate, DiscoveryRun, MarketTicker, WorldListing, WorldScan,
                                   WorldSignal)
from stockhunter.db.session import session_scope
from stockhunter.discovery.engine import CATEGORIES
from stockhunter.world import markets as mk

TOP_CHOICES = (5, 10, 20, 50, 0)       # 0 = aucune limite (toutes les sélections)
US_EXCHANGE = {"XNYS": "NYSE", "XNAS": "Nasdaq", "XASE": "NYSE American", "ARCX": "NYSE Arca", "BATS": "Cboe BZX"}


@dataclass
class Row:
    key: str                     # ISIN, ou « US:<symbole> » faute d'ISIN dans les données US gratuites
    ticker: str
    name: str | None
    market: str
    exchange: str
    country: str
    region: str
    continent: str
    currency: str
    as_of: str
    close: float
    close_eur: float | None
    close_usd: float | None
    categories: list[str]
    norm: float
    risk_pct: float
    risk: str
    confidence: float
    sector: str | None = None
    tier: str | None = None
    instrument: str = "action"
    also_listed: list[str] = field(default_factory=list)
    rank: int = 0


def risk_label(risk_pct: float, tier: str | None) -> str:
    """Risque lisible : distance du stop (2 ATR) en % du cours, aggravée d'un cran si la liquidité est faible."""
    lvl = 0 if risk_pct <= 0.06 else 1 if risk_pct <= 0.12 else 2
    if tier and tier.startswith("petite"):
        lvl = min(2, lvl + 1)
    return ("modéré", "élevé", "très élevé")[lvl]


def _us_rows(engine: Engine, fx) -> list[Row]:
    m = mk.get("US")
    with session_scope(engine) as s:
        run = s.scalar(select(DiscoveryRun).order_by(DiscoveryRun.as_of.desc(), DiscoveryRun.id.desc()).limit(1))
        if run is None:
            return []
        admitted = {k: v.get("admis", 0) for k, v in json.loads(run.overview_json).get("categories", {}).items()}
        cands = list(s.scalars(select(DiscoveryCandidate).where(DiscoveryCandidate.run_id == run.id)))
        tick = {t for c in cands for t in [c.ticker]}
        ref = {t: (ty, ex) for t, ty, ex in s.execute(select(MarketTicker.ticker, MarketTicker.type,
                                                             MarketTicker.primary_exchange)
                                                      .where(MarketTicker.ticker.in_(tick), MarketTicker.active.is_(True)))}
        as_of = run.as_of
    rows = []
    for c in cands:
        card = json.loads(c.card_json)
        ty, ex = ref.get(c.ticker, (None, None))
        eur = usd = None
        if fx is not None:
            try:
                eur, usd = c.close * fx.rate("USD", "EUR", as_of), c.close
            except KeyError:
                eur, usd = None, c.close
        adm = max(admitted.get(c.category, 0) or 0, c.rank)
        rows.append(Row(key=f"US:{c.ticker}", ticker=c.ticker, name=c.name, market="US",
                        exchange=US_EXCHANGE.get(ex or "", ex or "US"), country="US", region=m.region,
                        continent=m.continent, currency="USD", as_of=str(as_of), close=c.close, close_eur=eur,
                        close_usd=usd, categories=[c.category], norm=round(1 - (c.rank - 1) / max(adm, 1), 4),
                        risk_pct=card["plan"]["risque_pct"], risk=risk_label(card["plan"]["risque_pct"], card.get("tier")),
                        confidence=c.confidence, tier=card.get("tier"),
                        instrument="ADR" if ty == "ADRC" else "action"))
    return rows


def _world_rows(engine: Engine) -> list[Row]:
    rows = []
    with session_scope(engine) as s:
        latest = dict(s.execute(select(WorldScan.market, func.max(WorldScan.as_of)).group_by(WorldScan.market)).all())
        sectors = {}
        for market, as_of in latest.items():
            m = mk.get(market)
            sigs = list(s.scalars(select(WorldSignal).join(WorldScan, WorldScan.id == WorldSignal.scan_id)
                                  .where(WorldScan.market == market, WorldScan.as_of == as_of)))
            if sigs and not sectors:
                sectors = dict(s.execute(select(WorldListing.isin, WorldListing.sector)).all())
            for g in sigs:
                card = json.loads(g.card_json)
                rows.append(Row(key=g.isin, ticker=g.ticker, name=g.name, market=market, exchange=m.name,
                                country=m.country, region=m.region, continent=m.continent, currency=g.currency,
                                as_of=str(g.as_of), close=g.close, close_eur=g.close_eur, close_usd=g.close_usd,
                                categories=[g.category], norm=g.norm_score, risk_pct=g.risk_pct,
                                risk=risk_label(g.risk_pct, card.get("tier")), confidence=g.confidence,
                                sector=sectors.get(g.isin), tier=card.get("tier")))
    return rows


def merge(rows: list[Row], links: dict[str, str] | None = None) -> list[Row]:
    """Fusionne catégories d'un même listing, puis listings d'une même société (même ISIN, ou lien explicite
    ADR -> ISIN local dans `links`). Le listing gardé est celui de la place prioritaire."""
    links = links or {}
    by_listing: dict[tuple[str, str], Row] = {}
    for r in rows:
        k = (r.market, r.key)
        if k in by_listing:
            x = by_listing[k]
            x.categories = sorted(set(x.categories) | set(r.categories), key=list(CATEGORIES).index)
            x.norm = max(x.norm, r.norm)
            x.confidence = max(x.confidence, r.confidence)
        else:
            by_listing[k] = Row(**{**asdict(r), "categories": list(r.categories), "also_listed": []})
    by_company: dict[str, Row] = {}
    for r in by_listing.values():
        company = links.get(r.key, r.key)
        if company not in by_company:
            by_company[company] = r
            continue
        a, b = by_company[company], r
        keep, other = (a, b) if mk.get(a.market).priority <= mk.get(b.market).priority else (b, a)
        keep.also_listed = sorted(set(keep.also_listed) | {f"{other.ticker} ({other.market})"} | set(other.also_listed))
        keep.categories = sorted(set(keep.categories) | set(other.categories), key=list(CATEGORIES).index)
        keep.norm = max(keep.norm, other.norm)
        by_company[company] = keep
    return list(by_company.values())


def order(rows: list[Row]) -> list[Row]:
    out = sorted(rows, key=lambda r: (-r.norm, -len(r.categories), -r.confidence, r.market, r.ticker))
    for i, r in enumerate(out, 1):
        r.rank = i
    return out


def apply_filters(rows: list[Row], continent: str | None = None, region: str | None = None, country: str | None = None,
                  market: str | None = None, currency: str | None = None, sector: str | None = None,
                  tier: str | None = None, category: str | None = None) -> list[Row]:
    def ok(r: Row) -> bool:
        return ((not continent or r.continent == continent) and (not region or r.region == region)
                and (not country or r.country == country.upper()) and (not market or r.market == market)
                and (not currency or r.currency == currency.upper()) and (not sector or (r.sector or "N/D") == sector)
                and (not tier or (r.tier or "").startswith(tier)) and (not category or category in r.categories))
    return [r for r in rows if ok(r)]


def global_ranking(engine: Engine, fx=None, top: int | None = 20, links: dict[str, str] | None = None,
                   now=None, max_lag: int = 1, **filters) -> dict[str, Any]:
    raw = _us_rows(engine, fx) + _world_rows(engine)
    # 3.2.1 : une place dont la dernière sélection a plus d'une séance de retard n'entre pas dans le classement
    stale, fresh_rows = {}, []
    for r in raw:
        if r.market not in stale:
            try:
                f = mk.freshness(mk.get(r.market), date.fromisoformat(r.as_of), now)
                stale[r.market] = f.detail if (f.lag or 0) > max_lag else None
            except Exception as exc:
                stale[r.market] = f"fraîcheur inconnue : {exc}"
        if not stale[r.market]:
            fresh_rows.append(r)
    rows = order(merge(fresh_rows, links))
    filtered = apply_filters(rows, **filters)
    shown = filtered[:top] if top else filtered
    return {"total": len(rows), "apres_filtres": len(filtered), "lignes": shown,
            "seances": sorted({(r.market, r.as_of) for r in rows}),
            "exclues_perimees": {k: v for k, v in stale.items() if v},
            "avertissement": "Le score est une position relative dans la place et la catégorie, pas une probabilité "
                             "de gain. Cours = clôtures de la séance indiquée, jamais des cours en direct."}
