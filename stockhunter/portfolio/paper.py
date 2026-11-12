"""Trade Decision Engine — paper trading uniquement (aucun ordre réel, aucun courtier, pas de levier).

Distingue : opportunité (statut READY du scan) -> plan (PLANNED) -> entrée possible (ouverture J+1 dans la
zone) -> position suivie (OPEN) -> sortie (CLOSED) ou abandon (CANCELLED).

Méthode des niveaux (explicite, aucun objectif « inventé ») :
- R = distance du stop = 2 × ATR(14) au signal (même stop que la phase 3) ;
- zone d'entrée : ouverture de J+1 entre clôture − 1×ATR (invalidation) et clôture + 3 % (pas de poursuite de gap) ;
- TP1 = entrée + 1,5 R : vente de la moitié, stop remonté au prix d'entrée ;
- TP2 = entrée + 3 R : vente du solde ;
- stop suiveur = plus haute clôture − 3 × ATR ; sortie au plus tard après 40 séances.
Stop, stop suiveur et durée sont ceux testés en phase 3 ; TP1/TP2 ne l'ont pas été (signalé dans le plan).
Cas ambigu (stop et objectif touchés la même séance) : on suppose le stop d'abord (hypothèse prudente).
Prix : cours réels non ajustés ; un split pendant le trade ajuste quantité et niveaux.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from stockhunter.db.models import (
    Instrument,
    PaperTrade,
    PaperTradeEvent,
    PriceBar,
    ScanRun,
    Score,
)
from stockhunter.db.session import session_scope


@dataclass(frozen=True)
class PaperConfig:
    capital: float = 100_000.0
    risk_fraction: float = 0.005          # 0,5 % du capital risqué par position (stratégies non validées)
    max_positions: int = 8
    max_per_sector: int = 2
    max_exposure: float = 1.0             # 100 % du capital : pas de levier
    stop_atr: float = 2.0
    trail_atr: float = 3.0
    entry_gap_max: float = 0.03
    invalidation_atr: float = 1.0
    tp1_r: float = 1.5
    tp1_fraction: float = 0.5
    tp2_r: float = 3.0
    max_sessions: int = 40
    cost_bps: float = 10.0


DEFAULT_PAPER = PaperConfig()


def _event(s, t: PaperTrade, d: date, kind: str, price: float | None, shares: float | None, detail: str):
    s.add(PaperTradeEvent(trade_id=t.id, event_date=d, kind=kind, price=price, shares=shares, detail=detail))


# =========================================================================== création des plans
def plan_from_scan(engine: Engine, scan_run_id: int | None = None, cfg: PaperConfig = DEFAULT_PAPER) -> list[int]:
    """Crée un plan pour chaque action READY du scan (une seule position ou plan par action)."""
    created = []
    with session_scope(engine) as s:
        run = s.get(ScanRun, scan_run_id) if scan_run_id else \
            s.scalars(select(ScanRun).order_by(ScanRun.as_of.desc(), ScanRun.id.desc()).limit(1)).first()
        if run is None:
            return created
        busy = set(s.scalars(select(PaperTrade.instrument_id).where(PaperTrade.status.in_(("PLANNED", "OPEN")))))
        # un signal donné (scan, action) ne produit jamais deux plans, même après annulation/clôture
        busy |= set(s.scalars(select(PaperTrade.instrument_id).where(PaperTrade.scan_run_id == run.id)))
        for sc in s.scalars(select(Score).where(Score.scan_run_id == run.id, Score.status == "READY")):
            if sc.instrument_id in busy:
                continue
            m = json.loads(sc.metrics_json)
            close, atr_pct = sc.close, m.get("atr14_pct")
            if not close or not atr_pct:
                continue
            atr = close * atr_pct
            active = ",".join(k for k, v in json.loads(sc.strategies_json).items() if v.get("active"))
            plan = {
                "methode": "R = 2×ATR ; TP1 = +1,5 R (moitié) ; TP2 = +3 R ; stop suiveur = plus haute clôture − 3×ATR ; 40 séances max",
                "zone_entree": f"ouverture du {sc.as_of} + 1 séance entre {close - cfg.invalidation_atr * atr:.2f} et "
                               f"{close * (1 + cfg.entry_gap_max):.2f} $",
                "invalidation": f"ouverture < {close - cfg.invalidation_atr * atr:.2f} $ ou > {close * (1 + cfg.entry_gap_max):.2f} $",
                "statut_validation": "règles de stop/durée testées (phase 3) ; TP1/TP2 non testés ; signal non validé (RESEARCH)",
                "config": asdict(cfg),
            }
            t = PaperTrade(instrument_id=sc.instrument_id, scan_run_id=run.id, strategy_version_id=run.strategy_version_id,
                           strategies=active, signal_date=sc.as_of, signal_close=close, atr_at_signal=atr,
                           entry_max=close * (1 + cfg.entry_gap_max), entry_min=close - cfg.invalidation_atr * atr,
                           stop_initial_offset=cfg.stop_atr * atr, risk_fraction=cfg.risk_fraction,
                           plan_json=json.dumps(plan, ensure_ascii=False), status="PLANNED", last_processed=sc.as_of)
            s.add(t)
            s.flush()
            _event(s, t, sc.as_of, "PLANNED", close, None,
                   f"Signal READY ({active}) ; entrée conditionnelle {t.entry_min:.2f}–{t.entry_max:.2f} $, "
                   f"stop à −{t.stop_initial_offset:.2f} $ de l'entrée")
            created.append(t.id)
            busy.add(sc.instrument_id)
    return created


# =========================================================================== état du portefeuille
def portfolio_state(s, cfg: PaperConfig) -> dict[str, Any]:
    realized = s.scalar(select(func.coalesce(func.sum(PaperTrade.realized_pnl - PaperTrade.costs), 0.0))
                        .where(PaperTrade.status.in_(("CLOSED", "OPEN")))) or 0.0
    open_trades = list(s.scalars(select(PaperTrade).where(PaperTrade.status == "OPEN")))
    invested = sum((t.entry_price or 0) * (t.shares or 0) for t in open_trades)
    return {"equity_basis": cfg.capital + realized, "open": open_trades, "invested": invested}


# =========================================================================== suivi quotidien
def _bars_after(s, instrument_id: int, after: date) -> list:
    return list(s.execute(select(PriceBar.bar_date, PriceBar.open, PriceBar.high, PriceBar.low, PriceBar.close,
                                 PriceBar.split_factor)
                          .where(PriceBar.instrument_id == instrument_id, PriceBar.bar_date > after)
                          .order_by(PriceBar.bar_date)))


def _sell(s, t: PaperTrade, d: date, price: float, shares: float, cfg: PaperConfig, kind: str, why: str):
    cost = price * shares * cfg.cost_bps / 1e4
    t.realized_pnl += (price - t.entry_price) * shares
    t.costs += cost
    t.shares -= shares
    _event(s, t, d, kind, price, shares, why)
    if t.shares <= 1e-9:
        t.status, t.exit_date, t.close_reason = "CLOSED", d, why


def process(engine: Engine, cfg: PaperConfig = DEFAULT_PAPER) -> dict[str, int]:
    """Applique les séances nouvelles à chaque plan et position, dans l'ordre chronologique."""
    counts = {"ouvertes": 0, "annulees": 0, "fermees": 0, "tp1": 0}
    with session_scope(engine) as s:
        trades = list(s.scalars(select(PaperTrade).where(PaperTrade.status.in_(("PLANNED", "OPEN")))
                                .order_by(PaperTrade.signal_date, PaperTrade.id)))
        for t in trades:
            inst = s.get(Instrument, t.instrument_id)
            for b in _bars_after(s, t.instrument_id, t.last_processed or t.signal_date):
                if t.status not in ("PLANNED", "OPEN"):
                    break
                d = b.bar_date
                f = b.split_factor or 1.0
                if f != 1.0 and t.status == "OPEN":   # split pendant le trade
                    t.shares *= f
                    for attr in ("entry_price", "stop", "tp1", "tp2", "highest_close"):
                        setattr(t, attr, getattr(t, attr) / f)
                    _event(s, t, d, "STOP_MOVED", t.stop, t.shares, f"split {f:g}:1 : quantité et niveaux ajustés")
                if t.status == "PLANNED":
                    _try_open(s, t, inst, b, cfg, counts)
                    if t.status != "OPEN":
                        t.last_processed = d
                        continue
                _manage(s, t, b, cfg, counts)
                t.last_processed = d
    return counts


