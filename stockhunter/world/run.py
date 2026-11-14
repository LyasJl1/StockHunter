"""Orchestration mondiale (3.2) : collecte, scanners par place, suivi, portefeuille simulé, état par place.

Isolation des pannes : chaque place est traitée séparément ; l'échec d'une région est consigné et n'empêche ni
les autres régions ni le marché américain. Codes de sortie des commandes : 0 = tout va bien (y compris « aucune
opportunité »), 2 = au moins une place en échec technique ou en données manquantes (alerte, pas panne).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from stockhunter.db.models import (DiscoveryCandidate, KeyValue, WorldListing, WorldOutcome, WorldScan,
                                   WorldSignal)
from stockhunter.db.session import session_scope
from stockhunter.world import markets as mk
from stockhunter.world import providers as pv
from stockhunter.world import sync
from stockhunter.world.fx import FxTable
from stockhunter.world.store import WorldStore

KV_STATE = "world_state"
KV_PORTFOLIO = "world_portfolio"
SCANNED = ("XPAR", "XAMS", "XBRU", "XLIS", "XDUB", "XMIL", "XOSL", "XNSE", "XBOM")


def annotate(level: str, title: str, msg: str) -> None:
    """Annotation visible dans GitHub Actions (erreur / avertissement / note) ; simple ligne ailleurs."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level} title={title}::{msg}")
    else:
        print(f"[{level}] {title} : {msg}")


def _kv_set(engine: Engine, key: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, default=str)
    with session_scope(engine) as s:
        kv = s.get(KeyValue, key)
        if kv is None:
            s.add(KeyValue(key=key, value=payload))
        else:
            kv.value = payload


def kv_get(engine: Engine, key: str) -> Any:
    with session_scope(engine) as s:
        kv = s.get(KeyValue, key)
        return json.loads(kv.value) if kv else None


# ============================================================================ collecte
def collect(engine: Engine, store: WorldStore, fetch: pv.Fetcher | None = None, now: datetime | None = None,
            backfill_sessions: int = 260, max_files: int = 40, markets: tuple[str, ...] | None = None) -> int:
    fetch = fetch or pv.Fetcher()
    now = now or datetime.now(timezone.utc)
    reports: list[sync.Report] = []
    want = set(markets or SCANNED)
    for name, fn in (("change", lambda: [sync.sync_fx(store, fetch)]),
                     ("euronext", lambda: sync.sync_euronext(store, engine, fetch, now)
                      if want & set(sync.EURONEXT_MARKETS) else []),
                     ("nse", lambda: [sync.sync_india(store, engine, fetch, "XNSE", now, backfill_sessions, max_files)]
                      if "XNSE" in want else []),
                     ("bse", lambda: [sync.sync_india(store, engine, fetch, "XBOM", now, backfill_sessions, max_files)]
                      if "XBOM" in want else [])):
        try:
            reports += fn()
        except Exception as exc:                           # isolation : une région en panne n'arrête pas les autres
            reports.append(sync.Report(name, "échec", errors=[f"{type(exc).__name__} : {str(exc)[:200]}"]))
    bad = 0
    for r in reports:
        print("  " + r.line())
        if r.status == "échec":
            bad += 1
            annotate("warning", f"Collecte {r.market}", "; ".join(r.errors)[:300])
    state = kv_get(engine, KV_STATE) or {}
    state["collecte"] = {"at": now.isoformat(timespec="seconds"),
                         "places": {r.market: {"statut": r.status, "ecrites": len(r.written), "erreurs": r.errors[:3],
                                               "detail": r.detail} for r in reports},
                         "stockage": store.status()}
    _kv_set(engine, KV_STATE, state)
    print(f"Stockage mondial : {store.usage_mb():.1f} Mo / {store.budget_mb:.0f} Mo ; appels HTTP : {fetch.calls}")
    return 2 if bad else 0


