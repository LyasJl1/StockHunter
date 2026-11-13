"""Moteur de découverte v3 : classement quotidien multi-catégories sur l'univers large (calcul pur, sans base).

Pipeline en deux temps : (1) présélection rapide et vectorisée de tout l'univers négociable (quelques milliers de
titres) avec les MÊMES définitions de facteurs que le laboratoire de recherche ; (2) analyse approfondie des seuls
candidats retenus (fondamentaux SEC, dépôts, actualités : `discovery/deep.py`).

Principes :
- chaque catégorie a une règle d'entrée absolue : aucun nombre de titres n'est imposé, une catégorie peut être vide ;
- un filtre de marché peut suspendre les catégories de tendance (« aucune opportunité fiable aujourd'hui ») ;
- le rang est un classement relatif, la confiance porte sur la qualité des données, et aucun des deux n'est une
  probabilité de gain ; le niveau de validation de chaque stratégie vient du registre des expériences.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from stockhunter.lab import factors as fx
from stockhunter.lab.panel import Panel, UniverseFilter, liquidity_tier, universe_mask
from stockhunter.trading.rules import Rules, plan

ENGINE_VERSION = "3.0.0"
MAX_PER_CATEGORY = 10
TIERS = {0: "petite / moins liquide", 1: "moyenne", 2: "grande / très liquide"}


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    strategy: str                 # description courte de la règle
    experiments: tuple[str, ...]  # identifiants du registre qui portent sa validation
    trend: bool                   # suspendue quand le marché est défavorable


CATEGORIES: dict[str, Category] = {c.key: c for c in [
    Category("momentum", "Momentum", "Tendance 6 mois forte, propre au titre et ajustée du risque, au-dessus de ses "
             "moyennes 50 et 200 jours, sans excès", ("v3-V2", "v3-V3"), True),
    Category("breakout", "Sortie de consolidation", "Clôture au-dessus du plus haut de 60 séances après une phase de "
             "contraction, avec volume, toujours tenue", ("v3-V4",), True),
    Category("steady", "Tendance régulière à faible risque", "Tendance positive, volatilité faible, pas de comportement "
             "« loterie »", ("v3-MF", "v3-V9"), True),
    Category("shock", "Choc d'information", "Forte hausse à l'ouverture sur volume exceptionnel dans les 20 dernières "
             "séances, écart conservé (réaction à une nouvelle)", ("broad-H7",), False),
    Category("watch", "À surveiller", "Proche d'un plus haut de 60 séances dans une phase de contraction : "
             "configuration en préparation, sans signal", ("v3-V4",), False),
]}


def _last(df: pd.DataFrame, d) -> pd.Series:
    return df.loc[d]


def market_overview(p: Panel, mask: pd.DataFrame, tier: pd.DataFrame, d) -> dict[str, Any]:
    c = p.close
    m = mask.loc[d]
    n = int(m.sum())
    sma50, sma200 = c.rolling(50, min_periods=50).mean().loc[d], c.rolling(200, min_periods=200).mean().loc[d]
    last = c.loc[d]
    i = c.index.get_loc(d)

    def ret(k):
        return (last / c.iloc[i - k] - 1) if i >= k else pd.Series(np.nan, index=c.columns)

    def share(cond):
        x = cond[m]
        return float(x.mean()) if len(x) else None

    hi60 = p.high.iloc[max(0, i - 59):i + 1].max()
    lo60 = p.low.iloc[max(0, i - 59):i + 1].min()
    out: dict[str, Any] = {
        "univers": n,
        "part_au_dessus_mm50": share(last > sma50),
        "part_au_dessus_mm200": share((last > sma200)[sma200.notna()]) if sma200.notna().any() else None,
        "part_plus_haut_60j": share(last >= hi60 * 0.999),
        "part_plus_bas_60j": share(last <= lo60 * 1.001),
        "rendement_univers_1m": float(ret(21)[m].mean()), "rendement_univers_3m": float(ret(63)[m].mean()),
        "volatilite_mediane_60j": float(c.pct_change(fill_method=None).iloc[max(0, i - 59):i + 1].std()[m].median()),
        "par_taille_1m": {TIERS[k]: float(ret(21)[m & (tier.loc[d] == k)].mean()) for k in (0, 1, 2)},
    }
    if p.bench is not None and pd.notna(p.bench.loc[d]):
        b = p.bench
        s200 = b.rolling(200, min_periods=200).mean().loc[d]
        out["spy"] = {"rendement_1m": float(b.loc[d] / b.iloc[i - 21] - 1) if i >= 21 else None,
                      "rendement_3m": float(b.loc[d] / b.iloc[i - 63] - 1) if i >= 63 else None,
                      "au_dessus_mm200": bool(b.loc[d] > s200) if pd.notna(s200) else None}
    spy_ok = (out.get("spy") or {}).get("au_dessus_mm200")
    breadth = out["part_au_dessus_mm50"]
    hostile = spy_ok is False and breadth is not None and breadth < 0.40
    out["regime"] = ("DÉFAVORABLE" if hostile else
                     "FAVORABLE" if spy_ok and breadth is not None and breadth >= 0.55 else "NEUTRE")
    out["motif_regime"] = (f"SPY {'au-dessus' if spy_ok else 'sous' if spy_ok is False else 'N/D vs'} sa moyenne 200 jours ; "
                           f"{breadth:.0%} des titres au-dessus de leur moyenne 50 jours" if breadth is not None else "N/D")
    return out


def run(p: Panel, names: dict[str, str] | None = None, filt: UniverseFilter = UniverseFilter(),
        rules: Rules = Rules(), validation: dict[str, str] | None = None) -> dict[str, Any]:
    """Classement de la dernière séance du panel. `validation` : id d'expérience -> décision (registre)."""
    names, validation = names or {}, validation or {}
    d = p.close.index[-1]
    mask = universe_mask(p, filt)
    tier = liquidity_tier(p, mask)
    facs = fx.compute(p, mask)
    m = mask.loc[d]
    overview = market_overview(p, mask, tier, d)
    c, i = p.close, len(p.close) - 1
    last, raw = c.loc[d], p.raw_close.loc[d]
    sma50, sma200 = c.rolling(50, min_periods=50).mean().loc[d], c.rolling(200, min_periods=200).mean().loc[d]
    atr14 = fx.atr(p).loc[d]
    atr_pct = atr14 / last
    ext50 = last / sma50 - 1
    hi60_prev = p.high.iloc[max(0, i - 60):i].max()
    rk = {k: fx.rank_pct(facs[k].astype(float), mask).loc[d] for k in ("V2", "V3", "V9", "H4", "MF")}
    ret = lambda k: (last / c.iloc[i - k] - 1) if i >= k else pd.Series(np.nan, index=c.columns)
    r1, r3, r6 = ret(21), ret(63), ret(126)
    spy3 = (p.bench.loc[d] / p.bench.iloc[i - 63] - 1) if p.bench is not None and i >= 63 else np.nan
    med_dv = p.dollar_vol.rolling(20, min_periods=15).median().loc[d]
    hist = c.notna().sum()
    listed20 = c.notna().iloc[-20:].sum()
    trend_ok = (last > sma50) & ((last > sma200) | sma200.isna()) & ((sma50 > sma200) | sma200.isna())

    # ------------------------------------------------------------------ règles d'entrée des catégories
    recent_bo = facs["V4"].iloc[-5:].any()
    # niveau de la consolidation : plus haut des 60 séances closes AVANT la fenêtre de 5 jours où la sortie a pu
    # avoir lieu (sinon le niveau inclurait les plus hauts faits depuis la cassure)
    bo_level = p.high.iloc[max(0, i - 64):max(1, i - 4)].max()
    watch_level = hi60_prev
    rng = lambda n: p.high.iloc[-n - 1:-1].max() / p.low.iloc[-n - 1:-1].min() - 1
    contraction = rng(20) < 0.8 * rng(120)
    gap = p.open / c.shift(1) - 1
    vol60 = p.volume.rolling(60, min_periods=60).mean().shift(1)
    shock = (gap >= 0.05) & (p.volume >= 3 * vol60)
    shock_recent = shock.iloc[-20:]
    shock_day = shock_recent.apply(lambda col: col[col].index[-1] if col.any() else pd.NaT)
    shock_open = pd.Series({t: (p.open.at[sd, t] if pd.notna(sd) else np.nan) for t, sd in shock_day.items()})
    rules_by_cat = {
        "momentum": (m & (rk["V2"] >= 0.90) & (rk["V3"] >= 0.80) & trend_ok & (ext50 <= 0.20) & (atr_pct <= 0.06),
                     (rk["V2"] + rk["V3"]) / 2),
        "breakout": (m & recent_bo & (last >= bo_level * 0.98) & (atr_pct <= 0.08), rk["V2"].fillna(0.5)),
        "steady": (m & (rk["V2"] >= 0.70) & (rk["H4"] <= 0.30) & (rk["V9"] <= 0.30) & trend_ok, rk["MF"]),
        "shock": (m & shock_recent.any() & (last >= shock_open), (last / shock_open - 1).clip(upper=1.0)),
        "watch": (m & ~recent_bo & contraction & (last >= watch_level * 0.97) & (last < watch_level) & trend_ok,
                  last / watch_level),
    }
    hostile = overview["regime"] == "DÉFAVORABLE"
    categories, total = {}, 0
    for key, cat in CATEGORIES.items():
        cond, score = rules_by_cat[key]
        status = "; ".join(f"{e} : {validation.get(e, 'non enregistré')}" for e in cat.experiments)
        entry = {"label": cat.label, "regle": cat.strategy, "validation": status, "admis": int(cond.sum()),
                 "suspendue": bool(hostile and cat.trend), "candidats": []}
        if entry["suspendue"]:
            entry["motif"] = "marché défavorable : catégorie de tendance suspendue"
        else:
            picks = score[cond].dropna().sort_values(ascending=False).head(MAX_PER_CATEGORY)
            for rank, (t, sc) in enumerate(picks.items(), 1):
                entry["candidats"].append(_card(t, rank, float(sc), key, d, names, raw, last, atr14, atr_pct, r1, r3, r6,
                                                spy3, ext50, sma50, sma200, med_dv, tier, rk, hist, listed20,
                                                watch_level if key == "watch" else bo_level,
                                                shock_day, shock_open, gap, rules))
            total += len(entry["candidats"])
        categories[key] = entry
    conclusion = ("Aucune configuration ne remplit les règles aujourd'hui." if total == 0 else
                  f"{total} configuration(s) relevée(s). Aucune stratégie n'est validée : sélections exploratoires, "
                  f"suivies en simulation, pas des recommandations.")
    if hostile:
        conclusion = "Marché défavorable : catégories de tendance suspendues. " + conclusion
    return {"as_of": d.date(), "version": ENGINE_VERSION, "univers": int(m.sum()), "marche": overview,
            "categories": categories, "conclusion": conclusion}


