"""Synchronisation de l'univers large : séances manquantes (les plus récentes d'abord), référentiel, splits.
Reprise automatique : chaque séance est un fichier ; un run interrompu (quota, panne) reprend au suivant."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from stockhunter.data.calendar import last_completed_session, trading_days
from stockhunter.data.http import QuotaExceeded
from stockhunter.db.models import MarketSplit, MarketTicker
from stockhunter.db.session import session_scope
from stockhunter.db.upsert import upsert
from stockhunter.jobs.runner import JobContext
from stockhunter.market.provider import MassiveProvider
from stockhunter.market.store import MarketStore

log = logging.getLogger(__name__)
TICKERS_REFRESH_DAYS = 7


PLAN_MAX_AGE_DAYS = 729  # plan gratuit : 2 ans glissants ; au-delà Massive répond 403 NOT_AUTHORIZED


def missing_sessions(store: MarketStore, today: date, history_days: int) -> list[date]:
    last = last_completed_session(today)
    wanted = [d.date() for d in trading_days(last - timedelta(days=int(history_days * 1.5)), last)][-history_days:]
    wanted = [d for d in wanted if (last - d).days <= PLAN_MAX_AGE_DAYS]
    have = set(store.sessions())
    return sorted((d for d in wanted if d not in have), reverse=True)  # les plus récentes d'abord


def sync_reference(engine: Engine, ctx: JobContext, mv: MassiveProvider, force: bool = False) -> None:
    with session_scope(engine) as s:
        last = s.scalar(select(func.max(MarketTicker.fetched_at)))
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if not force and last and datetime.now(timezone.utc) - last < timedelta(days=TICKERS_REFRESH_DAYS):
        return
    for active in (True, False):
        try:
            rows = mv.tickers(active)
        except Exception as exc:
            ctx.failure(f"référentiel active={active}", exc)
            if isinstance(exc, QuotaExceeded):
                return
            continue
        now = datetime.now(timezone.utc)
        with session_scope(engine) as s:
            upsert(s, MarketTicker, [{**r, "fetched_at": now} for r in rows], ["ticker_key"])
        ctx.success()


def sync_splits(engine: Engine, ctx: JobContext, mv: MassiveProvider, today: date, history_days: int) -> None:
    with session_scope(engine) as s:
        last = s.scalar(select(func.max(MarketSplit.execution_date)))
    since = (last - timedelta(days=30)) if last else today - timedelta(days=int(history_days * 1.5))
    try:
        rows = mv.splits(since)
    except Exception as exc:
        ctx.failure("splits", exc)
        return
    with session_scope(engine) as s:
        upsert(s, MarketSplit, rows, ["ticker", "execution_date"])
    ctx.success()


def sync_sessions(ctx: JobContext, mv: MassiveProvider, store: MarketStore, today: date, history_days: int,
                  max_calls: int | None = None) -> None:
    todo = missing_sessions(store, today, history_days)
    if max_calls is not None:
        todo = todo[:max_calls]
    for d in todo:
        try:
            n = store.write(d, mv.grouped_daily(d))
            if n == 0:
                log.warning("séance %s : aucune barre renvoyée", d)
            ctx.success()
        except QuotaExceeded as exc:
            ctx.failure(str(d), exc)
            return
        except Exception as exc:
            ctx.failure(str(d), exc)


def load_splits(engine: Engine) -> pd.DataFrame:
    with session_scope(engine) as s:
        rows = s.execute(select(MarketSplit.ticker, MarketSplit.execution_date, MarketSplit.split_from,
                                MarketSplit.split_to)).all()
    return pd.DataFrame(rows, columns=["ticker", "execution_date", "split_from", "split_to"])
