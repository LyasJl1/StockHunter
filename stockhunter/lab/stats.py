"""Statistiques du laboratoire : t de Newey-West, p-valeurs, bootstrap par blocs, corrections de tests multiples."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from stockhunter.validation.backtest import newey_west_tstat  # noqa: F401  (ré-exporté)


def t_sf(t: float, df: int) -> float:
    """P(T > t) pour une loi de Student à df degrés de liberté (intégration numérique, sans scipy)."""
    if df < 1 or t is None or not math.isfinite(t):
        return float("nan")
    if t < 0:
        return 1.0 - t_sf(-t, df)
    x = np.linspace(0.0, 1.0, 20001)[1:-1]                 # changement de variable u = t / x sur [t, +inf)
    u = t / x if t > 0 else None
    if u is None:
        return 0.5
    c = math.exp(math.lgamma((df + 1) / 2) - math.lgamma(df / 2)) / math.sqrt(df * math.pi)
    pdf = c * (1 + u * u / df) ** (-(df + 1) / 2)
    trap = getattr(np, "trapezoid", None) or np.trapz
    return float(trap(pdf * t / (x * x), x))


def block_bootstrap_ci(x: pd.Series, block: int, n: int = 2000, seed: int = 20261112, level: float = 0.95):
    """Intervalle de confiance de la moyenne par bootstrap à blocs mobiles (conserve l'autocorrélation)."""
    v = np.asarray(x.dropna(), dtype=float)
    k = len(v)
    if k < 2 * max(block, 1):
        return None
    rng = np.random.default_rng(seed)
    block = max(1, block)
    nb = math.ceil(k / block)
    starts = rng.integers(0, k - block + 1, size=(n, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n, -1)[:, :k]
    means = v[idx].mean(axis=1)
    lo, hi = np.quantile(means, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(lo), float(hi)


def holm(pvals: dict[str, float]) -> dict[str, float]:
    items = sorted(((k, p) for k, p in pvals.items() if p is not None and not math.isnan(p)), key=lambda kv: kv[1])
    m, out, running = len(items), {}, 0.0
    for i, (k, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[k] = running
    return out


def benjamini_hochberg(pvals: dict[str, float]) -> dict[str, float]:
    items = sorted(((k, p) for k, p in pvals.items() if p is not None and not math.isnan(p)), key=lambda kv: kv[1])
    m, out, running = len(items), {}, 1.0
    for i in range(m - 1, -1, -1):
        k, p = items[i]
        running = min(running, p * m / (i + 1))
        out[k] = running
    return out


def curve_stats(equity: pd.Series) -> dict:
    """Rendement total, annualisé, volatilité, Sharpe (taux sans risque 0), drawdown maximal d'une courbe quotidienne."""
    e = equity.dropna()
    if len(e) < 2:
        return {}
    r = e.pct_change().dropna()
    years = len(r) / 252
    total = e.iloc[-1] / e.iloc[0] - 1
    vol = float(r.std() * math.sqrt(252)) if len(r) > 1 else None
    dd = float((e / e.cummax() - 1).min())
    return {"rendement_total": float(total), "annualise": float((1 + total) ** (1 / years) - 1) if years > 0 else None,
            "volatilite": vol, "sharpe": float(r.mean() / r.std() * math.sqrt(252)) if r.std() > 0 else None,
            "drawdown_max": dd, "seances": int(len(e))}