def _try_open(s, t: PaperTrade, inst: Instrument, b, cfg: PaperConfig, counts: dict):
    d, o = b.bar_date, b.open
    if o > t.entry_max:
        t.status, t.close_reason = "CANCELLED", f"gap d'ouverture à {o:.2f} $ au-dessus de la zone (max {t.entry_max:.2f})"
    elif o < t.entry_min:
        t.status, t.close_reason = "CANCELLED", f"ouverture à {o:.2f} $ sous le niveau d'invalidation ({t.entry_min:.2f})"
    else:
        st = portfolio_state(s, cfg)
        same_sector = sum(1 for x in st["open"] if s.get(Instrument, x.instrument_id).sector_etf == inst.sector_etf)
        risk_amount = st["equity_basis"] * t.risk_fraction
        shares = math.floor(risk_amount / t.stop_initial_offset)
        room = st["equity_basis"] * cfg.max_exposure - st["invested"]
        shares = min(shares, math.floor(max(room, 0) / o))
        if len(st["open"]) >= cfg.max_positions:
            t.status, t.close_reason = "CANCELLED", f"limite de {cfg.max_positions} positions atteinte"
        elif inst.sector_etf and same_sector >= cfg.max_per_sector:
            t.status, t.close_reason = "CANCELLED", f"limite de {cfg.max_per_sector} positions dans le secteur {inst.sector_etf}"
        elif shares < 1:
            t.status, t.close_reason = "CANCELLED", "exposition maximale atteinte (pas de levier)"
        else:
            r = t.stop_initial_offset
            t.status, t.entry_date, t.entry_price, t.shares = "OPEN", d, o, float(shares)
            t.stop, t.tp1, t.tp2, t.highest_close = o - r, o + cfg.tp1_r * r, o + cfg.tp2_r * r, o
            t.costs += o * shares * cfg.cost_bps / 1e4
            _event(s, t, d, "OPENED", o, float(shares),
                   f"Achat simulé de {shares} actions à {o:.2f} $ (risque {shares * r:.0f} $ = "
                   f"{shares * r / st['equity_basis']:.2%} du capital) ; stop {t.stop:.2f}, TP1 {t.tp1:.2f}, TP2 {t.tp2:.2f}")
            counts["ouvertes"] += 1
            return
    _event(s, t, d, "CANCELLED", o, None, t.close_reason)
    counts["annulees"] += 1


