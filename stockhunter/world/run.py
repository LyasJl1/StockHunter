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

from stockhunter.db.models import (DiscoveryCandidate, KeyValue, WorldListing, WorldScan,
                                   WorldSignal)
from stockhunter.db.session import session_scope
from stockhunter.world import markets as mk
from stockhunter.world import providers as pv
from stockhunter.world import sync
from stockhunter.world.fx import FxTable
from stockhunter.world.store import WorldStore

KV_STATE = "world_state"
KV_PORTFOLIO = "world_portfolio"
# places collectées et scannées : celles dont les cours sont réellement disponibles ET autorisés (registre)
SCANNED = tuple(c for c in ("XPAR", "XAMS", "XBRU", "XLIS", "XDUB", "XMIL", "XOSL", "XNSE", "XBOM")
                if mk.MARKETS[c].data == mk.PRICES)


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
    from stockhunter.world import archive
    try:                                    # cache Actions perdu ? on restaure d'abord l'historique non reconstituable
        rr = archive.restore(engine, store)
        if rr.restored or rr.mismatches:
            print(f"  Restauration depuis l'archive : {rr.restored} séance(s) ; divergences : {rr.mismatches[:3]}")
        if rr.mismatches:
            reports.append(sync.Report("ARCHIVE", "échec", critical=True, errors=rr.mismatches[:3]))
    except Exception as exc:
        reports.append(sync.Report("ARCHIVE", "échec", critical=True, errors=[f"restauration : {type(exc).__name__} : {exc}"[:200]]))
    want = set(markets or SCANNED)
    for name, fn in (("change", lambda: [sync.sync_fx(store, fetch)]),
                     ("euronext", lambda: (sync.sync_euronext(store, engine, fetch, now) if mk._euronext_enabled()
                                           else [sync.Report(c, "rien à faire", detail="collecte non autorisée par les "
                                                             "conditions d'euronext.com (désactivée)")
                                                 for c in sorted(want & set(sync.EURONEXT_MARKETS))])
                      if want & set(sync.EURONEXT_MARKETS) else []),
                     ("nse", lambda: [sync.sync_india(store, engine, fetch, "XNSE", now, backfill_sessions, max_files)]
                      if "XNSE" in want else []),
                     ("bse", lambda: [sync.sync_india(store, engine, fetch, "XBOM", now, backfill_sessions, max_files)]
                      if "XBOM" in want else [])):
        try:
            reports += fn()
        except Exception as exc:                           # isolation : une région en panne n'arrête pas les autres
            reports.append(sync.Report(name, "échec", critical=True,
                                       errors=[f"{type(exc).__name__} : {str(exc)[:200]}"]))
    for r in reports:
        print("  " + r.line())
        if r.status == "échec":
            annotate("error" if r.critical else "warning", f"Collecte {r.market}"
                     + (" — échec critique" if r.critical else " — indisponibilité temporaire"), "; ".join(r.errors)[:300])
    state = kv_get(engine, KV_STATE) or {}
    state["collecte"] = {"at": now.isoformat(timespec="seconds"),
                         "places": {r.market: {"statut": r.status, "critique": r.critical, "ecrites": len(r.written),
                                               "erreurs": r.errors[:3], "detail": r.detail} for r in reports},
                         "stockage": store.status()}
    fx = store.load_fx()
    if not fx.empty:                     # derniers taux BCE en base : le tableau de bord (Railway) n'a pas le Parquet
        last = fx[fx["date"] >= max(fx["date"]) - pd.Timedelta(days=10)]
        state["fx_recent"] = [[str(d), c, float(r)] for d, c, r in last[["date", "currency", "rate"]].itertuples(index=False)]
    _kv_set(engine, KV_STATE, state)
    print(f"Stockage mondial : {store.usage_mb():.1f} Mo / {store.budget_mb:.0f} Mo ; appels HTTP : {fetch.calls}")
    try:                                    # copie durable des séances non reconstituables (après la collecte)
        ar = archive.push(engine, store)
        state["archive"] = {"at": now.isoformat(timespec="seconds"), "ajoutees": ar.archived, "deja": ar.already,
                            "refus": ar.refused, "taille_mo": ar.size_mb, **archive.status(engine)}
        _kv_set(engine, KV_STATE, state)
        print(f"  Archive durable : {ar.archived} séance(s) ajoutée(s), {ar.already} déjà archivée(s), "
              f"{ar.size_mb} Mo" + (f" — REFUS : {ar.refused}" if ar.refused else ""))
        if ar.refused or ar.missing_local:
            reports.append(sync.Report("ARCHIVE", "échec", errors=[ar.refused or f"fichiers absents : {ar.missing_local[:3]}"]))
    except Exception as exc:
        reports.append(sync.Report("ARCHIVE", "échec", critical=True, errors=[f"archivage : {type(exc).__name__} : {exc}"[:200]]))
    places = {r.market: classify_collect(r) for r in reports}
    write_summary("Collecte mondiale", places)
    queue_alerts(engine, "collecte", places, now)
    return exit_code(places)


