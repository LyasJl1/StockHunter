"""Scanner mondial (3.2) : le MÊME moteur de découverte que pour les États-Unis, appliqué place par place.

Pour chaque place dotée de cours :
1. panel (séances × ISIN) construit depuis le stockage Parquet, prix ajustés des opérations sur titres quand la
   source le permet (Inde : cours de référence ajusté publié par la bourse) ;
2. historique TRONQUÉ à la dernière discontinuité inexpliquée (variation journalière > `max_jump`) : une division
   non corrigée ne fabrique ni momentum ni krach ;
3. univers et seuils de liquidité comparables entre places : montant échangé converti en USD au taux BCE du jour
   (point-in-time), cours minimal équivalent à 1 USD ;
4. référence de marché = indice ÉQUIPONDÉRÉ de l'univers de la place, construit à partir des données (ce n'est pas
   un indice officiel ; il sert au filtre de régime et à la force relative) ;
5. catégories et règles identiques ; normalisation intra-place (position dans la catégorie de la place).

Le rang n'est pas une probabilité. Une place sans historique suffisant est déclarée « historique insuffisant »
avec le nombre de séances disponibles et requises : rien n'est inventé.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from stockhunter.db.models import WorldOutcome, WorldScan, WorldSignal
from stockhunter.db.session import session_scope
from stockhunter.discovery import engine as disc
from stockhunter.lab.panel import Panel, UniverseFilter, forward_returns, universe_mask
from stockhunter.trading.rules import Bar, Rules, plan, step
from stockhunter.world import markets as mk
from stockhunter.world.fx import FxTable, FxUnavailable
from stockhunter.world.store import WorldStore

WORLD_VERSION = "3.2.0"
CCY_SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "INR": "₹", "NOK": "NOK", "CHF": "CHF", "SEK": "SEK"}
MIN_SESSIONS = UniverseFilter().min_history          # 130 séances : même exigence que pour les États-Unis
MAX_JUMP = {"euronext": 0.45, "nse": 0.45, "bse": 0.45}
UNIT_RISK = 1_000_000.0                               # même convention de suivi unitaire que discovery/store.py
SECONDARY_OF = {"XBOM": "XNSE"}                       # place -> place principale des mêmes sociétés (même ISIN)


def _adjust_factors(long: pd.DataFrame) -> pd.Series:
    """Facteur multiplicatif d'ajustement (≤ 1 avant une division) déduit du cours de référence ajusté publié
    par la bourse : f(t) = référence_ajustée(t) / clôture(t−1). Écart < 2 % ignoré (dividendes ordinaires)."""
    if long["prev_close_adj"].isna().all():
        return pd.Series(1.0, index=long.index)
    df = long.sort_values(["isin", "date"])
    prev_close = df.groupby("isin")["close"].shift(1)
    ratio = df["prev_close_adj"] / prev_close
    ev = ratio.where((ratio - 1).abs() > 0.02).fillna(1.0)           # événement à la date t (ex-date)
    # facteur appliqué aux séances ANTÉRIEURES à chaque événement : produit des ratios des événements postérieurs
    rev = ev.iloc[::-1].groupby(df["isin"].iloc[::-1]).cumprod().iloc[::-1]
    after = rev / ev                                                  # exclut l'événement du jour lui-même
    return after.reindex(long.index)


def known_fx_dates(code: str, days: list[date]) -> list[date]:
    """Pour chaque séance, date du dernier taux BCE PUBLIÉ à l'heure de clôture de la place (3.2.1) : à Mumbai
    (clôture 10 h UTC), le taux du jour (publié à 16 h CET) n'est pas encore connu."""
    from stockhunter.world.fx import ecb_known_date
    from stockhunter.world.portfolio import session_times
    times = session_times(code, days)
    return [ecb_known_date(times[d][1]) for d in days]


