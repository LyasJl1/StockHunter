"""Étude pré-enregistrée sur l'univers large (docs/research/2026-11-12-preenregistrement-univers-large.md).

Ce module implémente le protocole tel qu'il a été écrit AVANT les données : définitions, univers, découpage
et règles de décision. Ne pas modifier ces constantes au vu des résultats (un changement = nouvelle étude).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from stockhunter.db.models import MarketTicker
from stockhunter.db.session import session_scope
from stockhunter.market.store import MarketStore, split_factors
from stockhunter.market.sync import load_splits
from stockhunter.validation.backtest import newey_west_tstat

STUDY = "univers-large-preenregistre-2026-11-12"
EXCHANGES = {"XNYS", "XNAS", "XASE"}
MIN_PRICE = 5.0
MIN_DOLLAR_VOL = 2_000_000
MIN_HISTORY = 130
EVERY = 5
HORIZONS = (5, 20)
COST_BPS = 25.0
DEV_END = date(2026, 2, 28)
HOLDOUT_START = date(2026, 4, 1)
T_THRESHOLD = 2.7
HOLDOUT_T = 1.0
EXPECTED_SIGN = {"H1": 1, "H2": -1, "H3": 1, "H4": -1, "H5": 1, "H6": 1, "H7": 1, "C": 1}
LABELS = {"H1": "Momentum 6-1", "H2": "Retournement court terme", "H3": "Proximité du plus haut 52 s",
          "H4": "Volatilité 60 j", "H5": "Volume anormal 5/60 j", "H6": "Illiquidité (Amihud)",
          "H7": "Choc d'information (gap + volume)", "C": "Composite (H1, −H2, H5, H7)"}


@dataclass
class Panel:
    open: pd.DataFrame
    high: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    raw_close: pd.DataFrame
    dollar_vol: pd.DataFrame


def eligible_tickers(engine: Engine) -> set[str]:
    with session_scope(engine) as s:
        rows = s.execute(select(MarketTicker.ticker, MarketTicker.type, MarketTicker.primary_exchange)).all()
    return {t for t, ty, ex in rows if ty == "CS" and ex in EXCHANGES}


def build_panel(long: pd.DataFrame, splits: pd.DataFrame, tickers: set[str]) -> Panel:
    df = long[long["ticker"].isin(tickers) & ~long["otc"].fillna(False)].copy()
    f = split_factors(splits, df["date"], df["ticker"])
    for col in ("open", "high", "low", "close"):
        df[f"adj_{col}"] = df[col] * f
    df["adj_volume"] = df["volume"] / f
    df["dollar_vol"] = df["close"] * df["volume"]   # brut × brut = montant réellement échangé

    def wide(col):
        return df.pivot_table(index="date", columns="ticker", values=col, aggfunc="last").sort_index()

    return Panel(open=wide("adj_open"), high=wide("adj_high"), close=wide("adj_close"), volume=wide("adj_volume"),
                 raw_close=wide("close"), dollar_vol=wide("dollar_vol"))


def compute_factors(p: Panel) -> dict[str, pd.DataFrame]:
    """Chaque valeur à la date t n'utilise que des données <= t (vérifié par test)."""
    c, v = p.close, p.volume
    ret = c.pct_change(fill_method=None)
    vol60 = v.rolling(60, min_periods=60).mean()
    out = {
        "H1": c.shift(21) / c.shift(126) - 1,
        "H2": c / c.shift(21) - 1,
        "H3": c / p.high.rolling(252, min_periods=252).max(),
        "H4": ret.rolling(60, min_periods=60).std(),
        "H5": v.rolling(5, min_periods=5).mean() / vol60,
        "H6": (ret.abs() / p.dollar_vol.replace(0, np.nan)).rolling(60, min_periods=60).mean(),
    }
    gap = p.open / c.shift(1) - 1
    shock = gap.where(v >= 3 * vol60.shift(1))
    best = pd.DataFrame(0.0, index=c.index, columns=c.columns)
    for k in range(20):
        cand = shock.shift(k).fillna(0.0)
        best = best.where(best.abs() >= cand.abs(), cand)
    out["H7"] = best.where(vol60.notna())
    return out


