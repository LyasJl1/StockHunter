"""Machine learning (phase E) : modèles de classement cross-sectionnel en walk-forward purgé.

Comparaison obligatoire à une référence simple (composite équipondéré des rangs signés). Hyperparamètres fixés
par le pré-enregistrement, non optimisés. Un modèle n'est intégré que s'il bat la référence hors entraînement.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from stockhunter.lab.factors import PRIOR_SIGNS, rank_pct
from stockhunter.lab.stats import newey_west_tstat

FEATURES = {**PRIOR_SIGNS, "V2": 1, "V3": 1, "V9": -1}
HORIZON, PURGE, MIN_TRAIN_DATES, MIN_FEATURES = 20, 5, 12, 7
RIDGE_ALPHA = 10.0
GBM_PARAMS = {"max_depth": 3, "max_iter": 100, "learning_rate": 0.05, "random_state": 0}


def build_dataset(facs: dict, mask: pd.DataFrame, fwd20: pd.DataFrame, dates: pd.DatetimeIndex):
    """Par date : X = rangs (0-1) des variables, y = rang du rendement futur. Variable absente = 0,5 (neutre)."""
    ranks = {k: rank_pct(facs[k].astype(float), mask) for k in FEATURES}
    out = {}
    for d in dates:
        X = pd.DataFrame({k: ranks[k].loc[d] for k in FEATURES})
        y = fwd20.loc[d]
        ok = mask.loc[d] & (X.notna().sum(axis=1) >= MIN_FEATURES)
        if int(ok.sum()) < 100:
            continue
        X = X[ok].fillna(0.5)
        out[d] = (X, y[ok])        # y peut contenir des NaN (rendement pas encore observable) : filtré à l'usage
    return out


def _ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    Xc = np.column_stack([np.ones(len(X)), X])
    A = Xc.T @ Xc + alpha * np.eye(Xc.shape[1])
    A[0, 0] -= alpha                       # constante non pénalisée
    return np.linalg.solve(A, Xc.T @ y)


def train_dates(dates: list, pos: dict, d) -> list:
    """Dates d'entraînement admissibles pour tester `d` : fenêtre de rendement terminée + purge."""
    return [t for t in dates if pos[t] + HORIZON + PURGE < pos[d]]


def walk_forward(data: dict, sessions: pd.DatetimeIndex) -> dict:
    """Pour chaque date testée : entraînement sur les dates dont la fenêtre de rendement (20 séances) s'est
    terminée au moins PURGE séances avant -> aucune information postérieure à la date testée."""
    pos = {d: i for i, d in enumerate(sessions)}
    dates = sorted(data)
    signs = np.array([FEATURES[k] for k in FEATURES], dtype=float)
    try:
        from sklearn.ensemble import HistGradientBoostingRegressor
    except ImportError:                                               # dépendance optionnelle
        HistGradientBoostingRegressor = None
    rows, coefs = [], []
    for d in dates:
        train = train_dates(dates, pos, d)
        if len(train) < MIN_TRAIN_DATES:
            continue
        Xte, yte = data[d]
        ok = yte.notna()
        if int(ok.sum()) < 100:
            continue
        Xte, yte = Xte[ok], yte[ok]
        parts = [(data[t][0][data[t][1].notna()], data[t][1].dropna().rank(pct=True)) for t in train]
        Xtr = np.vstack([a.to_numpy() for a, _ in parts])
        ytr = np.concatenate([b.to_numpy() for _, b in parts])
        preds = {"reference": (Xte.to_numpy() - 0.5) @ signs}
        w = _ridge_fit(Xtr, ytr, RIDGE_ALPHA)
        coefs.append(w[1:])
        preds["ridge"] = Xte.to_numpy() @ w[1:]
        if HistGradientBoostingRegressor is not None:
            gbm = HistGradientBoostingRegressor(**GBM_PARAMS).fit(Xtr, ytr)
            preds["gbm"] = gbm.predict(Xte.to_numpy())
        row = {"date": d, "n_train_dates": len(train)}
        yr = yte.rank()
        for name, pr in preds.items():
            pr = pd.Series(pr, index=yte.index)
            row[f"ic_{name}"] = float(pr.rank().corr(yr))
            top = pr.rank(method="first") > 0.9 * len(pr)
            row[f"top_{name}"] = float(yte[top].mean() - yte.mean())
        rows.append(row)
    if not rows:
        return {"dates_testees": 0, "motif": f"moins de {MIN_TRAIN_DATES} dates d'entraînement purgées disponibles"}
    df = pd.DataFrame(rows).set_index("date")
    lag = math.ceil(HORIZON / 5)
    out = {"dates_testees": len(df), "premiere": str(df.index[0].date()), "derniere": str(df.index[-1].date()),
           "gbm_disponible": HistGradientBoostingRegressor is not None, "modeles": {}}
    for name in ("reference", "ridge", "gbm"):
        if f"ic_{name}" not in df:
            continue
        ic = df[f"ic_{name}"]
        m = {"ic": float(ic.mean()), "t": newey_west_tstat(ic, lag), "part_positive": float((ic > 0).mean()),
             "top_vs_univers_net_25pb": float(df[f"top_{name}"].mean() - 0.005)}
        if name != "reference":
            diff = ic - df["ic_reference"]
            m["ic_moins_reference"] = float(diff.mean())
            m["t_difference"] = newey_west_tstat(diff, lag)
            m["retenu"] = bool((m["t"] or 0) >= 3.0 and (m["t_difference"] or 0) >= 2.0)
        out["modeles"][name] = m
    c = np.array(coefs)
    out["ridge_coefficients"] = {k: {"moyenne": float(c[:, i].mean()), "ecart_type": float(c[:, i].std()),
                                     "signe_attendu": int(FEATURES[k])} for i, k in enumerate(FEATURES)}
    return out
