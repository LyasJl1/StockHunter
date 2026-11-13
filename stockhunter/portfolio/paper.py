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
from stockhunter.data.calendar import sessions_between
from stockhunter.db.session import session_scope
from stockhunter.trading.rules import Bar, Position, Rules, risk_size, step


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
    slippage_bps: float = 5.0             # glissement défavorable sur ordres au marché et stops
    max_correlation: float = 0.80         # refus si corrélation 60 j avec une position ouverte au-delà

    def rules(self) -> Rules:
        return Rules(stop_atr=self.stop_atr, trail_atr=self.trail_atr, entry_gap_max=self.entry_gap_max,
                     invalidation_atr=self.invalidation_atr, tp1_r=self.tp1_r, tp1_fraction=self.tp1_fraction,
                     tp2_r=self.tp2_r, max_sessions=self.max_sessions, cost_bps=self.cost_bps,
                     slippage_bps=self.slippage_bps)


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


def to_position(t: PaperTrade) -> Position:
    return Position(signal_close=t.signal_close, atr=t.atr_at_signal, entry_min=t.entry_min, entry_max=t.entry_max,
                    stop_offset=t.stop_initial_offset, status=t.status, entry_date=t.entry_date,
                    entry_price=t.entry_price, shares=t.shares or 0.0, stop=t.stop, tp1=t.tp1, tp2=t.tp2,
                    tp1_done=bool(t.tp1_done), highest_close=t.highest_close, sessions_held=t.sessions_held or 0,
                    realized=t.realized_pnl or 0.0, costs=t.costs or 0.0, initial_risk=t.initial_risk,
                    exit_date=t.exit_date, close_reason=t.close_reason)


def _write_back(t: PaperTrade, p: Position) -> None:
    t.signal_close, t.atr_at_signal, t.entry_min, t.entry_max = p.signal_close, p.atr, p.entry_min, p.entry_max
    t.stop_initial_offset, t.status, t.entry_date, t.entry_price = p.stop_offset, p.status, p.entry_date, p.entry_price
    t.shares, t.stop, t.tp1, t.tp2, t.tp1_done = p.shares, p.stop, p.tp1, p.tp2, p.tp1_done
    t.highest_close, t.sessions_held, t.realized_pnl, t.costs = p.highest_close, p.sessions_held, p.realized, p.costs
    t.initial_risk, t.exit_date, t.close_reason = p.initial_risk, p.exit_date, (p.close_reason or "")[:200] or None


def _correlation(s, a: int, b: int, until: date, window: int = 60) -> float | None:
    """Corrélation des rendements journaliers (clôtures ajustées) sur les `window` dernières séances communes."""
    def rets(iid):
        rows = s.execute(select(PriceBar.bar_date, func.coalesce(PriceBar.adj_close, PriceBar.close))
                         .where(PriceBar.instrument_id == iid, PriceBar.bar_date < until)
                         .order_by(PriceBar.bar_date.desc()).limit(window + 1)).all()
        return {d: c for d, c in rows}
    ra, rb = rets(a), rets(b)
    days = sorted(set(ra) & set(rb))
    if len(days) < 30:
        return None
    xa = [ra[d1] / ra[d0] - 1 for d0, d1 in zip(days, days[1:])]
    xb = [rb[d1] / rb[d0] - 1 for d0, d1 in zip(days, days[1:])]
    n = len(xa)
    ma, mb = sum(xa) / n, sum(xb) / n
    va, vb = sum((x - ma) ** 2 for x in xa), sum((x - mb) ** 2 for x in xb)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(xa, xb)) / math.sqrt(va * vb)


def _sizer(s, t: PaperTrade, inst: Instrument, d: date, cfg: PaperConfig):
    """Contraintes de portefeuille évaluées au moment de l'entrée ; chaque refus est motivé (journal de décision)."""
    def size(fill: float, stop_offset: float):
        st = portfolio_state(s, cfg)
        if len(st["open"]) >= cfg.max_positions:
            return 0, f"limite de {cfg.max_positions} positions atteinte"
        others = [(x, s.get(Instrument, x.instrument_id)) for x in st["open"]]
        if inst.sector_etf and sum(1 for _x, i in others if i.sector_etf == inst.sector_etf) >= cfg.max_per_sector:
            return 0, f"limite de {cfg.max_per_sector} positions dans le secteur {inst.sector_etf}"
        for x, i in others:
            c = _correlation(s, t.instrument_id, x.instrument_id, d)
            if c is not None and c > cfg.max_correlation:
                return 0, (f"trop corrélée à la position {i.symbol} (corrélation 60 j {c:.2f} > "
                           f"{cfg.max_correlation:.2f})")
        room = st["equity_basis"] * cfg.max_exposure - st["invested"]
        return risk_size(st["equity_basis"], t.risk_fraction, room)(fill, stop_offset)
    return size


STALE_SESSIONS = 10      # sans cotation pendant 10 séances : position valorisée au dernier cours et close
PLAN_VALID_SESSIONS = 5  # un plan non exécuté faute de cotation est annulé après 5 séances