def build_market_panel(store: WorldStore, code: str, fx: FxTable, max_sessions: int = 400,
                       end: date | None = None, truncate: bool = True) -> tuple[Panel | None, dict[str, Any]]:
    """`end` : dernière séance utilisable (point-in-time). `truncate` : troncature à la dernière discontinuité
    inexpliquée CONNUE à `end` (signal) ; False pour mesurer a posteriori un résultat (suivi, rendements futurs)."""
    m = mk.get(code)
    days = [d for d in store.stored_sessions(code) if end is None or d <= end][-max_sessions:]
    info: dict[str, Any] = {"place": code, "seances": len(days), "requises": MIN_SESSIONS,
                            "premiere": str(days[0]) if days else None, "derniere": str(days[-1]) if days else None}
    if not days:
        return None, info
    long = store.load(code, days[0], days[-1])
    long = long[long["traded"].astype(bool)]
    secondary = SECONDARY_OF.get(code)
    if secondary:                                     # listing secondaire : les sociétés cotées sur la place
        prim = store.stored_sessions(secondary)        # principale y sont scannées, pas ici (pas de doublon)
        if prim:
            dup = set(pd.read_parquet(store.path(secondary, prim[-1]), columns=["isin"])["isin"])
            info["exclus_listing_principal"] = int(long.loc[long["isin"].isin(dup), "isin"].nunique())
            long = long[~long["isin"].isin(dup)]
    last = long.sort_values("date").drop_duplicates("isin", keep="last")
    info["noms"] = {r.isin: (r.ticker, r.name) for r in last.itertuples(index=False)}   # symbole le plus récent
    f = _adjust_factors(long)
    raw = long["close"].copy()                                   # clôture réellement observée ce jour-là
    long = long.assign(**{c: long[c] * f for c in ("open", "high", "low", "close")}, volume=long["volume"] / f,
                       raw_close=raw)
    wide = lambda col: long.pivot_table(index="date", columns="isin", values=col, aggfunc="last").sort_index()
    idx = pd.DatetimeIndex(pd.to_datetime(days))
    frames = {k: wide(k).reindex(idx) for k in ("open", "high", "low", "close", "volume", "raw_close", "turnover")}
    # troncature à la dernière discontinuité inexpliquée
    jump = MAX_JUMP.get(m.providers[0], 0.45)
    ret = frames["close"].pct_change(fill_method=None)
    bad = ret.abs() > jump
    last_bad = bad[::-1].cumsum()[::-1] > 0          # vrai sur et avant la dernière discontinuité
    if truncate:
        cut = last_bad & bad.any()
        for k in frames:
            frames[k] = frames[k].mask(cut)
    info["titres_tronques"] = int(bad.any().sum()) if truncate else 0
    try:
        kd = pd.DatetimeIndex(pd.to_datetime(known_fx_dates(code, days)))
        uniq = fx.series(m.currency, "USD", kd.unique().sort_values())
        usd = pd.Series(uniq.reindex(kd).to_numpy(), index=idx)                    # taux connu à la clôture
    except FxUnavailable as exc:
        info["erreur"] = str(exc)
        return None, info
    dollar_vol = frames["turnover"].mul(usd, axis=0)
    if dollar_vol.isna().all().all():
        dollar_vol = (frames["raw_close"] * frames["volume"]).mul(usd, axis=0)
    rets = frames["close"].pct_change(fill_method=None)
    liquid = dollar_vol.rolling(20, min_periods=15).median() >= UniverseFilter().min_dollar_vol / 2
    ew = rets.where(liquid.shift(1, fill_value=False)).mean(axis=1).fillna(0.0)
    bench = (1 + ew).cumprod() * 100
    p = Panel(open=frames["open"], high=frames["high"], low=frames["low"], close=frames["close"],
              volume=frames["volume"], raw_close=frames["raw_close"], dollar_vol=dollar_vol,
              bench=bench, bench_open=bench.shift(1).fillna(100.0))
    info["usd_par_unite"] = float(usd.iloc[-1]) if np.isfinite(usd.iloc[-1]) else None
    return p, info


def market_filter(code: str, fx: FxTable, d) -> UniverseFilter:
    """Seuils de la place : 1 USD de cours minimal (converti), 2 M USD de montant médian échangé (comme aux US)."""
    m = mk.get(code)
    d = pd.Timestamp(d).date()
    one_usd_local = fx.rate("USD", m.currency, known_fx_dates(code, [d])[0])
    return replace(UniverseFilter(), min_price=one_usd_local)


