"""Persistance du moteur de découverte : classement figé, analyse approfondie, suivi du résultat de chaque signal."""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from stockhunter.catalysts.classify import classify_8k, classify_headline
from stockhunter.db.models import DiscoveryCandidate, DiscoveryDeep, DiscoveryOutcome, DiscoveryRun, MarketTicker
from stockhunter.db.session import session_scope
from stockhunter.lab.panel import Panel, forward_returns, universe_mask
from stockhunter.trading.rules import Bar, Rules, plan, step

log = logging.getLogger(__name__)
DEEP_TOP_PER_CATEGORY = 3
DEEP_MAX = 12


def ticker_names(engine: Engine) -> dict[str, str]:
    with session_scope(engine) as s:
        return {t: n for t, n in s.execute(select(MarketTicker.ticker, MarketTicker.name).where(MarketTicker.type == "CS")) if n}


def registry_decisions() -> dict[str, str]:
    from stockhunter.lab import registry
    return {e["id"]: e["decision"] for e in registry.load()["experiences"]}


def save(engine: Engine, result: dict[str, Any]) -> tuple[int, bool]:
    """Enregistre le classement d'une séance. Idempotent : une séance déjà enregistrée n'est jamais réécrite.
    Renvoie (identifiant, créé ?)."""
    with session_scope(engine) as s:
        prev = s.scalar(select(DiscoveryRun.id).where(DiscoveryRun.as_of == result["as_of"],
                                                      DiscoveryRun.engine_version == result["version"]))
        if prev:
            return prev, False
        cats = {k: {x: v[x] for x in v if x != "candidats"} for k, v in result["categories"].items()}
        run = DiscoveryRun(as_of=result["as_of"], engine_version=result["version"], universe_size=result["univers"],
                           overview_json=json.dumps({"marche": result["marche"], "categories": cats}, ensure_ascii=False),
                           conclusion=result["conclusion"])
        s.add(run)
        s.flush()
        for v in result["categories"].values():
            for c in v["candidats"]:
                s.add(DiscoveryCandidate(run_id=run.id, as_of=result["as_of"], ticker=c["ticker"], name=c.get("name"),
                                         category=c["category"], rank=c["rank"], score=c["score"], close=c["close"],
                                         confidence=c["confidence"], card_json=json.dumps(c, ensure_ascii=False)))
        return run.id, True


# ============================================================================ suivi du résultat de chaque signal
def update_outcomes(engine: Engine, p: Panel, rules: Rules = Rules(), stale_sessions: int = 10) -> dict[str, int]:
    """Rejoue, avec les règles de trading partagées, le plan figé de chaque candidat sur les séances parues depuis.
    Suivi unitaire (risque fixe de 1 000 $ par signal, sans contrainte de portefeuille) : mesure la qualité de la
    SÉLECTION. Un résultat final n'est plus jamais recalculé."""
    sessions = p.close.index
    pos_of = {d.date(): i for i, d in enumerate(sessions)}
    fw20 = forward_returns(p, 20)
    mask = universe_mask(p)
    counts = {"mis_a_jour": 0, "finalises": 0}
    with session_scope(engine) as s:
        final_ids = set(s.scalars(select(DiscoveryOutcome.candidate_id).where(DiscoveryOutcome.final.is_(True))))
        for c in s.scalars(select(DiscoveryCandidate).order_by(DiscoveryCandidate.id)):
            if c.id in final_ids or c.as_of not in pos_of or c.ticker not in p.close.columns:
                continue
            i0, t = pos_of[c.as_of], c.ticker
            card = json.loads(c.card_json)["plan"]
            adj0 = p.close.iat[i0, p.close.columns.get_loc(t)]
            if not np.isfinite(adj0) or not card["signal_close"]:
                continue
            scale = adj0 / card["signal_close"]               # unités du jour du signal -> unités ajustées actuelles
            pos = plan(adj0, card["atr"] * scale, rules)
            size = lambda fill, off: (1000.0 / off, None)
            last_seen = i0
            for i in range(i0 + 1, len(sessions)):
                o, h, lo, cl = (p.open.at[sessions[i], t], p.high.at[sessions[i], t], p.low.at[sessions[i], t],
                                p.close.at[sessions[i], t])
                if not (np.isfinite(o) and np.isfinite(cl)):
                    continue
                last_seen = i
                step(pos, Bar(sessions[i].date(), float(o), float(h), float(lo), float(cl)), size, rules)
                if pos.status in ("CLOSED", "CANCELLED"):
                    break
            delisted = pos.status in ("PLANNED", "OPEN") and len(sessions) - 1 - last_seen >= stale_sessions
            r = (pos.realized - pos.costs) / pos.initial_risk if pos.status == "CLOSED" and pos.initial_risk else None
            detail = pos.close_reason
            if delisted:
                if pos.status == "OPEN":
                    last_px = p.close.at[sessions[last_seen], t]
                    r = ((last_px - pos.entry_price) * pos.shares + pos.realized - pos.costs) / pos.initial_risk
                detail = "plus de cotation (radiation ou suspension) : valorisé au dernier cours connu"
            ret20 = fw20.iat[i0, fw20.columns.get_loc(t)]
            ret20 = float(ret20) if np.isfinite(ret20) else None
            univ = fw20.iloc[i0][mask.iloc[i0]].mean() if ret20 is not None else None
            done_trade = pos.status in ("CLOSED", "CANCELLED") or delisted
            final = done_trade and (ret20 is not None or delisted)
            row = s.get(DiscoveryOutcome, c.id) or DiscoveryOutcome(candidate_id=c.id, status="PLANNED")
            row.status = "CLOSED" if delisted and pos.status == "OPEN" else pos.status
            row.final, row.entry_date, row.exit_date = final, pos.entry_date, pos.exit_date
            row.r_multiple = None if r is None else float(r)
            row.ret_20, row.excess_20 = ret20, (None if ret20 is None else float(ret20 - univ))
            row.detail = detail
            s.add(row)
            counts["mis_a_jour"] += 1
            counts["finalises"] += int(final)
    return counts


