"""Indicateurs techniques déterministes, calculés à une date d'analyse (as_of) à partir des seules
barres EOD dont bar_date <= as_of. Les prix ajustés servent aux rendements/tendances ; le prix brut sert
au volume en dollars. Toute valeur non calculable (historique insuffisant) vaut None — jamais 0.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

# Fenêtres en séances
M1, M3, M6, M12 = 21, 63, 126, 252
BREAKOUT_LOOKBACK = 50
BREAKOUT_VOLUME_MULT = 1.5
MIN_HISTORY = 60


def _f(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def _ret(s: pd.Series, n: int) -> float | None:
    if len(s) <= n:
        return None
    return _f(s.iloc[-1] / s.iloc[-1 - n] - 1)


def bars_frame(rows: list[dict]) -> pd.DataFrame:
    """rows : dicts issus de price_bars. Index = date de séance, trié."""
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["bar_date"] = pd.to_datetime(df["bar_date"])
    return df.set_index("bar_date").sort_index()


def technical_snapshot(df: pd.DataFrame, bench: pd.DataFrame | None = None,
                       sector: pd.DataFrame | None = None) -> dict[str, Any]:
    """df / bench / sector : frames déjà tronqués à as_of (colonnes adj_*, close, volume)."""
    out: dict[str, Any] = {"bars": len(df)}
    if len(df) < MIN_HISTORY:
        out["insufficient_history"] = True
        return out

    c, h, lo, v = df["adj_close"], df["adj_high"], df["adj_low"], df["adj_volume"]
    out["price_date"] = df.index[-1].date().isoformat()
    out["close"] = _f(df["close"].iloc[-1])            # prix brut de clôture (affichage)
    out["adj_close"] = _f(c.iloc[-1])

    for name, n in (("ret_1m", M1), ("ret_3m", M3), ("ret_6m", M6), ("ret_12m", M12)):
        out[name] = _ret(c, n)
    # momentum 12-1 : 12 mois en excluant le dernier mois (effet de retournement court terme)
    out["mom_12_1"] = _f(c.iloc[-1 - M1] / c.iloc[-1 - M12] - 1) if len(c) > M12 else None

    sma = {n: c.rolling(n).mean() for n in (20, 50, 200)}
    for n, s in sma.items():
        out[f"sma{n}"] = _f(s.iloc[-1])
    if out["sma50"] is not None:
        out["above_sma50"] = bool(c.iloc[-1] > out["sma50"])
        out["ext_sma50"] = _f(c.iloc[-1] / out["sma50"] - 1)
        out["sma50_rising"] = bool(sma[50].iloc[-1] > sma[50].iloc[-21]) if len(c) > 70 else None
    if out["sma200"] is not None:
        out["above_sma200"] = bool(c.iloc[-1] > out["sma200"])
        out["sma50_above_sma200"] = bool(out["sma50"] > out["sma200"])

    win = min(len(c), M12)
    hi52 = h.iloc[-win:].max()
    out["dist_52w_high"] = _f(c.iloc[-1] / hi52 - 1)
    out["drawdown_from_high"] = out["dist_52w_high"]

    prev_close = c.shift(1)
    tr = pd.concat([h - lo, (h - prev_close).abs(), (lo - prev_close).abs()], axis=1).max(axis=1)
    out["atr14_pct"] = _f(tr.rolling(14).mean().iloc[-1] / c.iloc[-1])
    daily = c.pct_change()
    out["vol20_ann"] = _f(daily.iloc[-20:].std() * math.sqrt(252))
    out["gap_last"] = _f(df["adj_open"].iloc[-1] / c.iloc[-2] - 1)
    out["ret_last"] = _f(daily.iloc[-1])

    # Contraction de volatilité : largeur des bandes de Bollinger, rang dans les 126 dernières séances
    bbw = (4 * c.rolling(20).std() / c.rolling(20).mean()).dropna()
    if len(bbw) >= 60:
        hist = bbw.iloc[-M6:]
        out["squeeze_pct"] = _f((hist < hist.iloc[-1]).mean())  # 0 = plus étroite de la période

    # Volume et accumulation
    v20, v50 = v.iloc[-20:].mean(), v.iloc[-50:].mean()
    out["vol_ratio_20_50"] = _f(v20 / v50) if v50 else None
    up = daily.iloc[-50:] > 0
    vol50 = v.iloc[-50:]
    down_vol = vol50[~up].sum()
    out["updown_vol_50"] = _f(vol50[up].sum() / down_vol) if down_vol else None
    out["dollar_vol_20"] = _f((df["close"] * df["volume"]).iloc[-20:].mean())
    out["vol_last_vs_50"] = _f(v.iloc[-1] / v50) if v50 else None

    # Breakout : clôture au-dessus du plus haut des 50 séances précédentes, avec volume > 1,5× la moyenne
    if len(df) > BREAKOUT_LOOKBACK + 5:
        prior_high = h.shift(1).rolling(BREAKOUT_LOOKBACK).max()
        vol_avg = v.shift(1).rolling(50).mean()
        is_bo = (c > prior_high) & (v > BREAKOUT_VOLUME_MULT * vol_avg)
        recent = is_bo.iloc[-5:]
        out["breakout_today"] = bool(is_bo.iloc[-1])
        if recent.any():
            day = recent[recent].index[-1]
            level = float(prior_high.loc[day])
            out["breakout_date"] = day.date().isoformat()
            out["breakout_level"] = _f(level)
            out["breakout_holding"] = bool(c.iloc[-1] >= level)   # pas de retour sous le niveau cassé
        else:
            out["breakout_holding"] = False
        # Base constructive : 30 séances dans un range < 15 %, à moins de 8 % du plus haut 52 semaines
        rng = h.iloc[-30:].max() / lo.iloc[-30:].min() - 1
        out["base_range_30"] = _f(rng)
        out["constructive_base"] = bool(rng < 0.15 and (out["dist_52w_high"] or -1) > -0.08)

    # Force relative (excès de rendement) vs SPY et vs ETF sectoriel
    for tag, ref in (("spy", bench), ("sector", sector)):
        if ref is None or ref.empty:
            continue
        rc = ref["adj_close"].reindex(c.index).ffill()
        for name, n in (("1m", M1), ("3m", M3), ("6m", M6)):
            a, b = _ret(c, n), _ret(rc, n)
            out[f"rs_{tag}_{name}"] = _f(a - b) if a is not None and b is not None else None
        if tag == "spy" and len(c) > M1 + 1:
            line = c / rc
            out["rs_line_rising"] = bool(line.iloc[-1] > line.iloc[-1 - M1])
            out["rs_line_new_high"] = bool(line.iloc[-1] >= line.iloc[-min(len(line), M6):].max() * 0.995)
    return out


def window_return(df: pd.DataFrame, start_exclusive: pd.Timestamp, end_inclusive: pd.Timestamp) -> float | None:
    """Rendement ajusté entre la dernière clôture < start et la dernière clôture <= end."""
    c = df["adj_close"]
    before = c[c.index < start_exclusive]
    upto = c[c.index <= end_inclusive]
    if before.empty or upto.empty:
        return None
    return _f(upto.iloc[-1] / before.iloc[-1] - 1)


def pct_rank(values: dict[Any, float | None], higher_is_better: bool = True) -> dict[Any, float | None]:
    """Rang centile 0-100 au sein de l'univers (égalités = rang moyen). None reste None."""
    keys = [k for k, x in values.items() if x is not None]
    out: dict[Any, float | None] = {k: None for k in values}
    if not keys:
        return out
    if len(keys) == 1:
        out[keys[0]] = 50.0
        return out
    arr = np.array([values[k] for k in keys], dtype=float)
    ranks = pd.Series(arr).rank(method="average").to_numpy()
    pct = (ranks - 1) / (len(arr) - 1) * 100
    if not higher_is_better:
        pct = 100 - pct
    for k, p in zip(keys, pct):
        out[k] = float(p)
    return out