def scan_market(engine: Engine, store: WorldStore, code: str, fx: FxTable, validation: dict | None = None,
                rules: Rules = Rules(), save: bool = True, end: date | None = None) -> dict[str, Any]:
    m = mk.get(code)
    stored = store.stored_sessions(code)
    if save and end is None and stored:                 # séance déjà traitée : pas de recalcul (reprise, 3.2.1)
        with session_scope(engine) as s:
            done = s.scalar(select(WorldScan).where(WorldScan.market == code, WorldScan.as_of == stored[-1],
                                                    WorldScan.engine_version == WORLD_VERSION))
            if done is not None:
                ov = json.loads(done.overview_json)
                cats = {k: {**v, "candidats": [json.loads(g.card_json) for g in s.scalars(
                    select(WorldSignal).where(WorldSignal.scan_id == done.id, WorldSignal.category == k))]}
                    for k, v in (ov.get("categories") or {}).items()}
                return {"place": code, "version": WORLD_VERSION, "statut": done.status, "as_of": str(done.as_of),
                        "univers": done.universe_size, "categories": cats, "conclusion": done.conclusion,
                        "seances": ov.get("seances"), "deja_traitee": True}
    p, info = build_market_panel(store, code, fx, end=end)
    base = {"place": code, "version": WORLD_VERSION, **{k: v for k, v in info.items() if k != "noms"}}
    if p is None or len(p.close) < MIN_SESSIONS:
        res = {**base, "statut": "HISTORIQUE_INSUFFISANT", "as_of": info.get("derniere"), "univers": 0,
               "categories": {}, "conclusion": f"{m.name} : {info['seances']} séance(s) stockée(s), {MIN_SESSIONS} "
               f"requises pour le scanner. Historique en constitution : aucun classement publié."}
        if info.get("erreur"):
            res["statut"], res["conclusion"] = "ERREUR_DONNEES", f"{m.name} : {info['erreur']}"
        if save and info.get("derniere"):
            _save(engine, res, m, fx)
        return res
    d = p.close.index[-1]
    names = dict(info.get("noms") or {})
    from stockhunter.db.models import WorldListing
    with session_scope(engine) as s:
        for isin, ticker, name in s.execute(select(WorldListing.isin, WorldListing.ticker, WorldListing.name)
                                            .where(WorldListing.market == code)):
            names[isin] = (ticker, name)
    out = disc.run(p, {k: v[1] for k, v in names.items()}, market_filter(code, fx, d), rules, validation,
                   ccy=CCY_SYMBOL.get(m.currency, m.currency), bench_label=f"indice équipondéré {m.code}")
    total = 0
    for cat in out["categories"].values():
        for c in cat["candidats"]:
            c["isin"], c["ticker"] = c["ticker"], names.get(c["ticker"], (c["ticker"], None))[0]
            c["norm_score"] = round(1 - (c["rank"] - 1) / max(cat["admis"], 1), 4)
            c["admis"] = cat["admis"]
            total += 1
    res = {**base, **out, "statut": "OK" if total else "AUCUNE_OPPORTUNITE", "as_of": str(out["as_of"])}
    res["marche"]["reference"] = f"indice équipondéré StockHunter de l'univers {m.code} (pas un indice officiel)"
    if save:
        _save(engine, res, m, fx)
    return res


def _save(engine: Engine, res: dict, m: mk.Market, fx: FxTable) -> tuple[int, bool]:
    as_of = date.fromisoformat(str(res["as_of"]))
    with session_scope(engine) as s:
        old = s.scalar(select(WorldScan).where(WorldScan.market == m.code, WorldScan.as_of == as_of,
                                               WorldScan.engine_version == WORLD_VERSION))
        if old is not None:
            return old.id, False                       # jamais réécrit
        overview = {k: res.get(k) for k in ("marche", "seances", "requises", "premiere", "derniere", "titres_tronques")}
        overview["categories"] = {k: {kk: v.get(kk) for kk in ("label", "admis", "suspendue", "validation", "motif")}
                                  for k, v in res.get("categories", {}).items()}
        run = WorldScan(market=m.code, as_of=as_of, engine_version=WORLD_VERSION, universe_size=int(res.get("univers", 0)),
                        status=res["statut"], overview_json=json.dumps(overview, ensure_ascii=False, default=str),
                        conclusion=res["conclusion"])
        s.add(run)
        s.flush()
        for key, cat in res.get("categories", {}).items():
            for c in cat["candidats"]:
                try:
                    kd = known_fx_dates(m.code, [as_of])[0]          # taux connu à la clôture de la place
                    eur, usd, fxd = (c["close"] * fx.rate(m.currency, "EUR", kd),
                                     c["close"] * fx.rate(m.currency, "USD", kd), kd)
                except FxUnavailable:
                    eur = usd = fxd = None
                s.add(WorldSignal(scan_id=run.id, market=m.code, as_of=as_of, isin=c["isin"], ticker=c["ticker"],
                                  name=c.get("name"), category=key, rank=c["rank"], admitted=c["admis"],
                                  score=c["score"], norm_score=c["norm_score"], currency=m.currency, close=c["close"],
                                  close_eur=eur, close_usd=usd, fx_date=fxd, risk_pct=c["plan"]["risque_pct"],
                                  confidence=c["confidence"], card_json=json.dumps(c, ensure_ascii=False, default=str)))
        return run.id, True