def _manage(s, t: PaperTrade, b, cfg: PaperConfig, counts: dict):
    d = b.bar_date
    held = s.scalar(select(func.count()).select_from(PriceBar).where(
        PriceBar.instrument_id == t.instrument_id, PriceBar.bar_date >= t.entry_date, PriceBar.bar_date <= d))
    if d > t.entry_date and b.open <= t.stop:
        _sell(s, t, d, b.open, t.shares, cfg, "CLOSED", f"stop : ouverture en gap à {b.open:.2f} $ sous le stop {t.stop:.2f}")
    elif b.low <= t.stop:
        why = "stop au prix d'entrée après TP1" if t.tp1_done and abs(t.stop - t.entry_price) < 1e-9 else "stop"
        _sell(s, t, d, t.stop, t.shares, cfg, "CLOSED", f"{why} touché à {t.stop:.2f} $")
    else:
        if not t.tp1_done and b.high >= t.tp1:
            qty = math.floor(t.shares * cfg.tp1_fraction) or t.shares
            _sell(s, t, d, t.tp1, qty, cfg, "TP1", f"TP1 atteint : vente de {qty:g} actions à {t.tp1:.2f} $ ; stop remonté à l'entrée")
            t.tp1_done = True
            t.stop = max(t.stop, t.entry_price)
            counts["tp1"] += 1
        if t.status == "OPEN" and b.high >= t.tp2:
            _sell(s, t, d, t.tp2, t.shares, cfg, "CLOSED", f"TP2 atteint à {t.tp2:.2f} $")
    if t.status == "OPEN":
        t.highest_close = max(t.highest_close or b.close, b.close)
        trail = t.highest_close - cfg.trail_atr * t.atr_at_signal
        if trail > t.stop + 1e-9:
            t.stop = trail
            _event(s, t, d, "STOP_MOVED", trail, t.shares, f"stop suiveur relevé à {trail:.2f} $")
        if held >= cfg.max_sessions:
            _sell(s, t, d, b.close, t.shares, cfg, "CLOSED", f"durée maximale de {cfg.max_sessions} séances : vente à la clôture")
    if t.status == "CLOSED":
        counts["fermees"] += 1


