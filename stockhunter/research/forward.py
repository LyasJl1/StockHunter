"""Test prospectif (hors échantillon réel) des facteurs pré-enregistrés de l'étude univers large.

À partir du 13/11/2026, chaque semaine, le panier du décile favorable de chaque facteur est figé en base
(ForwardSignal) AVANT que son résultat soit connu. Les rendements sont mesurés ensuite, avec exactement les
définitions de l'étude (entrée ouverture J+1, sortie clôture J+h, frais 25 pb/côté, vs univers équipondéré).
C'est la seule évaluation qui ne peut pas avoir été influencée par la connaissance des résultats.
"""

from __future__ import annotations

import math
from datetime import date

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from stockhunter.data.calendar import sessions_between
from stockhunter.db.models import ForwardSignal
from stockhunter.db.session import session_scope
from stockhunter.db.upsert import upsert
from stockhunter.market.store import MarketStore
from stockhunter.market.sync import load_splits
from stockhunter.research import broad
from stockhunter.validation.backtest import newey_west_tstat

START = date(2026, 11, 13)
EVERY = broad.EVERY


def _panel(engine: Engine, store: MarketStore):
    long = store.load()
    if long.empty:
        raise RuntimeError("aucune séance de marché stockée")
    p = broad.build_panel(long, load_splits(engine), broad.eligible_tickers(engine))
    facs = broad.compute_factors(p)
    mask = broad.universe_mask(p)
    ranks = {k: broad._rank_pct(facs[k].where(mask)) for k in ("H1", "H2", "H5", "H7")}
    facs["C"] = (ranks["H1"] + (1 - ranks["H2"]) + ranks["H5"] + ranks["H7"]) / 4
    return p, facs, mask


def record(engine: Engine, store: MarketStore, today_session: date | None = None) -> int:
    """Fige les paniers de la dernière séance si elle est une date de signal (une par semaine de bourse)."""
    p, facs, mask = _panel(engine, store)
    d = pd.Timestamp(today_session) if today_session else p.close.index[-1]
    if d.date() < START:
        return 0
    with session_scope(engine) as s:
        last = s.scalar(select(func.max(ForwardSignal.signal_date)).where(ForwardSignal.study == broad.STUDY))
    if last and sessions_between(last, d.date()) < EVERY:
        return 0
    rows = []
    m = mask.loc[d]
    for k, fac in facs.items():
        x = (fac.loc[d][m] * broad.EXPECTED_SIGN[k]).dropna()
        if len(x) < 100:
            continue
        top = x[x.rank(method="first", pct=True) > 0.9]
        rows += [{"study": broad.STUDY, "factor": k, "signal_date": d.date(), "ticker": t, "value": float(v),
                  "universe_size": int(len(x))} for t, v in top.items()]
    with session_scope(engine) as s:
        upsert(s, ForwardSignal, rows, ["study", "factor", "signal_date", "ticker"], update_cols=[])
    return len(rows)


def report(engine: Engine, store: MarketStore) -> dict:
    with session_scope(engine) as s:
        sig = pd.DataFrame(s.execute(select(ForwardSignal.factor, ForwardSignal.signal_date, ForwardSignal.ticker)
                                     .where(ForwardSignal.study == broad.STUDY)).all(),
                           columns=["factor", "signal_date", "ticker"])
    out = {"dates": int(sig["signal_date"].nunique()) if len(sig) else 0, "facteurs": {}}
    if sig.empty:
        return out
    p, _, mask = _panel(engine, store)
    for h in broad.HORIZONS:
        fwd = broad.forward_returns(p, h)
        for k, g in sig.groupby("factor"):
            excess = []
            for d, gg in g.groupby("signal_date"):
                ts = pd.Timestamp(d)
                if ts not in fwd.index:
                    continue
                uni = fwd.loc[ts][mask.loc[ts]].dropna()
                basket = fwd.loc[ts].reindex(gg["ticker"]).dropna()
                if len(uni) < 100 or basket.empty:
                    continue   # horizon pas encore écoulé
                excess.append((ts, basket.mean() - uni.mean() - 2 * broad.COST_BPS / 1e4))
            e = pd.Series(dict(excess))
            out["facteurs"].setdefault(k, {})[h] = {
                "dates_mesurees": len(e), "excess_net_moyen": float(e.mean()) if len(e) else None,
                "t": newey_west_tstat(e, max(1, math.ceil(h / EVERY))) if len(e) >= 10 else None,
                "positif": float((e > 0).mean()) if len(e) else None}
    return out


def render(rep: dict) -> str:
    L = ["## Test prospectif des facteurs pré-enregistrés (depuis le 13/11/2026)", "",
         f"Paniers figés : {rep['dates']} date(s). Mesure : décile favorable vs univers équipondéré, net de frais.", ""]
    if not rep["facteurs"]:
        return "\n".join(L + ["Aucun résultat mesurable pour l'instant (il faut au moins 5 à 20 séances après chaque date)."])
    L += ["| Facteur | Dates mesurées (20 j) | Excès net 20 j | t | % positif |", "|---|---:|---:|---:|---:|"]
    for k in broad.LABELS:
        r = rep["facteurs"].get(k, {}).get(20, {})
        ex = r.get("excess_net_moyen")
        L.append(f"| {k} {broad.LABELS[k]} | {r.get('dates_mesurees', 0)} | "
                 f"{'N/D' if ex is None else f'{ex * 100:+.2f} %'} | {'N/D' if r.get('t') is None else f'{r['t']:+.2f}'} | "
                 f"{'N/D' if r.get('positif') is None else f'{r['positif']:.0%}'} |")
    L += ["", "Moins de 10 dates mesurées : pas de t-stat (échantillon trop petit pour conclure)."]
    return "\n".join(L)


__all__ = ["record", "report", "render", "START"]