# ============================================================================ scanners
def scan_all(engine: Engine, store: WorldStore, markets: tuple[str, ...] | None = None) -> int:
    from stockhunter.discovery.store import registry_decisions
    from stockhunter.world import scan as ws
    fx = FxTable.from_long(store.load_fx())
    if fx.wide.empty:
        annotate("error", "Scanner mondial", "aucun taux de change BCE stocké : conversion impossible")
        return 2
    validation = registry_decisions()
    out, bad = {}, 0
    for code in markets or SCANNED:
        try:
            res = ws.scan_market(engine, store, code, fx, validation)
            upd = ws.update_outcomes(engine, store, code, fx)
        except Exception as exc:
            bad += 1
            out[code] = {"statut": "ERREUR", "detail": f"{type(exc).__name__} : {str(exc)[:200]}"}
            annotate("error", f"Scanner {code}", out[code]["detail"])
            continue
        n = sum(len(c["candidats"]) for c in res.get("categories", {}).values())
        out[code] = {"statut": res["statut"], "as_of": res.get("as_of"), "univers": res.get("univers", 0),
                     "selections": n, "seances": res.get("seances"), "suivi": upd, "conclusion": res["conclusion"]}
        if res["statut"] == "ERREUR_DONNEES":
            bad += 1
            annotate("warning", f"Scanner {code}", res["conclusion"])
        elif res["statut"] == "AUCUNE_OPPORTUNITE":
            annotate("notice", f"Scanner {code}", f"séance du {res.get('as_of')} : aucune configuration ne remplit "
                     f"les règles (univers {res.get('univers')}) — résultat normal, pas une panne")
        print(f"  {code:5} {res['statut']:24} séance {res.get('as_of')}  univers {res.get('univers', 0):5}  "
              f"sélections {n:3}  suivi {upd}")
    state = kv_get(engine, KV_STATE) or {}
    state["scanners"] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "places": out}
    _kv_set(engine, KV_STATE, state)
    return 2 if bad else 0


# ============================================================================ portefeuille mondial
def load_signals(engine: Engine, store: WorldStore, fx: FxTable):
    from stockhunter.world import scan as ws
    from stockhunter.world.portfolio import SignalIn
    sigs: list[SignalIn] = []
    bars: dict = {}
    with session_scope(engine) as s:
        lots = dict(s.execute(select(WorldListing.isin, WorldListing.lot_size)).all())
        rows = list(s.execute(select(WorldSignal, WorldScan.status).join(WorldScan, WorldScan.id == WorldSignal.scan_id)))
        world = [(g.market, g.isin, g.ticker, g.as_of, g.category, g.norm_score, json.loads(g.card_json)["plan"],
                  g.currency) for g, _st in rows]
        us = [(c.ticker, c.as_of, c.category, c.rank, json.loads(c.card_json)) for c in s.scalars(select(DiscoveryCandidate))]
    panels = {}
    for code in {w[0] for w in world}:
        p, _ = ws.build_market_panel(store, code, fx)
        if p is not None:
            panels[code] = p
    for market, isin, ticker, as_of, cat, norm, pl, ccy in world:
        p = panels.get(market)
        if p is None or isin not in p.close.columns or pd.Timestamp(as_of) not in p.close.index:
            continue
        adj0 = p.close.at[pd.Timestamp(as_of), isin]
        if not np.isfinite(adj0):
            continue
        scale = adj0 / pl["signal_close"]
        sigs.append(SignalIn(market, isin, ticker, as_of, cat, norm, float(adj0), pl["atr"] * scale, ccy,
                             mk.get(market).country, None, int(lots.get(isin) or 1)))
        if (market, isin) not in bars:
            bars[(market, isin)] = pd.DataFrame({k: getattr(p, k)[isin] for k in ("open", "high", "low", "close")}).dropna()
    if us:
        try:
            from stockhunter.lab.panel import load_panel
            from stockhunter.market.store import MarketStore
            from stockhunter.config import get_settings
            p = load_panel(engine, MarketStore(get_settings().market_dir))
        except Exception as exc:                         # US indisponible : portefeuille sans les US, signalé
            annotate("warning", "Portefeuille mondial", f"cours US indisponibles : {exc}")
            p = None
        if p is not None:
            for ticker, as_of, cat, rank, card in us:
                if ticker not in p.close.columns or pd.Timestamp(as_of) not in p.close.index:
                    continue
                adj0 = p.close.at[pd.Timestamp(as_of), ticker]
                if not np.isfinite(adj0) or not card["plan"]["signal_close"]:
                    continue
                scale = adj0 / card["plan"]["signal_close"]
                sigs.append(SignalIn("US", f"US:{ticker}", ticker, as_of, cat, round(1 - (rank - 1) / 10, 4),
                                     float(adj0), card["plan"]["atr"] * scale, "USD", "US"))
                if ("US", f"US:{ticker}") not in bars:
                    bars[("US", f"US:{ticker}")] = pd.DataFrame(
                        {k: getattr(p, k)[ticker] for k in ("open", "high", "low", "close")}).dropna()
            panels["US"] = p
    return sigs, bars, panels


BENCH_ETFS = {"S&P 500 (SPY)": "SPY", "MSCI World (ETF URTH)": "URTH",
              "Europe développée (ETF VGK, proxy du STOXX 600)": "VGK", "MSCI India (ETF INDA)": "INDA"}


