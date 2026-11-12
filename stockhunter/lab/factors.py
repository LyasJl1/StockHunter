"""Bibliothèque de facteurs du programme v3 (docs/research/2026-11-12-preenregistrement-v3.md).

Chaque valeur à la date t n'utilise que des données <= t (test : tronquer le panel ne change aucune valeur passée).
Les définitions sont celles du pré-enregistrement : ne pas les modifier au vu des résultats.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from stockhunter.lab.panel import Panel


@dataclass(frozen=True)
class FactorSpec:
    key: str
    label: str
    sign: int
    kind: str            # "rank" (continu, IC) ou "event" (groupe signalé vs univers)
    family: str
    rationale: str
    min_sessions: int


SPECS: dict[str, FactorSpec] = {s.key: s for s in [
    FactorSpec("V1", "Momentum 12-1", +1, "rank", "momentum", "Sous-réaction à l'information (Jegadeesh & Titman 1993)", 253),
    FactorSpec("V2", "Tendance ajustée du risque (126 j)", +1, "rank", "momentum",
               "Momentum moins exposé aux titres loterie (Barroso & Santa-Clara 2015)", 127),
    FactorSpec("V3", "Momentum résiduel 6-1", +1, "rank", "momentum",
               "Momentum propre au titre (Blitz, Huij & Martens 2011)", 127),
    FactorSpec("V4", "Sortie de consolidation", +1, "event", "breakout",
               "Reprise de tendance après accumulation (George & Hwang 2004)", 121),
    FactorSpec("V9", "Effet MAX (plus forte hausse 21 j)", -1, "rank", "low-risk",
               "Préférence pour les titres loterie (Bali, Cakici & Whitelaw 2011)", 22),
    FactorSpec("MF", "Composite tendance + faible risque", +1, "rank", "multifactor",
               "Signaux complémentaires : V2, V3, −V9, −volatilité 60 j", 127),
]}

# Facteurs de l'étude du 12/11/2026 (déjà testés ; réutilisés uniquement comme variables du ML et du composite)
PRIOR_SIGNS = {"H1": 1, "H2": -1, "H3": 1, "H4": -1, "H5": 1, "H6": 1, "H7": 1}


def daily_returns(p: Panel) -> pd.DataFrame:
    return p.close.pct_change(fill_method=None)


def market_return(p: Panel, ret: pd.DataFrame, min_price: float = 5.0, min_dollar_vol: float = 2_000_000) -> pd.Series:
    """Rendement du marché équipondéré : titres liquides (cours >= 5 $, volume médian 20 j >= 2 M$) la VEILLE.
    Pas d'exigence d'historique ici, pour que la série de marché existe dès le début des données."""
    liquid = (p.raw_close >= min_price) & (p.dollar_vol.rolling(20, min_periods=15).median() >= min_dollar_vol)
    return ret.where(liquid.shift(1, fill_value=False)).mean(axis=1)


def residual_momentum(ret: pd.DataFrame, mkt: pd.Series, window: int = 105, skip: int = 21) -> pd.DataFrame:
    """Résidus r_i − β_i·marché (régression sans constante) sur [t−126, t−21] : moyenne ÷ écart-type."""
    r, m = ret.shift(skip), mkt.shift(skip)
    roll = lambda x: x.rolling(window, min_periods=window).mean()
    mm, m2 = roll(m), roll(m * m)
    mr, r1, r2 = roll(r.mul(m, axis=0)), roll(r), roll(r * r)
    beta = mr.div(m2, axis=0)
    res_mean = r1 - beta.mul(mm, axis=0)
    res_var = r2 - 2 * beta * mr + (beta * beta).mul(m2, axis=0) - res_mean * res_mean
    return res_mean / np.sqrt(res_var.where(res_var > 1e-12))


def breakout_flags(p: Panel) -> pd.DataFrame:
    c, v = p.close, p.volume
    prior_high = p.high.rolling(60, min_periods=60).max().shift(1)
    rng = lambda n: (p.high.rolling(n, min_periods=n).max() / p.low.rolling(n, min_periods=n).min() - 1).shift(1)
    vol_ok = v >= 1.5 * v.rolling(60, min_periods=60).mean().shift(1)
    return (c > prior_high) & (rng(20) < 0.8 * rng(120)) & vol_ok


def rank_pct(df: pd.DataFrame, mask: pd.DataFrame) -> pd.DataFrame:
    return df.where(mask).rank(axis=1, pct=True)


def compute(p: Panel, mask: pd.DataFrame) -> dict[str, pd.DataFrame]:
    from stockhunter.research.broad import compute_factors as prior_factors

    c = p.close
    ret = daily_returns(p)
    vol60 = ret.rolling(60, min_periods=60).std()
    out = dict(prior_factors(p))                                    # H1..H7 (définitions inchangées)
    out["V1"] = c.shift(21) / c.shift(252) - 1
    out["V2"] = (c / c.shift(126) - 1) / (vol60 * math.sqrt(126))
    out["V3"] = residual_momentum(ret, market_return(p, ret))
    out["V4"] = breakout_flags(p)
    out["V9"] = ret.rolling(21, min_periods=21).max()
    parts = [rank_pct(out["V2"], mask), rank_pct(out["V3"], mask), 1 - rank_pct(out["V9"], mask),
             1 - rank_pct(out["H4"], mask)]
    out["MF"] = sum(parts) / 4                                      # NaN si une composante manque
    return out


def atr(p: Panel, n: int = 14) -> pd.DataFrame:
    prev = p.close.shift(1)
    tr = np.maximum(p.high - p.low, np.maximum((p.high - prev).abs(), (p.low - prev).abs()))
    return tr.rolling(n, min_periods=n).mean()
