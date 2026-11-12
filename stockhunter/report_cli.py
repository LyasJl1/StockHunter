"""Commandes de consultation du scanner : scan, top, explain, history."""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import select

from stockhunter.db.models import Instrument, ScanRun, Score, StatusChange, StrategyVersion
from stockhunter.db.session import session_scope
from stockhunter.scan import ScanError, run_scan
from stockhunter.scoring.engine import fmt_pct

DISCLAIMER = ("Score = classement relatif multicritère au sein de l'univers suivi, PAS une probabilité de hausse. "
              "Stratégies au stade RESEARCH : non validées par backtest (phase 3). Aucun conseil d'investissement.")
STATUS_ORDER = {"READY": 0, "DEVELOPING": 1, "WATCHLIST": 2, "REJECTED": 3}


def _latest_run(s, run_id: int | None = None) -> ScanRun | None:
    q = select(ScanRun)
    q = q.where(ScanRun.id == run_id) if run_id else q.order_by(ScanRun.as_of.desc(), ScanRun.id.desc())
    return s.scalars(q.limit(1)).first()


def cmd_scan(settings, engine, args) -> int:
    try:
        as_of = date.fromisoformat(args.as_of) if args.as_of else None
        if args.if_new or args.require_fresh:
            from stockhunter.scan import build_inputs, existing_scan, stale_instruments
            target = build_inputs(engine, as_of)[0]
            if args.require_fresh:
                stale = stale_instruments(engine, target)
                if stale:
                    print(f"Scan reporté : {len(stale)} série(s) de prix pas à jour au {target} "
                          f"({', '.join(stale[:10])}{'…' if len(stale) > 10 else ''}).")
                    return 0
            if args.if_new and (prev := existing_scan(engine, target)):
                print(f"Scan déjà réalisé pour le {target} (#{prev}) : rien à faire.")
                return 0
        run_id = run_scan(engine, as_of)
    except ScanError as exc:
        print(f"ÉCHEC du scan : {exc}")
        return 1
    print(f"Scan #{run_id} enregistré.")
    args.n, args.run, args.all = 10, run_id, False
    return cmd_top(settings, engine, args)


def cmd_top(settings, engine, args) -> int:
    with session_scope(engine) as s:
        run = _latest_run(s, getattr(args, "run", None))
        if run is None:
            print("Aucun scan : lancer `stockhunter scan`.")
            return 1
        sv = s.get(StrategyVersion, run.strategy_version_id)
        rows = s.execute(select(Score, Instrument.symbol).join(Instrument).where(Score.scan_run_id == run.id)).all()
        changes = s.execute(select(StatusChange, Instrument.symbol).join(Instrument)
                            .where(StatusChange.scan_run_id == run.id)).all()
        regime = json.loads(run.regime_json)
        sectors = json.loads(run.sectors_json)

    print(f"StockHunter — scan #{run.id} | données de clôture au {run.as_of} | {sv.name} {sv.version} [{sv.stage}]")
    print(f"Régime de marché : {regime['label']} — " + " ; ".join(regime["reasons"]))
    top_sec = sorted(sectors.items(), key=lambda x: x[1]["rank"])
    print("Secteurs (force relative 1-3 m) : " + ", ".join(f"{k} {fmt_pct(v['rs_blend'])}" for k, v in top_sec))
    if run.notes:
        print(f"Notes : {run.notes}")
    print(f"Couverture : {run.scored} actions scorées sur {run.universe_size} instruments suivis (pas tout le marché).")

    counts = {}
    for sc, _ in rows:
        counts[sc.status] = counts.get(sc.status, 0) + 1
    print("Statuts : " + ", ".join(f"{k}={counts.get(k, 0)}" for k in STATUS_ORDER))

    shown = sorted((r for r in rows if args.all or r[0].status in ("READY", "DEVELOPING")),
                   key=lambda r: (STATUS_ORDER[r[0].status], -(r[0].total or 0), r[1]))
    if not args.all:
        shown = shown[: args.n]
    if not shown:
        print("\nAucune action ne remplit les critères DEVELOPING/READY aujourd'hui (`top --all` pour tout voir).")
    else:
        print(f"\n{'#':>3} {'Action':6} {'Score':>5} {'Conf.':>5} {'Statut':10} {'Clôture':>10} {'Date':10}  Stratégies actives")
        for sc, sym in shown:
            strat = json.loads(sc.strategies_json)
            act = ",".join(f"{k}" for k, v in strat.items() if v.get("active")) or "-"
            close = f"{sc.close:.2f}".replace(".", ",") if sc.close else "N/D"
            print(f"{sc.rank or '-':>3} {sym:6} {sc.total if sc.total is not None else 'N/D':>5} {sc.confidence:>5.2f} "
                  f"{sc.status:10} {close:>10} {sc.price_date}  {act}")
    if changes:
        print("\nChangements de statut :")
        for ch, sym in sorted(changes, key=lambda x: x[1]):
            print(f"  {sym:6} {ch.from_status or '(nouveau)'} -> {ch.to_status} : {ch.reason}")
    print(f"\n{DISCLAIMER}")
    return 0


