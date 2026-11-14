"""Portefeuille mondial SIMULÉ (3.2) : sélections de toutes les places, une devise de base, contraintes de risque.

Aucun ordre réel. Rejoue, avec les règles de trading partagées (`trading/rules.py`, identiques au paper trading US
et aux backtests), les sélections figées de chaque place sur leurs séances suivantes.

Conventions (docs/v3-2/conventions.md) :
- Devise de base EUR ou USD. Les positions vivent en devise locale ; elles sont valorisées chaque jour au dernier
  taux BCE publié ≤ ce jour. Le capital, le risque et les plafonds sont mesurés dans la devise de base.
- Ordre de traitement : jour par jour ; dans un jour, les places dans l'ordre de leur heure de clôture UTC (Tokyo,
  Mumbai, Paris, New York) ; dans une place, entrées avant gestion des positions (comme le paper trading US).
- Entrée à l'ouverture de la séance suivant le signal, sur la place du signal. Sans cotation pendant 5 séances :
  plan annulé. Position sans cotation pendant 10 séances : clôturée au dernier cours connu (suspension, radiation).
- Taille : risque = capital × `risk_per_position` (distance du stop convertie en base), arrondi à la quotité ;
  plafonds : positions, poids par ligne, exposition par pays, par devise (hors devise de base), par secteur
  (seulement si le secteur est connu), corrélation (rendements journaliers en base sur les dates communes).
- Frais : courtage `cost_bps` et glissement `slippage_bps` des règles partagées ; change `fx_cost_bps` à chaque
  conversion (achat et vente) pour une devise différente de la base. Pas de levier : la trésorerie doit couvrir
  le montant, le courtage et le change.
- Dividendes NON inclus (rendement de prix) ; divisions incluses via des cours ajustés.
- Courbe de capital = trésorerie + valeur de marché des positions, en devise de base, à chaque date de séance.
Performance SIMULÉE : jamais mélangée à une performance réelle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from stockhunter.trading.rules import Bar, Position, Rules, plan, step
from stockhunter.world import markets as mk
from stockhunter.world.fx import FxTable, FxUnavailable


@dataclass(frozen=True)
class GlobalConfig:
    base: str = "EUR"
    capital: float = 100_000.0
    risk_per_position: float = 0.005
    max_positions: int = 15
    max_weight: float = 0.15
    max_country: float = 0.40
    max_currency: float = 0.50          # exposition par devise étrangère (la devise de base n'est pas plafonnée)
    max_sector: float = 0.30
    max_corr: float = 0.80
    corr_window: int = 60
    corr_min_common: int = 40
    fx_cost_bps: float = 25.0
    max_new_per_day: int = 5
    plan_ttl_sessions: int = 5
    stale_sessions: int = 10


@dataclass
class SignalIn:
    market: str
    key: str
    ticker: str
    as_of: date
    category: str
    norm: float
    signal_close: float
    atr: float
    currency: str
    country: str
    sector: str | None = None
    lot: int = 1


@dataclass
class Holding:
    sig: SignalIn
    pos: Position
    waited: int = 0
    unseen: int = 0
    last_close: float | None = None
    fx_fees: float = 0.0
    commissions: float = 0.0


@dataclass
class Result:
    equity: pd.Series
    trades: list[dict]
    refused: list[dict]
    summary: dict[str, Any]
    exposures: dict[str, dict[str, float]] = field(default_factory=dict)


def _market_order(codes) -> list[str]:
    def close_minutes(c):
        t = mk.get(c).close_local
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        return datetime.combine(date(2026, 1, 15), t, ZoneInfo(mk.get(c).tz)).astimezone(timezone.utc).hour * 60 + t.minute
    return sorted(codes, key=close_minutes)


def simulate(signals: list[SignalIn], bars: dict[tuple[str, str], pd.DataFrame], fx: FxTable,
             cfg: GlobalConfig = GlobalConfig(), rules: Rules = Rules()) -> Result:
    """`bars[(place, clé)]` : DataFrame indexé par date (Timestamp) avec open/high/low/close en devise locale,
    ajustés des divisions, uniquement les séances où le titre a coté."""
    dates = sorted({d for df in bars.values() for d in df.index} | {pd.Timestamp(s.as_of) for s in signals})
    if not dates:
        return Result(pd.Series(dtype=float), [], [], {"statut": "aucune donnée"})
    sessions_by_market: dict[str, set] = {}
    for (m, _), df in bars.items():
        sessions_by_market.setdefault(m, set()).update(df.index)
    order = _market_order(sessions_by_market)
    pending: list[Holding] = []
    open_: list[Holding] = []
    trades, refused = [], []
    cash = cfg.capital
    equity_pts: dict[pd.Timestamp, float] = {}
    sig_by_day: dict[pd.Timestamp, list[SignalIn]] = {}
    for s in sorted(signals, key=lambda s: (s.as_of, -s.norm, s.market, s.ticker)):
        sig_by_day.setdefault(pd.Timestamp(s.as_of), []).append(s)
    fees = {"courtage": 0.0, "change": 0.0}

    def px(h: Holding, d) -> float | None:
        df = bars.get((h.sig.market, h.sig.key))
        if df is None:
            return None
        s = df["close"].loc[:d].dropna()
        return float(s.iloc[-1]) if len(s) else None

    def to_base(amount: float, ccy: str, d) -> float:
        return amount * fx.rate(ccy, cfg.base, d)

    def value(d) -> float:
        tot = cash
        for h in open_:
            p = px(h, d)
            if p is not None:
                tot += to_base(p * h.pos.shares, h.sig.currency, d)
        return tot

    def base_returns(h_or_sig, d) -> pd.Series:
        sig = h_or_sig.sig if isinstance(h_or_sig, Holding) else h_or_sig
        df = bars.get((sig.market, sig.key))
        if df is None:
            return pd.Series(dtype=float)
        c = df["close"].loc[:d].dropna().iloc[-(cfg.corr_window + 1):]
        try:
            r = fx.series(sig.currency, cfg.base, c.index)
        except FxUnavailable:
            return pd.Series(dtype=float)
        return (c * r).pct_change().dropna()

    def exposure(d, attr: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for h in open_:
            p = px(h, d)
            if p is None:
                continue
            k = getattr(h.sig, attr) or "N/D"
            out[k] = out.get(k, 0.0) + to_base(p * h.pos.shares, h.sig.currency, d)
        return out

    def flow(h: Holding, ev, d) -> None:
        nonlocal cash
        amount = ev.price * ev.shares
        comm = amount * rules.cost_bps / 1e4
        gross = to_base(amount, h.sig.currency, d)
        comm_b = to_base(comm, h.sig.currency, d)
        fxf = gross * cfg.fx_cost_bps / 1e4 if h.sig.currency != cfg.base else 0.0
        if ev.kind == "OPENED":
            cash -= gross + comm_b + fxf
        else:
            cash += gross - comm_b - fxf
        h.fx_fees += fxf
        h.commissions += comm_b
        fees["courtage"] += comm_b
        fees["change"] += fxf

    for d in dates:
        todays = {m for m in order if d in sessions_by_market.get(m, set())}
        for m in order:
            if m not in todays:
                continue
            # ---------------------------------------------------------- 1. entrées des plans en attente
            for h in [h for h in pending if h.sig.market == m]:
                df = bars.get((m, h.sig.key))
                if df is None or d not in df.index or not np.isfinite(df.at[d, "open"]):
                    h.waited += 1
                    if h.waited >= cfg.plan_ttl_sessions:
                        pending.remove(h)
                        refused.append({"ticker": h.sig.ticker, "place": m, "date": str(d.date()),
                                        "motif": "aucune cotation dans les 5 séances suivant le signal"})
                    continue
                pending.remove(h)
                eq = value(d)
                b = df.loc[d]
                why = _check_caps(h, eq, d, open_, exposure, base_returns, cfg)

                def size(fill: float, off: float, h=h, eq=eq, why=why):
                    if why:
                        return 0, why
                    unit_base = to_base(1.0, h.sig.currency, d)
                    risk_shares = eq * cfg.risk_per_position / (off * unit_base) if off > 0 else 0
                    cap_shares = min(eq * cfg.max_weight, cash / (1 + (rules.cost_bps + cfg.fx_cost_bps) / 1e4)) / (fill * unit_base)
                    n = math.floor(min(risk_shares, cap_shares) / h.sig.lot) * h.sig.lot
                    if n < 1:
                        return 0, ("trésorerie ou poids maximal insuffisant pour une quotité" if risk_shares >= h.sig.lot
                                   else "risque par position trop faible pour une quotité")
                    return n, None

                evs = step(h.pos, Bar(d.date(), float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])),
                           size, rules)
                if h.pos.status == "CANCELLED":
                    refused.append({"ticker": h.sig.ticker, "place": m, "date": str(d.date()), "motif": h.pos.close_reason})
                    continue
                for ev in evs:
                    if ev.kind in ("OPENED", "TP1", "CLOSED"):
                        flow(h, ev, d)
                h.last_close = float(b["close"])
                if h.pos.status == "OPEN":
                    open_.append(h)
                else:
                    trades.append(_trade(h, cfg, fx))
            # ---------------------------------------------------------- 2. gestion des positions ouvertes
            for h in [h for h in open_ if h.sig.market == m and h.pos.entry_date != d.date()]:
                df = bars.get((m, h.sig.key))
                if df is None or d not in df.index or not np.isfinite(df.at[d, "open"]):
                    h.unseen += 1
                    if h.unseen >= cfg.stale_sessions and h.last_close:
                        _force_close(h, d, rules, flow)
                        open_.remove(h)
                        trades.append(_trade(h, cfg, fx))
                    continue
                h.unseen = 0
                b = df.loc[d]
                for ev in step(h.pos, Bar(d.date(), float(b["open"]), float(b["high"]), float(b["low"]),
                                          float(b["close"])), lambda f, o: (0, "déjà ouverte"), rules):
                    if ev.kind in ("TP1", "CLOSED"):
                        flow(h, ev, d)
                h.last_close = float(b["close"])
                if h.pos.status == "CLOSED":
                    open_.remove(h)
                    trades.append(_trade(h, cfg, fx))
            # ---------------------------------------------------------- 3. nouveaux signaux de la séance
            day_sigs = [s for s in sig_by_day.get(d, []) if s.market == m][: cfg.max_new_per_day]
            for s in day_sigs:
                held = {(h.sig.market, h.sig.key) for h in open_ + pending}
                if (s.market, s.key) in held:
                    continue
                pending.append(Holding(sig=s, pos=plan(s.signal_close, s.atr, rules)))
        try:
            equity_pts[d] = value(d)
        except FxUnavailable:
            continue
    eq = pd.Series(equity_pts).sort_index()
    last = dates[-1]
    exp = {k: {kk: round(vv / eq.iloc[-1], 4) for kk, vv in exposure(last, k).items()} for k in ("country", "currency", "sector")} \
        if len(eq) else {}
    return Result(eq, trades, refused, _summary(eq, trades, fees, cfg, open_), exp)


def _check_caps(h: Holding, eq: float, d, open_, exposure, base_returns, cfg: GlobalConfig) -> str | None:
    if len(open_) >= cfg.max_positions:
        return f"nombre maximal de positions ({cfg.max_positions}) atteint"
    nominal = eq * cfg.max_weight                       # poids maximal hypothétique de la nouvelle ligne
    if exposure(d, "country").get(h.sig.country, 0.0) + nominal > eq * cfg.max_country + 1e-9:
        return f"exposition maximale au pays {h.sig.country} ({cfg.max_country:.0%})"
    if h.sig.currency != cfg.base and exposure(d, "currency").get(h.sig.currency, 0.0) + nominal > eq * cfg.max_currency + 1e-9:
        return f"exposition maximale à la devise {h.sig.currency} ({cfg.max_currency:.0%})"
    if h.sig.sector and exposure(d, "sector").get(h.sig.sector, 0.0) + nominal > eq * cfg.max_sector + 1e-9:
        return f"exposition maximale au secteur {h.sig.sector} ({cfg.max_sector:.0%})"
    r_new = base_returns(h, d)
    for o in open_:
        r_o = base_returns(o, d)
        common = r_new.index.intersection(r_o.index)
        if len(common) >= cfg.corr_min_common:
            c = float(np.corrcoef(r_new.loc[common], r_o.loc[common])[0, 1])
            if np.isfinite(c) and c > cfg.max_corr:
                return f"corrélation {c:.2f} > {cfg.max_corr:.2f} avec {o.sig.ticker} ({o.sig.market})"
    return None


def _force_close(h: Holding, d, rules: Rules, flow) -> None:
    from stockhunter.trading.rules import _sell
    n0 = len(h.pos.events)
    _sell(h.pos, d.date(), h.last_close, h.pos.shares, rules, "CLOSED",
          "plus de cotation depuis 10 séances (suspension ou radiation) : vente au dernier cours connu")
    for ev in h.pos.events[n0:]:
        flow(h, ev, d)


def _trade(h: Holding, cfg: GlobalConfig, fx: FxTable) -> dict:
    p = h.pos
    r = (p.realized - p.costs) / p.initial_risk if p.initial_risk else None
    return {"ticker": h.sig.ticker, "place": h.sig.market, "pays": h.sig.country, "devise": h.sig.currency,
            "categorie": h.sig.category, "signal": str(h.sig.as_of), "entree": str(p.entry_date),
            "sortie": str(p.exit_date), "r": None if r is None else round(r, 3), "motif": p.close_reason,
            "frais_change_base": round(h.fx_fees, 2), "courtage_base": round(h.commissions, 2)}


def _summary(eq: pd.Series, trades: list[dict], fees: dict, cfg: GlobalConfig, open_) -> dict[str, Any]:
    if eq.empty:
        return {"statut": "aucune valorisation possible"}
    ret = eq.pct_change().dropna()
    days = (eq.index[-1] - eq.index[0]).days
    total = float(eq.iloc[-1] / cfg.capital - 1)
    dd = float((eq / eq.cummax() - 1).min())
    rs = [t["r"] for t in trades if t["r"] is not None]
    return {"devise_base": cfg.base, "capital_initial": cfg.capital, "valeur_finale": round(float(eq.iloc[-1]), 2),
            "rendement_total": total, "annualise": (1 + total) ** (365 / days) - 1 if days >= 365 else None,
            "drawdown_max": dd, "volatilite_annuelle": float(ret.std() * math.sqrt(252)) if len(ret) > 20 else None,
            "debut": str(eq.index[0].date()), "fin": str(eq.index[-1].date()), "trades_clos": len(trades),
            "positions_ouvertes": len(open_), "gagnants": (sum(r > 0 for r in rs) / len(rs)) if rs else None,
            "r_moyen": (sum(rs) / len(rs)) if rs else None, "frais": {k: round(v, 2) for k, v in fees.items()},
            "nature": "SIMULATION (aucun ordre réel)",
            "lecture": "échantillon insuffisant pour conclure" if len(rs) < 30 else "descriptif, non validé"}


def benchmarks(series: dict[str, tuple[pd.Series, str]], fx: FxTable, base: str, start, end) -> dict[str, Any]:
    """Rendement de chaque référence sur LA MÊME période et dans LA MÊME devise que le portefeuille.
    `series[nom] = (clôtures, devise)`. Une référence sans donnée sur la période : N/D."""
    out = {}
    for name, (s, ccy) in series.items():
        s = s.dropna()
        if s.empty:
            out[name] = None
            continue
        s = s.loc[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
        if len(s) < 2:
            out[name] = None
            continue
        try:
            conv = s * fx.series(ccy, base, s.index)
        except FxUnavailable:
            out[name] = None
            continue
        conv = conv.dropna()
        out[name] = float(conv.iloc[-1] / conv.iloc[0] - 1) if len(conv) >= 2 else None
    return out
