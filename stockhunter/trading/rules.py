"""Règles de trading simulé — UNE seule implémentation, utilisée par le paper trading et par le backtest.

Machine à états pure (aucun accès base, aucun réseau) : `step(position, barre)` applique une séance journalière
et renvoie les événements. Le paper trading l'appelle chaque soir ; le backtest l'appelle en boucle. Les deux
produisent donc exactement les mêmes entrées, sorties et frais pour les mêmes barres (testé).

Hypothèses, limitées à ce qu'une barre journalière permet d'observer :
- L'ouverture est le premier prix de la séance. Entrée : ordre à l'ouverture de J+1, annulé si l'ouverture sort
  de la zone [clôture − invalidation×ATR ; clôture × (1 + gap max)].
- Stop et objectif touchés la même séance : l'ordre réel est inconnu -> on retient le stop (cas défavorable).
- Gap sous le stop : sortie à l'ouverture (pire prix), pas au niveau du stop. Gap au-dessus d'un objectif :
  l'ordre limite est exécuté à l'ouverture.
- Après TP1, le stop ne passe au prix d'entrée qu'à la séance SUIVANTE : on ne suppose pas savoir si le plus bas
  du jour a précédé ou suivi le TP1.
- Le stop suiveur est recalculé à la clôture et ne vaut que pour les séances suivantes.
- Glissement (slippage) appliqué dans le sens défavorable aux ordres au marché et aux stops ; aucun sur les
  ordres limites (objectifs). Frais proportionnels sur chaque exécution.
- Split : quantités et niveaux par action ajustés le jour du split (la barre de ce jour est déjà post-split).
- Position d'une seule action : TP1 vend la totalité (on ne fractionne pas une action).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Callable


@dataclass(frozen=True)
class Rules:
    stop_atr: float = 2.0
    trail_atr: float = 3.0
    entry_gap_max: float = 0.03
    invalidation_atr: float = 1.0
    tp1_r: float = 1.5
    tp1_fraction: float = 0.5
    tp2_r: float = 3.0
    max_sessions: int = 40
    cost_bps: float = 10.0
    slippage_bps: float = 5.0


@dataclass(frozen=True)
class Bar:
    date: date
    open: float
    high: float
    low: float
    close: float
    split: float = 1.0


@dataclass
class Event:
    date: date
    kind: str            # OPENED | CANCELLED | TP1 | CLOSED | STOP_MOVED | SPLIT
    price: float | None
    shares: float | None
    detail: str


@dataclass
class Position:
    signal_close: float
    atr: float                       # ATR(14) en $ au signal
    entry_min: float
    entry_max: float
    stop_offset: float               # distance du stop initial en $ (R)
    status: str = "PLANNED"          # PLANNED | OPEN | CLOSED | CANCELLED
    entry_date: date | None = None
    entry_price: float | None = None
    shares: float = 0.0
    stop: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    tp1_done: bool = False
    highest_close: float | None = None
    sessions_held: int = 0
    realized: float = 0.0            # P&L brut réalisé en $
    costs: float = 0.0               # frais cumulés en $
    initial_risk: float | None = None  # risque en $ à l'entrée (quantité × R), base des multiples de R
    exit_date: date | None = None
    close_reason: str | None = None
    events: list[Event] = field(default_factory=list)


def plan(signal_close: float, atr: float, r: Rules = Rules()) -> Position:
    return Position(signal_close=signal_close, atr=atr, entry_min=signal_close - r.invalidation_atr * atr,
                    entry_max=signal_close * (1 + r.entry_gap_max), stop_offset=r.stop_atr * atr)


# taille : (prix d'entrée, distance du stop) -> (quantité, motif de refus ou None)
SizeFn = Callable[[float, float], tuple[float, str | None]]


def _ev(p: Position, d: date, kind: str, price: float | None, shares: float | None, detail: str) -> Event:
    e = Event(d, kind, price, shares, detail)
    p.events.append(e)
    return e


def _sell(p: Position, d: date, price: float, shares: float, r: Rules, kind: str, why: str) -> None:
    p.realized += (price - p.entry_price) * shares
    p.costs += price * shares * r.cost_bps / 1e4
    p.shares -= shares
    _ev(p, d, kind, price, shares, why)
    if p.shares <= 1e-9:
        p.shares, p.status, p.exit_date, p.close_reason = 0.0, "CLOSED", d, why


def _apply_split(p: Position, b: Bar) -> None:
    f = b.split
    for a in ("signal_close", "atr", "entry_min", "entry_max", "stop_offset", "entry_price", "stop", "tp1", "tp2",
              "highest_close"):
        v = getattr(p, a)
        if v is not None:
            setattr(p, a, v / f)
    if p.status == "OPEN":
        p.shares *= f
    _ev(p, b.date, "SPLIT", f, p.shares if p.status == "OPEN" else None,
        f"split {f:g}:1 : quantité et niveaux par action ajustés")


def step(p: Position, b: Bar, size: SizeFn, r: Rules = Rules()) -> list[Event]:
    """Applique une séance. Modifie `p` et renvoie les événements de la séance."""
    n0 = len(p.events)
    if p.status not in ("PLANNED", "OPEN"):
        return []
    if b.split and b.split != 1.0:
        _apply_split(p, b)
    slip = r.slippage_bps / 1e4
    entered_today = False
    if p.status == "PLANNED":
        o = b.open
        if o > p.entry_max:
            why = f"gap d'ouverture à {o:.2f} $ au-dessus de la zone (max {p.entry_max:.2f})"
        elif o < p.entry_min:
            why = f"ouverture à {o:.2f} $ sous le niveau d'invalidation ({p.entry_min:.2f})"
        else:
            fill = o * (1 + slip)
            shares, why = size(fill, p.stop_offset)
            if why is None and shares < 1:
                why = "taille calculée inférieure à une action"
        if why is not None:
            p.status, p.close_reason = "CANCELLED", why
            _ev(p, b.date, "CANCELLED", o, None, why)
            return p.events[n0:]
        p.status, p.entry_date, p.entry_price, p.shares = "OPEN", b.date, fill, float(shares)
        p.stop, p.tp1, p.tp2 = fill - p.stop_offset, fill + r.tp1_r * p.stop_offset, fill + r.tp2_r * p.stop_offset
        p.highest_close, p.initial_risk = fill, shares * p.stop_offset
        p.costs += fill * shares * r.cost_bps / 1e4
        _ev(p, b.date, "OPENED", fill, float(shares),
            f"Achat simulé de {shares:g} actions à {fill:.2f} $ (ouverture {o:.2f} + glissement) ; "
            f"stop {p.stop:.2f}, TP1 {p.tp1:.2f}, TP2 {p.tp2:.2f}")
        entered_today = True
    elif p.tp1_done and p.stop < p.entry_price:
        p.stop = p.entry_price          # TP1 atteint à une séance précédente : stop au prix d'entrée
        _ev(p, b.date, "STOP_MOVED", p.stop, p.shares, f"stop remonté au prix d'entrée ({p.stop:.2f} $) après TP1")

    if not entered_today and b.open <= p.stop:
        px = b.open * (1 - slip)
        _sell(p, b.date, px, p.shares, r, "CLOSED", f"stop : ouverture en gap à {b.open:.2f} $ sous le stop {p.stop:.2f}")
    elif b.low <= p.stop:
        px = p.stop * (1 - slip)
        label = "stop au prix d'entrée après TP1" if p.tp1_done and abs(p.stop - p.entry_price) < 1e-9 else "stop"
        _sell(p, b.date, px, p.shares, r, "CLOSED", f"{label} touché à {p.stop:.2f} $")
    else:
        if not p.tp1_done and b.high >= p.tp1:
            qty = math.floor(p.shares * r.tp1_fraction) or p.shares
            px = max(p.tp1, b.open) if not entered_today else p.tp1
            _sell(p, b.date, px, qty, r, "TP1", f"TP1 atteint : vente de {qty:g} actions à {px:.2f} $ ; "
                                                   f"stop au prix d'entrée à partir de la séance suivante")
            p.tp1_done = True
        if p.status == "OPEN" and b.high >= p.tp2:
            px = max(p.tp2, b.open) if not entered_today else p.tp2
            _sell(p, b.date, px, p.shares, r, "CLOSED", f"TP2 atteint à {px:.2f} $")
    if p.status == "OPEN":
        p.sessions_held += 1
        p.highest_close = max(p.highest_close, b.close)
        trail = p.highest_close - r.trail_atr * p.atr
        if trail > p.stop + 1e-9:
            p.stop = trail
            _ev(p, b.date, "STOP_MOVED", trail, p.shares, f"stop suiveur relevé à {trail:.2f} $")
        if p.sessions_held >= r.max_sessions:
            _sell(p, b.date, b.close * (1 - slip), p.shares, r, "CLOSED",
                  f"durée maximale de {r.max_sessions} séances : vente à la clôture")
    return p.events[n0:]


def risk_size(equity: float, risk_fraction: float, room: float) -> SizeFn:
    """Taille au risque : quantité = (capital × risque) ÷ distance du stop, plafonnée par l'exposition restante."""
    def size(fill: float, stop_offset: float) -> tuple[float, str | None]:
        shares = math.floor(equity * risk_fraction / stop_offset) if stop_offset > 0 else 0
        capped = min(shares, math.floor(max(room, 0.0) / fill))
        if capped < 1:
            return 0, "exposition maximale atteinte (pas de levier)" if shares >= 1 else "risque par position trop faible pour une action"
        return capped, None
    return size