def summary(engine: Engine, cfg: PaperConfig = DEFAULT_PAPER) -> dict[str, Any]:
    """Performance simulée du portefeuille papier (jamais mélangée avec un portefeuille réel)."""
    with session_scope(engine) as s:
        trades = list(s.scalars(select(PaperTrade)))
        closed = [t for t in trades if t.status == "CLOSED"]
        open_ = [t for t in trades if t.status == "OPEN"]
        mtm = 0.0
        positions = []
        for t in open_:
            last = s.execute(select(PriceBar.close, PriceBar.bar_date).where(PriceBar.instrument_id == t.instrument_id)
                             .order_by(PriceBar.bar_date.desc()).limit(1)).first()
            sym = s.get(Instrument, t.instrument_id).symbol
            u = (last.close - t.entry_price) * t.shares if last else 0.0
            mtm += u
            positions.append({"symbol": sym, "entry": t.entry_price, "entry_date": str(t.entry_date), "shares": t.shares,
                              "stop": t.stop, "tp1": t.tp1, "tp2": t.tp2, "tp1_done": t.tp1_done,
                              "last": last.close if last else None, "last_date": str(last.bar_date) if last else None,
                              "unrealized": u})
        realized = sum(t.realized_pnl - t.costs for t in trades if t.status in ("CLOSED", "OPEN"))
        r_mult = [(t.realized_pnl - t.costs) / (t.stop_initial_offset * _initial_shares(s, t)) for t in closed
                  if _initial_shares(s, t)]
        return {"capital": cfg.capital, "equity": cfg.capital + realized + mtm, "realized": realized, "unrealized": mtm,
                "trades_total": len(trades), "planned": sum(t.status == "PLANNED" for t in trades), "open": len(open_),
                "closed": len(closed), "cancelled": sum(t.status == "CANCELLED" for t in trades),
                "win_rate": (sum((t.realized_pnl - t.costs) > 0 for t in closed) / len(closed)) if closed else None,
                "avg_r": (sum(r_mult) / len(r_mult)) if r_mult else None, "positions": positions}


def _initial_shares(s, t: PaperTrade) -> float:
    return s.scalar(select(PaperTradeEvent.shares).where(PaperTradeEvent.trade_id == t.id,
                                                          PaperTradeEvent.kind == "OPENED")) or 0.0


def load_config(engine: Engine) -> PaperConfig:
    """Paramètres du paper trading : valeurs par défaut, surchargées depuis la page Réglages (kv_store)."""
    from stockhunter.db.models import KeyValue
    with session_scope(engine) as s:
        kv = s.get(KeyValue, "paper_config")
        overrides = json.loads(kv.value) if kv else {}
    allowed = {k: v for k, v in overrides.items() if k in PaperConfig.__dataclass_fields__}
    return PaperConfig(**{**asdict(DEFAULT_PAPER), **allowed})


def equity_curve(engine: Engine, cfg: PaperConfig | None = None) -> list[dict[str, Any]]:
    """Valeur quotidienne du portefeuille simulé : capital + réalisé (net de frais) + latent aux clôtures."""
    cfg = cfg or load_config(engine)
    with session_scope(engine) as s:
        trades = list(s.scalars(select(PaperTrade).where(PaperTrade.entry_date.is_not(None))))
        if not trades:
            return []
        events = list(s.scalars(select(PaperTradeEvent).where(PaperTradeEvent.trade_id.in_([t.id for t in trades]))
                                .order_by(PaperTradeEvent.event_date, PaperTradeEvent.id)))
        start = min(t.entry_date for t in trades)
        spy = s.scalar(select(Instrument.id).where(Instrument.symbol == "SPY"))
        days = [d for (d,) in s.execute(select(PriceBar.bar_date).where(PriceBar.instrument_id == spy,
                                                                         PriceBar.bar_date >= start)
                                        .order_by(PriceBar.bar_date))]
        closes: dict[int, dict[date, float]] = {}
        for t in trades:
            closes[t.id] = {d: c for d, c in s.execute(select(PriceBar.bar_date, PriceBar.close).where(
                PriceBar.instrument_id == t.instrument_id, PriceBar.bar_date >= start))}
        spy_close = {d: c for d, c in s.execute(select(PriceBar.bar_date, PriceBar.close).where(
            PriceBar.instrument_id == spy, PriceBar.bar_date >= start))}
    out = []
    for d in days:
        realized, mtm = 0.0, 0.0
        for t in trades:
            evs = [e for e in events if e.trade_id == t.id and e.event_date <= d]
            held, entry, cost = 0.0, t.entry_price, 0.0
            for ev in evs:
                if ev.kind == "OPENED":
                    held = ev.shares
                    cost += ev.price * ev.shares * cfg.cost_bps / 1e4
                elif ev.kind in ("TP1", "CLOSED") and ev.shares:
                    realized += (ev.price - entry) * ev.shares
                    cost += ev.price * ev.shares * cfg.cost_bps / 1e4
                    held -= ev.shares
            realized -= cost
            if held > 1e-9:
                px = closes[t.id].get(d)
                if px is not None:
                    mtm += (px - entry) * held
        out.append({"date": d.isoformat(), "equity": cfg.capital + realized + mtm, "spy": spy_close.get(d)})
    return out
