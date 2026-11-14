"""Portefeuille mondial SIMULÉ (3.2.1) : moteur ÉVÉNEMENTIEL horodaté, sans information future.

Aucun ordre réel. Rejoue, avec les règles de trading partagées (`trading/rules.py`, identiques au paper trading US
et aux backtests), les sélections figées de chaque place sur leurs séances suivantes.

Pourquoi 3.2.1 : le simulateur 3.2 décidait les entrées « à l'ouverture » avec la valeur du portefeuille, les
expositions et les corrélations calculées sur les CLÔTURES DU JOUR (y compris celles de places pas encore
ouvertes), et convertissait au taux BCE du jour, publié à 16 h à Francfort, même pour un ordre passé à Mumbai à
4 h UTC. Ces informations n'existaient pas au moment de la décision (audit indépendant du 14/11/2026).

Modèle temporel (tous les instants en UTC, calendriers exchange_calendars par place, heures d'été comprises) :
- Événements : OUVERTURE et CLÔTURE de chaque séance de chaque place, VALORISATION à 23:59:59 UTC chaque jour.
  À instant égal : clôtures, puis ouvertures, puis valorisation ; ensuite par code de place, puis par symbole.
- Information disponible à l'instant t :
  * clôture d'une séance : connue à partir de son heure de clôture (une place encore ouverte n'apporte rien) ;
  * ouverture d'une séance : connue à son heure d'ouverture (prix d'exécution) ;
  * plus haut, plus bas, clôture du jour : connus seulement à la clôture ;
  * taux BCE daté D : connu à partir de 16 h (Francfort) le jour D ;
  * sélection d'une séance : connue à sa clôture + délai de publication de la place (chaîne nocturne).
- À l'OUVERTURE d'une place : pour chaque plan en attente déjà connu, décision d'entrée avec les seules
  informations disponibles (capital = trésorerie + positions au dernier cours connu, une position ouverte le
  jour même valant son prix d'entrée tant qu'aucune clôture postérieure n'est connue ; expositions, corrélations
  sur des rendements dont la clôture est passée ; change connu). La zone d'entrée est vérifiée sur la seule
  ouverture. La trésorerie est débitée immédiatement (montant + courtage + change) : aucune position ne peut
  consommer plus que la trésorerie disponible à cet instant.
- À la CLÔTURE : la séance complète est rejouée par `trading.rules.step` avec la quantité décidée à l'ouverture
  (stop, TP1, TP2 et stop suiveur dans la séance, mêmes hypothèses défavorables que le paper trading US) ; les
  sorties sont converties au taux connu à la clôture.

Conventions inchangées : risque 0,5 % par position, plafonds pays / devise / secteur / ligne / corrélation,
courtage 10 pb + glissement 5 pb (règles partagées), change 25 pb par conversion, dividendes NON inclus,
divisions via des cours ajustés, plan annulé sans cotation pendant 5 séances, position sans cotation pendant 10
séances clôturée au dernier cours connu. Performance SIMULÉE : jamais mélangée à une performance réelle.
"""

from __future__ import annotations

import bisect
import copy
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from stockhunter.trading.rules import Bar, Position, Rules, _sell, plan, step
from stockhunter.world import markets as mk
from stockhunter.world.fx import FxTable, FxUnavailable, ecb_known_date

ENGINE = "événementiel horodaté 3.2.1"
CLOSE, OPEN, VALUATION = 0, 1, 2          # ordre de traitement à instant égal


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


@dataclass(eq=False)                # identité d'objet : deux plans identiques restent distincts
class Holding:
    sig: SignalIn
    pos: Position
    known_at: datetime | None = None       # instant où la sélection est connue
    waited: int = 0
    unseen: int = 0
    shares_decided: float = 0.0
    entry_session: date | None = None
    fill: float | None = None
    last_close: float | None = None
    fx_fees: float = 0.0
    commissions: float = 0.0
    cost_base: float = 0.0                 # décaissé à l'entrée (montant + courtage + change), devise de base
    proceeds_base: float = 0.0             # encaissé aux sorties (net de courtage et de change)


@dataclass
class Result:
    equity: pd.Series
    trades: list[dict]
    refused: list[dict]
    summary: dict[str, Any]
    exposures: dict[str, dict[str, float]] = field(default_factory=dict)
    decisions: list[dict] = field(default_factory=list)   # journal : instant, information utilisée, décision