def universe_mask(p: Panel) -> pd.DataFrame:
    hist = p.close.notna().cumsum()
    med_dv = p.dollar_vol.rolling(20, min_periods=15).median()
    return (p.raw_close >= MIN_PRICE) & (med_dv >= MIN_DOLLAR_VOL) & (hist >= MIN_HISTORY)


def forward_returns(p: Panel, h: int) -> pd.DataFrame:
    """Entrée à l'ouverture de t+1, sortie à la clôture de t+h ; titre radié : dernière clôture disponible."""
    entry = p.open.shift(-1)
    last_close = p.close.ffill()
    exit_ = last_close.shift(-h)
    observable = pd.Series(np.arange(len(p.close)) + h < len(p.close), index=p.close.index)
    return (exit_ / entry - 1).where(observable, axis=0)


def _rank_pct(df: pd.DataFrame) -> pd.DataFrame:
    return df.rank(axis=1, pct=True)


@dataclass
class StudyResult:
    meta: dict
    factors: dict = field(default_factory=dict)
    verdicts: dict = field(default_factory=dict)


def evaluate_factor(fac: pd.DataFrame, fwd: dict[int, pd.DataFrame], mask: pd.DataFrame, dates: pd.DatetimeIndex,
                    sign: int) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for h, fw in fwd.items():
        ics, tops, deciles, sizes = [], [], [], []
        for d in dates:
            m = mask.loc[d] & fac.loc[d].notna() & fw.loc[d].notna()
            n = int(m.sum())
            if n < 100:
                continue
            x, y = fac.loc[d][m] * sign, fw.loc[d][m]
            if x.nunique() < 10 or y.nunique() < 10:
                continue  # facteur ou rendements (quasi) constants à cette date : corrélation non définie
            ics.append((d, x.rank().corr(y.rank())))
            q = pd.qcut(x.rank(method="first"), 10, labels=False)
            dec = y.groupby(q).mean()
            deciles.append(dec)
            tops.append((d, dec.iloc[-1] - y.mean() - 2 * COST_BPS / 1e4))
            sizes.append(n)
        if not ics:
            res[h] = {"dates": 0}
            continue
        ic = pd.Series(dict(ics))
        top = pd.Series(dict(tops))
        lag = max(1, math.ceil(h / EVERY))
        dec_mean = pd.concat(deciles, axis=1).mean(axis=1)
        res[h] = {"dates": len(ic), "titres_moyens": int(np.mean(sizes)), "ic": float(ic.mean()),
                  "t_ic": newey_west_tstat(ic, lag), "ic_positif": float((ic > 0).mean()),
                  "top_vs_univers_net": float(top.mean()), "t_top": newey_west_tstat(top, lag),
                  "deciles": [float(x) for x in dec_mean.values]}
    return res


def run_study(engine: Engine, store: MarketStore, include_holdout: bool = False) -> StudyResult:
    long = store.load()
    if long.empty:
        raise RuntimeError("aucune séance de marché stockée : lancer « Univers large - chargement »")
    p = build_panel(long, load_splits(engine), eligible_tickers(engine))
    facs = compute_factors(p)
    mask = universe_mask(p)
    ranks = {k: _rank_pct(facs[k].where(mask)) for k in ("H1", "H2", "H5", "H7")}
    facs["C"] = (ranks["H1"] + (1 - ranks["H2"]) + ranks["H5"] + ranks["H7"]) / 4
    fwd = {h: forward_returns(p, h) for h in HORIZONS}
    all_dates = p.close.index[::EVERY]
    dev = all_dates[all_dates <= pd.Timestamp(DEV_END)]
    hol = all_dates[all_dates >= pd.Timestamp(HOLDOUT_START)]
    meta = {"etude": STUDY, "seances": len(p.close), "premiere": str(p.close.index[0].date()),
            "derniere": str(p.close.index[-1].date()), "titres_eligibles": int(p.close.shape[1]),
            "univers_moyen": int(mask.sum(axis=1).mean()), "holdout_evalue": include_holdout,
            "radies_dans_univers": int((p.close.iloc[-1].isna() & mask.any()).sum())}
    out = StudyResult(meta=meta)
    for k, fac in facs.items():
        sign = EXPECTED_SIGN[k]
        out.factors[k] = {"developpement": evaluate_factor(fac, fwd, mask, dev, sign)}
        if include_holdout:
            out.factors[k]["holdout"] = evaluate_factor(fac, fwd, mask, hol, sign)
    out.verdicts = decide(out.factors, include_holdout)
    return out


