"""Validation Engine : rejoue le moteur de scoring RÉEL (le même code que le scan quotidien) à des dates
passées, puis mesure ce qui se serait produit ensuite.

Garanties :
- Point-in-time : chaque date rejouée passe par DataStore/inputs_from_store, qui ne voit que les données
  connues à cette date (testé par test_no_look_ahead).
- Exécution réaliste : signal à la clôture J, entrée à l'OUVERTURE J+1 (jamais au prix du signal).
- Prix ajustés (splits + dividendes) : rendements totaux.
- Frais : coût par côté (spread + slippage), avec analyse de sensibilité.
- Comparaisons : SPY, univers équipondéré (même biais de sélection que la stratégie) et règles simples.
- Période finale réservée (holdout) : jamais utilisée pour régler quoi que ce soit.

Limite majeure, documentée dans chaque rapport : l'univers est composé de sociétés qui sont de grandes
capitalisations AUJOURD'HUI (biais du survivant). L'univers équipondéré, qui porte le même biais, est donc
la référence la plus juste : une stratégie n'apporte quelque chose que si elle le bat.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Callable

import numpy as np
import pandas as pd

from stockhunter.scan import BENCHMARK, DataStore, inputs_from_store
from stockhunter.scoring.config import DEFAULT_CONFIG, ScoringConfig
from stockhunter.scoring.engine import score_universe


@dataclass(frozen=True)
class BacktestParams:
    every_n_sessions: int = 5             # fréquence de rejeu (hebdomadaire)
    warmup_sessions: int = 260            # historique minimal avant la 1re date rejouée
    horizons: tuple[int, ...] = (5, 20, 60)
    holdout_start: date = date(2024, 11, 12)   # 24 derniers mois réservés
    embargo_days: int = 90                     # zone tampon avant la période réservée
    cost_bps_per_side: float = 10.0
    cost_sensitivity_bps: tuple[float, ...] = (0.0, 10.0, 25.0)
    # Simulation de trades (règles de sortie testées)
    stop_atr: float = 2.0
    trail_atr: float = 3.0
    max_hold_sessions: int = 40
    # Portefeuille
    max_positions: int = 10
    position_weight: float = 0.10
    # Critères de validation
    min_trades: int = 100
    min_fold_hit_ratio: float = 0.6
    min_tstat: float = 2.0
    top_n: int = 5


# =========================================================================== rejeu
def replay(store: DataStore, cfg: ScoringConfig = DEFAULT_CONFIG, params: BacktestParams = BacktestParams(),
           start: date | None = None, end: date | None = None,
           progress: Callable[[int, int], None] | None = None) -> pd.DataFrame:
    spy = store.bars[BENCHMARK]
    sessions = spy.index[params.warmup_sessions:]
    if start:
        sessions = sessions[sessions >= pd.Timestamp(start)]
    if end:
        sessions = sessions[sessions <= pd.Timestamp(end)]
    dates = sessions[:: params.every_n_sessions]
    cover = coverage_starts(store)
    rows = []
    for k, ts in enumerate(dates):
        as_of = ts.date()
        _, inputs, ctx, _ = inputs_from_store(store, as_of, cfg)
        res = score_universe(inputs, ctx, cfg)
        by = {i.symbol: i for i in inputs}
        for sym, r in res.items():
            t = by[sym].tech
            strat = r.get("strategies", {})
            cs = cover.get(sym)
            rows.append({
                "date": ts, "symbol": sym, "total": r["total"], "status": r["status"],
                "confidence": r["confidence"], "regime": ctx.regime["label"],
                **{f"strat_{c}": bool(strat.get(c, {}).get("active")) for c in "ABCD"},
                "atr_pct": t.get("atr14_pct"), "mom_12_1": t.get("mom_12_1"),
                "events_covered": bool(cs is not None and as_of >= cs),
            })
        if progress:
            progress(k + 1, len(dates))
    return pd.DataFrame(rows)


def coverage_starts(store: DataStore) -> dict[str, date]:
    """Date à partir de laquelle l'historique des 8-K d'une société est connu (+60 jours de marge).
    Avant cette date, « pas de résultats récents » signifie « inconnu », pas « absent »."""
    out = {}
    for sym, fl in store.filings.items():
        d = [f.filed_date for f in fl if f.form in ("8-K", "8-K/A")]
        if d:
            out[sym] = (pd.Timestamp(min(d)) + pd.Timedelta(days=60)).date()
    return out


# =========================================================================== rendements à terme
class PriceBook:
    """Accès positionnel rapide aux prix ajustés."""

    def __init__(self, store: DataStore):
        self.idx: dict[str, pd.DatetimeIndex] = {}
        self.o: dict[str, np.ndarray] = {}
        self.c: dict[str, np.ndarray] = {}
        self.lo: dict[str, np.ndarray] = {}
        for sym, df in store.bars.items():
            if df.empty:
                continue
            self.idx[sym] = df.index
            self.o[sym] = df["adj_open"].to_numpy(float)
            self.c[sym] = df["adj_close"].to_numpy(float)
            self.lo[sym] = df["adj_low"].to_numpy(float)

    def pos_after(self, sym: str, ts: pd.Timestamp) -> int | None:
        """Position de la première séance STRICTEMENT après ts (séance d'entrée)."""
        ix = self.idx.get(sym)
        if ix is None:
            return None
        p = int(ix.searchsorted(ts, side="right"))
        return p if p < len(ix) else None

    def fwd(self, sym: str, ts: pd.Timestamp, h: int) -> float | None:
        p = self.pos_after(sym, ts)
        if p is None or p + h - 1 >= len(self.c[sym]):
            return None
        return float(self.c[sym][p + h - 1] / self.o[sym][p] - 1)


def add_forward_returns(sig: pd.DataFrame, book: PriceBook, horizons: tuple[int, ...]) -> pd.DataFrame:
    sig = sig.copy()
    for h in horizons:
        sig[f"fwd_{h}"] = [book.fwd(s, d, h) for s, d in zip(sig["symbol"], sig["date"])]
        spy = {d: book.fwd(BENCHMARK, d, h) for d in sig["date"].unique()}
        sig[f"spy_{h}"] = sig["date"].map(spy)
        sig[f"xs_spy_{h}"] = sig[f"fwd_{h}"] - sig[f"spy_{h}"]
        ew = sig.groupby("date")[f"fwd_{h}"].transform("mean")   # univers équipondéré, même date
        sig[f"xs_ew_{h}"] = sig[f"fwd_{h}"] - ew
    return sig


# =========================================================================== groupes de signaux
def signal_groups(sig: pd.DataFrame, top_n: int) -> dict[str, pd.Series]:
    ok = sig["status"] != "REJECTED"
    rank = sig[ok].groupby("date")["total"].rank(ascending=False, method="first")
    top = pd.Series(False, index=sig.index)
    top.loc[rank[rank <= top_n].index] = True
    mrank = sig.groupby("date")["mom_12_1"].rank(ascending=False, method="first")
    groups = {
        "READY": sig["status"] == "READY",
        "DEVELOPING ou READY": sig["status"].isin(["READY", "DEVELOPING"]),
        f"Top {top_n} du score": top,
        "A Early momentum": sig["strat_A"],
        "B Earnings momentum": sig["strat_B"] & sig["events_covered"],
        "C Growth acceleration": sig["strat_C"],
        "D Sector rotation": sig["strat_D"],
        # Règles simples de comparaison
        f"Règle simple : top {top_n} momentum 12-1": mrank <= top_n,
        "Référence : toutes les actions (univers EW)": pd.Series(True, index=sig.index),
    }
    return groups


def tstat(x: pd.Series) -> float | None:
    x = x.dropna()
    if len(x) < 3 or x.std() == 0:
        return None
    return float(x.mean() / (x.std() / math.sqrt(len(x))))


def newey_west_tstat(x: pd.Series, lags: int) -> float | None:
    """t-stat de la moyenne avec écart-type de Newey-West (robuste à l'autocorrélation créée par des
    fenêtres de rendement qui se chevauchent)."""
    x = x.dropna().to_numpy(float)
    n = len(x)
    if n < max(10, 2 * lags + 2):
        return None
    if np.std(x) < 1e-12:
        return None  # série constante (ex. référence = univers lui-même) : t-stat non défini
    e = x - x.mean()
    var = e @ e / n
    for k in range(1, lags + 1):
        var += 2 * (1 - k / (lags + 1)) * (e[k:] @ e[:-k]) / n
    if var <= 0:
        return None
    return float(x.mean() / math.sqrt(var / n))


def date_clustered_tstat(df: pd.DataFrame, col: str, horizon: int = 20, every: int = 5) -> float | None:
    """Moyenne par date (signaux d'une même date non indépendants), puis t-stat de Newey-West avec un
    retard égal au chevauchement des fenêtres (horizon / fréquence de rejeu)."""
    per_date = df.groupby("date")[col].mean()
    return newey_west_tstat(per_date, max(1, math.ceil(horizon / every)))


def event_study(sig: pd.DataFrame, groups: dict[str, pd.Series], horizons: tuple[int, ...],
                every: int = 5) -> list[dict]:
    out = []
    for name, mask in groups.items():
        g = sig[mask]
        row: dict[str, Any] = {"groupe": name, "signaux": int(len(g)), "dates": int(g["date"].nunique())}
        for h in horizons:
            col = f"fwd_{h}"
            gg = g.dropna(subset=[col])
            row[f"n_{h}"] = int(len(gg))
            row[f"moy_{h}"] = _r(gg[col].mean())
            row[f"gagnants_{h}"] = _r((gg[col] > 0).mean())
            row[f"xs_spy_{h}"] = _r(gg[f"xs_spy_{h}"].mean())
            row[f"xs_ew_{h}"] = _r(gg[f"xs_ew_{h}"].mean())
            row[f"t_xs_ew_{h}"] = _r(date_clustered_tstat(gg, f"xs_ew_{h}", h, every))
        out.append(row)
    return out


def information_coefficient(sig: pd.DataFrame, h: int, every: int = 5) -> dict[str, Any]:
    """Corrélation de rang (Spearman) par date entre le score et le rendement à terme."""
    ics = []
    for _, g in sig[sig["status"] != "REJECTED"].dropna(subset=[f"fwd_{h}", "total"]).groupby("date"):
        if len(g) >= 10:
            ics.append(g["total"].rank().corr(g[f"fwd_{h}"].rank()))
    s = pd.Series(ics).dropna()
    return {"horizon": h, "dates": int(len(s)), "ic_moyen": _r(s.mean()),
            "t_stat": _r(newey_west_tstat(s, max(1, math.ceil(h / every)))),
            "part_ic_positif": _r((s > 0).mean())}


def by_bucket(sig: pd.DataFrame, mask: pd.Series, key: str, h: int) -> list[dict]:
    g = sig[mask].dropna(subset=[f"fwd_{h}"])
    out = []
    for k, gg in g.groupby(key):
        out.append({key: str(k), "signaux": int(len(gg)), "moy": _r(gg[f"fwd_{h}"].mean()),
                    "xs_ew": _r(gg[f"xs_ew_{h}"].mean()), "gagnants": _r((gg[f"fwd_{h}"] > 0).mean())})
    return out


# =========================================================================== trades et portefeuille
@dataclass
class Trade:
    symbol: str
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    entry: float
    exit_date: pd.Timestamp
    exit: float
    reason: str
    gross: float
    sessions: int
    stop0: float


def simulate_trades(sig: pd.DataFrame, mask: pd.Series, book: PriceBook, p: BacktestParams) -> list[Trade]:
    """Une position à la fois par action. Stop initial = entrée − stop_atr×ATR ; trailing = plus haute
    clôture − trail_atr×ATR ; sortie au plus tard après max_hold_sessions. Un gap sous le stop est exécuté
    à l'ouverture (pire prix), pas au niveau du stop."""
    trades: list[Trade] = []
    busy_until: dict[str, pd.Timestamp] = {}
    for _, row in sig[mask].sort_values(["date", "symbol"]).iterrows():
        sym, d, atr = row["symbol"], row["date"], row["atr_pct"]
        if atr is None or not np.isfinite(atr) or (sym in busy_until and d <= busy_until[sym]):
            continue
        pos = book.pos_after(sym, d)
        if pos is None:
            continue
        o, c, lo, ix = book.o[sym], book.c[sym], book.lo[sym], book.idx[sym]
        entry = o[pos]
        atr_abs = atr * entry
        stop = entry - p.stop_atr * atr_abs
        stop0, hi = stop, entry
        exit_px, reason, k = None, "", pos
        last = min(len(c) - 1, pos + p.max_hold_sessions - 1)
        for k in range(pos, last + 1):
            if k > pos and o[k] <= stop:
                exit_px, reason = o[k], "stop (gap)"
                break
            if lo[k] <= stop:
                exit_px, reason = stop, "stop"
                break
            hi = max(hi, c[k])
            stop = max(stop, hi - p.trail_atr * atr_abs)
        if exit_px is None:
            if last < pos + p.max_hold_sessions - 1:
                continue  # trade pas encore terminé à la fin des données : exclu, pas supposé gagnant
            exit_px, reason, k = c[last], "durée max", last
        trades.append(Trade(sym, d, ix[pos], float(entry), ix[k], float(exit_px), reason,
                            float(exit_px / entry - 1), k - pos + 1, float(stop0)))
        busy_until[sym] = ix[k]
    return trades


def trade_metrics(trades: list[Trade], cost_bps: float) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    net = np.array([t.gross - 2 * cost_bps / 1e4 for t in trades])
    risk = np.array([max(1e-9, 1 - t.stop0 / t.entry) for t in trades])
    wins, losses = net[net > 0], net[net <= 0]
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else None
    reasons = pd.Series([t.reason for t in trades]).value_counts().to_dict()
    return {"trades": len(trades), "gagnants": _r((net > 0).mean()), "moy_net": _r(net.mean()),
            "mediane_net": _r(float(np.median(net))), "profit_factor": _r(pf),
            "esperance_R": _r(float((net / risk).mean())), "gain_moyen": _r(wins.mean() if len(wins) else None),
            "perte_moyenne": _r(losses.mean() if len(losses) else None),
            "duree_moy": _r(float(np.mean([t.sessions for t in trades]))), "sorties": reasons,
            "t_stat": _r(tstat(pd.Series(net)))}


def simulate_portfolio(trades: list[Trade], book: PriceBook, p: BacktestParams, cost_bps: float,
                       start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """Equity quotidienne : max N positions, chacune p.position_weight de l'equity à l'entrée, reste en cash.
    Les trades sont ceux de simulate_trades (mêmes sorties) ; ceux qui ne trouvent pas de place sont ignorés."""
    cal = book.idx[BENCHMARK]
    cal = cal[(cal >= start) & (cal <= end)]
    equity, cash = 1.0, 1.0
    open_pos: list[dict] = []
    by_entry: dict[pd.Timestamp, list[Trade]] = {}
    for t in sorted(trades, key=lambda t: (t.entry_date, t.symbol)):
        by_entry.setdefault(t.entry_date, []).append(t)
    curve = []
    for d in cal:
        # entrées à l'ouverture (taille calculée sur l'equity de la veille)
        for t in by_entry.get(d, []):
            if len(open_pos) >= p.max_positions:
                continue
            alloc = min(cash, equity * p.position_weight)
            if alloc <= 0:
                continue
            cash -= alloc
            open_pos.append({"t": t, "shares": alloc * (1 - cost_bps / 1e4) / t.entry})
        # sorties du jour (au prix de sortie simulé, y compris un stop touché le jour même de l'entrée)
        still = []
        for ps in open_pos:
            if ps["t"].exit_date == d:
                cash += ps["shares"] * ps["t"].exit * (1 - cost_bps / 1e4)
            else:
                still.append(ps)
        open_pos = still
        mtm = 0.0
        for ps in open_pos:
            sym = ps["t"].symbol
            k = int(book.idx[sym].searchsorted(d, side="right")) - 1
            mtm += ps["shares"] * book.c[sym][k]
        equity = cash + mtm
        curve.append(equity)
    return pd.Series(curve, index=cal)


def curve_metrics(eq: pd.Series, exposure: pd.Series | None = None) -> dict[str, Any]:
    if len(eq) < 2:
        return {}
    r = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else None
    dd = (eq / eq.cummax() - 1).min()
    down = r[r < 0].std()
    return {"rendement_total": _r(eq.iloc[-1] / eq.iloc[0] - 1), "rendement_annualise": _r(cagr),
            "drawdown_max": _r(dd), "volatilite": _r(r.std() * math.sqrt(252)),
            "sharpe": _r(r.mean() / r.std() * math.sqrt(252) if r.std() else None),
            "sortino": _r(r.mean() / down * math.sqrt(252) if down else None)}


def benchmark_curves(book: PriceBook, symbols: list[str], start: pd.Timestamp, end: pd.Timestamp) -> dict[str, pd.Series]:
    cal = book.idx[BENCHMARK]
    cal = cal[(cal >= start) & (cal <= end)]
    spy = pd.Series(book.c[BENCHMARK], index=book.idx[BENCHMARK]).reindex(cal)
    rets = []
    for s in symbols:
        ser = pd.Series(book.c[s], index=book.idx[s]).reindex(cal).ffill()
        rets.append(ser.pct_change())
    ew = (1 + pd.concat(rets, axis=1).mean(axis=1).fillna(0)).cumprod()  # rééquilibré chaque jour
    return {"SPY": spy / spy.iloc[0], "Univers équipondéré": ew / ew.iloc[0]}


# =========================================================================== orchestration
def _r(x: Any, n: int = 4) -> Any:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return x
    return None if math.isnan(f) or math.isinf(f) else round(f, n)


@dataclass
class ValidationReport:
    params: dict
    period: dict
    coverage: dict
    event_study: dict = field(default_factory=dict)
    ic: dict = field(default_factory=dict)
    trades: dict = field(default_factory=dict)
    portfolio: dict = field(default_factory=dict)
    folds: dict = field(default_factory=dict)
    regimes: dict = field(default_factory=dict)
    verdicts: dict = field(default_factory=dict)


def evaluate(sig: pd.DataFrame, store: DataStore, p: BacktestParams, include_holdout: bool) -> ValidationReport:
    book = PriceBook(store)
    sig = add_forward_returns(sig, book, p.horizons)
    hold = pd.Timestamp(p.holdout_start)
    # Zone tampon : un signal de développement ne doit pas se dénouer avec des prix de la période réservée
    dev = sig[sig["date"] < hold - pd.Timedelta(days=p.embargo_days)]
    hol = sig[sig["date"] >= hold]
    stocks = sorted(sig["symbol"].unique())
    cov = sig.groupby("date")["events_covered"].mean()
    rep = ValidationReport(
        params={k: (str(v) if isinstance(v, date) else v) for k, v in asdict(p).items()},
        period={"debut": str(sig["date"].min().date()), "fin": str(sig["date"].max().date()),
                "fin_developpement": str(dev["date"].max().date()) if len(dev) else None,
                "dates_rejouees": int(sig["date"].nunique()), "holdout_debut": str(p.holdout_start),
                "holdout_evalue": include_holdout},
        coverage={"actions": len(stocks),
                  "part_dates_historique_8K_complet": _r((cov >= 0.999).mean()),
                  "couverture_8K_moyenne": _r(cov.mean())})
    h_main = 20
    parts = {"developpement": dev}
    if include_holdout:
        parts["holdout"] = hol
    for label, part in parts.items():
        if part.empty:
            continue
        groups = signal_groups(part, p.top_n)
        rep.event_study[label] = event_study(part, groups, p.horizons, p.every_n_sessions)
        rep.ic[label] = [information_coefficient(part, h, p.every_n_sessions) for h in p.horizons]
        end = part["date"].max() + pd.Timedelta(days=90)
        rep.trades[label], rep.portfolio[label] = {}, {}
        for name in ("READY", "DEVELOPING ou READY", f"Top {p.top_n} du score", "A Early momentum",
                     "B Earnings momentum", "C Growth acceleration", "D Sector rotation",
                     f"Règle simple : top {p.top_n} momentum 12-1"):
            tr = simulate_trades(part, groups[name], book, p)
            rep.trades[label][name] = {f"{c:g} bps": trade_metrics(tr, c) for c in p.cost_sensitivity_bps}
            eq = simulate_portfolio(tr, book, p, p.cost_bps_per_side, part["date"].min(), end)
            rep.portfolio[label][name] = curve_metrics(eq)
        for bname, curve in benchmark_curves(book, stocks, part["date"].min(), end).items():
            rep.portfolio[label][f"Référence : {bname}"] = curve_metrics(curve)
        if label == "developpement":
            rep.regimes[label] = {n: by_bucket(part, groups[n], "regime", h_main)
                                  for n in ("READY", f"Top {p.top_n} du score")}
            part = part.assign(annee=part["date"].dt.year)
            rep.folds[label] = {n: by_bucket(part, groups[n], "annee", h_main) for n in groups}
    rep.verdicts = verdicts(rep, p, h_main)
    return rep


def verdicts(rep: ValidationReport, p: BacktestParams, h: int) -> dict[str, dict]:
    """Règles de décision fixées AVANT de voir les résultats. Une stratégie ne passe au paper trading que si
    toutes sont remplies ; sinon elle reste en RESEARCH (ou est rejetée si elle détruit de la valeur)."""
    out = {}
    dev = {r["groupe"]: r for r in rep.event_study.get("developpement", [])}
    hol = {r["groupe"]: r for r in rep.event_study.get("holdout", [])}
    cost_key = f"{p.cost_bps_per_side:g} bps"
    for name, row in dev.items():
        if name.startswith("Référence") or name.startswith("Règle simple"):
            continue
        tm = rep.trades.get("developpement", {}).get(name, {}).get(cost_key, {})
        folds = rep.folds.get("developpement", {}).get(name, [])
        fold_hits = [f for f in folds if f["xs_ew"] is not None and f["signaux"] >= 10]
        hit_ratio = (sum(f["xs_ew"] > 0 for f in fold_hits) / len(fold_hits)) if fold_hits else None
        checks = {
            f"≥ {p.min_trades} trades": (tm.get("trades", 0) >= p.min_trades, tm.get("trades", 0)),
            "espérance nette > 0 (après frais)": ((tm.get("moy_net") or -1) > 0, tm.get("moy_net")),
            f"bat l'univers EW à {h} j (t ≥ {p.min_tstat})":
                ((row.get(f"t_xs_ew_{h}") or 0) >= p.min_tstat, row.get(f"t_xs_ew_{h}")),
            f"positif ≥ {p.min_fold_hit_ratio:.0%} des années": ((hit_ratio or 0) >= p.min_fold_hit_ratio, _r(hit_ratio)),
        }
        if hol:
            hrow = hol.get(name, {})
            checks["holdout : bat l'univers EW"] = ((hrow.get(f"xs_ew_{h}") or -1) > 0, hrow.get(f"xs_ew_{h}"))
        passed = all(v[0] for v in checks.values())
        destroys = (row.get(f"t_xs_ew_{h}") or 0) <= -p.min_tstat
        decision = ("ÉLIGIBLE PAPER TRADING" if passed and hol else
                    "PROMETTEUR — holdout à évaluer" if passed else
                    "REJETÉE (sous-performe significativement)" if destroys else "RESTE EN RESEARCH")
        out[name] = {"decision": decision, "criteres": {k: {"ok": bool(v[0]), "valeur": v[1]} for k, v in checks.items()}}
    return out