class _Prices:
    """Cours d'un titre avec l'instant UTC de publication de chaque clôture (pour « ce qui est connu à t »)."""

    def __init__(self, market: str, df: pd.DataFrame):
        self.market = market
        self.df = df.sort_index()
        times = session_times(market, [d.date() for d in self.df.index])
        self.close_t = [times[d.date()][1] for d in self.df.index]
        self.open_t = [times[d.date()][0] for d in self.df.index]
        self.closes = self.df["close"].to_numpy(dtype=float)

    def known_close(self, t: datetime, since: date | None = None) -> float | None:
        i = bisect.bisect_right(self.close_t, t) - 1
        while i >= 0 and not np.isfinite(self.closes[i]):
            i -= 1
        if i < 0 or (since is not None and self.df.index[i].date() < since):
            return None
        return float(self.closes[i])

    def known_history(self, t: datetime, n: int) -> tuple[list[datetime], np.ndarray, list[date]]:
        i = bisect.bisect_right(self.close_t, t)
        lo = max(0, i - n)
        return self.close_t[lo:i], self.closes[lo:i], [d.date() for d in self.df.index[lo:i]]

    def bar(self, d: date):
        ts = pd.Timestamp(d)
        if ts not in self.df.index:
            return None
        b = self.df.loc[ts]
        if not (np.isfinite(b["open"]) and np.isfinite(b["close"])):
            return None
        return b


def session_times(market: str, days: list[date]) -> dict[date, tuple[datetime, datetime]]:
    """(ouverture, clôture) UTC de chaque séance : calendrier de la place, sinon horaires habituels (signalé)."""
    m = mk.get(market)
    if not days:
        return {}
    cal = {s.session: (s.open_utc, s.close_utc) for s in mk.sessions(m, min(days), max(days))}
    tz = ZoneInfo(m.tz)
    out = {}
    for d in days:
        if d in cal and cal[d][0] is not None:
            out[d] = cal[d]
        else:                                    # séance hors calendrier (donnée un jour férié) : horaires usuels
            out[d] = (datetime.combine(d, m.open_local or time(9, 0), tz).astimezone(timezone.utc),
                      datetime.combine(d, m.close_local, tz).astimezone(timezone.utc))
    return out


