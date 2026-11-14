"""Commandes de consultation du scanner : scan, top, explain, history."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

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
                    return 2          # partiel attendu : le passage suivant terminera (quota horaire des prix)
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
        rep = forward.report(engine, st)
        # résultat enregistré pour le dashboard (le serveur web n'a pas les données de marché complètes)
        from stockhunter.db.models import KeyValue
        payload = json.dumps({**rep, "libelles": forward.labels(), "genere_le": date.today().isoformat()}, ensure_ascii=False)
        with session_scope(engine) as s:
            kv = s.get(KeyValue, "forward_report")
            if kv is None:
                s.add(KeyValue(key="forward_report", value=payload))
            else:
                kv.value = payload
        print(forward.render(rep))
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
    from stockhunter.db.copy import copy_database, tune
    from stockhunter.db.session import get_engine
    if args.action == "tune":
        tune(engine)
        return 0
    if args.action == "check":
        from stockhunter.db.copy import storage
        from stockhunter.notifications.engine import enqueue
        st = storage(engine, settings.db_volume_mb)
        print(f"Stockage : {st}")
        if st["niveau"] == "ok":
            return 0
        msg = (f"<b>STOCKHUNTER AI — Stockage de la base : {st['niveau'].upper()}</b>\n"
               f"{st['utilise_mo']:.0f} Mo utilisés sur {st['volume_mo']:.0f} Mo ({st['part'] * 100:.0f} %), dont WAL "
               f"{st['wal_mo']} Mo. " + ("Chaîne nocturne arrêtée pour éviter la saturation : intervention requise "
               "(workflow « Sauvegarde de la base » avec entretien)." if st["niveau"] == "critique" else
               "Aucune action immédiate ; surveiller la page Santé."))
        enqueue(engine, "alert", f"storage:{date.today()}:{st['niveau']}", msg)
        return 3 if st["niveau"] == "critique" else 2
    if args.action == "diag":
        from stockhunter.db.copy import diagnose
        print(json.dumps(diagnose(engine), ensure_ascii=False, indent=1, default=str))
        return 0
    if not args.target:
        print("ÉCHEC : URL de la base cible manquante")
        return 1
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


def cmd_analyse(settings, engine, args) -> int:
    from stockhunter.analysis.synthesis import build, to_markdown
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    syn = build(engine, args.symbol, as_of)
    if syn is None:
        print(f"{args.symbol.upper()} ne fait pas partie de l'univers suivi.")
        return 1
    text = to_markdown(syn)
    if args.out:
        Path(args.out).write_text(text)
        print(f"Synthèse écrite dans {args.out}")
    else:
        print(text)
    return 0


def cmd_discover(settings, engine, args) -> int:
    """Moteur de découverte v3 : classement multi-catégories de l'univers large, analyse approfondie, suivi."""
    from stockhunter.discovery import engine as disc
    from stockhunter.discovery import store as dstore
    from stockhunter.lab.panel import load_panel
    from stockhunter.market.store import MarketStore
    if args.action == "status":
        perf = dstore.performance(engine)
        if not perf:
            print("Aucun signal de découverte enregistré.")
        for cat, r in perf.items():
            print(f"  {cat:10} signaux {r['signaux']:4}  finalisés {r['finalises']:4}  trades {r['trades']:4}  "
                  f"R moyen {r['r_moyen'] if r['r_moyen'] is None else round(r['r_moyen'], 2)}  ({r['lecture']})")
        return 0
    from stockhunter.pipeline import expected_session
    from stockhunter.world.run import annotate
    try:
        p = load_panel(engine, MarketStore(settings.market_dir))
    except RuntimeError as exc:                      # données absentes : alerte (code 2), pas une panne technique
        annotate("warning", "Découverte : données absentes", str(exc))
        return 2
    res = disc.run(p, dstore.ticker_names(engine), validation=dstore.registry_decisions())
    run_id, created = dstore.save(engine, res)
    print(f"Découverte du {res['as_of']} : univers {res['univers']} titres, marché {res['marche']['regime']} "
          f"({'enregistrée' if created else 'déjà enregistrée, non réécrite'}, #{run_id})")
    for k, v in res["categories"].items():
        names = ", ".join(c["ticker"] for c in v["candidats"][:10])
        print(f"  {v['label']:36} admis {v['admis']:4}  {'SUSPENDUE' if v['suspendue'] else names or '—'}")
    print(res["conclusion"])
    if created:
        sec = fin = None
        try:
            sec = SecEdgarProviderLazy(settings, engine)
        except Exception as exc:
            print(f"  analyse SEC indisponible : {exc}")
        try:
            from stockhunter.data.providers import FinnhubProvider
            fin = FinnhubProvider(settings, engine)
        except Exception as exc:
            print(f"  actualités indisponibles : {exc}")
        print(f"Analyse approfondie : {dstore.deepen(engine, res, p, sec, fin)}")
    print(f"Suivi des signaux : {dstore.update_outcomes(engine, p)}")
    n = sum(len(v["candidats"]) for v in res["categories"].values())
    if n == 0:
        annotate("notice", "Découverte : aucune opportunité", f"séance du {res['as_of']} : aucune configuration ne "
                 f"remplit les règles (univers {res['univers']} titres). Résultat normal, pas une panne.")
    exp = expected_session()
    if res["as_of"] < exp:                           # séance attendue absente : réessayée au créneau suivant
        annotate("warning", "Découverte : séance attendue absente", f"dernière séance des données de marché "
                 f"{res['as_of']}, séance attendue {exp} : classement du {exp} non encore produit")
        return 2
    return 0