# ============================================================================ statuts (3.2.1)
OK, NO_OPP, PARTIAL, CRITICAL = "succès", "aucune opportunité", "partiel", "échec critique"


def classify_collect(r: sync.Report) -> tuple[str, str]:
    if r.critical:
        return CRITICAL, "; ".join(r.errors)[:200]
    if r.status in ("échec", "partiel", "en attente"):
        return PARTIAL, "; ".join(r.errors)[:200] or r.detail
    return OK, r.detail                                     # « rien à faire » : place fermée ou déjà à jour


def exit_code(places: dict[str, tuple[str, str]], all_failed_is_critical: bool = False) -> int:
    """1 = au moins un échec critique (chaîne rouge) ; 2 = dégradation (alerte) ; 0 = succès ou aucune opportunité.
    `all_failed_is_critical` : si AUCUNE place n'a abouti, c'est une panne d'ensemble (rouge), même si chaque
    défaillance prise isolément est temporaire."""
    st = [v[0] for v in places.values()]
    if CRITICAL in st:
        return 1
    if all_failed_is_critical and st and all(x == PARTIAL for x in st):
        return 1
    return 2 if PARTIAL in st else 0


def write_summary(title: str, places: dict[str, tuple[str, str]]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    icon = {OK: "✅", NO_OPP: "⚪", PARTIAL: "⚠️", CRITICAL: "❌"}
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n### {title}\n\n| Place | État | Détail |\n|---|---|---|\n")
        for k, (st, detail) in sorted(places.items()):
            f.write(f"| {k} | {icon.get(st, '')} {st} | {str(detail or '').replace('|', '/')[:160]} |\n")


def queue_alerts(engine: Engine, stage: str, places: dict[str, tuple[str, str]], now: datetime) -> int:
    """Alerte Telegram ciblée : une seule par étape, place défaillante et séance attendue (déduplication).
    Les places Euronext défaillantes ensemble (même fichier) sont regroupées en un message."""
    from stockhunter.notifications.engine import enqueue
    bad = {k: v for k, v in places.items() if v[0] in (PARTIAL, CRITICAL)}
    if not bad:
        return 0
    groups: dict[str, list[str]] = {}
    for k in bad:
        prov = mk.MARKETS[k].providers[0] if k in mk.MARKETS and mk.MARKETS[k].providers else k
        groups.setdefault(prov, []).append(k)
    sent = 0
    for prov, codes in sorted(groups.items()):
        try:
            exp = mk.last_completed_session(mk.get(codes[0]), now) if codes[0] in mk.MARKETS else now.date()
        except Exception:
            exp = now.date()
        worst = CRITICAL if any(bad[c][0] == CRITICAL for c in codes) else PARTIAL
        key = f"world:{stage}:{prov}:{worst}:{exp}"
        text = (f"<b>⚠ StockHunter — {stage} mondiale : {worst}</b>\n{', '.join(sorted(codes))} (séance attendue {exp})\n"
                f"{bad[codes[0]][1][:300]}\n<i>Les autres places et les États-Unis continuent. Détail : page Monde.</i>")
        if enqueue(engine, "alert", key, text):
            sent += 1
    return sent


def research_markets(store) -> tuple[str, ...]:
    """3.3 : places dont l'historique vient UNIQUEMENT d'imports (mode recherche) : scannées sur la dernière séance
    connue, jamais présentées comme à jour, sans alerte de fraîcheur (elles ne sont pas censées s'actualiser)."""
    imp = getattr(store, "imports", None)
    if imp is None:
        return ()
    return tuple(c for c in mk.MARKETS if c != "US" and c not in SCANNED and imp.stored_sessions(c))


def scannable(store) -> tuple[str, ...]:
    return _active_scanned() + research_markets(store)


def stale_markets(engine: Engine, store: WorldStore, now: datetime | None = None, max_lag: int = 1) -> dict[str, str]:
    """Places dotées de cours dont les données ont plus de `max_lag` séance(s) de retard (place fermée : 0)."""
    out = {}
    for code in _active_scanned():
        m = mk.get(code)
        stored = store.stored_sessions(code)
        if not stored:
            continue
        f = mk.freshness(m, stored[-1], now)
        if f.lag is not None and f.lag > max_lag:
            out[code] = f"dernière séance {f.last}, attendue {f.expected} ({f.detail})"
    return out


# ============================================================================ droits des places
INDIA_MARKETS = ("XNSE", "XBOM")


def _market_allowed(code: str) -> bool:
    return code not in INDIA_MARKETS or mk._india_enabled(code)


def _active_scanned() -> tuple[str, ...]:
    return tuple(code for code in SCANNED if _market_allowed(code))


# ============================================================================ scanners
def scan_all(engine: Engine, store: WorldStore, markets: tuple[str, ...] | None = None) -> int:
    from stockhunter.discovery.store import registry_decisions
    from stockhunter.world import scan as ws
    fx = FxTable.from_long(store.load_fx())
    if fx.wide.empty:
        annotate("error", "Scanner mondial — échec critique", "aucun taux de change BCE stocké : conversion impossible")
        return 1
    validation = registry_decisions()
    out, places = {}, {}
    stale = stale_markets(engine, store)
    research = set(research_markets(store))
    for code in markets or scannable(store):
        if not _market_allowed(code):
            out[code] = {"statut": "DESACTIVE", "detail": "droits de collecte et de simulation non confirmés"}
            places[code] = (OK, "place désactivée par défaut (autorisation requise)")
            continue
        try:
            res = ws.scan_market(engine, store, code, fx, validation)
            upd = ws.update_outcomes(engine, store, code, fx)
        except Exception as exc:                            # panne technique : ROUGE, les autres places continuent
            out[code] = {"statut": "ERREUR", "detail": f"{type(exc).__name__} : {str(exc)[:200]}"}
            places[code] = (CRITICAL, out[code]["detail"])
            annotate("error", f"Scanner {code} — échec critique", out[code]["detail"])
            continue
        n = sum(len(c["candidats"]) for c in res.get("categories", {}).values())
        if n and not res.get("deja_traitee") and code not in research and code not in stale:
            try:                                          # nouvelles sélections : un message par place et par séance
                from stockhunter.notifications.engine import enqueue
                from stockhunter.world.telegram import opportunity_message
                msg = opportunity_message(code, res)
                if msg:
                    enqueue(engine, "alert", f"world:signals:{code}:{res.get('as_of')}", msg)
            except Exception as exc:
                annotate("warning", f"Alerte Telegram {code}", f"{type(exc).__name__} : {exc}"[:200])
        out[code] = {"statut": res["statut"], "as_of": res.get("as_of"), "univers": res.get("univers", 0),
                     "selections": n, "seances": res.get("seances"), "suivi": upd, "conclusion": res["conclusion"],
                     "perime": stale.get(code)}
        if res["statut"] == "ERREUR_DONNEES":
            places[code] = (PARTIAL, res["conclusion"])
            annotate("warning", f"Scanner {code}", res["conclusion"])
        elif code in research:                              # mode recherche : historique importé, non actualisé
            out[code]["mode"] = "recherche (historique importé)"
            places[code] = (NO_OPP if res["statut"] == "AUCUNE_OPPORTUNITE" else OK,
                            f"mode recherche : données importées jusqu'au {res.get('as_of')} (non actualisées)")
        elif code in stale:
            places[code] = (PARTIAL, f"données périmées : {stale[code]}")
            annotate("warning", f"Scanner {code} — données périmées", stale[code])
        elif res["statut"] == "AUCUNE_OPPORTUNITE":
            places[code] = (NO_OPP, f"séance {res.get('as_of')}, univers {res.get('univers')}")
            annotate("notice", f"Scanner {code}", f"séance du {res.get('as_of')} : aucune configuration ne remplit "
                     f"les règles (univers {res.get('univers')}) — résultat normal, pas une panne")
        elif res["statut"] == "HISTORIQUE_INSUFFISANT":
            places[code] = (OK, res["conclusion"])            # attendu (Euronext en constitution), pas une panne
        else:
            places[code] = (OK, f"séance {res.get('as_of')}, {n} sélection(s) sur {res.get('univers')} titres")
        print(f"  {code:5} {res['statut']:24} séance {res.get('as_of')}  univers {res.get('univers', 0):5}  "
              f"sélections {n:3}  suivi {upd}" + (f"  PÉRIMÉ : {stale[code]}" if code in stale else ""))
    state = kv_get(engine, KV_STATE) or {}
    state["scanners"] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "places": out,
                         "etats": {k: list(v) for k, v in places.items()}}
    _kv_set(engine, KV_STATE, state)
    write_summary("Scanners mondiaux", places)
    queue_alerts(engine, "scanner", places, datetime.now(timezone.utc))
    # tous les scanners étrangers en échec = panne d'ensemble, même si le scanner US a réussi
    return exit_code(places, all_failed_is_critical=True)


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
    for code in {w[0] for w in world if _market_allowed(w[0])}:
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
    if base == "EUR":                                      # suivi Telegram des positions simulées, sans doublon
        from datetime import timedelta as _td
        from stockhunter.notifications.engine import enqueue
        from stockhunter.world.telegram import opened_message, trade_message
        recent = str((datetime.now(timezone.utc) - _td(days=4)).date())
        for t in res.trades:
            if t["sortie"] >= recent:
                enqueue(engine, "alert", f"world:trade:{t['place']}:{t['ticker']}:{t['entree']}:{t['sortie']}",
                        trade_message(t, base))
        for x in res.decisions:
            if x["decision"] == "entrée" and x["seance"] >= recent:
                enqueue(engine, "alert", f"world:open:{x['place']}:{x['ticker']}:{x['seance']}", opened_message(x))
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
    remote = ((state.get("collecte") or {}).get("stockage") or {}).get("places", {})
    for m in mk.MARKETS.values():
        stored = store.stored_sessions(m.code)
        last = stored[-1] if stored else None
        n_stored = len(stored)
        if not stored and m.code in remote:            # tableau de bord sans stockage local : état consigné en base
            n_stored = remote[m.code]["seances"]
            last = pd.Timestamp(remote[m.code]["derniere"]).date() if remote[m.code]["derniere"] else None
        row = {"code": m.code, "nom": m.name, "pays": m.country_name, "region": m.region, "devise": m.currency,
               "donnees": m.data, "fournisseurs": ", ".join(m.providers) or "—", "note": m.note,
               "seances": n_stored, "derniere": str(last) if last else None, "listings": cnt.get(m.code, {}),
               "collecte": coll.get(m.code), "scanner": scans.get(m.code),
               "etat": _worst(((state.get("scanners") or {}).get("etats") or {}).get(m.code),
                              coll.get(m.code), coll.get(_EURONEXT_GROUP.get(m.code, "")))}
        if m.code in INDIA_MARKETS and not _market_allowed(m.code):
            row["donnees"] = mk.NOT_AUTHORIZED
            row["note"] = m.note + " Collecte et simulation suspendues en attente de droits."
            row["etat"] = "suspendu (autorisation requise)"
        if m.code == "US":
            row.update(seances=None, derniere=str(us_run.as_of) if us_run else None,
                       listings={"actifs": us_n, "principal": us_n},
                       scanner={"statut": "OK", "as_of": str(us_run.as_of) if us_run else None,
                                "univers": us_run.universe_size if us_run else 0})
            last = us_run.as_of if us_run else None
        if row["donnees"] == mk.PRICES:
            try:
                f = mk.freshness(m, last, now)
                row["fraicheur"] = f.detail
                row["attendue"] = str(f.expected)
            except Exception as exc:
                row["fraicheur"] = f"calendrier : {exc}"
        out.append(row)
    return out