def simulate(signals: list[SignalIn], bars: dict[tuple[str, str], pd.DataFrame], fx: FxTable,
             cfg: GlobalConfig = GlobalConfig(), rules: Rules = Rules()) -> Result:
    """`bars[(place, clé)]` : DataFrame indexé par date (Timestamp) avec open/high/low/close en devise locale,
    ajustés des divisions, uniquement les séances où le titre a coté. Déterministe."""
    if not signals:
        return Result(pd.Series(dtype=float), [], [], {"statut": "aucune donnée", "moteur": ENGINE})
    prices = {k: _Prices(k[0], df) for k, df in bars.items() if len(df)}
    first = min(s.as_of for s in signals)
    # ---------------------------------------------------------------- calendrier des événements
    events: list[tuple] = []
    sessions_of: dict[str, list[date]] = {}
    for (m, _), p in prices.items():
        sessions_of.setdefault(m, set()).update(d.date() for d in p.df.index if d.date() >= first)
    for m in list(sessions_of):
        days = sorted(sessions_of[m])
        sessions_of[m] = days
        for d, (o, c) in session_times(m, days).items():
            events.append((o, OPEN, m, d))
            events.append((c, CLOSE, m, d))
    all_days = sorted({e[0].date() for e in events} | {first})
    for d in all_days:
        events.append((datetime.combine(d, time(23, 59, 59), timezone.utc), VALUATION, "", d))
    events.sort(key=lambda e: (e[0], e[1], e[2], e[3]))

    sig_close_t = {}
    for s in signals:
        if s.market not in sessions_of and (s.market, s.key) not in prices:
            continue
        ct = session_times(s.market, [s.as_of])[s.as_of][1]
        sig_close_t[id(s)] = ct + timedelta(minutes=mk.get(s.market).delay_min)
    waiting = sorted([s for s in signals if id(s) in sig_close_t],
                     key=lambda s: (sig_close_t[id(s)], -s.norm, s.market, s.ticker))
    wi = 0
    pending: list[Holding] = []
    entering: list[Holding] = []
    open_: list[Holding] = []
    trades, refused, decisions = [], [], []
    cash = cfg.capital
    fees = {"courtage": 0.0, "change": 0.0}
    equity_pts: dict[pd.Timestamp, float] = {}
    conv_cache: dict[tuple[str, str], tuple[list[datetime], np.ndarray]] = {}

    def rate(ccy: str, t: datetime) -> float:
        return fx.known_rate(ccy, cfg.base, t)

    def held_value(h: Holding, t: datetime) -> float:
        """Valeur en base au dernier cours connu à t ; position du jour sans clôture connue : prix d'entrée."""
        p = prices.get((h.sig.market, h.sig.key))
        px = p.known_close(t, since=h.entry_session) if p else None
        if px is None:
            px = h.fill if h.fill is not None else (h.last_close or 0.0)
        shares = h.shares_decided if h in entering else h.pos.shares
        return px * shares * rate(h.sig.currency, t)

    def holdings() -> list[Holding]:
        return open_ + entering

    def equity(t: datetime) -> float:
        return cash + sum(held_value(h, t) for h in holdings())

    def exposure(t: datetime, attr: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for h in holdings():
            k = getattr(h.sig, attr) or "N/D"
            out[k] = out.get(k, 0.0) + held_value(h, t)
        return out

    def base_returns(sig: SignalIn, t: datetime) -> pd.Series:
        """Rendements journaliers en base sur les SEULES clôtures passées à t ; chaque clôture convertie au taux
        connu à son heure de clôture."""
        key = (sig.market, sig.key)
        p = prices.get(key)
        if p is None:
            return pd.Series(dtype=float)
        if key not in conv_cache:
            vals = []
            for ct, c in zip(p.close_t, p.closes):
                try:
                    vals.append(c * fx.known_rate(sig.currency, cfg.base, ct))
                except FxUnavailable:
                    vals.append(np.nan)
            conv_cache[key] = (p.close_t, np.array(vals))
        ts, vals = conv_cache[key]
        i = bisect.bisect_right(ts, t)
        lo = max(0, i - cfg.corr_window - 1)
        days = [d.date() for d in p.df.index[lo:i]]
        return pd.Series(vals[lo:i], index=days).pct_change().dropna()

    def caps(h: Holding, eq: float, t: datetime) -> str | None:
        if len(holdings()) >= cfg.max_positions:
            return f"nombre maximal de positions ({cfg.max_positions}) atteint"
        nominal = eq * cfg.max_weight
        if exposure(t, "country").get(h.sig.country, 0.0) + nominal > eq * cfg.max_country + 1e-9:
            return f"exposition maximale au pays {h.sig.country} ({cfg.max_country:.0%})"
        if (h.sig.currency != cfg.base
                and exposure(t, "currency").get(h.sig.currency, 0.0) + nominal > eq * cfg.max_currency + 1e-9):
            return f"exposition maximale à la devise {h.sig.currency} ({cfg.max_currency:.0%})"
        if h.sig.sector and exposure(t, "sector").get(h.sig.sector, 0.0) + nominal > eq * cfg.max_sector + 1e-9:
            return f"exposition maximale au secteur {h.sig.sector} ({cfg.max_sector:.0%})"
        r_new = base_returns(h.sig, t)
        for o in holdings():
            r_o = base_returns(o.sig, t)
            common = r_new.index.intersection(r_o.index)
            if len(common) >= cfg.corr_min_common:
                c = float(np.corrcoef(r_new.loc[common], r_o.loc[common])[0, 1])
                if np.isfinite(c) and c > cfg.max_corr:
                    return f"corrélation {c:.2f} > {cfg.max_corr:.2f} avec {o.sig.ticker} ({o.sig.market})"
        return None

    def book(h: Holding, ev, t: datetime) -> None:
        """Flux de trésorerie d'une sortie (TP1, clôture) au taux connu à t."""
        nonlocal cash
        amount = ev.price * ev.shares
        r = rate(h.sig.currency, t)
        gross, comm = amount * r, amount * rules.cost_bps / 1e4 * r
        fxf = gross * cfg.fx_cost_bps / 1e4 if h.sig.currency != cfg.base else 0.0
        cash += gross - comm - fxf
        h.proceeds_base += gross - comm - fxf
        h.commissions += comm
        h.fx_fees += fxf
        fees["courtage"] += comm
        fees["change"] += fxf

    def finish(h: Holding) -> None:
        trades.append(_trade(h))

    for t, kind, m, d in events:
        if kind == VALUATION:
            try:
                equity_pts[pd.Timestamp(d)] = equity(t)
            except FxUnavailable:
                pass
            continue
        # sélections devenues connues (clôture + délai de publication) : plans en attente
        while wi < len(waiting) and sig_close_t[id(waiting[wi])] <= t:
            s = waiting[wi]
            wi += 1
            if any((x.sig.market, x.sig.key) == (s.market, s.key) for x in pending + holdings()):
                continue
            pending.append(Holding(sig=s, pos=plan(s.signal_close, s.atr, rules), known_at=sig_close_t[id(s)]))
        if kind == OPEN:
            todays = [h for h in pending if h.sig.market == m and h.known_at <= t]
            todays.sort(key=lambda h: (-h.sig.norm, h.sig.ticker))
            n_new = 0
            for h in todays:
                p = prices.get((m, h.sig.key))
                b = p.bar(d) if p else None
                if b is None:
                    h.waited += 1
                    if h.waited >= cfg.plan_ttl_sessions:
                        pending.remove(h)
                        refused.append(_ref(h, d, "aucune cotation dans les 5 séances suivant le signal"))
                    continue
                pending.remove(h)
                if n_new >= cfg.max_new_per_day:
                    refused.append(_ref(h, d, f"plus de {cfg.max_new_per_day} entrées sur la place ce jour"))
                    continue
                try:
                    eq = equity(t)
                    unit = rate(h.sig.currency, t)
                except FxUnavailable as exc:
                    refused.append(_ref(h, d, f"change inconnu : {exc}"))
                    continue
                why = caps(h, eq, t)
                o = float(b["open"])

                def size(fill: float, off: float, why=why, eq=eq, unit=unit, h=h):
                    if why:
                        return 0, why
                    risk_shares = eq * cfg.risk_per_position / (off * unit) if off > 0 else 0
                    budget = min(eq * cfg.max_weight,
                                 cash / (1 + (rules.cost_bps + cfg.fx_cost_bps) / 1e4))
                    n = math.floor(min(risk_shares, budget / (fill * unit)) / h.sig.lot) * h.sig.lot
                    if n < 1:
                        return 0, ("trésorerie ou poids maximal insuffisant pour une quotité"
                                   if risk_shares >= h.sig.lot else "risque par position trop faible pour une quotité")
                    return n, None

                # décision sur la SEULE ouverture : copie du plan, séance réduite à son prix d'ouverture
                probe = copy.deepcopy(h.pos)
                step(probe, Bar(d, o, o, o, o), size, rules)
                decisions.append({"instant": t.isoformat(), "place": m, "ticker": h.sig.ticker, "seance": str(d),
                                  "capital_connu": round(eq, 2), "taux": unit, "taux_bce_du": str(ecb_known_date(t)),
                                  "decision": "entrée" if probe.status != "CANCELLED" else probe.close_reason,
                                  "quantite": probe.shares if probe.status != "CANCELLED" else 0})
                if probe.status == "CANCELLED":
                    refused.append(_ref(h, d, probe.close_reason))
                    continue
                h.shares_decided, h.fill, h.entry_session = probe.shares, probe.entry_price, d
                gross = h.fill * h.shares_decided * unit
                comm = gross * rules.cost_bps / 1e4
                fxf = gross * cfg.fx_cost_bps / 1e4 if h.sig.currency != cfg.base else 0.0
                cash -= gross + comm + fxf
                h.cost_base += gross + comm + fxf
                h.commissions += comm
                h.fx_fees += fxf
                fees["courtage"] += comm
                fees["change"] += fxf
                entering.append(h)
                n_new += 1
            continue
        # ------------------------------------------------------------ CLÔTURE de la place m, séance d
        for h in [h for h in entering if h.sig.market == m and h.entry_session == d]:
            b = prices[(m, h.sig.key)].bar(d)
            evs = step(h.pos, Bar(d, float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])),
                       lambda fill, off, n=h.shares_decided: (n, None), rules)
            entering.remove(h)
            assert h.pos.entry_price is not None and abs(h.pos.entry_price - h.fill) < 1e-9
            for ev in evs:
                if ev.kind in ("TP1", "CLOSED"):
                    book(h, ev, t)
            h.last_close = float(b["close"])
            if h.pos.status == "OPEN":
                open_.append(h)
            else:
                finish(h)
        for h in [h for h in open_ if h.sig.market == m and h.entry_session != d]:
            p = prices[(m, h.sig.key)]
            b = p.bar(d)
            if b is None:
                h.unseen += 1
                if h.unseen >= cfg.stale_sessions and h.last_close:
                    n0 = len(h.pos.events)
                    _sell(h.pos, d, h.last_close, h.pos.shares, rules, "CLOSED",
                          "plus de cotation depuis 10 séances (suspension ou radiation) : vente au dernier cours connu")
                    for ev in h.pos.events[n0:]:
                        book(h, ev, t)
                    open_.remove(h)
                    finish(h)
                continue
            h.unseen = 0
            for ev in step(h.pos, Bar(d, float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])),
                           lambda f, o: (0, "déjà ouverte"), rules):
                if ev.kind in ("TP1", "CLOSED"):
                    book(h, ev, t)
            h.last_close = float(b["close"])
            if h.pos.status == "CLOSED":
                open_.remove(h)
                finish(h)
        if cash < -1e-6:
            raise AssertionError(f"trésorerie négative ({cash:.2f}) : incohérence du simulateur")

    eq = pd.Series(equity_pts).sort_index()
    last_t = events[-1][0]
    exp = {}
    if len(eq):
        tot = float(eq.iloc[-1])
        exp = {k: {kk: round(vv / tot, 4) for kk, vv in exposure(last_t, k).items()} for k in ("country", "currency", "sector")}
    return Result(eq, trades, refused, _summary(eq, trades, fees, cfg, open_ + entering, cash), exp, decisions)


