"""Backtest de portefeuille avec les règles de trading partagées (`stockhunter.trading.rules`).

Mêmes entrées, sorties, frais et glissement que le paper trading : ce module ne fait que fournir les barres et
tenir la caisse. Prix ajustés des splits (unités cohérentes dans le temps, donc aucun split à appliquer).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from stockhunter.lab.factors import atr
from stockhunter.lab.panel import Panel
from stockhunter.lab.stats import curve_stats
from stockhunter.trading.rules import Bar, Position, Rules, plan, risk_size, step


@dataclass(frozen=True)
class PortfolioParams:
    capital: float = 100_000.0
    risk_fraction: float = 0.005
    max_positions: int = 8
    max_exposure: float = 1.0
    max_correlation: float = 0.80
    top_n: int = 20


def simulate(p: Panel, score: pd.DataFrame, mask: pd.DataFrame, signal_dates: pd.DatetimeIndex, end: pd.Timestamp,
             rules: Rules = Rules(), pp: PortfolioParams = PortfolioParams()) -> dict:
    """À chaque date de signal : plans pour les `top_n` meilleurs scores de l'univers ; entrée conditionnelle à
    l'ouverture suivante ; suivi séance par séance jusqu'à `end` (positions restantes valorisées à la clôture)."""
    a14 = atr(p)
    cf = p.close.ffill()
    ret = p.close.pct_change(fill_method=None)
    sessions = p.close.index[(p.close.index >= signal_dates[0]) & (p.close.index <= end)]
    sig = set(signal_dates)
    cash = pp.capital
    live: dict[str, Position] = {}
    done: list[tuple[str, Position]] = []
    refusals: dict[str, int] = {}
    curve, expo = [], []
    for i, d in enumerate(sessions):
        for sym in list(live):
            pos = live[sym]
            o, h, lo, c = (p.open.at[d, sym], p.high.at[d, sym], p.low.at[d, sym], p.close.at[d, sym])
            if not np.isfinite(c) or not np.isfinite(o):
                continue                                           # pas de cotation ce jour : rien d'observable

            def size(fill, off, sym=sym):
                opened = [(s, q) for s, q in live.items() if q.status == "OPEN"]
                if len(opened) >= pp.max_positions:
                    return 0, f"limite de {pp.max_positions} positions atteinte"
                win = ret.loc[:d].iloc[-61:-1]
                for s, _q in opened:
                    cc = win[sym].corr(win[s])
                    if np.isfinite(cc) and cc > pp.max_correlation:
                        return 0, "trop corrélée à une position ouverte"
                invested = sum(q.shares * q.entry_price for _s, q in opened)
                realized = sum(q.realized - q.costs for q in live.values()) + sum(q.realized - q.costs for _s, q in done)
                basis = pp.capital + realized
                return risk_size(basis, pp.risk_fraction, basis * pp.max_exposure - invested)(fill, off)

            for ev in step(pos, Bar(d.date(), float(o), float(h), float(lo), float(c)), size, rules):
                if ev.kind == "OPENED":
                    cash -= ev.price * ev.shares * (1 + rules.cost_bps / 1e4)
                elif ev.kind in ("TP1", "CLOSED"):
                    cash += ev.price * ev.shares * (1 - rules.cost_bps / 1e4)
                elif ev.kind == "CANCELLED":
                    key = ev.detail.split(" (")[0].split(" à ")[0][:40]
                    refusals[key] = refusals.get(key, 0) + 1
            if pos.status in ("CLOSED", "CANCELLED"):
                done.append((sym, live.pop(sym)))
        mtm = sum(q.shares * cf.at[d, s] for s, q in live.items() if q.status == "OPEN")
        curve.append(cash + mtm)
        expo.append(mtm / (cash + mtm) if cash + mtm > 0 else 0.0)
        if d in sig and i < len(sessions) - 1:
            x = score.loc[d].where(mask.loc[d]).dropna().sort_values(ascending=False).head(pp.top_n)
            for sym in x.index:
                at, cl = a14.at[d, sym], p.close.at[d, sym]
                if sym in live or not np.isfinite(at) or at <= 0:
                    continue
                live[sym] = plan(float(cl), float(at), rules)
    eq = pd.Series(curve, index=sessions)
    closed = [q for _s, q in done if q.status == "CLOSED"]
    net = np.array([q.realized - q.costs for q in closed])
    rmult = np.array([(q.realized - q.costs) / q.initial_risk for q in closed if q.initial_risk])
    out = {"courbe": curve_stats(eq), "trades_clos": len(closed), "positions_encore_ouvertes":
           sum(q.status == "OPEN" for q in live.values()), "plans_annules": sum(q.status == "CANCELLED" for _s, q in done),
           "motifs_annulation": refusals, "gagnants": float((net > 0).mean()) if len(net) else None,
           "r_moyen": float(rmult.mean()) if len(rmult) else None, "exposition_moyenne": float(np.mean(expo)),
           "debut": str(sessions[0].date()), "fin": str(sessions[-1].date())}
    if p.bench is not None:
        out["spy"] = curve_stats(p.bench.reindex(sessions))
    univ = (1 + ret.where(mask.shift(1, fill_value=False)).mean(axis=1).reindex(sessions).fillna(0)).cumprod()
    out["univers_equipondere"] = curve_stats(univ)
    return out