def _card(t, rank, score, key, d, names, raw, last, atr14, atr_pct, r1, r3, r6, spy3, ext50, sma50, sma200, med_dv, tier,
          rk, hist, listed20, bo_level, shock_day, shock_open, gap, rules: Rules) -> dict[str, Any]:
    f = lambda x: None if x is None or not np.isfinite(x) else float(x)
    scale = raw[t] / last[t]                                   # ajusté des splits -> cours réellement affiché
    close, atr_raw = float(raw[t]), float(atr14[t] * scale)
    pl = plan(close, atr_raw, rules)
    pct = lambda x: f"{x * 100:+.1f} %".replace(".", ",")
    pos, neg = [], []
    if f(r3[t]) is not None:
        vs = f" ({pct(r3[t] - spy3)} vs SPY)" if np.isfinite(spy3) else ""
        (pos if r3[t] > 0 else neg).append(f"{pct(r3[t])} sur 3 mois{vs}")
    if f(r6[t]) is not None:
        (pos if r6[t] > 0 else neg).append(f"{pct(r6[t])} sur 6 mois")
    if f(rk["V2"][t]) is not None and rk["V2"][t] >= 0.8:
        pos.append(f"Tendance ajustée du risque dans les {max(1, round(100 - rk['V2'][t] * 100))} % meilleurs de l'univers")
    if f(rk["V3"][t]) is not None and rk["V3"][t] >= 0.8:
        pos.append("Hausse propre au titre (au-delà du mouvement du marché)")
    if key == "breakout":
        pos.append(f"Sortie par le haut d'une consolidation de 60 séances (niveau {bo_level[t] * scale:.2f} $), avec volume")
    if key == "shock" and pd.notna(shock_day[t]):
        pos.append(f"Ouverture en hausse de {pct(gap.at[shock_day[t], t])} le {shock_day[t]:%d/%m/%Y} sur volume ≥ 3× "
                   f"la normale ; écart conservé depuis")
    if key == "watch":
        pos.append(f"À {pct(last[t] / bo_level[t] - 1)} de son plus haut de 60 séances ({bo_level[t] * scale:.2f} $), "
                   f"amplitude en contraction")
    if f(rk["H4"][t]) is not None and rk["H4"][t] <= 0.3:
        pos.append("Volatilité parmi les plus faibles de l'univers")
    if f(ext50[t]) is not None and ext50[t] > 0.12:
        neg.append(f"Déjà {pct(ext50[t])} au-dessus de sa moyenne 50 jours (risque de repli)")
    if f(atr_pct[t]) is not None and atr_pct[t] > 0.04:
        neg.append(f"Titre nerveux : amplitude quotidienne moyenne {atr_pct[t] * 100:.1f} %".replace(".", ","))
    if f(rk["V9"][t]) is not None and rk["V9"][t] >= 0.9:
        neg.append("Hausse journalière extrême récente (profil « loterie »)")
    if tier.loc[d][t] == 0:
        neg.append("Liquidité modeste : écarts de cours et glissement plus élevés")
    if f(r1[t]) is not None and r1[t] < -0.05:
        neg.append(f"{pct(r1[t])} sur 1 mois")
    confidence = round(min(1.0, hist[t] / 252) * 0.6 + (listed20[t] / 20) * 0.4, 2)
    return {
        "ticker": t, "name": names.get(t), "category": key, "rank": rank, "score": round(score, 4),
        "close": close, "tier": TIERS[int(tier.loc[d][t])], "confidence": confidence,
        "metrics": {"ret_1m": f(r1[t]), "ret_3m": f(r3[t]), "ret_6m": f(r6[t]),
                    "rs_spy_3m": f(r3[t] - spy3) if np.isfinite(spy3) else None, "atr_pct": f(atr_pct[t]),
                    "ext_sma50": f(ext50[t]), "sma50": f(sma50[t] * scale), "sma200": f(sma200[t] * scale),
                    "dollar_vol_med20": f(med_dv[t]), "rank_trend": f(rk["V2"][t]), "rank_resid_mom": f(rk["V3"][t]),
                    "rank_volatility": f(rk["H4"][t]), "rank_max": f(rk["V9"][t]), "history_sessions": int(hist[t])},
        "pos": pos, "neg": neg,
        "plan": {"signal_close": close, "atr": round(atr_raw, 4), "entry_min": round(pl.entry_min, 2),
                 "entry_max": round(pl.entry_max, 2), "stop_offset": round(pl.stop_offset, 4),
                 "stop_si_entree_au_cours": round(close - pl.stop_offset, 2),
                 "tp1": round(close + rules.tp1_r * pl.stop_offset, 2), "tp2": round(close + rules.tp2_r * pl.stop_offset, 2),
                 "ratio_rendement_risque": f"{rules.tp1_r:g} R (moitié) puis {rules.tp2_r:g} R",
                 "risque_pct": round(pl.stop_offset / close, 4), "horizon_seances": rules.max_sessions,
                 "annulation": f"ouverture suivante hors de la zone {pl.entry_min:.2f}–{pl.entry_max:.2f} $"},
    }