def run_portfolio(engine: Engine, store: WorldStore, base: str = "EUR") -> int:
    from stockhunter.world.portfolio import GlobalConfig, benchmarks, simulate
    fx = FxTable.from_long(store.load_fx())
    if fx.wide.empty:
        annotate("error", "Portefeuille mondial", "aucun taux de change BCE")
        return 2
    sigs, bars, panels = load_signals(engine, store, fx)
    res = simulate(sigs, bars, fx, GlobalConfig(base=base))
    bench = {}
    if len(res.equity):
        start, end = res.equity.index[0], res.equity.index[-1]
        series = {}
        us = panels.get("US")
        if us is not None:
            raw = us.close
            for label, t in BENCH_ETFS.items():
                if t == "SPY" and us.bench is not None:
                    series[label] = (us.bench, "USD")
                elif t in raw.columns:
                    series[label] = (raw[t], "USD")
        for code, p in panels.items():
            if code != "US" and p.bench is not None:
                series[f"Équipondéré {code} (univers StockHunter)"] = (p.bench, mk.get(code).currency)
        bench = benchmarks(series, fx, base, start, end)
        for label in BENCH_ETFS:
            bench.setdefault(label, None)
    eq = res.equity
    step_ = max(1, len(eq) // 300)
    payload = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "base": base,
               "resume": res.summary, "references": bench, "expositions": res.exposures,
               "courbe": [[str(d.date()), round(float(v), 2)] for d, v in eq.iloc[::step_].items()],
               "trades": res.trades[-50:], "refus": len(res.refused), "refus_exemples": res.refused[-10:],
               "signaux": len(sigs), "places": sorted({s.market for s in sigs})}
    _kv_set(engine, f"{KV_PORTFOLIO}_{base}", payload)
    sm = res.summary
    print(f"Portefeuille mondial simulé ({base}) : {len(sigs)} signaux, {sm.get('trades_clos', 0)} trades clos, "
          f"{sm.get('positions_ouvertes', 0)} positions ouvertes, valeur {sm.get('valeur_finale')} ; "
          f"refus {len(res.refused)} ; références {bench}")
    return 0


# ============================================================================ état par place (tableau de bord)
def market_table(engine: Engine, store: WorldStore, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    state = kv_get(engine, KV_STATE) or {}
    coll = (state.get("collecte") or {}).get("places", {})
    scans = (state.get("scanners") or {}).get("places", {})
    cnt = sync.counts(engine)
    from stockhunter.db.models import MarketTicker
    from stockhunter.research.broad import EXCHANGES
    with session_scope(engine) as s:
        us_n = s.scalar(select(func.count()).select_from(MarketTicker)
                        .where(MarketTicker.active.is_(True), MarketTicker.type == "CS",
                               MarketTicker.primary_exchange.in_(EXCHANGES))) or 0
        from stockhunter.db.models import DiscoveryRun
        us_run = s.scalar(select(DiscoveryRun).order_by(DiscoveryRun.as_of.desc()).limit(1))
    out = []
    for m in mk.MARKETS.values():
        stored = store.stored_sessions(m.code)
        last = stored[-1] if stored else None
        row = {"code": m.code, "nom": m.name, "pays": m.country_name, "region": m.region, "devise": m.currency,
               "donnees": m.data, "fournisseurs": ", ".join(m.providers) or "—", "note": m.note,
               "seances": len(stored), "derniere": str(last) if last else None, "listings": cnt.get(m.code, {}),
               "collecte": coll.get(m.code), "scanner": scans.get(m.code)}
        if m.code == "US":
            row.update(seances=None, derniere=str(us_run.as_of) if us_run else None,
                       listings={"actifs": us_n, "principal": us_n},
                       scanner={"statut": "OK", "as_of": str(us_run.as_of) if us_run else None,
                                "univers": us_run.universe_size if us_run else 0})
            last = us_run.as_of if us_run else None
        if m.data == mk.PRICES:
            try:
                f = mk.freshness(m, last, now)
                row["fraicheur"] = f.detail
                row["attendue"] = str(f.expected)
            except Exception as exc:
                row["fraicheur"] = f"calendrier : {exc}"
        out.append(row)
    return out


def status_text(engine: Engine, store: WorldStore) -> str:
    lines = [f"Stockage mondial : {store.usage_mb():.1f} Mo / {store.budget_mb:.0f} Mo"]
    for r in market_table(engine, store):
        if r["donnees"] == mk.NONE:
            continue
        ls = r["listings"] or {}
        sc = r.get("scanner") or {}
        lines.append(f"  {r['code']:5} {r['nom'][:38]:38} {r['donnees'][:26]:26} listings {ls.get('actifs', 0):5}  "
                     f"séances {r['seances'] if r['seances'] is not None else '—':>4}  dernière {r['derniere'] or '—'}  "
                     f"{r.get('fraicheur', '')}  scanner {sc.get('statut', '—')} ({sc.get('univers', 0)} analysées)")
    na = [r["code"] for r in market_table(engine, store) if r["donnees"] == mk.NONE]
    lines.append(f"  Sans source gratuite fiable : {', '.join(na)}")
    return "\n".join(lines)