def process(engine: Engine, cfg: PaperConfig = DEFAULT_PAPER) -> dict[str, int]:
    """Applique les séances nouvelles avec les règles partagées `stockhunter.trading.rules`.

    Ordre strictement chronologique, séance par séance pour TOUT le portefeuille (jamais un trade entier avant
    le suivant) : le résultat est le même qu'on traite une séance par soir ou dix d'un coup. Dans une séance, les
    entrées (à l'ouverture) sont décidées AVANT la gestion des positions ouvertes : une place ou du capital
    libéré en cours de séance ne sert pas à une entrée faite à l'ouverture du même jour."""
    counts = {"ouvertes": 0, "annulees": 0, "fermees": 0, "tp1": 0}
    rules = cfg.rules()
    with session_scope(engine) as s:
        trades = list(s.scalars(select(PaperTrade).where(PaperTrade.status.in_(("PLANNED", "OPEN")))
                                .order_by(PaperTrade.signal_date, PaperTrade.id)))
        insts = {t.id: s.get(Instrument, t.instrument_id) for t in trades}
        pending: dict[int, dict] = {}
        for t in trades:
            rows = _bars_after(s, t.instrument_id, t.last_processed or t.signal_date)
            pending[t.id] = {b.bar_date: b for b in rows
                             if None not in (b.open, b.high, b.low, b.close)}     # barre incomplète : ignorée

        def apply(t: PaperTrade, b) -> None:
            pos = to_position(t)
            bar = Bar(b.bar_date, b.open, b.high, b.low, b.close, b.split_factor or 1.0)
            for ev in step(pos, bar, _sizer(s, t, insts[t.id], b.bar_date, cfg), rules):
                _event(s, t, ev.date, ev.kind, ev.price, ev.shares, ev.detail)
                key = {"OPENED": "ouvertes", "CANCELLED": "annulees", "TP1": "tp1"}.get(ev.kind)
                if key:
                    counts[key] += 1
            _write_back(t, pos)
            t.last_processed = b.bar_date
            if t.status == "CLOSED":
                counts["fermees"] += 1
            s.flush()

        for d in sorted({d for bars in pending.values() for d in bars}):
            todays = [t for t in trades if d in pending[t.id] and t.status in ("PLANNED", "OPEN")]
            was_open = {t.id for t in todays if t.status == "OPEN"}
            for t in todays:                                   # 1) entrées à l'ouverture
                if t.id not in was_open:
                    apply(t, pending[t.id][d])
            for t in todays:                                   # 2) gestion des positions déjà ouvertes
                if t.id in was_open:
                    apply(t, pending[t.id][d])

        # titres sans cotation (suspension, radiation) : ne pas laisser un plan ou une position en suspens
        latest = s.scalar(select(func.max(PriceBar.bar_date)))
        for t in trades:
            if t.status not in ("PLANNED", "OPEN") or latest is None:
                continue
            ref = t.last_processed or t.signal_date
            gap = sessions_between(ref, latest)
            if t.status == "PLANNED" and gap > PLAN_VALID_SESSIONS:
                t.status, t.close_reason = "CANCELLED", f"aucune cotation pendant {PLAN_VALID_SESSIONS} séances après le signal"
                _event(s, t, latest, "CANCELLED", None, None, t.close_reason)
                counts["annulees"] += 1
            elif t.status == "OPEN" and gap > STALE_SESSIONS:
                px = s.scalar(select(PriceBar.close).where(PriceBar.instrument_id == t.instrument_id, PriceBar.bar_date <= ref)
                              .order_by(PriceBar.bar_date.desc()).limit(1))
                why = f"plus de cotation depuis {STALE_SESSIONS} séances : sortie au dernier cours connu"
                t.realized_pnl += (px - t.entry_price) * t.shares
                t.costs += px * t.shares * cfg.cost_bps / 1e4
                _event(s, t, latest, "CLOSED", px, t.shares, why)
                t.shares, t.status, t.exit_date, t.close_reason = 0.0, "CLOSED", latest, why
                counts["fermees"] += 1
    return counts


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
        r_mult = [(t.realized_pnl - t.costs) / t.initial_risk for t in closed if t.initial_risk]
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
            held, entry, cost = 0.0, None, 0.0
            for ev in (e for e in events if e.trade_id == t.id and e.event_date <= d):
                if ev.kind == "OPENED":
                    held, entry = ev.shares, ev.price
                    cost += ev.price * ev.shares * cfg.cost_bps / 1e4
                elif ev.kind == "SPLIT" and entry is not None and ev.price:
                    held, entry = held * ev.price, entry / ev.price      # ev.price = facteur du split
                elif ev.kind in ("TP1", "CLOSED") and ev.shares and entry is not None:
                    realized += (ev.price - entry) * ev.shares
                    cost += ev.price * ev.shares * cfg.cost_bps / 1e4
                    held -= ev.shares
            realized -= cost
            if held > 1e-9:
                known = [dd for dd in closes[t.id] if dd <= d]             # dernier cours connu (jour sans cotation)
                if known:
                    mtm += (closes[t.id][max(known)] - entry) * held
        out.append({"date": d.isoformat(), "equity": cfg.capital + realized + mtm, "spy": spy_close.get(d)})
    return out


def risk_stats(engine: Engine, cfg: PaperConfig = DEFAULT_PAPER) -> dict:
    """Risque et comparaison du portefeuille simulé, à partir de sa courbe de capital (transactions simulées) :
    rendement, drawdown maximal, volatilité, rendement du SPY sur la même période. Rien si moins de 2 séances."""
    import math
    curve = equity_curve(engine, cfg)
    if len(curve) < 2:
        return {"seances": len(curve)}
    eq = [c["equity"] for c in curve]
    peak, dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    rets = [b / a - 1 for a, b in zip(eq, eq[1:]) if a]
    vol = (sum((r - sum(rets) / len(rets)) ** 2 for r in rets) / max(1, len(rets) - 1)) ** 0.5 * math.sqrt(252)
    spy = [c["spy"] for c in curve if c["spy"] is not None]
    return {"seances": len(curve), "debut": curve[0]["date"], "fin": curve[-1]["date"],
            "rendement": eq[-1] / eq[0] - 1, "drawdown_max": dd, "volatilite_annualisee": vol,
            "spy": (spy[-1] / spy[0] - 1) if len(spy) >= 2 else None}
