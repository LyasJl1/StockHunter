"""Scanner d'opportunités : charge les données connues à la date d'analyse, calcule les scores, les
enregistre (insertion seule, jamais de mise à jour) et trace les changements de statut.

Point-in-time :
- prix : barres dont bar_date <= as_of ;
- fondamentaux : faits déposés au plus tard à as_of ;
- dépôts 8-K : filed_date <= as_of ;
- calendrier des résultats : il n'est daté que de sa collecte. Pour un scan dans le passé, il n'est pas
  fiable et n'est donc pas utilisé (signalé dans les notes du scan).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from stockhunter.analytics.fundamentals import PERIODIC_FORMS, fundamental_snapshot, valuation
from stockhunter.analytics.technical import bars_frame, technical_snapshot, window_return
from stockhunter.data.calendar import sessions_between, trading_days
from stockhunter.db.models import (
    Filing,
    FundamentalFact,
    Instrument,
    MacroObservation,
    MacroSeries,
    MarketEvent,
    PriceBar,
    ScanRun,
    Score,
    StatusChange,
    StrategyVersion,
)
from stockhunter.db.session import session_scope
from stockhunter.scoring.config import DEFAULT_CONFIG, ScoringConfig
from stockhunter.scoring.engine import Context, StockInput, market_regime, rank_sectors, score_universe

log = logging.getLogger(__name__)
BARS_NEEDED = 320
BENCHMARK = "SPY"


class ScanError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- chargement
def _bars(s, instrument_id: int, as_of: date) -> pd.DataFrame:
    rows = s.execute(
        select(PriceBar.bar_date, PriceBar.open, PriceBar.close, PriceBar.volume, PriceBar.adj_open,
               PriceBar.adj_high, PriceBar.adj_low, PriceBar.adj_close, PriceBar.adj_volume)
        .where(PriceBar.instrument_id == instrument_id, PriceBar.bar_date <= as_of)
        .order_by(PriceBar.bar_date.desc()).limit(BARS_NEEDED)
    ).mappings().all()
    return bars_frame([dict(r) for r in rows])


def _latest_macro(s, code: str, as_of: date) -> tuple[float, date] | None:
    row = s.execute(
        select(MacroObservation.value, MacroObservation.obs_date).join(MacroSeries)
        .where(MacroSeries.series_code == code, MacroObservation.obs_date <= as_of,
               MacroObservation.value.is_not(None))
        .order_by(MacroObservation.obs_date.desc()).limit(1)
    ).first()
    return (row[0], row[1]) if row else None


def _earnings_event(s, inst_id: int, df: pd.DataFrame, spy: pd.DataFrame, as_of: date,
                    max_sessions: int) -> dict[str, Any] | None:
    """Dernier 8-K item 2.02 (résultats) et réaction du marché, mesurée sur données de clôture.

    Fenêtre de réaction : de la dernière clôture AVANT le jour de dépôt jusqu'à la clôture de la séance
    qui suit le dépôt (couvre un dépôt avant ouverture comme après clôture)."""
    rows = s.execute(
        select(Filing.filed_date, Filing.url, Filing.items).where(
            Filing.instrument_id == inst_id, Filing.form.in_(("8-K", "8-K/A")),
            Filing.filed_date <= as_of).order_by(Filing.filed_date.desc()).limit(40)
    ).all()
    ev = next((r for r in rows if r.items and "2.02" in r.items.split(",")), None)
    if ev is None:
        return None
    ago = sessions_between(ev.filed_date, as_of)
    if ago > max_sessions:
        return None
    sessions = trading_days(pd.Timestamp(ev.filed_date) + pd.Timedelta(days=1), pd.Timestamp(as_of))
    if len(sessions) == 0:
        return {"filed_date": ev.filed_date.isoformat(), "url": ev.url, "sessions_ago": ago,
                "reaction_excess": None, "pending": "réaction pas encore observable"}
    end = sessions[0]
    start = pd.Timestamp(ev.filed_date)
    r, b = window_return(df, start, end), window_return(spy, start, end)
    out = {"filed_date": ev.filed_date.isoformat(), "url": ev.url, "sessions_ago": ago,
           "reaction_abs": r, "reaction_excess": (r - b) if r is not None and b is not None else None}
    dr = window_return(df, end + pd.Timedelta(days=1), pd.Timestamp(as_of))
    db = window_return(spy, end + pd.Timedelta(days=1), pd.Timestamp(as_of))
    out["drift_excess"] = (dr - db) if dr is not None and db is not None and len(sessions) > 1 else None
    return out


def build_inputs(engine: Engine, as_of: date | None = None, cfg: ScoringConfig = DEFAULT_CONFIG):
    with session_scope(engine) as s:
        insts = list(s.scalars(select(Instrument).where(Instrument.is_active.is_(True)).order_by(Instrument.symbol)))
        bench = next((i for i in insts if i.symbol == BENCHMARK), None)
        if bench is None:
            raise ScanError(f"benchmark {BENCHMARK} absent de l'univers")
        if as_of is None:
            as_of = s.scalar(select(func.max(PriceBar.bar_date)).where(PriceBar.instrument_id == bench.id))
            if as_of is None:
                raise ScanError("aucun prix pour SPY : lancer l'ingestion")
        spy = _bars(s, bench.id, as_of)
        if spy.empty or spy.index[-1].date() != as_of:
            raise ScanError(f"pas de clôture SPY au {as_of} : date d'analyse invalide (jour non ouvré ?)")

        frames = {i.symbol: _bars(s, i.id, as_of) for i in insts}
        tech = {}
        for i in insts:
            sec = frames.get(i.sector_etf) if i.sector_etf else None
            tech[i.symbol] = technical_snapshot(frames[i.symbol], spy, sec)

        stocks = [i for i in insts if i.asset_type == "stock"]
        above = [tech[i.symbol].get("above_sma50") for i in stocks if tech[i.symbol].get("above_sma50") is not None]
        breadth = sum(above) / len(above) if above else None
        regime = market_regime(tech[BENCHMARK], _latest_macro(s, "VIXCLS", as_of), breadth,
                               _latest_macro(s, "T10Y2Y", as_of))
        sectors = rank_sectors({i.symbol: tech[i.symbol] for i in insts if i.role == "sector_etf"
                                and i.symbol != "SMH" and not tech[i.symbol].get("insufficient_history")})

        latest_session = s.scalar(select(func.max(PriceBar.bar_date)))
        calendar_reliable = as_of >= latest_session
        inputs: list[StockInput] = []
        for i in insts:
            t = tech[i.symbol]
            stale = t.get("price_date") is not None and date.fromisoformat(t["price_date"]) < as_of
            src = [{"type": "prix", "source": "Tiingo EOD", "detail": f"clôture du {t.get('price_date')}"}]
            si = StockInput(symbol=i.symbol, instrument_id=i.id, asset_type=i.asset_type, tech=t,
                            sector_etf=i.sector_etf, industry_etf=i.industry_etf, price_stale=stale, sources=src)
            if i.asset_type == "stock":
                facts = [dict(r) for r in s.execute(
                    select(FundamentalFact.concept, FundamentalFact.period_start, FundamentalFact.period_end,
                           FundamentalFact.form, FundamentalFact.filed_date, FundamentalFact.value)
                    .where(FundamentalFact.instrument_id == i.id, FundamentalFact.filed_date <= as_of)
                ).mappings()]
                si.fund = fundamental_snapshot(facts, as_of)
                si.val = valuation(si.fund, t.get("close"))
                if si.fund.get("last_filed"):
                    lf = date.fromisoformat(si.fund["last_filed"])
                    si.fund_lag = [{"form": r.form, "filed_date": r.filed_date.isoformat(), "url": r.url}
                                   for r in s.execute(select(Filing.form, Filing.filed_date, Filing.url).where(
                                       Filing.instrument_id == i.id, Filing.form.in_(tuple(PERIODIC_FORMS)),
                                       Filing.filed_date > lf, Filing.filed_date <= as_of))]
                    last = s.execute(select(Filing.form, Filing.filed_date, Filing.url).where(
                        Filing.instrument_id == i.id, Filing.form.in_(tuple(PERIODIC_FORMS)),
                        Filing.filed_date <= lf).order_by(Filing.filed_date.desc()).limit(1)).first()
                    if last:
                        src.append({"type": "fondamentaux", "source": "SEC EDGAR XBRL",
                                    "detail": f"{last.form} déposé le {last.filed_date}", "url": last.url})
                if not frames[i.symbol].empty:
                    si.earnings = _earnings_event(s, i.id, frames[i.symbol], spy, as_of,
                                                  cfg.thresholds.earnings_event_max_sessions)
                    if si.earnings:
                        src.append({"type": "résultats", "source": "SEC EDGAR 8-K item 2.02",
                                    "detail": f"déposé le {si.earnings['filed_date']}", "url": si.earnings["url"]})
                if calendar_reliable:
                    ev = s.execute(select(MarketEvent.event_date, MarketEvent.title).where(
                        MarketEvent.instrument_id == i.id, MarketEvent.event_type == "earnings",
                        MarketEvent.event_date > as_of).order_by(MarketEvent.event_date).limit(1)).first()
                    if ev:
                        si.upcoming = {"date": ev.event_date.isoformat(), "title": ev.title,
                                       "sessions_until": sessions_between(as_of, ev.event_date)}
                        src.append({"type": "calendrier", "source": "Finnhub (annonce non vérifiée)",
                                    "detail": ev.title})
            inputs.append(si)
    notes = [] if calendar_reliable else ["calendrier des résultats non utilisé (scan dans le passé)"]
    return as_of, inputs, Context(as_of=as_of, regime=regime, sectors=sectors), notes


# ----------------------------------------------------------------------------- persistance
def _strategy_version(s, cfg: ScoringConfig) -> StrategyVersion:
    sv = s.scalar(select(StrategyVersion).where(StrategyVersion.name == cfg.name,
                                                StrategyVersion.version == cfg.version))
    if sv is None:
        sv = StrategyVersion(name=cfg.name, version=cfg.version, stage=cfg.stage,
                             params_json=cfg.params_json(), params_hash=cfg.params_hash())
        s.add(sv)
        s.flush()
    elif sv.params_hash != cfg.params_hash():
        raise ScanError(f"les paramètres de {cfg.name} {cfg.version} ont changé : incrémenter SCORING_VERSION")
    return sv


def existing_scan(engine: Engine, as_of: date, cfg: ScoringConfig = DEFAULT_CONFIG) -> int | None:
    with session_scope(engine) as s:
        return s.scalar(select(ScanRun.id).join(StrategyVersion).where(
            ScanRun.as_of == as_of, StrategyVersion.name == cfg.name, StrategyVersion.version == cfg.version)
            .order_by(ScanRun.id.desc()).limit(1))


def stale_instruments(engine: Engine, as_of: date) -> list[str]:
    with session_scope(engine) as s:
        return list(s.scalars(select(Instrument.symbol).where(
            Instrument.is_active.is_(True),
            (Instrument.last_price_date.is_(None)) | (Instrument.last_price_date < as_of))))


def run_scan(engine: Engine, as_of: date | None = None, cfg: ScoringConfig = DEFAULT_CONFIG) -> int:
    as_of, inputs, ctx, notes = build_inputs(engine, as_of, cfg)
    results = score_universe(inputs, ctx, cfg)
    ranked = sorted((k for k, r in results.items() if r["status"] != "REJECTED"),
                    key=lambda k: (-results[k]["total"], k))
    rank = {k: n + 1 for n, k in enumerate(ranked)}
    by_symbol = {i.symbol: i for i in inputs}

    with session_scope(engine) as s:
        sv = _strategy_version(s, cfg)
        run = ScanRun(strategy_version_id=sv.id, as_of=as_of, universe_size=len(inputs),
                      scored=len(results), regime_json=json.dumps(ctx.regime, default=str),
                      sectors_json=json.dumps(ctx.sectors), notes="; ".join(notes) or None)
        s.add(run)
        s.flush()
        for sym, r in results.items():
            inp = by_symbol[sym]
            m = r.get("metrics", {})
            s.add(Score(
                scan_run_id=run.id, instrument_id=inp.instrument_id, as_of=as_of,
                price_date=date.fromisoformat(m["price_date"]) if m.get("price_date") else None,
                close=m.get("close"), total=r["total"], confidence=r["confidence"], status=r["status"],
                status_reason=r["status_reason"], rank=rank.get(sym),
                subscores_json=json.dumps(r["subscores"], default=str),
                strategies_json=json.dumps(r["strategies"], default=str),
                reasons_pos_json=json.dumps(r["reasons_pos"], ensure_ascii=False),
                reasons_neg_json=json.dumps(r["reasons_neg"], ensure_ascii=False),
                metrics_json=json.dumps(m, default=str),
                sources_json=json.dumps(r["sources"], default=str, ensure_ascii=False)))
            prev = s.execute(
                select(Score.status).join(ScanRun).where(
                    Score.instrument_id == inp.instrument_id, ScanRun.id < run.id,
                    ScanRun.strategy_version_id == sv.id)
                .order_by(ScanRun.as_of.desc(), ScanRun.id.desc()).limit(1)).scalar()
            if prev != r["status"]:
                s.add(StatusChange(instrument_id=inp.instrument_id, scan_run_id=run.id, as_of=as_of,
                                   from_status=prev, to_status=r["status"], reason=r["status_reason"]))
        return run.id
