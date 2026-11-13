"""Registre versionné des expériences (fichier JSON dans le dépôt, livré avec le paquet).

Chaque test statistique jamais exécuté y figure — retenu ou non — avec ses données, son code et sa décision.
La taille de la famille (nombre total de tests) sert à corriger les tests multiples ; une période réservée
consultée y est consignée et ne redevient jamais vierge.
"""

from __future__ import annotations

import json
from pathlib import Path

from stockhunter.lab.stats import benjamini_hochberg, holm, t_sf

PATH = Path(__file__).with_name("registry.json")


def load(path: Path = PATH) -> dict:
    return json.loads(path.read_text())


def save(reg: dict, path: Path = PATH) -> None:
    path.write_text(json.dumps(reg, ensure_ascii=False, indent=1) + "\n")


def tests(reg: dict) -> list[dict]:
    return [e for e in reg["experiences"] if e.get("type") == "test"]


def family_pvalues(reg: dict) -> dict[str, dict]:
    """P-valeurs unilatérales (sens attendu) de tous les tests, brutes et ajustées (Holm, Benjamini-Hochberg)."""
    raw = {}
    for e in tests(reg):
        t, n = e.get("t"), e.get("dates")
        if t is not None and n and n > 1:
            raw[e["id"]] = t_sf(t, n - 1)
    m = len(tests(reg))                      # famille entière, y compris les tests sans statistique
    h, bh = holm(raw, m), benjamini_hochberg(raw, m)
    return {k: {"p": raw[k], "holm": h[k], "bh": bh[k]} for k in raw}


def holdout_consulted(reg: dict) -> frozenset[str]:
    return frozenset(e["id"] for e in reg["experiences"] if e.get("holdout_consulte"))


def record_run(reg: dict, run: dict) -> dict:
    """Ajoute les résultats d'une exécution du programme v3 (idempotent : remplace les entrées de même id).
    Une consultation de période réservée est irréversible : le drapeau n'est jamais remis à faux, et la période
    passe à l'état « CONSULTÉE » dans le registre."""
    already = holdout_consulted(reg)
    meta = run["meta"]
    new = []
    for key, r in run["facteurs"].items():
        d = r["developpement"].get("20", r["developpement"].get(20, {}))
        new.append({"id": f"v3-{key}", "type": "test", "programme": "v3", "date": meta["date"],
                    "hypothese": r["label"], "famille": r["famille"], "sens_attendu": r["sens"],
                    "protocole": "docs/research/2026-11-12-preenregistrement-v3.md", "code": meta.get("commit"),
                    "donnees": meta["donnees"], "periode": "développement (signaux <= 2026-02-28)",
                    "dates": d.get("dates", 0), "stat": d.get("stat"), "t": d.get("t"),
                    "top_vs_univers_net_25pb": (d.get("top_vs_univers_net") or {}).get("25pb"),
                    "decision": r["decision"]["decision"],
                    "holdout_consulte": "holdout" in r or f"v3-{key}" in already})
        if "holdout" in r:
            for h in reg["periodes_reservees"]:
                if h["etude"].startswith("univers large") and h["etat"] == "INTACTE":
                    h["etat"] = f"CONSULTÉE le {meta['date']} pour v3-{key} (ne redevient jamais vierge)"
    for name, m in (run.get("ml", {}).get("modeles") or {}).items():
        if name == "reference":
            continue
        new.append({"id": f"v3-ML-{name}", "type": "test", "programme": "v3", "date": meta["date"],
                    "hypothese": f"Modèle {name} (classement à 20 j, walk-forward purgé)", "famille": "ml",
                    "sens_attendu": 1, "protocole": "docs/research/2026-11-12-preenregistrement-v3.md",
                    "code": meta.get("commit"), "donnees": meta["donnees"], "periode": "développement, hors entraînement",
                    "dates": run["ml"]["dates_testees"], "stat": m["ic"], "t": m["t"],
                    "top_vs_univers_net_25pb": m["top_vs_univers_net_25pb"],
                    "decision": "RETENU" if m.get("retenu") else "NON RETENU (ne bat pas la référence simple)",
                    "holdout_consulte": False})
    ids = {e["id"] for e in new}
    reg["experiences"] = [e for e in reg["experiences"] if e["id"] not in ids] + new
    return reg
