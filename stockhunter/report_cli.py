"""Commandes de consultation du scanner : scan, top, explain, history."""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import func, select

from stockhunter.db.models import Instrument, ScanRun, Score, StatusChange, StrategyVersion, ValidationRun
from stockhunter.db.session import session_scope
from stockhunter.scan import ScanError, run_scan
from stockhunter.scoring.engine import fmt_pct

DISCLAIMER = ("Score = classement relatif multicritère au sein de l'univers suivi, PAS une probabilité de hausse. "
              "Stratégies au stade RESEARCH : non validées par backtest (phase 3). Aucun conseil d'investissement.")
STATUS_ORDER = {"READY": 0, "DEVELOPING": 1, "WATCHLIST": 2, "REJECTED": 3}


def validation_line(s, strategy_version_id: int) -> str:
    """Résumé de la dernière validation historique de cette version, affiché avec chaque classement."""
    vr = s.scalars(select(ValidationRun).where(ValidationRun.strategy_version_id == strategy_version_id)
                   .order_by(ValidationRun.holdout_evaluated.desc(), ValidationRun.id.desc()).limit(1)).first()
    if vr is None:
        # La validation a pu être faite dans un autre environnement : le registre versionné fait foi.
        from stockhunter.db.models import StrategyVersion as _SV
        from stockhunter.validation.run import LEDGER
        sv = s.get(_SV, strategy_version_id)
        entries = []
        if sv is not None and LEDGER.exists():
            entries = [c for c in json.loads(LEDGER.read_text(encoding="utf-8")).get("consultations", [])
                       if c.get("name") == sv.name and c.get("version") == sv.version]
        if entries:
            c = entries[-1]
            return (f"Validation historique ({c['date']}, période réservée incluse) : {c['verdict'].upper()} — "
                    f"le classement est informatif, pas un signal fiable (rapport : {c['rapport']}).")
        return "Validation historique : aucune (stratégies non testées)."
    v = json.loads(vr.verdicts_json)
    ok = [k for k, x in v.items() if x["decision"].startswith("ÉLIGIBLE")]
    rej = [k.split(" ")[0] for k, x in v.items() if x["decision"].startswith("REJETÉE")]
    scope = "y compris période réservée" if vr.holdout_evaluated else "période de développement seulement"
    head = f"Validation historique (run #{vr.id}, {scope}) : "
    if ok:
        return head + "validées : " + ", ".join(ok) + (f" ; rejetées : {', '.join(rej)}" if rej else "")
    return head + "AUCUNE stratégie validée — le classement est informatif, pas un signal fiable" + \
        (f" ; rejetées : {', '.join(rej)}" if rej else "") + "."


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
        vline = validation_line(s, run.strategy_version_id)

    print(f"StockHunter — scan #{run.id} | données de clôture au {run.as_of} | {sv.name} {sv.version} [{sv.stage}]")
    print(vline)
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
        vline = validation_line(s, run.strategy_version_id)

    sub, strat = json.loads(sc.subscores_json), json.loads(sc.strategies_json)
    m, src = json.loads(sc.metrics_json), json.loads(sc.sources_json)
    print(f"{inst.symbol} — {inst.name or ''} | {inst.sector or 'N/D'} ({inst.sector_etf or '-'})")
    close = f"{sc.close:.2f} $".replace(".", ",") if sc.close else "N/D"
    print(f"Clôture du {sc.price_date} : {close} (cours de clôture, pas un cours en temps réel)")
    print(f"Scan #{run.id} du {run.as_of} — {sv.name} {sv.version} [{sv.stage}]")
    print(vline)
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