def decide(factors: dict, include_holdout: bool) -> dict[str, dict]:
    v = {}
    for k, r in factors.items():
        d20 = r["developpement"].get(20, {})
        t, top = d20.get("t_ic"), d20.get("top_vs_univers_net")
        if not d20.get("dates"):
            v[k] = {"decision": "NON TESTABLE (historique insuffisant)"}
            continue
        if t is not None and t <= -T_THRESHOLD:
            v[k] = {"decision": "INVERSÉ (non retenu : hypothèse nouvelle à tester sur données futures)"}
            continue
        dev_ok = t is not None and t >= T_THRESHOLD and (top or -1) > 0
        if not dev_ok:
            v[k] = {"decision": "NON RETENU"}
            continue
        if not include_holdout:
            v[k] = {"decision": "PROMETTEUR — période réservée à évaluer"}
            continue
        h20 = r.get("holdout", {}).get(20, {})
        ok = (h20.get("t_ic") or 0) >= HOLDOUT_T
        v[k] = {"decision": "RETENU" if ok else "NON CONFIRMÉ sur la période réservée"}
    return v


def render(res: StudyResult) -> str:
    m = res.meta
    L = [f"# Étude pré-enregistrée — univers large ({m['etude']})", "",
         "> Protocole : `docs/research/2026-11-12-preenregistrement-univers-large.md` (écrit avant les données).",
         "> Rendements simulés, frais de 25 pb par côté inclus pour le décile supérieur. Aucune garantie future.", "",
         f"- Séances : {m['seances']} ({m['premiere']} → {m['derniere']}) ; titres éligibles (actions ordinaires "
         f"NYSE/Nasdaq/NYSE American, y compris radiés) : {m['titres_eligibles']} ; univers moyen par date : {m['univers_moyen']}.",
         f"- Seuil de significativité corrigé des tests multiples : t ≥ {T_THRESHOLD}. "
         f"Période réservée : {'**évaluée**' if m['holdout_evalue'] else 'non évaluée'}.", "",
         "| # | Facteur | Sens attendu | Dates | IC 20 j | t | % IC > 0 | Décile sup. vs univers (net, 20 j) | t | Décision |",
         "|---|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for k, r in res.factors.items():
        d = r["developpement"].get(20, {})
        f = lambda x, n=3: "N/D" if x is None else f"{x:+.{n}f}".replace(".", ",")
        pct = lambda x: "N/D" if x is None else f"{x * 100:+.2f} %".replace(".", ",")
        L.append(f"| {k} | {LABELS[k]} | {'+' if EXPECTED_SIGN[k] > 0 else '−'} | {d.get('dates', 0)} | "
                 f"{f(d.get('ic'))} | {f(d.get('t_ic'), 2)} | {'N/D' if d.get('ic_positif') is None else f'{d['ic_positif']:.0%}'} | "
                 f"{pct(d.get('top_vs_univers_net'))} | {f(d.get('t_top'), 2)} | {res.verdicts[k]['decision']} |")
    if m["holdout_evalue"]:
        L += ["", "## Période réservée (20 j)", "", "| # | IC | t | Décile sup. vs univers (net) |", "|---|---:|---:|---:|"]
        for k, r in res.factors.items():
            h = r.get("holdout", {}).get(20, {})
            L.append(f"| {k} | {h.get('ic', 0):+.3f} | {h.get('t_ic') or 0:+.2f} | {(h.get('top_vs_univers_net') or 0) * 100:+.2f} % |")
    L += ["", "## Rendement moyen à 20 j par décile (développement, du moins au plus favorable)", ""]
    for k, r in res.factors.items():
        dec = r["developpement"].get(20, {}).get("deciles")
        if dec:
            L.append(f"- {k} : " + " · ".join(f"{x * 100:+.1f}" for x in dec) + " (%)")
    return "\n".join(L)