def _ref(h: Holding, d: date, why: str | None) -> dict:
    return {"ticker": h.sig.ticker, "place": h.sig.market, "date": str(d), "motif": why}


def _trade(h: Holding) -> dict:
    p = h.pos
    r = (p.realized - p.costs) / p.initial_risk if p.initial_risk else None
    return {"ticker": h.sig.ticker, "place": h.sig.market, "pays": h.sig.country, "devise": h.sig.currency,
            "categorie": h.sig.category, "signal": str(h.sig.as_of), "entree": str(p.entry_date),
            "sortie": str(p.exit_date), "r": None if r is None else round(r, 3), "motif": p.close_reason,
            "quantite": h.shares_decided, "frais_change_base": round(h.fx_fees, 2), "courtage_base": round(h.commissions, 2),
            "pnl_base": round(h.proceeds_base - h.cost_base, 2),
            "rendement": round(h.proceeds_base / h.cost_base - 1, 5) if h.cost_base else None}


def _summary(eq: pd.Series, trades: list[dict], fees: dict, cfg: GlobalConfig, live, cash: float) -> dict[str, Any]:
    if eq.empty:
        return {"statut": "aucune valorisation possible", "moteur": ENGINE}
    ret = eq.pct_change().dropna()
    days = (eq.index[-1] - eq.index[0]).days
    total = float(eq.iloc[-1] / cfg.capital - 1)
    dd = float((eq / eq.cummax() - 1).min())
    rs = [t["r"] for t in trades if t["r"] is not None]
    pnl = [t["pnl_base"] for t in trades]
    gains, losses = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)

    def group(key):
        out = {}
        for t in trades:
            g = out.setdefault(t[key], {"trades": 0, "pnl_base": 0.0, "gagnants": 0})
            g["trades"] += 1
            g["pnl_base"] = round(g["pnl_base"] + t["pnl_base"], 2)
            g["gagnants"] += int(t["pnl_base"] > 0)
        return out
    return {"profit_factor": (gains / losses) if losses > 0 else None,
            "rendement_moyen_par_trade": (sum(t["rendement"] for t in trades if t["rendement"] is not None) / len(trades))
            if trades else None,
            "par_strategie": group("categorie"), "par_pays": group("pays"),
            "devise_base": cfg.base, "capital_initial": cfg.capital, "valeur_finale": round(float(eq.iloc[-1]), 2),
            "tresorerie_finale": round(cash, 2), "rendement_total": total,
            "annualise": (1 + total) ** (365 / days) - 1 if days >= 365 else None,
            "drawdown_max": dd, "volatilite_annuelle": float(ret.std() * math.sqrt(252)) if len(ret) > 20 else None,
            "debut": str(eq.index[0].date()), "fin": str(eq.index[-1].date()), "trades_clos": len(trades),
            "positions_ouvertes": len(live), "gagnants": (sum(r > 0 for r in rs) / len(rs)) if rs else None,
            "r_moyen": (sum(rs) / len(rs)) if rs else None, "frais": {k: round(v, 2) for k, v in fees.items()},
            "moteur": ENGINE, "nature": "SIMULATION (aucun ordre réel)",
            "lecture": "échantillon insuffisant pour conclure" if len(rs) < 30 else "descriptif, non validé"}


def benchmarks(series: dict[str, tuple[pd.Series, str]], fx: FxTable, base: str, start, end) -> dict[str, Any]:
    """Rendement de chaque référence sur LA MÊME période et dans LA MÊME devise que le portefeuille, valorisée
    comme lui en fin de journée UTC (taux BCE du jour, publié à 16 h CET). `series[nom] = (clôtures, devise)`.
    Une référence sans donnée sur la période : N/D."""
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