def cmd_market(settings, engine, args) -> int:
    from datetime import date as _date

    from stockhunter.data.http import ProviderError
    from stockhunter.db.models import MarketSplit, MarketTicker
    from stockhunter.jobs.runner import run_job
    from stockhunter.market.provider import MassiveProvider
    from stockhunter.market.store import MarketStore
    from stockhunter.market.sync import missing_sessions, sync_reference, sync_sessions, sync_splits

    store = MarketStore(settings.market_dir)
    if args.action == "sync":
        try:
            mv = MassiveProvider(settings, engine)
        except ProviderError as exc:
            print(f"IGNORÉ : {exc}")
            return 2
        today = _date.today()

        def job(ctx):
            sync_reference(engine, ctx, mv, force=args.reference)
            sync_splits(engine, ctx, mv, today, settings.market_history_days)
            sync_sessions(ctx, mv, store, today, settings.market_history_days, args.max_calls)

        run = run_job(engine, "market_sync", job)
        print(f"[market_sync] statut={run.status} ok={run.items_ok} échecs={run.items_failed}")
        if run.detail:
            print("  " + run.detail.replace("\n", "\n  ")[:2000])
    sessions = store.sessions()
    todo = missing_sessions(store, _date.today(), settings.market_history_days)
    with session_scope(engine) as s:
        n_t = s.scalar(select(func.count()).select_from(MarketTicker))
        n_cs = s.scalar(select(func.count()).select_from(MarketTicker).where(MarketTicker.type == "CS"))
        n_del = s.scalar(select(func.count()).select_from(MarketTicker).where(MarketTicker.active.is_(False)))
        n_sp = s.scalar(select(func.count()).select_from(MarketSplit))
    print(f"Univers large (Massive) : {len(sessions)} séances stockées"
          + (f" ({sessions[0]} → {sessions[-1]})" if sessions else "")
          + f", {len(todo)} manquante(s) sur les {settings.market_history_days} visées")
    print(f"Référentiel : {n_t} titres dont {n_cs} actions ordinaires, {n_del} retirés de la cote ; {n_sp} splits")
    return 0 if args.action == "status" or not todo else 2


def cmd_research(settings, engine, args) -> int:
    import json as _json
    from pathlib import Path

    from stockhunter.market.store import MarketStore
    from stockhunter.research import broad
    from stockhunter.validation.run import LEDGER

    ledger = _json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else {"consultations": []}
    seen = [c for c in ledger["consultations"] if c["name"] == broad.STUDY]
    if args.holdout and seen and not args.allow_holdout_again:
        print(f"REFUS : la période réservée de l'étude {broad.STUDY} a déjà été évaluée le {seen[0]['date']}.")
        return 1
    try:
        res = broad.run_study(engine, MarketStore(settings.market_dir), include_holdout=args.holdout)
    except RuntimeError as exc:
        print(f"ÉCHEC : {exc}")
        return 1
    md = broad.render(res)
    if args.holdout:
        md += ("\n\n> Ajouter cette consultation au registre `stockhunter/validation/holdout_ledger.json` "
               f"(name = {broad.STUDY}).")
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
    print(md)
    return 0