def cmd_explain(settings, engine, args) -> int:
    with session_scope(engine) as s:
        inst = s.scalar(select(Instrument).where(Instrument.symbol == args.symbol.upper()))
        if inst is None:
            print("Symbole hors univers.")
            return 1
        q = select(Score, ScanRun).join(ScanRun).where(Score.instrument_id == inst.id)
        q = q.where(ScanRun.id == args.run) if args.run else q.order_by(ScanRun.as_of.desc(), ScanRun.id.desc())
        row = s.execute(q.limit(1)).first()
        if row is None:
            print("Aucun score pour ce symbole.")
            return 1
        sc, run = row
        hist = s.execute(select(StatusChange).where(StatusChange.instrument_id == inst.id)
                         .order_by(StatusChange.as_of, StatusChange.id)).scalars().all()
        sv = s.get(StrategyVersion, run.strategy_version_id)

    sub, strat = json.loads(sc.subscores_json), json.loads(sc.strategies_json)
    m, src = json.loads(sc.metrics_json), json.loads(sc.sources_json)
    print(f"{inst.symbol} — {inst.name or ''} | {inst.sector or 'N/D'} ({inst.sector_etf or '-'})")
    close = f"{sc.close:.2f} $".replace(".", ",") if sc.close else "N/D"
    print(f"Clôture du {sc.price_date} : {close} (cours de clôture, pas un cours en temps réel)")
    print(f"Scan #{run.id} du {run.as_of} — {sv.name} {sv.version} [{sv.stage}]")
    print(f"\nSCORE {sc.total}/100 | confiance données {sc.confidence:.2f} | rang {sc.rank or '-'} | statut {sc.status}")
    print(f"  Motif du statut : {sc.status_reason}")
    print("\nSous-scores (poids) :")
    for k, v in sub.items():
        val = "N/D (neutre 50)" if v["score"] is None else v["score"]
        print(f"  {v['label']:32} {val!s:>16}  ({v['weight'] * 100:.0f} %, couverture {v['coverage']:.0%})")
        for ck, cv in v.get("components", {}).items():
            if cv is not None:
                print(f"      · {ck} : {round(cv, 1) if isinstance(cv, float) else cv}")
    print("\nStratégies :")
    for k, v in strat.items():
        state = "ACTIVE" if v.get("active") else ("—" if not v.get("implemented", True) else "inactive")
        extra = f" — manque : {', '.join(v['missing'])}" if v.get("missing") and not v.get("active") else ""
        detail = f" ({v['detail']})" if v.get("detail") else ""
        print(f"  {k}. {v['label']} : {state}{detail}{extra}")
    print("\nPourquoi elle pourrait monter :")
    for r in json.loads(sc.reasons_pos_json) or ["(aucun argument favorable identifié)"]:
        print(f"  + {r}")
    print("Pourquoi rester prudent :")
    for r in json.loads(sc.reasons_neg_json) or ["(aucun point défavorable identifié)"]:
        print(f"  - {r}")
    print("\nIndicateurs :")
    for k, v in m.items():
        if v is not None:
            print(f"  {k} : {round(v, 4) if isinstance(v, float) else v}")
    print("\nSources :")
    for x in src:
        print(f"  [{x['type']}] {x['source']} — {x['detail']}" + (f" — {x['url']}" if x.get("url") else ""))
    if hist:
        print("\nHistorique des statuts :")
        for h in hist:
            print(f"  {h.as_of} : {h.from_status or '(nouveau)'} -> {h.to_status} ({h.reason})")
    print(f"\n{DISCLAIMER}")
    return 0


def cmd_validate(settings, engine, args) -> int:
    from pathlib import Path

    from stockhunter.scoring.config import DEFAULT_CONFIG
    from stockhunter.validation.run import render_markdown, run_validation
    try:
        run_id, rep, prior = run_validation(
            engine, include_holdout=args.holdout, allow_holdout_again=args.allow_holdout_again,
            signals_cache=Path(args.signals_cache) if args.signals_cache else None,
            progress=lambda k, n: print(f"  rejeu {k}/{n}", flush=True) if k % 50 == 0 or k == n else None)
    except ScanError as exc:
        print(f"ÉCHEC : {exc}")
        return 1
    md = render_markdown(run_id, rep, DEFAULT_CONFIG, prior)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"Rapport écrit dans {args.out}")
    print(md)
    return 0


def add_parsers(sub) -> dict:
    sc = sub.add_parser("scan", help="calcule et enregistre le classement")
    sc.add_argument("--as-of", help="date d'analyse AAAA-MM-JJ (séance clôturée), défaut : dernière séance")
    sc.add_argument("--if-new", action="store_true", help="ne rien faire si un scan existe déjà pour cette séance")
    sc.add_argument("--require-fresh", action="store_true", help="reporter si des séries de prix sont en retard")
    tp = sub.add_parser("top", help="affiche le dernier classement")
    tp.add_argument("-n", type=int, default=10)
    tp.add_argument("--all", action="store_true", help="toutes les actions, y compris WATCHLIST/REJECTED")
    tp.add_argument("--run", type=int)
    ex = sub.add_parser("explain", help="fiche explicative d'une action")
    ex.add_argument("symbol")
    ex.add_argument("--run", type=int)
    va = sub.add_parser("validate", help="backtest du moteur de scoring réel (phase 3)")
    va.add_argument("--holdout", action="store_true", help="évalue aussi la période réservée (une fois, méthode figée)")
    va.add_argument("--allow-holdout-again", action="store_true")
    va.add_argument("--out", help="fichier Markdown du rapport")
    va.add_argument("--signals-cache", help="cache du rejeu (réutilisé si données et version identiques)")
    return {"scan": cmd_scan, "top": cmd_top, "explain": cmd_explain, "validate": cmd_validate}
