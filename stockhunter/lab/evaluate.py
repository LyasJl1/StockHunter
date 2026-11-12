"""Évaluation d'un facteur : IC, écart du décile favorable, robustesse, coûts, capacité, décision pré-enregistrée."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from stockhunter.lab.panel import Panel
from stockhunter.lab.stats import block_bootstrap_ci, newey_west_tstat, t_sf

EVERY = 5
HORIZONS = (20, 5)
COSTS_BPS = (10.0, 25.0, 50.0)
BASE_COST = 25.0
T_THRESHOLD = 3.0
HOLDOUT_T = 1.0
MIN_DATES = 20
MIN_NAMES = 100
MIN_EVENT_NAMES = 5


def _per_date_rank(fac, fw, mask, tier, dv, bench, dates, sign) -> pd.DataFrame:
    rows = []
    for d in dates:
        m = mask.loc[d] & fac.loc[d].notna() & fw.loc[d].notna()
        if int(m.sum()) < MIN_NAMES:
            continue
        x, y = fac.loc[d][m].astype(float) * sign, fw.loc[d][m]
        if x.nunique() < 10 or y.nunique() < 10:
            continue
        xr = x.rank(method="first")
        top = xr > 0.9 * len(xr)
        row = {"date": d, "n": int(m.sum()), "ic": float(xr.corr(y.rank())), "top": float(y[top].mean()),
               "univ": float(y.mean()), "bot": float(y[xr <= 0.1 * len(xr)].mean()),
               "top_dv": float(dv.loc[d][m][top].median()),
               "bench": float(bench.loc[d]) if bench is not None and pd.notna(bench.loc[d]) else np.nan}
        t = tier.loc[d][m]
        for k in (0, 1, 2):
            mk = t == k
            row[f"ic_t{k}"] = float(x[mk].rank().corr(y[mk].rank())) if int(mk.sum()) >= 30 else np.nan
        rows.append(row)
    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame()


def _per_date_event(flag, fw, mask, dv, bench, dates) -> pd.DataFrame:
    recent = flag.astype(float).rolling(EVERY, min_periods=1).max() > 0      # signalé sur les 5 dernières séances
    rows = []
    for d in dates:
        base = mask.loc[d] & fw.loc[d].notna()
        g = base & recent.loc[d]
        if int(base.sum()) < MIN_NAMES or int(g.sum()) < MIN_EVENT_NAMES:
            continue
        y = fw.loc[d]
        rows.append({"date": d, "n": int(g.sum()), "top": float(y[g].mean()), "univ": float(y[base].mean()),
                     "top_dv": float(dv.loc[d][g].median()),
                     "bench": float(bench.loc[d]) if bench is not None and pd.notna(bench.loc[d]) else np.nan})
    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame()


def summarize(pd_: pd.DataFrame, h: int, kind: str) -> dict[str, Any]:
    if pd_.empty:
        return {"dates": 0}
    lag = max(1, math.ceil(h / EVERY))
    spread = pd_["top"] - pd_["univ"]
    key = pd_["ic"] if kind == "rank" else spread                    # statistique de décision
    t = newey_west_tstat(key, lag)
    n = len(key)
    half = n // 2
    out: dict[str, Any] = {
        "dates": n, "titres_moyens": int(pd_["n"].mean()), "stat": float(key.mean()), "t": t,
        "p_unilateral": t_sf(t, n - 1) if t is not None else None,
        "part_positive": float((key > 0).mean()),
        "ic_boot95": block_bootstrap_ci(key, lag + 1),
        "moitie_1": float(key.iloc[:half].mean()) if half else None,
        "moitie_2": float(key.iloc[half:].mean()) if half else None,
        "top_vs_univers_brut": float(spread.mean()),
        "top_vs_univers_net": {f"{int(c)}pb": float(spread.mean() - 2 * c / 1e4) for c in COSTS_BPS},
        "t_top_net": newey_west_tstat(spread - 2 * BASE_COST / 1e4, lag),
        "top_vs_spy_net": float((pd_["top"] - pd_["bench"]).mean() - 2 * BASE_COST / 1e4)
        if pd_["bench"].notna().any() else None,
        "capacite_volume_median_usd": float(pd_["top_dv"].median()),
    }
    if kind == "rank":
        out["long_short_brut"] = float((pd_["top"] - pd_["bot"]).mean())
        out["ic_par_liquidite"] = {name: (float(pd_[c].mean()) if pd_[c].notna().any() else None)
                                   for name, c in (("faible", "ic_t0"), ("moyenne", "ic_t1"), ("forte", "ic_t2"))}
    by_sem = key.groupby([key.index.year, (key.index.month - 1) // 6 + 1]).mean()
    out["par_semestre"] = {f"{y}-S{s}": float(v) for (y, s), v in by_sem.items()}
    return out


def evaluate(fac: pd.DataFrame, kind: str, sign: int, p: Panel, mask: pd.DataFrame, tier: pd.DataFrame,
             fwd: dict[int, pd.DataFrame], bench_fwd: dict[int, pd.Series | None], dates: pd.DatetimeIndex) -> dict:
    res = {}
    for h in HORIZONS:
        if kind == "rank":
            per = _per_date_rank(fac, fwd[h], mask, tier, p.dollar_vol, bench_fwd[h], dates, sign)
        else:
            per = _per_date_event(fac, fwd[h], mask, p.dollar_vol, bench_fwd[h], dates)
        res[h] = summarize(per, h, kind)
    return res


def decide(dev: dict, holdout: dict | None = None) -> dict:
    """Règle du pré-enregistrement v3 (section 6). Renvoie la décision et les critères un par un."""
    d = dev.get(20, {})
    if d.get("dates", 0) < MIN_DATES:
        return {"decision": "NON TESTABLE", "motif": f"{d.get('dates', 0)} dates de signal (< {MIN_DATES})"}
    boot = d.get("ic_boot95")
    crit = {"t >= 3,0": d["t"] is not None and d["t"] >= T_THRESHOLD,
            "décile net > univers": d["top_vs_univers_net"]["25pb"] > 0,
            "positif sur les deux moitiés": (d.get("moitie_1") or -1) > 0 and (d.get("moitie_2") or -1) > 0,
            "borne basse bootstrap > 0": boot is not None and boot[0] > 0}
    if not all(crit.values()):
        return {"decision": "NON RETENU", "criteres": crit}
    if holdout is None:
        return {"decision": "CANDIDAT", "criteres": crit}
    h = holdout.get(20, {})
    ok = (h.get("t") or 0) >= HOLDOUT_T and h.get("top_vs_univers_net", {}).get("25pb", -1) > 0
    return {"decision": "CONFIRMÉ HORS ÉCHANTILLON" if ok else "NON CONFIRMÉ (période réservée)", "criteres": crit}