def cmd_paper(settings, engine, args) -> int:
    from stockhunter.portfolio import paper
    if args.action == "run":
        cfg = paper.load_config(engine)
        created = paper.plan_from_scan(engine, cfg=cfg)
        counts = paper.process(engine, cfg)
        print(f"Plans créés : {len(created)} ; " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    sm = paper.summary(engine)
    wr = "N/D" if sm["win_rate"] is None else f"{sm['win_rate']:.0%}"
    print(f"Portefeuille SIMULÉ : valeur {sm['equity']:,.0f} $ (capital {sm['capital']:,.0f} $), réalisé "
          f"{sm['realized']:+,.0f} $, latent {sm['unrealized']:+,.0f} $".replace(",", " "))
    print(f"Trades : {sm['trades_total']} (plans {sm['planned']}, ouverts {sm['open']}, clos {sm['closed']}, "
          f"annulés {sm['cancelled']}) ; gagnants {wr}")
    for p_ in sm["positions"]:
        print(f"  {p_['symbol']:6} {p_['shares']:g} @ {p_['entry']:.2f} depuis {p_['entry_date']} → {p_['last']} "
              f"({p_['last_date']}) stop {p_['stop']:.2f} TP1 {p_['tp1']:.2f} TP2 {p_['tp2']:.2f}")
    return 0


def cmd_forward(settings, engine, args) -> int:
    from stockhunter.market.store import MarketStore
    from stockhunter.research import forward
    st = MarketStore(settings.market_dir)
    try:
        if args.action == "record":
            print(f"Test prospectif : {forward.record(engine, st)} titre(s) figé(s) dans les paniers.")
        print(forward.render(forward.report(engine, st)))
    except RuntimeError as exc:
        print(f"IGNORÉ : {exc}")
        return 2
    return 0


def cmd_notify(settings, engine, args) -> int:
    from stockhunter.notifications.engine import run_notifications
    if args.preview:
        from stockhunter.reports.daily import build_report
        print(build_report(engine, args.kind)[0])
        return 0
    out = run_notifications(engine, settings, args.kind, force=args.force)
    print("Notifications : " + ", ".join(f"{k} = {v}" for k, v in out.items()))
    return 1 if out.get("echecs") else 0


def cmd_dashboard(settings, engine, args) -> int:
    if args.action == "hash-password":
        import getpass
        import secrets as _secrets

        from stockhunter.dashboard.auth import hash_password
        pw = getpass.getpass("Mot de passe du dashboard (12 caractères min.) : ")
        if len(pw) < 12:
            print("Trop court : 12 caractères minimum.")
            return 1
        if pw != getpass.getpass("Confirmer : "):
            print("Les deux saisies diffèrent.")
            return 1
        print("\nÀ placer dans les variables d'environnement (jamais dans le code) :")
        print(f"SH_DASHBOARD_PASSWORD_HASH={hash_password(pw)}")
        print(f"SH_DASHBOARD_SECRET={_secrets.token_urlsafe(48)}")
        return 0
    import uvicorn

    from stockhunter.dashboard.app import create_app
    app = create_app(engine, settings, secure_cookies=not args.insecure_cookies)
    if args.telegram_worker:
        from stockhunter.notifications import worker
        worker.start(engine, settings)
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")
    return 0


def cmd_db(settings, engine, args) -> int:
    from stockhunter.db.copy import copy_database
    from stockhunter.db.session import get_engine
    target = get_engine(args.target)
    print(f"Copie de {engine.url.render_as_string(hide_password=True)} vers "
          f"{target.url.render_as_string(hide_password=True)}")
    try:
        copied = copy_database(engine, target)
    except RuntimeError as exc:
        print(f"ÉCHEC : {exc}")
        return 1
    print(f"Copie vérifiée : {sum(copied.values())} lignes dans {sum(1 for v in copied.values() if v)} tables.")
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
    mk = sub.add_parser("market", help="univers large : toutes les actions US (Massive)")
    mk.add_argument("action", choices=["sync", "status"])
    mk.add_argument("--max-calls", type=int, help="nombre max de séances à télécharger dans ce run")
    mk.add_argument("--reference", action="store_true", help="forcer le rafraîchissement du référentiel")
    rs = sub.add_parser("research", help="études pré-enregistrées")
    rs.add_argument("study", choices=["broad"])
    rs.add_argument("--holdout", action="store_true")
    rs.add_argument("--allow-holdout-again", action="store_true")
    rs.add_argument("--out")
    pp = sub.add_parser("paper", help="portefeuille simulé (paper trading)")
    pp.add_argument("action", choices=["run", "status"])
    fw = sub.add_parser("forward", help="test prospectif des facteurs pré-enregistrés")
    fw.add_argument("action", choices=["record", "report"])
    nt = sub.add_parser("notify", help="rapport quotidien et alertes Telegram")
    nt.add_argument("kind", choices=["morning", "evening"])
    nt.add_argument("--force", action="store_true", help="ignorer l'heure de Paris prévue")
    nt.add_argument("--preview", action="store_true", help="afficher le rapport sans rien envoyer")
    db = sub.add_parser("dashboard", help="interface web")
    db.add_argument("action", choices=["serve", "hash-password"])
    db.add_argument("--host", default="127.0.0.1")
    db.add_argument("--port", type=int, default=8000)
    db.add_argument("--insecure-cookies", action="store_true", help="HTTP local uniquement (pas de HTTPS)")
    db.add_argument("--telegram-worker", action="store_true", help="répondre aux commandes Telegram en continu")
    cp = sub.add_parser("db", help="copie de la base vers une autre (migration vers PostgreSQL)")
    cp.add_argument("action", choices=["copy-to"])
    cp.add_argument("target", help="URL de la base cible, déjà migrée et vide")
    return {"scan": cmd_scan, "top": cmd_top, "explain": cmd_explain, "validate": cmd_validate, "market": cmd_market,
            "research": cmd_research, "paper": cmd_paper, "forward": cmd_forward, "notify": cmd_notify,
            "dashboard": cmd_dashboard, "db": cmd_db}