def performance(engine: Engine) -> dict[str, Any]:
    """Bilan prospectif par catégorie (signaux finalisés uniquement). Descriptif tant que l'échantillon est petit."""
    with session_scope(engine) as s:
        rows = s.execute(select(DiscoveryCandidate.category, DiscoveryCandidate.as_of, DiscoveryOutcome.status,
                                DiscoveryOutcome.final, DiscoveryOutcome.r_multiple, DiscoveryOutcome.excess_20)
                         .join(DiscoveryOutcome, DiscoveryOutcome.candidate_id == DiscoveryCandidate.id, isouter=True)).all()
    out: dict[str, Any] = {}
    df = pd.DataFrame(rows, columns=["category", "as_of", "status", "final", "r", "excess"])
    for cat, g in df.groupby("category"):
        f = g[g["final"] == True]  # noqa: E712
        traded = f[f["r"].notna()]
        by_date = f.dropna(subset=["excess"]).groupby("as_of")["excess"].mean()
        t = None
        if len(by_date) >= 8 and by_date.std() > 0:
            t = float(by_date.mean() / by_date.std() * math.sqrt(len(by_date)))
        out[cat] = {"signaux": int(len(g)), "finalises": int(len(f)), "trades": int(len(traded)),
                    "annules": int((f["status"] == "CANCELLED").sum()),
                    "r_moyen": float(traded["r"].mean()) if len(traded) else None,
                    "gagnants": float((traded["r"] > 0).mean()) if len(traded) else None,
                    "exces_20j_moyen": float(f["excess"].mean()) if f["excess"].notna().any() else None,
                    "dates": int(len(by_date)), "t_exces": t,
                    "lecture": "échantillon insuffisant (< 8 dates)" if len(by_date) < 8 else
                               "descriptif : dates qui se chevauchent, non corrigé"}
    return out


# ============================================================================ analyse approfondie des candidats
def _series(p: Panel, t: str, n: int = 260) -> dict[str, list]:
    c = p.close[t]
    scale = p.raw_close[t].iloc[-1] / c.iloc[-1] if np.isfinite(c.iloc[-1]) and c.iloc[-1] else 1.0
    df = pd.DataFrame({"close": c * scale, "sma50": c.rolling(50, min_periods=50).mean() * scale,
                       "sma200": c.rolling(200, min_periods=200).mean() * scale}).iloc[-n:].dropna(subset=["close"])
    r = lambda x: [None if pd.isna(v) else round(float(v), 2) for v in x]
    return {"dates": [d.date().isoformat() for d in df.index], "close": r(df["close"]), "sma50": r(df["sma50"]),
            "sma200": r(df["sma200"]), "note": "clôtures ajustées des splits, exprimées au cours actuel"}