# ============================================================================ suivi des sélections
def update_outcomes(engine: Engine, store: WorldStore, code: str, fx: FxTable, rules: Rules = Rules(),
                    stale_sessions: int = 10) -> dict[str, int]:
    """Même suivi unitaire que les États-Unis (règles partagées, frais et glissement), en devise locale, puis
    rendement à 20 séances converti en EUR (effet de change séparé). Un résultat final n'est jamais recalculé."""
    p, _ = build_market_panel(store, code, fx, truncate=False)      # mesure a posteriori : aucune troncature
    counts = {"mis_a_jour": 0, "finalises": 0}
    if p is None:
        return counts
    m = mk.get(code)
    sessions = p.close.index
    pos_of = {d.date(): i for i, d in enumerate(sessions)}
    fw20 = forward_returns(p, 20)
    mask = universe_mask(p, market_filter(code, fx, sessions[-1]))
    try:
        eur = fx.series(m.currency, "EUR", sessions)
    except FxUnavailable:
        eur = pd.Series(np.nan, index=sessions)
    with session_scope(engine) as s:
        final_ids = set(s.scalars(select(WorldOutcome.signal_id).where(WorldOutcome.final.is_(True))))
        for c in s.scalars(select(WorldSignal).where(WorldSignal.market == code).order_by(WorldSignal.id)):
            if c.id in final_ids or c.as_of not in pos_of or c.isin not in p.close.columns:
                continue
            i0, t = pos_of[c.as_of], c.isin
            card = json.loads(c.card_json)["plan"]
            adj0 = p.close.iat[i0, p.close.columns.get_loc(t)]
            if not np.isfinite(adj0) or not card["signal_close"]:
                continue
            scale = adj0 / card["signal_close"]
            pos = plan(adj0, card["atr"] * scale, rules)
            size = lambda fill, off: (UNIT_RISK / off, None)
            last_seen = i0
            for i in range(i0 + 1, len(sessions)):
                o, h, lo, cl = (p.open.iat[i, p.open.columns.get_loc(t)], p.high.iat[i, p.high.columns.get_loc(t)],
                                p.low.iat[i, p.low.columns.get_loc(t)], p.close.iat[i, p.close.columns.get_loc(t)])
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
                    last_px = p.close.iat[last_seen, p.close.columns.get_loc(t)]
                    exit_cost = last_px * pos.shares * rules.cost_bps / 1e4
                    r = ((last_px - pos.entry_price) * pos.shares + pos.realized - pos.costs - exit_cost) / pos.initial_risk
                detail = "plus de cotation (radiation ou suspension) : valorisé au dernier cours connu"
            ret20 = fw20.iat[i0, fw20.columns.get_loc(t)]
            ret20 = float(ret20) if np.isfinite(ret20) else None
            row = s.get(WorldOutcome, c.id) or WorldOutcome(signal_id=c.id, status="PLANNED")
            row.ret_20_local = ret20
            if ret20 is not None and i0 + 20 < len(sessions):
                x0, x1 = eur.iloc[i0 + 1], eur.iloc[i0 + 20]
                if np.isfinite(x0) and np.isfinite(x1):
                    conv = (1 + ret20) * (x1 / x0) - 1
                    row.ret_20_eur, row.fx_effect_eur = float(conv), float(conv - ret20)
                univ = fw20.iloc[i0][mask.iloc[i0]].mean()
                row.excess_20 = float(ret20 - univ) if np.isfinite(univ) else None
            done_trade = pos.status in ("CLOSED", "CANCELLED") or delisted
            final = done_trade and (ret20 is not None or delisted or i0 + 20 < len(sessions))
            row.status = "CLOSED" if delisted and pos.status == "OPEN" else pos.status
            row.final, row.entry_date, row.exit_date = final, pos.entry_date, pos.exit_date
            row.r_multiple, row.detail = (None if r is None else float(r)), detail
            s.add(row)
            counts["mis_a_jour"] += 1
            counts["finalises"] += int(final)
    return counts