def cmd_world(settings, engine, args) -> int:
    """3.2 : marchés mondiaux (registre, collecte, scanners, classement, portefeuille simulé)."""
    from stockhunter.world import markets as wm
    from stockhunter.world import run as wr
    from stockhunter.world.store import WorldStore
    store = WorldStore(settings.world_dir, settings.world_budget_mb)
    codes = tuple(c.strip().upper() for c in args.markets.split(",")) if args.markets else None
    if args.action == "markets":
        for m in wm.MARKETS.values():
            print(f"{m.code:5} {m.country} {m.region:16} {m.currency} {m.data:40} {m.name}")
        return 0
    if args.action == "sync":
        return wr.collect(engine, store, backfill_sessions=args.backfill if args.backfill is not None
                          else settings.world_backfill_sessions,
                          max_files=args.max_files or settings.world_max_files, markets=codes)
    if args.action == "scan":
        return wr.scan_all(engine, store, codes)
    if args.action in ("archive", "archive-verify", "restore"):
        from stockhunter.world import archive as wa
        mk_codes = codes or wa.NON_RECONSTRUCTIBLE
        rep = (wa.push(engine, store, mk_codes, budget_mb=args.budget_mb) if args.action == "archive" else
               wa.verify(engine, store, mk_codes) if args.action == "archive-verify" else wa.restore(engine, store, mk_codes))
        print(f"{args.action} : archivées {rep.archived}, déjà {rep.already}, restaurées {rep.restored}, vérifiées "
              f"{rep.verified}, divergences {len(rep.mismatches)}, absentes localement {len(rep.missing_local)}, "
              f"non archivées {len(rep.missing_archive)}, archive {rep.size_mb} Mo" + (f", REFUS : {rep.refused}" if rep.refused else ""))
        for x in rep.mismatches[:10]:
            print("  DIVERGENCE", x)
        print(wa.status(engine))
        return 1 if rep.mismatches else (2 if rep.refused or rep.missing_archive else 0)
    if args.action == "portfolio":
        return wr.run_portfolio(engine, store, args.base)
    if args.action == "replay":                      # étude pré-enregistrée : rien n'est écrit en base
        import json as _json
        from stockhunter.world import replay as rp
        from stockhunter.world.fx import FxTable
        from stockhunter.world.portfolio import GlobalConfig, benchmarks, simulate
        fx = FxTable.from_long(store.load_fx())
        out = []
        for code in codes or ("XNSE",):
            res = rp.replay(store, code, fx)
            print(f"Réplication {code} : {res['statut']} — {res.get('dates_signal', 0)} dates de signal "
                  f"({res.get('premiere')} → {res.get('derniere')})")
            for k, c in res["categories"].items():
                t = "N/D" if c["t"] is None else f"{c['t']:+.2f}"
                ex = "N/D" if c["exces_net_moyen"] is None else f"{c['exces_net_moyen'] * 100:+.2f} %"
                print(f"  {k:9} dates {c['dates']:3}  écart net moyen {ex:>9}  t(NW) {t:>6}  moitiés "
                      f"{['N/D' if h is None else round(h * 100, 2) for h in c['moities']]}  -> {c['decision']}")
            pf = {}
            if res.get("signaux"):
                p = res["panel"]
                bars = {(code, k): __import__("pandas").DataFrame({c: getattr(p, c)[k] for c in ("open", "high", "low", "close")}).dropna()
                        for k in {s.key for s in res["signaux"]}}
                engines = [("3.2.1", simulate, GlobalConfig, None)]
                if args.compare_legacy:                # même jeu de signaux rejoué par le simulateur 3.2 (avant / après)
                    import importlib.util as _iu
                    from pathlib import Path as _P
                    _f = _P.cwd() / "tests" / "legacy" / "portfolio_v32.py"     # copie figée (dépôt), hors paquet
                    _sp = _iu.spec_from_file_location("portfolio_v32_legacy", _f)
                    legacy = _iu.module_from_spec(_sp)
                    import sys as _sys
                    _sys.modules["portfolio_v32_legacy"] = legacy
                    _sp.loader.exec_module(legacy)
                    engines.append(("3.2 (look-ahead)", legacy.simulate, legacy.GlobalConfig, legacy.SignalIn))
                for name, sim_fn, cfg_cls, sig_cls in engines[1:]:
                    sigs_l = [sig_cls(**{k: getattr(x, k) for k in ("market", "key", "ticker", "as_of", "category", "norm",
                                                                     "signal_close", "atr", "currency", "country")})
                              for x in res["signaux"]]
                    for base in ("EUR", "USD"):
                        sm = sim_fn(sigs_l, bars, fx, cfg_cls(base=base)).summary
                        print(f"  portefeuille simulé {name} ({base}) : " + str({k: sm.get(k) for k in (
                            "rendement_total", "drawdown_max", "trades_clos", "gagnants", "r_moyen", "debut", "fin", "frais")}))
                for base in ("EUR", "USD"):
                    sim = simulate(res["signaux"], bars, fx, GlobalConfig(base=base))
                    import collections as _c
                    print(f"  motifs de refus ({base}) : " + str(_c.Counter((r["motif"] or "")[:40] for r in sim.refused).most_common(6)))
                    sm = sim.summary
                    ref = benchmarks({f"équipondéré {code}": (p.bench, wm.get(code).currency)}, fx, base,
                                     sm.get("debut"), sm.get("fin")) if sm.get("debut") else {}
                    pf[base] = {**{k: sm.get(k) for k in ("rendement_total", "drawdown_max", "trades_clos", "gagnants",
                                                         "r_moyen", "debut", "fin", "frais", "lecture")},
                                "references": ref, "refus": len(sim.refused)}
                    print(f"  portefeuille simulé ({base}) : {pf[base]}")
            out.append({k: v for k, v in res.items() if k not in ("signaux", "panel")} | {"portefeuille": pf})
        if args.json:
            with open(args.json, "w") as fh:
                _json.dump(out, fh, ensure_ascii=False, indent=1, default=str)
        print("RESULTATS_JSON " + _json.dumps(out, ensure_ascii=False, default=str))
        return 0
    if args.action == "ranking":
        from stockhunter.world.fx import FxTable
        from stockhunter.world.ranking import global_ranking
        res = global_ranking(engine, FxTable.from_long(store.load_fx()), top=args.top,
                             region=args.region, country=args.country)
        print(f"Classement mondial : {res['total']} sélections distinctes ({res['apres_filtres']} après filtres)")
        print(f"{'Rang':>4}  {'Action':28} {'Pays':4} {'Bourse':26} {'Dev':3} {'Catégories':28} {'Score':>5} Risque")
        for r in res["lignes"]:
            print(f"{r.rank:>4}  {(r.ticker + ' ' + (r.name or ''))[:28]:28} {r.country:4} {r.exchange[:26]:26} "
                  f"{r.currency:3} {', '.join(r.categories)[:28]:28} {r.norm:5.2f} {r.risk}  (séance {r.as_of})")
        print(res["avertissement"])
        return 0
    print(wr.status_text(engine, store))
    return 0


