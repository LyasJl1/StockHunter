"""Orchestration de la validation : rejeu, évaluation, enregistrement, rapport Markdown."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Callable

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from stockhunter.db.models import ValidationRun
from stockhunter.db.session import session_scope
from stockhunter.scan import DataStore, ScanError, _strategy_version
from stockhunter.scoring.config import DEFAULT_CONFIG, ScoringConfig
from stockhunter.validation.backtest import BacktestParams, ValidationReport, evaluate, replay


def data_fingerprint(store: DataStore) -> str:
    parts = []
    for sym in sorted(store.bars):
        df = store.bars[sym]
        parts.append(f"{sym}:{len(df)}:{df.index[-1].date() if len(df) else '-'}")
    parts.append(f"facts:{sum(len(v) for v in store.facts.values())}")
    parts.append(f"filings:{sum(len(v) for v in store.filings.values())}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def holdout_count(engine: Engine, cfg: ScoringConfig) -> int:
    with session_scope(engine) as s:
        sv = _strategy_version(s, cfg)
        return s.scalar(select(func.count()).select_from(ValidationRun).where(
            ValidationRun.strategy_version_id == sv.id, ValidationRun.holdout_evaluated.is_(True))) or 0


def run_validation(engine: Engine, include_holdout: bool = False, allow_holdout_again: bool = False,
                   params: BacktestParams = BacktestParams(), cfg: ScoringConfig = DEFAULT_CONFIG,
                   signals_cache: Path | None = None,
                   progress: Callable[[int, int], None] | None = None) -> tuple[int, ValidationReport, int]:
    prior = holdout_count(engine, cfg)
    if include_holdout and prior and not allow_holdout_again:
        raise ScanError(f"la période réservée a déjà été évaluée {prior} fois pour {cfg.name} {cfg.version} : "
                        "la réévaluer après avoir vu ses résultats la rend moins probante "
                        "(--allow-holdout-again pour passer outre, ce sera noté dans le rapport)")
    store = DataStore(engine)
    fp = data_fingerprint(store)
    sig = None
    if signals_cache and signals_cache.exists():
        cached = pd.read_pickle(signals_cache)
        if cached.attrs.get("fingerprint") == fp and cached.attrs.get("version") == cfg.version:
            sig = cached
    if sig is None:
        sig = replay(store, cfg, params, progress=progress)
        sig.attrs.update({"fingerprint": fp, "version": cfg.version})
        if signals_cache:
            signals_cache.parent.mkdir(parents=True, exist_ok=True)
            sig.to_pickle(signals_cache)
    rep = evaluate(sig, store, params, include_holdout)
    with session_scope(engine) as s:
        sv = _strategy_version(s, cfg)
        vr = ValidationRun(strategy_version_id=sv.id, period_start=date.fromisoformat(rep.period["debut"]),
                           period_end=date.fromisoformat(rep.period["fin"]), holdout_evaluated=include_holdout,
                           data_fingerprint=fp, params_json=json.dumps(rep.params, default=str),
                           report_json=json.dumps(asdict(rep), default=str),
                           verdicts_json=json.dumps(rep.verdicts, ensure_ascii=False))
        s.add(vr)
        s.flush()
        return vr.id, rep, prior


# =========================================================================== rapport
def _pct(x, signed=True):
    if x is None:
        return "N/D"
    return (f"{x * 100:+.2f} %" if signed else f"{x * 100:.1f} %").replace(".", ",")


def _num(x, d=2):
    return "N/D" if x is None else f"{x:.{d}f}".replace(".", ",")


def render_markdown(run_id: int, rep: ValidationReport, cfg: ScoringConfig, prior_holdout: int) -> str:
    p = rep.params
    L = [f"# Validation historique — {cfg.name} {cfg.version} (run #{run_id})", ""]
    L += ["> Résultats **simulés** sur données passées, frais inclus. Ils ne garantissent aucun résultat futur.",
          "> Un score n'est pas une probabilité de hausse.", ""]
    L += ["## Verdicts", "", "| Stratégie / groupe | Décision | Critères |", "|---|---|---|"]
    for name, v in rep.verdicts.items():
        crit = " ; ".join(f"{'✅' if c['ok'] else '❌'} {k} ({c['valeur']})" for k, c in v["criteres"].items())
        L.append(f"| {name} | **{v['decision']}** | {crit} |")
    L += ["", "## Périmètre et limites", ""]
    L += [f"- Rejeu du moteur de scoring réel tous les {p['every_n_sessions']} jours de bourse, du "
          f"{rep.period['debut']} au {rep.period['fin']} ({rep.period['dates_rejouees']} dates, {rep.coverage['actions']} actions).",
          f"- Période de développement : jusqu'au {rep.period.get('fin_developpement')} (zone tampon de "
          f"{p['embargo_days']} jours avant la période réservée qui commence le {rep.period['holdout_debut']}).",
          f"- Période réservée (holdout) : {'**évaluée dans ce rapport**' if rep.period['holdout_evalue'] else 'NON évaluée (réservée pour une méthode figée)'}"
          + (f" — ⚠️ déjà évaluée {prior_holdout} fois auparavant pour cette version" if rep.period['holdout_evalue'] and prior_holdout else ""),
          f"- Entrée à l'ouverture du lendemain du signal ; frais {p['cost_bps_per_side']:g} pb par côté "
          f"(sensibilité {', '.join(f'{c:g}' for c in p['cost_sensitivity_bps'])} pb) ; prix ajustés (dividendes, splits).",
          f"- Sorties testées : stop initial {p['stop_atr']:g}×ATR, stop suiveur {p['trail_atr']:g}×ATR sous le plus haut, "
          f"durée max {p['max_hold_sessions']} séances ; un gap sous le stop est exécuté à l'ouverture.",
          "- t-stats : moyennes par date, écart-type de Newey-West (fenêtres qui se chevauchent).",
          "- ⚠️ **Biais du survivant** : l'univers = 42 sociétés qui sont de grandes capitalisations aujourd'hui. "
          "Leur rendement passé est donc surestimé ; la référence pertinente est l'**univers équipondéré**, "
          "qui porte le même biais (colonne « vs univers »).",
          f"- Historique des publications de résultats (8-K) complet sur {_pct(rep.coverage['couverture_8K_moyenne'], False)} "
          "des couples action/date en moyenne ; la stratégie B n'est évaluée que là où il l'est.", ""]
    for label, rows in rep.event_study.items():
        L += [f"## Rendements après signal — {label}", "",
              "| Groupe | Signaux | Rdt moyen 20 j | % gagnants | vs SPY 20 j | vs univers 20 j | t | vs univers 60 j | t |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for r in rows:
            L.append(f"| {r['groupe']} | {r['signaux']} | {_pct(r.get('moy_20'))} | {_pct(r.get('gagnants_20'), False)} | "
                     f"{_pct(r.get('xs_spy_20'))} | {_pct(r.get('xs_ew_20'))} | {_num(r.get('t_xs_ew_20'))} | "
                     f"{_pct(r.get('xs_ew_60'))} | {_num(r.get('t_xs_ew_60'))} |")
        ic = rep.ic.get(label, [])
        L += ["", "Pouvoir prédictif du score (corrélation de rang score / rendement futur, par date) :", "",
              "| Horizon | IC moyen | t | % de dates IC > 0 |", "|---:|---:|---:|---:|"]
        L += [f"| {x['horizon']} j | {_num(x['ic_moyen'], 3)} | {_num(x['t_stat'])} | {_pct(x['part_ic_positif'], False)} |" for x in ic]
        L += ["", f"### Trades simulés ({label}, frais {p['cost_bps_per_side']:g} pb/côté)", "",
              "| Groupe | Trades | % gagnants | Rdt net moyen | Profit factor | Espérance (R) | Durée moy. | Rdt net à 0 / 25 pb |",
              "|---|---:|---:|---:|---:|---:|---:|---|"]
        key = f"{p['cost_bps_per_side']:g} bps"
        for name, by_cost in rep.trades.get(label, {}).items():
            m = by_cost.get(key, {})
            sens = " / ".join(_pct(by_cost.get(f"{c:g} bps", {}).get("moy_net")) for c in (0, 25))
            L.append(f"| {name} | {m.get('trades', 0)} | {_pct(m.get('gagnants'), False)} | {_pct(m.get('moy_net'))} | "
                     f"{_num(m.get('profit_factor'))} | {_num(m.get('esperance_R'))} | {_num(m.get('duree_moy'), 1)} | {sens} |")
        L += ["", f"### Portefeuille simulé ({label}) : max {p['max_positions']} positions de {p['position_weight']:.0%}", "",
              "| Portefeuille | Rdt annualisé | Drawdown max | Volatilité | Sharpe | Sortino |", "|---|---:|---:|---:|---:|---:|"]
        for name, m in rep.portfolio.get(label, {}).items():
            L.append(f"| {name} | {_pct(m.get('rendement_annualise'))} | {_pct(m.get('drawdown_max'))} | "
                     f"{_pct(m.get('volatilite'), False)} | {_num(m.get('sharpe'))} | {_num(m.get('sortino'))} |")
        L.append("")
    for label, groups in rep.folds.items():
        L += [f"## Stabilité par année ({label}) — rendement 20 j vs univers", ""]
        years = sorted({r["annee"] for rows in groups.values() for r in rows})
        L += ["| Groupe | " + " | ".join(years) + " |", "|---|" + "---:|" * len(years)]
        for name, rows in groups.items():
            if name.startswith("Référence"):
                continue
            d = {r["annee"]: r for r in rows}
            L.append(f"| {name} | " + " | ".join(_pct(d[y]["xs_ew"]) if y in d else "–" for y in years) + " |")
        L.append("")
    FACTOR_LABELS = {"sub_": "sous-score ", "f_": ""}
    for label, rows in rep.factors.items():
        L += [f"## Diagnostic facteur par facteur ({label}) — IC à 20 j", "",
              "Chaque ingrédient pris isolément. Avec ~25 facteurs testés, 1 ou 2 |t| > 2 peuvent apparaître par hasard.", "",
              "| Facteur | IC moyen | t | % de dates IC > 0 |", "|---|---:|---:|---:|"]
        for r in rows:
            name = r["facteur"]
            for k, v in FACTOR_LABELS.items():
                if name.startswith(k):
                    name = v + name[len(k):]
            L.append(f"| {name} | {_num(r['ic_moyen'], 3)} | {_num(r['t_stat'])} | {_pct(r['part_ic_positif'], False)} |")
        L.append("")
    for label, groups in rep.regimes.items():
        L += [f"## Par régime de marché ({label}) — rendement 20 j", "",
              "| Groupe | Régime | Signaux | Rdt moyen | vs univers | % gagnants |", "|---|---|---:|---:|---:|---:|"]
        for name, rows in groups.items():
            for r in rows:
                L.append(f"| {name} | {r['regime']} | {r['signaux']} | {_pct(r['moy'])} | {_pct(r['xs_ew'])} | {_pct(r['gagnants'], False)} |")
        L.append("")
    return "\n".join(L)