def fx_for_display(engine: Engine, store: WorldStore) -> FxTable:
    """Taux BCE du stockage local, sinon derniers taux consignés en base (tableau de bord hébergé)."""
    fx = store.load_fx()
    if fx.empty:
        rows = (kv_get(engine, KV_STATE) or {}).get("fx_recent") or []
        fx = pd.DataFrame([{"date": pd.Timestamp(d).date(), "currency": c, "rate": r} for d, c, r in rows],
                          columns=["date", "currency", "rate"])
    return FxTable.from_long(fx)


_EURONEXT_GROUP: dict[str, str] = {}


def _worst(scan_state, coll, _unused=None) -> str | None:
    """État affiché d'une place : le pire de la collecte et du scanner (échec critique > partiel > succès)."""
    order = [CRITICAL, PARTIAL, NO_OPP, OK]
    cands = []
    if scan_state:
        cands.append(scan_state[0])
    if coll:
        cands.append(CRITICAL if coll.get("critique") else PARTIAL if coll.get("statut") in ("échec", "partiel", "en attente")
                     else OK)
    return min(cands, key=order.index) if cands else None


def status_text(engine: Engine, store: WorldStore) -> str:
    lines = [f"Stockage mondial : {store.usage_mb():.1f} Mo / {store.budget_mb:.0f} Mo"]
    for r in market_table(engine, store):
        if r["donnees"] == mk.NONE:
            continue
        ls = r["listings"] or {}
        sc = r.get("scanner") or {}
        lines.append(f"  {r['code']:5} {r['nom'][:38]:38} {r['donnees'][:26]:26} listings {ls.get('actifs', 0):5} (principal {ls.get('principal', 0):5})  "
                     f"séances {r['seances'] if r['seances'] is not None else '—':>4}  dernière {r['derniere'] or '—'}  "
                     f"{r.get('fraicheur', '')}  scanner {sc.get('statut', '—')} ({sc.get('univers', 0)} analysées)")
    na = [r["code"] for r in market_table(engine, store) if r["donnees"] == mk.NONE]
    lines.append(f"  Sans source gratuite fiable : {', '.join(na)}")
    return "\n".join(lines)