def SecEdgarProviderLazy(settings, engine):
    from stockhunter.data.providers import SecEdgarProvider
    return SecEdgarProvider(settings, engine)


def cmd_history(settings, engine, args) -> int:
    """Historique long Tiingo, stocké hors base (Parquet). Plafonné en titres DISTINCTS nouveaux par mois."""
    from stockhunter.data.http import ProviderError
    from stockhunter.data.providers import TiingoProvider
    from stockhunter.history.store import HistoryStore, candidate_tickers, fetch
    from stockhunter.universe import load_universe
    st = HistoryStore(settings.history_dir)
    tracked = {u.symbol for u in load_universe(settings.universe_file)}
    if args.action == "status":
        m = st.manifest()
        print(f"Historique long : {len(st.tickers())} titres, {st.size_mb()} Mo ; nouveaux symboles par mois : "
              f"{ {k: len(v) for k, v in m['symboles_du_mois'].items()} }")
        return 0
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = candidate_tickers(engine, settings.market_dir, args.sample)
    try:
        tg = TiingoProvider(settings, engine)
    except ProviderError as exc:
        print(f"IGNORÉ : {exc}")
        return 2
    out = fetch(st, tg, tickers, date.fromisoformat(args.start), args.max_new, already_used=tracked)
    print(f"Collecte : {len(out['collectes'])} titre(s) ; déjà présents {out['ignores_deja_presents']} ; "
          f"plafond atteint : {out['plafond_atteint']} ; nouveaux symboles ce mois : {out['nouveaux_symboles_ce_mois']} ; "
          f"erreurs : {out['erreurs'] or 'aucune'} ; stockage {st.size_mb()} Mo")
    return 2 if out["erreurs"] else 0