def _fundamentals(sec, cik: str, t: str, as_of: date, price: float) -> dict[str, Any]:
    from stockhunter.analytics.fundamentals import fundamental_snapshot, valuation
    facts = sec.fetch_companyfacts(cik, t)
    fund = fundamental_snapshot([f for f in facts if f["filed_date"] <= as_of], as_of)
    if fund.get("no_data"):
        return {"disponible": False}
    keys = ("last_quarter_end", "last_filed", "revenue_yoy_q", "revenue_accel", "op_margin_ttm", "op_margin_delta",
            "net_income_yoy_q", "eps_yoy_q", "roe_ttm", "revenue_ttm", "net_income_ttm", "fcf_ttm")
    out = {k: fund.get(k) for k in keys if fund.get(k) is not None}
    out.update({k: v for k, v in valuation(fund, price).items() if v is not None})
    out.update(disponible=True, source="SEC EDGAR (XBRL, companyfacts)",
               url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-Q")
    return out


def deepen(engine: Engine, result: dict[str, Any], p: Panel, sec=None, finnhub=None, today: date | None = None) -> dict:
    """Série de cours pour tous les candidats (aucun appel réseau) ; fondamentaux SEC, dépôts 8-K classés et
    actualités pour les mieux classés de chaque catégorie (quelques appels, dans les quotas gratuits)."""
    as_of = result["as_of"]
    today = today or date.today()
    cards = [c for v in result["categories"].values() for c in v["candidats"]]
    top = []
    for v in result["categories"].values():
        for c in v["candidats"][:DEEP_TOP_PER_CATEGORY]:
            if c["ticker"] not in top and len(top) < DEEP_MAX:
                top.append(c["ticker"])
    with session_scope(engine) as s:
        ciks = dict(s.execute(select(MarketTicker.ticker, MarketTicker.cik)
                              .where(MarketTicker.ticker.in_([c["ticker"] for c in cards]), MarketTicker.active.is_(True))).all())
    missing = [t for t in top if not ciks.get(t)]
    if sec is not None and missing:                    # référentiel sans CIK : table officielle SEC (1 appel)
        try:
            tm = sec.fetch_ticker_map()
            ciks.update({t: tm[t]["cik"] for t in missing if t in tm})
        except Exception as exc:
            log.warning("table des tickers SEC indisponible : %s", exc)
    stats = {"series": 0, "approfondis": 0, "erreurs": 0}
    seen = set()
    for c in cards:
        t = c["ticker"]
        if t in seen or t not in p.close.columns:
            continue
        seen.add(t)
        payload: dict[str, Any] = {"series": _series(p, t), "erreurs": []}
        stats["series"] += 1
        if t in top:
            cik = str(ciks[t]).lstrip("0").zfill(10) if ciks.get(t) else None
            if sec is not None and cik:
                try:
                    payload["fondamentaux"] = _fundamentals(sec, cik, t, as_of, c["close"])
                    subs = sec.fetch_submissions(cik, t)
                    payload["depots"] = [
                        {"form": f["form"], "date": f["filed_date"].isoformat(), "items": f.get("items"), "url": f["url"],
                         "label": " ; ".join(x.label for x in classify_8k(f.get("items")) if x.importance >= 2) or None,
                         "vigilance": any(x.watch for x in classify_8k(f.get("items")))}
                        for f in sorted(subs.filings, key=lambda f: f["filed_date"], reverse=True)
                        if f["form"] in ("8-K", "8-K/A", "10-Q", "10-K") and f["filed_date"] <= as_of
                        and f["filed_date"] > as_of - timedelta(days=120)][:10]
                    payload["societe"] = {"nom": subs.name, "activite": subs.sic_description}
                except Exception as exc:
                    payload["erreurs"].append(f"SEC : {type(exc).__name__} {str(exc)[:120]}")
            elif sec is not None:
                payload["erreurs"].append("SEC : identifiant CIK inconnu pour ce titre")
            if finnhub is not None:
                try:
                    news = finnhub.fetch_company_news(t, today - timedelta(days=10), today)
                    payload["actualites"] = [
                        {"date": a["published_at"].date().isoformat(), "headline": a["headline"], "publisher": a["publisher"],
                         "url": a["url"], "category": classify_headline(a["headline"])}
                        for a in sorted(news, key=lambda a: a["published_at"], reverse=True)[:8]]
                except Exception as exc:
                    payload["erreurs"].append(f"Finnhub : {type(exc).__name__} {str(exc)[:120]}")
            stats["approfondis"] += 1
            stats["erreurs"] += len(payload["erreurs"])
        with session_scope(engine) as s:
            row = s.get(DiscoveryDeep, t)
            if row is None:
                s.add(DiscoveryDeep(ticker=t, as_of=as_of, payload=json.dumps(payload, ensure_ascii=False)))
            else:
                prev = json.loads(row.payload)
                if t not in top:                       # conserve l'analyse approfondie d'un passage précédent
                    for k in ("fondamentaux", "depots", "actualites", "societe"):
                        if k in prev:
                            payload[k] = prev[k]
                            payload["approfondi_le"] = prev.get("approfondi_le", row.as_of.isoformat())
                else:
                    payload["approfondi_le"] = as_of.isoformat()
                row.as_of, row.payload, row.fetched_at = as_of, json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc)
    return stats
