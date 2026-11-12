"""Interface en ligne de commande.

  stockhunter init-db                 crée / migre la base (Alembic)
  stockhunter sync-universe           charge l'univers et résout les CIK SEC
  stockhunter ingest {prices,fundamentals,macro,earnings,all} [--symbols A,B] [--full]
  stockhunter quality                 contrôles qualité + anomalies en base
  stockhunter status                  couverture réelle, fraîcheur, quotas, derniers jobs
  stockhunter prices SYMBOL [-n 10]   consulter les barres stockées (datées, jamais présentées comme live)
  stockhunter verify-sources          appel réel et minimal à chaque API configurée
  stockhunter prune [--days 90]       rétention du journal d'appels API et des jobs
  stockhunter scan [--as-of DATE]     classement multicritère (enregistré, jamais modifié)
  stockhunter top [-n 10] [--all]     dernier classement
  stockhunter explain SYMBOL          fiche explicative : sous-scores, raisons, sources, historique
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, select

from stockhunter.config import Settings, get_settings
from stockhunter.data import ingestion
from stockhunter.data.calendar import last_completed_session
from stockhunter.data.http import ProviderError
from stockhunter.data.providers import FinnhubProvider, FredProvider, SecEdgarProvider, TiingoProvider
from stockhunter.data.quality import run_quality_checks
from stockhunter.db.models import (
    ApiCall,
    DataAnomaly,
    Filing,
    FundamentalFact,
    Instrument,
    JobRun,
    MacroObservation,
    MacroSeries,
    MarketEvent,
    PriceBar,
)
from stockhunter.db.session import get_engine, session_scope
from stockhunter.jobs.runner import run_job

ROOT = Path(__file__).resolve().parent.parent


def migrate(settings: Settings) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    get_engine(settings.database_url)  # crée le dossier SQLite si besoin
    command.upgrade(cfg, "head")


def _print_run(run: JobRun) -> int:
    print(f"[{run.job_name}] statut={run.status} ok={run.items_ok} échecs={run.items_failed}")
    if run.detail:
        print("  " + run.detail.replace("\n", "\n  "))
    return 0 if run.status in ("success", "skipped") else (1 if run.status == "failed" else 2)


def cmd_sync_universe(settings: Settings, engine, args) -> int:
    try:
        sec = SecEdgarProvider(settings, engine)
    except ProviderError as exc:
        print(f"AVERTISSEMENT : {exc} — univers chargé sans résolution des CIK")
        sec = None
    ingestion.ensure_sources(engine)
    return _print_run(run_job(engine, "sync_universe",
                              lambda ctx: ingestion.sync_universe(engine, ctx, settings.universe_file, sec)))


def cmd_ingest(settings: Settings, engine, args) -> int:
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    targets = ["prices", "fundamentals", "macro", "earnings"] if args.what == "all" else [args.what]
    code = 0
    for t in targets:
        try:
            if t == "prices":
                p = TiingoProvider(settings, engine)
                run = run_job(engine, "ingest_prices", lambda ctx: ingestion.ingest_prices(
                    engine, ctx, p, settings.price_history_years, symbols, args.full))
            elif t == "fundamentals":
                p = SecEdgarProvider(settings, engine)
                run = run_job(engine, "ingest_fundamentals",
                              lambda ctx: ingestion.ingest_fundamentals(engine, ctx, p, symbols))
            elif t == "macro":
                p = FredProvider(settings, engine)
                run = run_job(engine, "ingest_macro",
                              lambda ctx: ingestion.ingest_macro(engine, ctx, p, settings.price_history_years))
            else:
                p = FinnhubProvider(settings, engine)
                run = run_job(engine, "ingest_earnings",
                              lambda ctx: ingestion.ingest_earnings_calendar(engine, ctx, p))
        except ProviderError as exc:
            print(f"[{t}] IGNORÉ : {exc}")
            code = max(code, 2)
            continue
        code = max(code, _print_run(run))
    return code


def cmd_quality(settings: Settings, engine, args) -> int:
    holder = {}

    def _job(ctx):
        holder["f"] = run_quality_checks(engine, stale_sessions=settings.stale_price_business_days,
                                         job_run_id=ctx.run_id)
        ctx.success(1)

    _print_run(run_job(engine, "quality_checks", _job))
    findings = holder.get("f", [])
    if not findings:
        print("Aucune anomalie détectée.")
    for f in sorted(findings, key=lambda x: {"critical": 0, "warning": 1, "info": 2}[x.severity]):
        print(f"  [{f.severity.upper():8}] {f.check:24} {f.detail}")
    return 1 if any(f.severity == "critical" for f in findings) else 0


def cmd_status(settings: Settings, engine, args) -> int:
    today = date.today()
    expected = last_completed_session(today)
    print(f"StockHunter AI — état au {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print(f"Dernière séance US complète attendue : {expected}\n")
    print("Sources :")
    for k, v in settings.provider_status().items():
        print(f"  {k:10} {v}")
    with session_scope(engine) as s:
        n_inst = s.scalar(select(func.count()).select_from(Instrument).where(Instrument.is_active.is_(True)))
        by_role = s.execute(select(Instrument.role, func.count()).where(Instrument.is_active.is_(True))
                            .group_by(Instrument.role)).all()
        with_px = s.scalar(select(func.count()).select_from(Instrument).where(
            Instrument.is_active.is_(True), Instrument.last_price_date.is_not(None)))
        fresh = s.scalar(select(func.count()).select_from(Instrument).where(
            Instrument.is_active.is_(True), Instrument.last_price_date >= expected))
        n_bars = s.scalar(select(func.count()).select_from(PriceBar))
        rng = s.execute(select(func.min(PriceBar.bar_date), func.max(PriceBar.bar_date))).one()
        n_facts = s.scalar(select(func.count()).select_from(FundamentalFact))
        n_filings = s.scalar(select(func.count()).select_from(Filing))
        n_macro = s.scalar(select(func.count()).select_from(MacroSeries))
        n_obs = s.scalar(select(func.count()).select_from(MacroObservation))
        n_events = s.scalar(select(func.count()).select_from(MarketEvent).where(MarketEvent.event_date >= today))
        open_anom = s.execute(select(DataAnomaly.severity, func.count()).where(DataAnomaly.resolved_at.is_(None))
                              .group_by(DataAnomaly.severity)).all()
        calls_24h = s.execute(select(ApiCall.provider, func.count()).where(
            ApiCall.called_at >= datetime.now(timezone.utc) - timedelta(days=1), ApiCall.from_cache.is_(False))
            .group_by(ApiCall.provider)).all()
        last_runs = list(s.scalars(select(JobRun).order_by(JobRun.id.desc()).limit(8)))

    print(f"\nCouverture RÉELLE : {n_inst} instruments actifs ({', '.join(f'{r}={c}' for r, c in by_role)})")
    print("  (l'univers suivi est limité par les quotas gratuits : ce n'est PAS tout le marché US)")
    print(f"  avec prix : {with_px}/{n_inst}  |  à jour au {expected} : {fresh}/{n_inst}")
    print(f"  barres EOD : {n_bars}  période : {rng[0]} → {rng[1]}  (données de clôture, pas de cours live)")
    print(f"  faits XBRL : {n_facts}  |  dépôts SEC : {n_filings}")
    print(f"  séries macro : {n_macro} ({n_obs} obs.)  |  événements à venir : {n_events}")
    print(f"\nAnomalies ouvertes : {dict(open_anom) or 'aucune'}")
    print(f"Appels API réels 24 h : {dict(calls_24h) or 'aucun'}")
    print("\nDerniers jobs :")
    for r in last_runs:
        print(f"  #{r.id} {r.job_name:22} {r.status:8} ok={r.items_ok} ko={r.items_failed} "
              f"début={r.started_at:%Y-%m-%d %H:%M}")
    return 0


def cmd_prices(settings: Settings, engine, args) -> int:
    with session_scope(engine) as s:
        inst = s.scalar(select(Instrument).where(Instrument.symbol == args.symbol.upper()))
        if inst is None:
            print("Symbole hors univers.")
            return 1
        bars = list(s.scalars(select(PriceBar).where(PriceBar.instrument_id == inst.id)
                              .order_by(PriceBar.bar_date.desc()).limit(args.n)))
    print(f"{inst.symbol} — {inst.name or ''} | secteur : {inst.sector or 'N/D'} | {inst.industry or ''}")
    print("Cours de CLÔTURE historiques (source Tiingo) — aucune valeur n'est un cours en temps réel.")
    print(f"{'séance':10} {'open':>10} {'high':>10} {'low':>10} {'close':>10} {'adj_close':>10} {'volume':>14}  récupéré (UTC)")
    for b in reversed(bars):
        print(f"{b.bar_date} {b.open:>10.2f} {b.high:>10.2f} {b.low:>10.2f} {b.close:>10.2f} "
              f"{b.adj_close:>10.2f} {b.volume:>14,.0f}  {b.fetched_at:%Y-%m-%d %H:%M}"
              + (f"  div={b.div_cash}" if b.div_cash else "") + (f"  split={b.split_factor}" if b.split_factor != 1 else ""))
    return 0


def cmd_verify_sources(settings: Settings, engine, args) -> int:
    """Appel réel minimal par source. Ne rapporte « OK » que si la réponse a été reçue et interprétée."""
    ok = True
    checks = [
        ("tiingo", lambda: TiingoProvider(settings, engine).fetch_prices("SPY", date.today() - timedelta(days=10))),
        ("sec_edgar", lambda: SecEdgarProvider(settings, engine).fetch_submissions("320193", "AAPL").filings[:1]),
        ("fred", lambda: FredProvider(settings, engine).fetch_observations("DGS10", date.today() - timedelta(days=14))),
        ("finnhub", lambda: FinnhubProvider(settings, engine).fetch_earnings_calendar(date.today(), date.today() + timedelta(days=3))),
    ]
    for name, fn in checks:
        try:
            res = fn()
            print(f"  {name:10} OK    ({len(res)} élément(s) reçu(s))")
        except ProviderError as exc:
            print(f"  {name:10} ÉCHEC {exc}")
            ok = False
    return 0 if ok else 1


def cmd_prune(settings: Settings, engine, args) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    with session_scope(engine) as s:
        a = s.execute(delete(ApiCall).where(ApiCall.called_at < cutoff)).rowcount
        d = s.execute(delete(DataAnomaly).where(DataAnomaly.resolved_at < cutoff)).rowcount
    print(f"Supprimés : {a} appels API, {d} anomalies résolues (> {args.days} j). Prix et fondamentaux conservés.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stockhunter", description="StockHunter AI — données et scanner")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    sub.add_parser("sync-universe")
    ing = sub.add_parser("ingest")
    ing.add_argument("what", choices=["prices", "fundamentals", "macro", "earnings", "all"])
    ing.add_argument("--symbols")
    ing.add_argument("--full", action="store_true", help="recharge l'historique complet")
    sub.add_parser("quality")
    sub.add_parser("status")
    pr = sub.add_parser("prices")
    pr.add_argument("symbol")
    pr.add_argument("-n", type=int, default=10)
    sub.add_parser("verify-sources")
    pn = sub.add_parser("prune")
    pn.add_argument("--days", type=int, default=90)
    from stockhunter.report_cli import add_parsers
    p.set_defaults(_extra=add_parsers(sub))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # les URL contiennent des tokens
    settings = get_settings()
    if args.cmd == "init-db":
        migrate(settings)
        print("Base migrée.")
        return 0
    engine = get_engine(settings.database_url)
    handlers = {
        "sync-universe": cmd_sync_universe, "ingest": cmd_ingest, "quality": cmd_quality,
        "status": cmd_status, "prices": cmd_prices, "verify-sources": cmd_verify_sources, "prune": cmd_prune,
        **args._extra,
    }
    return handlers[args.cmd](settings, engine, args)


if __name__ == "__main__":
    sys.exit(main())