def cmd_pipeline(settings, engine, args) -> int:
    from stockhunter import pipeline
    return pipeline.run(engine, force=args.force)


def cmd_lab(settings, engine, args) -> int:
    """Laboratoire de recherche v3 : programme pré-enregistré sur l'univers large, registre des expériences."""
    import json as _json

    from stockhunter.lab import registry
    if args.action == "registry":
        reg = registry.load()
        fam = registry.family_pvalues(reg)
        print(f"{len(registry.tests(reg))} tests dans la famille ; périodes réservées :")
        for h in reg["periodes_reservees"]:
            print(f"  - {h['etude']} : {h['etat']}")
        for e in reg["experiences"]:
            p = fam.get(e["id"], {})
            print(f"  {e['id']:14} {e.get('t') if e.get('t') is not None else '':>6}  "
                  f"{('p=%.3f holm=%.3f' % (p['p'], p['holm'])) if p else '':24} {e['decision']}")
        return 0
    from stockhunter.lab.panel import load_panel
    from stockhunter.lab.run import render, run_program
    from stockhunter.market.store import MarketStore
    end = date.fromisoformat(args.end) if args.end else None
    reg0 = registry.load()
    res = run_program(load_panel(engine, MarketStore(settings.market_dir), end),
                      holdout_consulted=registry.holdout_consulted(reg0))
    reg = registry.record_run(reg0, _json.loads(_json.dumps(res, default=str)))
    registry.save(reg)        # trace locale ; le registre versionné est mis à jour par commit à partir de ce résultat
    if any("holdout" in r for r in res["facteurs"].values()):
        print("ATTENTION : période réservée consultée lors de cette exécution — à consigner dans le registre versionné.")
    text = render(res, registry.family_pvalues(reg))
    if args.out:
        Path(args.out).write_text(text)
    if args.json:
        Path(args.json).write_text(_json.dumps(res, default=str, ensure_ascii=False))
    print(text)
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
    an = sub.add_parser("analyse", help="synthèse écrite et sourcée d'une action (phase 6)")
    an.add_argument("symbol")
    an.add_argument("--as-of", help="date AAAA-MM-JJ (point-in-time), défaut : dernier scan")
    an.add_argument("--out", help="fichier Markdown")
    hi = sub.add_parser("history", help="historique long (Tiingo, Parquet hors base), collecte plafonnée")
    hi.add_argument("action", choices=["fetch", "status"])
    hi.add_argument("--tickers", help="liste explicite (sinon : échantillon des plus liquides de l'univers large)")
    hi.add_argument("--sample", type=int, default=400)
    hi.add_argument("--max-new", type=int, default=0, help="titres DISTINCTS nouveaux autorisés ce mois (défaut 0)")
    hi.add_argument("--start", default="2014-01-01")
    dc = sub.add_parser("discover", help="moteur de découverte v3 (univers large, multi-catégories)")
    dc.add_argument("action", choices=["run", "status"])
    pl = sub.add_parser("pipeline", help="chaîne nocturne complète (collecte → rapports), échecs critiques non masqués")
    pl.add_argument("name", choices=["nightly"])
    pl.add_argument("--force", action="store_true", help="relancer même si la séance a déjà été traitée")
    lb = sub.add_parser("lab", help="laboratoire de recherche v3 (programme pré-enregistré, registre)")
    lb.add_argument("action", choices=["run", "registry"])
    lb.add_argument("--end", help="dernière séance utilisée AAAA-MM-JJ (défaut : toutes)")
    lb.add_argument("--out")
    lb.add_argument("--json")
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
    wo = sub.add_parser("world", help="3.2 : marchés mondiaux (registre, collecte, scanners, classement, portefeuille)")
    wo.add_argument("action", choices=["markets", "sync", "scan", "ranking", "portfolio", "replay", "status",
                                       "archive", "archive-verify", "restore"])
    wo.add_argument("--json", help="replay : fichier de résultats")
    wo.add_argument("--compare-legacy", action="store_true", help="replay : rejoue aussi avec le simulateur 3.2")
    wo.add_argument("--budget-mb", type=float, default=60.0, help="archive : budget de l'archive durable (Mo)")
    wo.add_argument("--markets", help="places (codes du registre, séparés par des virgules), défaut : toutes")
    wo.add_argument("--backfill", type=int, help="séances d'historique à reconstituer (défaut : réglage)")
    wo.add_argument("--max-files", type=int, help="fichiers au plus par place et par passage")
    wo.add_argument("--base", default="EUR", choices=["EUR", "USD"])
    wo.add_argument("--top", type=int, default=20, help="5, 10, 20, 50 ou 0 (toutes)")
    wo.add_argument("--region")
    wo.add_argument("--country")
    cp = sub.add_parser("db", help="copie de la base vers une autre (migration vers PostgreSQL)")
    cp.add_argument("action", choices=["copy-to", "tune", "diag", "check"])
    cp.add_argument("target", nargs="?", help="copy-to : URL de la base cible, déjà migrée et vide")
    return {"scan": cmd_scan, "top": cmd_top, "explain": cmd_explain, "analyse": cmd_analyse, "lab": cmd_lab, "pipeline": cmd_pipeline, "discover": cmd_discover, "history": cmd_history, "validate": cmd_validate, "market": cmd_market,
            "research": cmd_research, "paper": cmd_paper, "forward": cmd_forward, "notify": cmd_notify,
            "dashboard": cmd_dashboard, "db": cmd_db, "world": cmd_world}
