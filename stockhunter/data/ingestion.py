"""Ingestion : univers, prix EOD, fondamentaux SEC, dépôts SEC, macro FRED, calendrier des résultats.

Chaque fonction est idempotente (upsert) et peut être relancée sans créer de doublons.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from stockhunter.data.http import NotFound
from stockhunter.data.providers import (
    FRED_SERIES,
    SOURCE_REGISTRY,
    FinnhubProvider,
    FredProvider,
    SecEdgarProvider,
    TiingoProvider,
    sic_to_sector,
)
from stockhunter.db.models import (
    DataSource,
    Filing,
    FundamentalFact,
    Instrument,
    MacroObservation,
    MacroSeries,
    MarketEvent,
    PriceBar,
)
from stockhunter.db.session import session_scope
from stockhunter.db.upsert import upsert
from stockhunter.jobs.runner import JobContext
from stockhunter.universe import load_universe

log = logging.getLogger(__name__)

# Recouvrement lors des mises à jour incrémentales (corrections tardives de la source)
PRICE_OVERLAP_DAYS = 7


def ensure_sources(engine: Engine) -> dict[str, int]:
    with session_scope(engine) as s:
        for name, meta in SOURCE_REGISTRY.items():
            src = s.scalar(select(DataSource).where(DataSource.name == name))
            if src is None:
                s.add(DataSource(name=name, **meta))
            else:
                for k, v in meta.items():
                    setattr(src, k, v)
        s.flush()
        return {src.name: src.id for src in s.scalars(select(DataSource))}


def _instruments(engine: Engine, symbols: list[str] | None = None, asset_type: str | None = None):
    with session_scope(engine) as s:
        q = select(Instrument).where(Instrument.is_active.is_(True))
        if symbols:
            q = q.where(Instrument.symbol.in_([x.upper() for x in symbols]))
        if asset_type:
            q = q.where(Instrument.asset_type == asset_type)
        rows = list(s.scalars(q.order_by(Instrument.symbol)))
        for r in rows:
            s.expunge(r)
        return rows


# --------------------------------------------------------------------------- univers
def sync_universe(engine: Engine, ctx: JobContext, universe_file: Path | None = None,
                  sec: SecEdgarProvider | None = None) -> None:
    entries = load_universe(universe_file)
    ticker_map = {}
    if sec is not None:
        try:
            ticker_map = sec.fetch_ticker_map()
        except Exception as exc:
            ctx.failure("sec_ticker_map", exc)

    wanted = {e.symbol for e in entries}
    with session_scope(engine) as s:
        for e in entries:
            inst = s.scalar(select(Instrument).where(Instrument.symbol == e.symbol))
            if inst is None:
                inst = Instrument(symbol=e.symbol)
                s.add(inst)
            inst.asset_type, inst.role, inst.is_active = e.asset_type, e.role, True
            if e.sector:
                inst.sector = e.sector
            info = ticker_map.get(e.symbol)
            if info:
                inst.cik = info["cik"]
                inst.name = inst.name or info["name"]
                inst.exchange = info["exchange"]
            elif e.asset_type == "stock" and ticker_map:
                ctx.failure(e.symbol, RuntimeError("ticker introuvable dans la table SEC (CIK inconnu)"))
                continue
            ctx.success()
        # désactive (sans supprimer : historique conservé) les symboles retirés de l'univers
        for inst in s.scalars(select(Instrument).where(Instrument.is_active.is_(True))):
            if inst.symbol not in wanted:
                inst.is_active = False


# --------------------------------------------------------------------------- prix
def ingest_prices(engine: Engine, ctx: JobContext, tiingo: TiingoProvider, history_years: int,
                  symbols: list[str] | None = None, full: bool = False, today: date | None = None) -> None:
    sources = ensure_sources(engine)
    src_id = sources[TiingoProvider.NAME]
    today = today or date.today()

    for inst in _instruments(engine, symbols):
        try:
            if inst.name is None or inst.first_price_date is None:
                try:
                    meta = tiingo.fetch_metadata(inst.symbol)
                    with session_scope(engine) as s:
                        db_inst = s.get(Instrument, inst.id)
                        db_inst.name = db_inst.name or meta.get("name")
                except NotFound:
                    raise RuntimeError("symbole inconnu de Tiingo")

            incremental = (not full) and inst.last_price_date is not None
            start = (inst.last_price_date - timedelta(days=PRICE_OVERLAP_DAYS)) if incremental \
                else today - timedelta(days=365 * history_years)
            bars = tiingo.fetch_prices(inst.symbol, start)

            # Un split ou un dividende dans la fenêtre récente modifie TOUS les prix ajustés passés :
            # on recharge alors l'historique complet pour garder une série ajustée cohérente.
            if incremental and any(b["split_factor"] != 1.0 or b["div_cash"] > 0 for b in bars
                                   if b["bar_date"] > inst.last_price_date):
                log.info("%s : corporate action détectée, rechargement complet", inst.symbol)
                start = today - timedelta(days=365 * history_years)
                bars = tiingo.fetch_prices(inst.symbol, start)

            if not bars:
                raise RuntimeError(f"aucune barre retournée depuis {start}")

            rows = [{**b, "instrument_id": inst.id, "source_id": src_id} for b in bars]
            with session_scope(engine) as s:
                upsert(s, PriceBar, rows, ["instrument_id", "bar_date", "source_id"])
                lo, hi = s.execute(
                    select(func.min(PriceBar.bar_date), func.max(PriceBar.bar_date))
                    .where(PriceBar.instrument_id == inst.id)
                ).one()
                db_inst = s.get(Instrument, inst.id)
                db_inst.first_price_date, db_inst.last_price_date = lo, hi
            ctx.success()
        except Exception as exc:
            ctx.failure(inst.symbol, exc)
            if type(exc).__name__ == "QuotaExceeded":
                log.warning("quota Tiingo atteint : arrêt propre, reprise au prochain run")
                break


# --------------------------------------------------------------------------- fondamentaux SEC
def ingest_fundamentals(engine: Engine, ctx: JobContext, sec: SecEdgarProvider,
                        symbols: list[str] | None = None) -> None:
    sources = ensure_sources(engine)
    src_id = sources[SecEdgarProvider.NAME]
    for inst in _instruments(engine, symbols, asset_type="stock"):
        if not inst.cik:
            ctx.failure(inst.symbol, RuntimeError("CIK absent : lancer sync-universe"))
            continue
        try:
            facts = sec.fetch_companyfacts(inst.cik, inst.symbol)
            subs = sec.fetch_submissions(inst.cik, inst.symbol)
            with session_scope(engine) as s:
                upsert(s, FundamentalFact,
                       [{**f, "instrument_id": inst.id, "source_id": src_id} for f in facts],
                       ["instrument_id", "fact_key"])
                upsert(s, Filing,
                       [{**f, "instrument_id": inst.id, "source_id": src_id} for f in subs.filings],
                       ["accession"])
                db_inst = s.get(Instrument, inst.id)
                db_inst.name = db_inst.name or subs.name
                if subs.sic_description:
                    db_inst.industry = subs.sic_description
                if not db_inst.sector:
                    db_inst.sector = sic_to_sector(subs.sic)
            ctx.success()
        except Exception as exc:
            ctx.failure(inst.symbol, exc)


# --------------------------------------------------------------------------- macro FRED
def ingest_macro(engine: Engine, ctx: JobContext, fred: FredProvider, history_years: int,
                 series: dict[str, str] | None = None, today: date | None = None) -> None:
    sources = ensure_sources(engine)
    src_id = sources[FredProvider.NAME]
    today = today or date.today()
    for code, fallback_name in (series or FRED_SERIES).items():
        try:
            with session_scope(engine) as s:
                ser = s.scalar(select(MacroSeries).where(MacroSeries.series_code == code))
                last = s.scalar(select(func.max(MacroObservation.obs_date))
                                .where(MacroObservation.series_id == ser.id)) if ser else None
            if ser is None:
                info = fred.fetch_series_info(code)
                with session_scope(engine) as s:
                    ser = MacroSeries(series_code=code, name=info["name"] or fallback_name,
                                      frequency=info["frequency"], units=info["units"], source_id=src_id)
                    s.add(ser)
                    s.flush()
                    s.expunge(ser)
            start = (last - timedelta(days=45)) if last else today - timedelta(days=365 * history_years)
            obs = fred.fetch_observations(code, start)
            with session_scope(engine) as s:
                upsert(s, MacroObservation, [{**o, "series_id": ser.id} for o in obs], ["series_id", "obs_date"])
            ctx.success()
        except Exception as exc:
            ctx.failure(code, exc)


# --------------------------------------------------------------------------- calendrier résultats
def ingest_earnings_calendar(engine: Engine, ctx: JobContext, finnhub: FinnhubProvider,
                             days_back: int = 7, days_ahead: int = 45, today: date | None = None) -> None:
    sources = ensure_sources(engine)
    src_id = sources[FinnhubProvider.NAME]
    today = today or date.today()
    tracked = {i.symbol: i.id for i in _instruments(engine, asset_type="stock")}
    try:
        events = finnhub.fetch_earnings_calendar(today - timedelta(days=days_back), today + timedelta(days=days_ahead))
    except Exception as exc:
        ctx.failure("earnings_calendar", exc)
        return
    rows = []
    for e in events:
        iid = tracked.get(e["symbol"])
        if iid is None:
            continue
        raw = e["raw"]
        when = {"bmo": "avant ouverture", "amc": "après clôture"}.get(raw.get("hour"), "horaire non précisé")
        rows.append({
            "instrument_id": iid, "source_id": src_id, "event_type": "earnings",
            "event_date": e["event_date"],
            "title": f"Résultats {e['symbol']} T{raw.get('quarter')} {raw.get('year')} ({when})",
            "payload": json.dumps(raw), "url": None,
            "verified": False,  # calendrier = annonce prévisionnelle, confirmée ensuite par le 8-K
        })
    with session_scope(engine) as s:
        upsert(s, MarketEvent, rows, ["instrument_id", "event_type", "event_date", "source_id"])
    ctx.success(len(rows))
