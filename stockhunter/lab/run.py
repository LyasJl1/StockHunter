"""Exécution du programme de recherche v3 (pré-enregistré) et rendu du rapport."""

from __future__ import annotations

import os
from datetime import date

import pandas as pd

from stockhunter.lab import evaluate as ev
from stockhunter.lab import factors as fx
from stockhunter.lab import ml, portfolio_sim
from stockhunter.lab.panel import Panel, bench_forward, forward_returns, liquidity_tier, universe_mask

DEV_END = date(2026, 2, 28)
HOLDOUT_START = date(2026, 4, 1)


GRID_ANCHOR = date(2024, 11, 12)      # première séance des données : la grille de signal ne dépend pas du panel chargé


def signal_grid(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Une date de signal toutes les 5 séances de bourse comptées depuis GRID_ANCHOR (calendrier NYSE) : la grille
    reste la même quand la fenêtre de données glisse."""
    from stockhunter.data.calendar import sessions_between
    return pd.DatetimeIndex([d for d in index if d.date() >= GRID_ANCHOR
                             and sessions_between(GRID_ANCHOR, d.date()) % ev.EVERY == 0])


def run_program(p: Panel, with_ml: bool = True, with_portfolio: bool = True, today: date | None = None,
                holdout_consulted: frozenset[str] = frozenset()) -> dict:
    """`holdout_consulted` : identifiants (ex. « v3-V2 ») dont la période réservée a déjà été consultée d'après
    le registre — elle n'est JAMAIS réévaluée (consultation unique)."""
    mask = universe_mask(p)
    tier = liquidity_tier(p, mask)
    facs = fx.compute(p, mask)
    fwd = {h: forward_returns(p, h) for h in ev.HORIZONS}
    bfw = {h: bench_forward(p, h) for h in ev.HORIZONS}
    all_dates = signal_grid(p.close.index)
    dev = all_dates[all_dates <= pd.Timestamp(DEV_END)]
    hol = all_dates[all_dates >= pd.Timestamp(HOLDOUT_START)]
    last = mask.iloc[-1]
    out: dict = {
        "meta": {"programme": "v3", "date": str(today or date.today()), "commit": os.environ.get("GITHUB_SHA", "local")[:12],
                 "donnees": p.fingerprint(), "dates_dev": len(dev), "dates_reservees_non_consultees": len(hol)},
        "univers": {"moyen_par_date": int(mask.sum(axis=1)[mask.sum(axis=1) > 0].mean()) if mask.any().any() else 0,
                    "derniere_date": int(last.sum()),
                    "par_liquidite_derniere_date": {k: int((tier.iloc[-1] == i).sum())
                                                    for i, k in enumerate(("faible", "moyenne", "forte"))},
                    "titres_passes_par_l_univers": int(mask.any().sum()),
                    "dont_radies_depuis": int((mask.any() & p.close.iloc[-1].isna()).sum())},
        "facteurs": {},
    }
    for key, spec in fx.SPECS.items():
        dev_res = ev.evaluate(facs[key], spec.kind, spec.sign, p, mask, tier, fwd, bfw, dev)
        entry = {"label": spec.label, "famille": spec.family, "sens": spec.sign, "type": spec.kind,
                 "fondement": spec.rationale, "developpement": dev_res, "decision": ev.decide(dev_res)}
        if entry["decision"]["decision"] == "CANDIDAT" and f"v3-{key}" in holdout_consulted:
            entry["decision"]["decision"] = "CANDIDAT (période réservée déjà consultée : voir le registre)"
        elif entry["decision"]["decision"] == "CANDIDAT":        # consultation unique prévue par le protocole
            hol_res = ev.evaluate(facs[key], spec.kind, spec.sign, p, mask, tier, fwd, bfw, hol)
            entry["holdout"] = hol_res
            entry["decision"] = ev.decide(dev_res, hol_res)
        out["facteurs"][key] = entry
    if with_ml:
        data = ml.build_dataset(facs, mask, fwd[20], dev)
        out["ml"] = ml.walk_forward(data, p.close.index)
    if with_portfolio and len(dev) > 30:
        start = dev[dev >= mask.index[mask.any(axis=1)][0]]
        mf_dates = pd.DatetimeIndex([d for d in start if facs["MF"].loc[d].notna().sum() >= 100])
        if len(mf_dates) > 5:
            end = p.close.index[p.close.index < pd.Timestamp(HOLDOUT_START)][-1]
            out["portefeuille"] = portfolio_sim.simulate(p, facs["MF"], mask, mf_dates, end)
    return out


def _f(x, n=3, pct=False):
    if x is None or (isinstance(x, float) and x != x):
        return "N/D"
    return (f"{x * 100:+.2f} %" if pct else f"{x:+.{n}f}").replace(".", ",")


def render(res: dict, family: dict | None = None) -> str:
    m, u = res["meta"], res["univers"]
    d = m["donnees"]
    L = ["# Programme de recherche v3 — résultats", "",
         "> Protocole : `docs/research/2026-11-12-preenregistrement-v3.md` (commité avant l'exécution).",
         "> Rendements simulés sur données historiques ; aucune garantie pour l'avenir.", "",
         f"- Données : {d['seances']} séances ({d['premiere']} → {d['derniere']}), {d['titres']} actions ordinaires "
         f"(radiées comprises), empreinte `{d['empreinte']}` ; code `{m['commit']}`.",
         f"- Univers dynamique : {u['moyen_par_date']} titres en moyenne par date ; {u['titres_passes_par_l_univers']} "
         f"titres y sont passés, dont {u['dont_radies_depuis']} radiés depuis. Dernière date : {u['derniere_date']} "
         f"({u['par_liquidite_derniere_date']}).",
         f"- Dates de signal : {m['dates_dev']} en développement ; {m['dates_reservees_non_consultees']} réservées.", "",
         "## Facteurs (développement, horizon 20 séances)", "",
         "| # | Hypothèse | Sens | Dates | Stat. | t (NW) | IC bootstrap 95 % | Moitié 1 / 2 | Décile net 25 pb vs univers | vs SPY | Décision |",
         "|---|---|---|---:|---:|---:|---|---|---:|---:|---|"]
    for k, r in res["facteurs"].items():
        x = r["developpement"].get(20, r["developpement"].get("20", {}))
        boot = x.get("ic_boot95")
        L.append(f"| {k} | {r['label']} | {'+' if r['sens'] > 0 else '−'} | {x.get('dates', 0)} | {_f(x.get('stat'))} | "
                 f"{_f(x.get('t'), 2)} | {'N/D' if not boot else f'[{_f(boot[0])} ; {_f(boot[1])}]'} | "
                 f"{_f(x.get('moitie_1'))} / {_f(x.get('moitie_2'))} | "
                 f"{_f((x.get('top_vs_univers_net') or {}).get('25pb'), pct=True)} | {_f(x.get('top_vs_spy_net'), pct=True)} | "
                 f"**{r['decision']['decision']}** |")
    L += ["", "Stat. = IC de rang moyen (facteurs continus) ou écart moyen vs univers (événement V4). "
          "Seuil : t ≥ 3,0 (16 tests dans la famille).", "", "### Diagnostics", ""]
    for k, r in res["facteurs"].items():
        x = r["developpement"].get(20, r["developpement"].get("20", {}))
        if not x.get("dates"):
            continue
        net = x.get("top_vs_univers_net") or {}
        liq = x.get("ic_par_liquidite")
        L.append(f"- **{k}** — frais 10/25/50 pb : {_f(net.get('10pb'), pct=True)} / {_f(net.get('25pb'), pct=True)} / "
                 f"{_f(net.get('50pb'), pct=True)} ; semestres : "
                 + ", ".join(f"{s} {_f(v)}" for s, v in (x.get('par_semestre') or {}).items())
                 + (f" ; IC par liquidité (faible/moyenne/forte) : {_f(liq['faible'])} / {_f(liq['moyenne'])} / {_f(liq['forte'])}"
                    if liq else "")
                 + f" ; volume médian du groupe : {x.get('capacite_volume_median_usd', 0) / 1e6:.1f} M$/j.")
    if family:
        L += ["", "## Tests multiples (toute la famille, registre)", "", "| Test | p unilatérale | Holm | Benjamini-Hochberg |",
              "|---|---:|---:|---:|"]
        for k, v in sorted(family.items(), key=lambda kv: kv[1]["p"]):
            L.append(f"| {k} | {v['p']:.3f} | {v['holm']:.3f} | {v['bh']:.3f} |")
    mlr = res.get("ml")
    if mlr:
        L += ["", "## Machine learning (walk-forward purgé, hors entraînement)", ""]
        if not mlr.get("dates_testees"):
            L.append(f"Non exécutable : {mlr.get('motif')}.")
        else:
            L += [f"{mlr['dates_testees']} dates testées ({mlr['premiere']} → {mlr['derniere']}).", "",
                  "| Modèle | IC | t | % IC > 0 | Décile net vs univers | IC − référence | t différence | Retenu |",
                  "|---|---:|---:|---:|---:|---:|---:|---|"]
            for name, x in mlr["modeles"].items():
                L.append(f"| {name} | {_f(x['ic'])} | {_f(x['t'], 2)} | {x['part_positive']:.0%} | "
                         f"{_f(x['top_vs_univers_net_25pb'], pct=True)} | {_f(x.get('ic_moins_reference'))} | "
                         f"{_f(x.get('t_difference'), 2)} | {'—' if name == 'reference' else ('oui' if x['retenu'] else 'non')} |")
            if not mlr.get("gbm_disponible"):
                L.append("\nGradient boosting non exécuté (scikit-learn absent de cet environnement).")
            L += ["", "Coefficients Ridge (moyenne ± écart-type entre ré-entraînements ; stabilité des variables) : "
                  + ", ".join(f"{k} {v['moyenne']:+.3f}±{v['ecart_type']:.3f}" for k, v in mlr["ridge_coefficients"].items())]
    pf = res.get("portefeuille")
    if pf:
        c, s, e = pf["courbe"], pf.get("spy", {}), pf["univers_equipondere"]
        L += ["", "## Simulation de portefeuille (descriptive, règles de trading partagées, composite MF)", "",
              f"Période {pf['debut']} → {pf['fin']} ; {pf['trades_clos']} trades clos, {pf['plans_annules']} plans annulés, "
              f"exposition moyenne {pf['exposition_moyenne']:.0%}, gagnants "
              f"{'N/D' if pf['gagnants'] is None else f'{pf['gagnants']:.0%}'}, R moyen {_f(pf['r_moyen'], 2)}.", "",
              "| | Rendement | Sharpe | Drawdown max |", "|---|---:|---:|---:|",
              f"| Portefeuille simulé | {_f(c.get('rendement_total'), pct=True)} | {_f(c.get('sharpe'), 2)} | {_f(c.get('drawdown_max'), pct=True)} |",
              f"| SPY (achat-conservation) | {_f(s.get('rendement_total'), pct=True)} | {_f(s.get('sharpe'), 2)} | {_f(s.get('drawdown_max'), pct=True)} |",
              f"| Univers équipondéré | {_f(e.get('rendement_total'), pct=True)} | {_f(e.get('sharpe'), 2)} | {_f(e.get('drawdown_max'), pct=True)} |",
              "", "Ce tableau ne valide rien : il décrit le comportement des règles sur un signal non validé."]
    return "\n".join(L) + "\n"
