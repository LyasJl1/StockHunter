"""Panel large (toutes les actions US, radiées comprises) et univers dynamique point-in-time.

Tout est indexé (date × ticker). Les prix sont ajustés des splits (comparables dans le temps) ; `raw_close` et
`dollar_vol` restent bruts (prix et montants réellement observés ce jour-là).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy.engine import Engine

from stockhunter.market.store import MarketStore, split_factors

BENCH = "SPY"


@dataclass
class Panel:
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    raw_close: pd.DataFrame
    dollar_vol: pd.DataFrame
    bench: pd.Series | None = None        # clôture ajustée du SPY (référence de marché)
    bench_open: pd.Series | None = None

    def fingerprint(self) -> dict:
        idx = self.close.index
        h = hashlib.sha256(("|".join(str(d.date()) for d in idx) + f"#{self.close.shape[1]}"
                            + f"#{float(np.nansum(self.raw_close.iloc[-1].to_numpy())):.2f}").encode()).hexdigest()[:16]
        return {"seances": len(idx), "premiere": str(idx[0].date()), "derniere": str(idx[-1].date()),
                "titres": int(self.close.shape[1]), "empreinte": h}

    def truncate(self, end) -> "Panel":
        """Panel limité aux séances <= end (sert aux tests d'absence de look-ahead)."""
        cut = lambda x: None if x is None else x.loc[:end]
        return Panel(*(cut(getattr(self, k)) for k in ("open", "high", "low", "close", "volume", "raw_close",
                                                         "dollar_vol", "bench", "bench_open")))


def build_panel(long: pd.DataFrame, splits: pd.DataFrame, tickers: set[str]) -> Panel:
    keep = long["ticker"].isin(tickers | {BENCH})
    df = long[keep & ~long["otc"].fillna(False).astype(bool)].copy()
    f = split_factors(splits, df["date"], df["ticker"])
    for col in ("open", "high", "low", "close"):
        df[f"adj_{col}"] = df[col] * f
    df["adj_volume"] = df["volume"] / f
    df["dollar_vol"] = df["close"] * df["volume"]

    def wide(col):
        w = df.pivot_table(index="date", columns="ticker", values=col, aggfunc="last").sort_index()
        return w

    frames = {k: wide(c) for k, c in (("open", "adj_open"), ("high", "adj_high"), ("low", "adj_low"),
                                      ("close", "adj_close"), ("volume", "adj_volume"), ("raw_close", "close"),
                                      ("dollar_vol", "dollar_vol"))}
    bench = frames["close"][BENCH] if BENCH in frames["close"] else None
    bench_open = frames["open"][BENCH] if BENCH in frames["open"] else None
    if BENCH not in tickers:
        frames = {k: v.drop(columns=[BENCH], errors="ignore") for k, v in frames.items()}
    return Panel(bench=bench, bench_open=bench_open, **frames)


def load_panel(engine: Engine, store: MarketStore, end: date | None = None) -> Panel:
    from stockhunter.market.sync import load_splits
    from stockhunter.research.broad import eligible_tickers
    long = store.load(end=end)
    if long.empty:
        raise RuntimeError("aucune séance de marché stockée : lancer « Univers large - chargement »")
    return build_panel(long, load_splits(engine), eligible_tickers(engine))


# ============================================================================ univers dynamique
@dataclass(frozen=True)
class UniverseFilter:
    min_price: float = 5.0                 # cours brut minimum ($)
    min_dollar_vol: float = 2_000_000      # médiane 20 j du volume échangé ($)
    min_history: int = 130                 # séances d'historique
    min_listed_20: int = 15                # séances cotées sur les 20 dernières (risque de suspension)
    max_daily_move: float = 0.80           # variation journalière au-delà = donnée suspecte (20 séances)


def universe_mask(p: Panel, f: UniverseFilter = UniverseFilter()) -> pd.DataFrame:
    """Univers négociable à chaque date, calculé avec les seules données <= cette date."""
    has = p.close.notna()
    hist = has.cumsum()
    med_dv = p.dollar_vol.rolling(20, min_periods=15).median()
    listed = has.rolling(20, min_periods=1).sum() >= f.min_listed_20
    ret = p.close.pct_change(fill_method=None)
    suspect = (ret.abs() > f.max_daily_move).astype(float).rolling(20, min_periods=1).max() > 0
    return has & (p.raw_close >= f.min_price) & (med_dv >= f.min_dollar_vol) & (hist >= f.min_history) & listed & ~suspect


def liquidity_tier(p: Panel, mask: pd.DataFrame) -> pd.DataFrame:
    """0 = tiers le moins liquide de l'univers, 2 = le plus liquide (proxy de taille, faute de capitalisations)."""
    med_dv = p.dollar_vol.rolling(20, min_periods=15).median().where(mask)
    r = med_dv.rank(axis=1, pct=True)
    return np.floor(r * 3).clip(upper=2)


def forward_returns(p: Panel, h: int) -> pd.DataFrame:
    """Entrée à l'ouverture de t+1, sortie à la clôture de t+h ; titre radié : dernière clôture connue."""
    entry = p.open.shift(-1)
    exit_ = p.close.ffill().shift(-h)
    observable = pd.Series(np.arange(len(p.close)) + h < len(p.close), index=p.close.index)
    return (exit_ / entry - 1).where(observable, axis=0)


def bench_forward(p: Panel, h: int) -> pd.Series | None:
    if p.bench is None or p.bench_open is None:
        return None
    return p.bench.shift(-h) / p.bench_open.shift(-1) - 1
