"""Réplication internationale pré-enregistrée (docs/research/2026-11-14-preenregistrement-international.md).

Rejoue le scanner d'une place à des dates passées, point-in-time (panel tronqué à chaque date), SANS rien
enregistrer en base ni modifier aucune règle. Mesure l'écart net à l'univers de la place à 20 séances par
catégorie, et fournit les sélections rejouées au portefeuille mondial simulé (descriptif).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from stockhunter.discovery import engine as disc
from stockhunter.lab.panel import forward_returns, universe_mask
from stockhunter.lab.stats import newey_west_tstat
from stockhunter.world import markets as mk
from stockhunter.world import scan as ws
from stockhunter.world.fx import FxTable
from stockhunter.world.portfolio import SignalIn
from stockhunter.world.store import WorldStore

STEP, HORIZON, COST_ROUND_TRIP, NW_LAG, MIN_DATES, T_MIN = 5, 20, 0.0050, 4, 20, 3.0
PROTOCOL = "docs/research/2026-11-14-preenregistrement-international.md"


def replay(store: WorldStore, code: str, fx: FxTable) -> dict[str, Any]:
    """3.2.1 : à chaque date de signal, panel reconstruit avec les SEULES séances ≤ date (troncature des
    discontinuités, univers, seuils de change connus à cette date) ; rendements futurs mesurés sur le panel complet
    non tronqué (une forte hausse future n'est plus écartée de la mesure)."""
    full, info = ws.build_market_panel(store, code, fx, truncate=False)
    if full is None or len(full.close) < ws.MIN_SESSIONS + HORIZON + 1:
        return {"place": code, "statut": "NON TESTABLE", "motif": f"{info.get('seances', 0)} séances stockées",
                "categories": {}, "signaux": []}
    m = mk.get(code)
    fw = forward_returns(full, HORIZON)
    n = len(full.close)
    rows, signals, idx = [], [], []
    names = info.get("noms") or {}
    for i in range(ws.MIN_SESSIONS - 1, n - HORIZON - 1, STEP):
        d = full.close.index[i]
        pit, _ = ws.build_market_panel(store, code, fx, end=d.date())
        filt = ws.market_filter(code, fx, d)
        mask_d = universe_mask(pit, filt).iloc[-1]
        if mask_d.sum() < 20:
            continue
        idx.append(i)
        res = disc.run(pit, {}, filt, ccy=m.currency, bench_label=f"équipondéré {code}")
        univ = fw.loc[d].reindex(mask_d[mask_d].index).mean()
        for key, cat in res["categories"].items():
            picks = [c["ticker"] for c in cat["candidats"]]
            r = fw.loc[d].reindex(picks).dropna() if picks else pd.Series(dtype=float)
            rows.append({"date": d, "categorie": key, "n": len(r),
                         "exces_net": float(r.mean() - univ - COST_ROUND_TRIP) if len(r) and np.isfinite(univ) else np.nan})
            for c in cat["candidats"]:
                t = c["ticker"]
                adj = float(full.close.at[d, t]) if t in full.close.columns else np.nan
                if not np.isfinite(adj) or not c["plan"]["signal_close"]:
                    continue
                signals.append(SignalIn(code, t, names.get(t, (t,))[0], d.date(), key,
                                        round(1 - (c["rank"] - 1) / max(cat["admis"], 1), 4), adj,
                                        float(c["plan"]["atr"] * adj / c["plan"]["signal_close"]), m.currency, m.country))
    p = full
    df = pd.DataFrame(rows)
    cats = {}
    for key in disc.CATEGORIES:
        s = df[(df["categorie"] == key)].dropna(subset=["exces_net"]).set_index("date")["exces_net"] if len(df) else pd.Series(dtype=float)
        k = len(s)
        t = newey_west_tstat(s, NW_LAG) if k >= 3 and s.std() > 0 else None
        t = float(t) if t is not None and np.isfinite(t) else None
        halves = [float(h.mean()) for h in (s.iloc[: k // 2], s.iloc[k // 2:])] if k >= 4 else [None, None]
        if k < MIN_DATES:
            decision = "NON TESTABLE"
        elif t is not None and t >= T_MIN and all(h is not None and h > 0 for h in halves):
            decision = "CANDIDAT INTERNATIONAL (ni validé ni promu : suivi prospectif requis)"
        else:
            decision = "NON RETENU"
        cats[key] = {"dates": k, "exces_net_moyen": float(s.mean()) if k else None, "t": t, "moities": halves,
                     "selections_moyennes": float(df[df["categorie"] == key]["n"].mean()) if len(df) else None,
                     "decision": decision}
    return {"place": code, "statut": "OK", "dates_signal": len(idx),
            "premiere": str(p.close.index[idx[0]].date()) if idx else None,
            "derniere": str(p.close.index[idx[-1]].date()) if idx else None, "categories": cats,
            "signaux": signals, "panel": p}


def register(results: list[dict], meta: dict, reg_path=None) -> dict:
    """Consigne chaque test au registre (jamais de suppression ; même id remplacé = réexécution identique)."""
    from stockhunter.lab import registry as R
    reg = R.load(reg_path) if reg_path else R.load()
    ids = []
    for res in results:
        for key, c in res["categories"].items():
            eid = f"intl-{res['place']}-{key}"
            ids.append(eid)
            entry = {"id": eid, "type": "test", "programme": "3.2-international", "date": meta["date"],
                     "hypothese": f"Catégorie « {disc.CATEGORIES[key].label} » (moteur 3.0.0 figé) sur {res['place']} : "
                                  f"écart net à 20 séances vs univers de la place", "famille": "international",
                     "sens_attendu": 1, "protocole": PROTOCOL, "code": meta.get("commit"),
                     "donnees": {"place": res["place"], "premiere_date_signal": res.get("premiere"),
                                 "derniere_date_signal": res.get("derniere")},
                     "periode": "hors échantillon (règles jamais développées sur cette place)",
                     "dates": c["dates"], "stat": c["exces_net_moyen"], "t": c["t"], "decision": c["decision"],
                     "holdout_consulte": False}
            prev = [e for e in reg["experiences"] if e.get("id") == eid]
            if prev:                     # réexécution (correction de code) : l'ancien résultat est conservé, jamais effacé
                old = {k: v for k, v in prev[0].items() if k not in ("historique",)}
                if (old.get("stat"), old.get("t"), old.get("code")) != (entry["stat"], entry["t"], entry["code"]):
                    entry["historique"] = prev[0].get("historique", []) + [old]
                else:
                    entry["historique"] = prev[0].get("historique", [])
                entry["motif_reexecution"] = meta.get("motif")
            reg["experiences"] = [e for e in reg["experiences"] if e.get("id") != eid] + [entry]
    reg["executions"].append({"programme": "3.2-international", "date": meta["date"], "commit": meta.get("commit"),
                              "workflow_run": meta.get("workflow_run"), "tests": ids,
                              "donnees": {"seances": sum(r.get("dates_signal", 0) for r in results),
                                          "titres": None, "empreinte": ", ".join(r["place"] for r in results)},
                              "duree_s": meta.get("duree_s"), "memoire_max_mo": None,
                              "resume": "; ".join(f"{r['place']} : " + ", ".join(
                                  f"{k} {c['decision'].split(' (')[0]}" for k, c in r["categories"].items())
                                  for r in results)})
    fam = R.family_pvalues(reg)
    for e in reg["experiences"]:
        if e.get("id") in fam:
            e["p_ajustees"] = {k: round(v, 4) for k, v in fam[e["id"]].items()}
    R.save(reg, reg_path) if reg_path else R.save(reg)
    return {"famille": len(R.tests(reg)), "ajustees": {i: fam.get(i) for i in ids}}
