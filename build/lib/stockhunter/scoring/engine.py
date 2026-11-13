"""Moteur de scoring : calcul pur et déterministe (aucun accès base, aucun LLM).

Score global 0-100 = somme pondérée de 6 sous-scores 0-100. La plupart des composantes sont des RANGS
CENTILES au sein de l'univers suivi à la date d'analyse : un sous-score de 80 signifie « mieux que ~80 %
des actions suivies sur ce critère », pas une qualité absolue, et le score n'est PAS une probabilité.
Un sous-score sans aucune donnée vaut 50 (neutre) et fait baisser la confiance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from statistics import mean
from typing import Any

from stockhunter.analytics.technical import pct_rank
from stockhunter.scoring.config import DEFAULT_CONFIG, SUBSCORE_LABELS, ScoringConfig

REJECTED, WATCHLIST, DEVELOPING, READY = "REJECTED", "WATCHLIST", "DEVELOPING", "READY"


def fmt_pct(x: float | None, signed: bool = True, digits: int = 1) -> str:
    if x is None:
        return "N/D"
    s = f"{x * 100:+.{digits}f}" if signed else f"{x * 100:.{digits}f}"
    return s.replace(".", ",") + " %"


def fmt_pt(x: float | None) -> str:
    """Écart entre deux taux, en points de pourcentage."""
    return "N/D" if x is None else f"{x * 100:+.1f} pt".replace(".", ",")


@dataclass
class StockInput:
    symbol: str
    instrument_id: int
    asset_type: str
    tech: dict[str, Any]
    fund: dict[str, Any] = field(default_factory=dict)
    val: dict[str, Any] = field(default_factory=dict)
    earnings: dict[str, Any] | None = None      # dernier 8-K item 2.02 et réaction du marché
    upcoming: dict[str, Any] | None = None      # prochaine publication annoncée
    fund_lag: list[dict[str, Any]] = field(default_factory=list)  # 10-Q/10-K publiés sans XBRL
    sector_etf: str | None = None
    industry_etf: str | None = None
    price_stale: bool = False
    sources: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Context:
    as_of: date
    regime: dict[str, Any]
    sectors: dict[str, dict[str, Any]]  # ETF -> {rank, n, rs_blend}


# ----------------------------------------------------------------------------- marché
def market_regime(spy: dict[str, Any], vix: tuple[float, date] | None, breadth: float | None,
                  curve: tuple[float, date] | None) -> dict[str, Any]:
    """Régime de marché par règles explicites (pas d'apprentissage)."""
    reasons, pts, n = [], 0, 0
    above200 = spy.get("above_sma200")
    if above200 is not None:
        n += 1
        pts += 1 if above200 else -1
        reasons.append(f"SPY {'au-dessus' if above200 else 'sous'} sa moyenne 200 j")
    if spy.get("sma50_above_sma200") is not None:
        n += 1
        pts += 1 if spy["sma50_above_sma200"] else -1
    r3 = spy.get("ret_3m")
    if r3 is not None:  # tendance récente de l'indice
        n += 1
        pts += 1 if r3 > 0.02 else (-1 if r3 < -0.02 else 0)
        reasons.append(f"SPY {fmt_pct(r3)} sur 3 mois")
    if vix:
        n += 1
        v = vix[0]
        pts += 1 if v < 18 else (-1 if v > 25 else 0)
        reasons.append(f"VIX {v:.1f} ({vix[1].isoformat()})".replace(".", ",", 1))
    if breadth is not None:
        n += 1
        pts += 1 if breadth > 0.55 else (-1 if breadth < 0.40 else 0)
        reasons.append(f"{breadth * 100:.0f} % des actions suivies au-dessus de leur moyenne 50 j")
    if curve:
        reasons.append(f"écart 10 ans - 2 ans : {curve[0]:+.2f} pt ({curve[1].isoformat()})".replace(".", ",", 1))
    if n == 0:
        label, score = "INCONNU", 50.0
    else:
        r = pts / n
        label = "RISK-ON" if r >= 0.5 else ("RISK-OFF" if r <= -0.5 else "NEUTRE")
        score = 50 + 40 * r
    return {"label": label, "score": round(score, 1), "reasons": reasons,
            "vix": vix[0] if vix else None, "breadth": breadth,
            "spy_ret_1m": spy.get("ret_1m"), "spy_ret_3m": spy.get("ret_3m")}


def rank_sectors(etf_tech: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Classe les ETF sectoriels par force relative vs SPY (moyenne 1 mois / 3 mois)."""
    blend = {}
    for etf, t in etf_tech.items():
        a, b = t.get("rs_spy_1m"), t.get("rs_spy_3m")
        blend[etf] = (a + b) / 2 if a is not None and b is not None else None
    ranked = sorted((k for k, v in blend.items() if v is not None), key=lambda k: -blend[k])
    return {k: {"rank": i + 1, "n": len(ranked), "rs_blend": blend[k]} for i, k in enumerate(ranked)}


# ----------------------------------------------------------------------------- scoring
def _avg(parts: list[float | None], expected: int) -> tuple[float | None, float]:
    vals = [p for p in parts if p is not None]
    return (mean(vals) if vals else None), (len(vals) / expected if expected else 0.0)


def _earnings_score(e: dict[str, Any] | None) -> float | None:
    if not e or e.get("reaction_excess") is None:
        return None
    x = e["reaction_excess"]
    base = 90 if x > 0.05 else 72 if x > 0.02 else 50 if x > -0.02 else 30 if x > -0.05 else 12
    d = e.get("drift_excess")
    if d is not None:  # le marché confirme-t-il la réaction depuis ?
        base += 8 if d > 0.02 else (-8 if d < -0.02 else 0)
    return float(max(0, min(100, base)))


def score_universe(stocks: list[StockInput], ctx: Context,
                   cfg: ScoringConfig = DEFAULT_CONFIG) -> dict[str, dict[str, Any]]:
    th, w = cfg.thresholds, asdict(cfg.weights)
    cands = [s for s in stocks if s.asset_type == "stock" and not s.tech.get("insufficient_history")]
    T = {s.symbol: s.tech for s in cands}
    F = {s.symbol: s.fund for s in cands}
    V = {s.symbol: s.val for s in cands}

    def pr(src: dict, key: str, higher: bool = True) -> dict[str, float | None]:
        return pct_rank({k: d.get(key) for k, d in src.items()}, higher)

    p = {
        "rs3": pr(T, "rs_spy_3m"), "rs6": pr(T, "rs_spy_6m"), "mom": pr(T, "mom_12_1"),
        "high": pr(T, "dist_52w_high"), "atr": pr(T, "atr14_pct"),
        "updown": pr(T, "updown_vol_50"), "volr": pr(T, "vol_ratio_20_50"),
        "revg": pr(F, "revenue_yoy_q"), "acc": pr(F, "revenue_accel"), "mrg": pr(F, "op_margin_delta"),
        "roe": pr(F, "roe_ttm"), "fcfy": pr(V, "fcf_yield"), "psg": pr(V, "ps_to_growth", higher=False),
        "eps": pct_rank({k: (d.get("eps_yoy_q") if d.get("eps_yoy_q") is not None else d.get("net_income_yoy_q"))
                         for k, d in F.items()}),
    }

    results: dict[str, dict[str, Any]] = {}
    for s in stocks:
        if s.asset_type != "stock":
            continue
        results[s.symbol] = _score_one(s, ctx, cfg, p, w, th)
    return results


def _score_one(s: StockInput, ctx: Context, cfg: ScoringConfig, p: dict, w: dict, th) -> dict[str, Any]:
    t, f, v, sym = s.tech, s.fund, s.val, s.symbol
    pos: list[str] = []
    neg: list[str] = []
    sub: dict[str, dict[str, Any]] = {}

    if t.get("insufficient_history"):
        return {"status": REJECTED, "status_reason": f"historique insuffisant ({t.get('bars', 0)} séances)",
                "total": None, "confidence": 0.0, "subscores": {}, "strategies": {}, "reasons_pos": [],
                "reasons_neg": ["Historique de prix insuffisant"], "metrics": {}, "sources": s.sources}

    # --- Technique & momentum
    trend_parts = [t.get("above_sma50"), t.get("above_sma200"), t.get("sma50_above_sma200"), t.get("sma50_rising")]
    trend_known = [x for x in trend_parts if x is not None]
    trend = 100 * sum(trend_known) / len(trend_known) if trend_known else None
    tech, cov = _avg([p["rs3"][sym], p["rs6"][sym], p["mom"][sym], p["high"][sym], trend], 5)
    if tech is not None and t.get("breakout_holding"):
        tech = min(100.0, tech + 10)
    sub["technical"] = {"score": tech, "coverage": cov, "components": {
        "rang RS 3 m": p["rs3"][sym], "rang RS 6 m": p["rs6"][sym], "rang momentum 12-1": p["mom"][sym],
        "rang proximité plus haut 52 s": p["high"][sym], "tendance (MM50/MM200)": trend,
        "bonus breakout tenu": 10 if t.get("breakout_holding") else 0}}
    rs3 = t.get("rs_spy_3m")
    if rs3 is not None:
        (pos if rs3 > 0 else neg).append(f"{'Surperforme' if rs3 > 0 else 'Sous-performe'} le SPY de {fmt_pct(abs(rs3), False)} sur 3 mois")
    if t.get("above_sma200") is False:
        neg.append("Cours sous sa moyenne mobile 200 jours (tendance de fond baissière)")
    elif t.get("sma50_above_sma200") and t.get("above_sma50"):
        pos.append("Tendance haussière : cours > MM50 > MM200")
    if t.get("breakout_holding"):
        pos.append(f"Cassure du plus haut 50 séances le {t['breakout_date']} avec volume, niveau {t['breakout_level']:.2f} $ tenu".replace(".", ",", 1))
    dh = t.get("dist_52w_high")
    if dh is not None and dh < -0.25:
        neg.append(f"À {fmt_pct(abs(dh), False)} sous son plus haut 52 semaines")

    # --- Fondamentaux & valorisation
    fparts = [p["revg"][sym], p["acc"][sym], p["mrg"][sym], p["eps"][sym], p["roe"][sym], p["fcfy"][sym], p["psg"][sym]]
    fund_score, fcov = _avg(fparts, len(fparts))
    if sum(x is not None for x in fparts) < 3:
        fund_score = None
    if s.fund_lag:
        fcov *= 0.5
    sub["fundamental"] = {"score": fund_score, "coverage": fcov, "components": {
        "rang croissance CA (a/a)": p["revg"][sym], "rang accélération CA": p["acc"][sym],
        "rang évolution marge op.": p["mrg"][sym], "rang croissance bénéfice": p["eps"][sym],
        "rang ROE": p["roe"][sym], "rang rendement FCF": p["fcfy"][sym], "rang P/S ÷ croissance (bas = mieux)": p["psg"][sym]}}
    g, acc = f.get("revenue_yoy_q"), f.get("revenue_accel")
    if g is not None:
        msg = f"CA du trimestre clos le {f.get('last_quarter_end')} : {fmt_pct(g)} sur un an"
        if acc is not None:
            msg += f" ({fmt_pt(acc)} vs la croissance du trimestre précédent)"
        if g >= 0.10 and (acc is None or acc > -0.05):
            pos.append(msg)
        elif g < 0.05 or (acc is not None and acc < -0.05):
            neg.append(msg)
    md = f.get("op_margin_delta")
    if md is not None and abs(md) >= 0.01:
        (pos if md > 0 else neg).append(f"Marge opérationnelle TTM {fmt_pct(f.get('op_margin_ttm'), False)}, {fmt_pt(md)} en un an")
    if f.get("emerging_profitability"):
        pos.append("Rentabilité émergente : bénéfice TTM redevenu positif")
    if v.get("pe_ttm"):
        msg = f"P/E TTM {v['pe_ttm']:.1f}".replace(".", ",")
        if v.get("ps_ttm"):
            msg += f", P/S {v['ps_ttm']:.1f}".replace(".", ",")
        if v["pe_ttm"] > 60:
            neg.append(msg + " : valorisation exigeante")
        elif v["pe_ttm"] < 20 and (g or 0) > 0.05:
            pos.append(msg + " : valorisation modérée au regard de la croissance")
    elif f.get("net_income_ttm") is not None and f["net_income_ttm"] <= 0:
        neg.append("Bénéfice TTM négatif : P/E non significatif")
    if p["psg"][sym] is not None and p["psg"][sym] < 25:
        neg.append(f"Valorisation élevée : P/S {v['ps_ttm']:.1f} rapporté à la croissance, parmi les plus chers "
                   f"de l'univers suivi".replace(".", ",", 1))
    if s.fund_lag:
        neg.append(f"Fondamentaux en retard : {len(s.fund_lag)} rapport(s) publié(s) absents des données XBRL SEC "
                   f"(dernier utilisable : {f.get('last_filed')})")

    # --- Catalyseurs documentés (phase 2 : résultats via 8-K item 2.02)
    e = s.earnings
    cat = _earnings_score(e)
    sub["catalyst"] = {"score": cat if cat is not None else 40.0, "coverage": 1.0 if cat is not None else 0.5,
                       "components": {"réaction aux derniers résultats": cat}}
    if e and e.get("reaction_excess") is not None:
        x = e["reaction_excess"]
        msg = (f"Résultats (8-K du {e['filed_date']}) : réaction {fmt_pct(e['reaction_abs'])}, "
               f"soit {fmt_pct(x)} vs SPY")
        if e.get("drift_excess") is not None:
            msg += f" ; depuis : {fmt_pct(e['drift_excess'])} vs SPY"
        (pos if x > 0.02 else neg if x < -0.02 else neg).append(msg + ("" if abs(x) > 0.02 else " (réaction neutre)"))
    else:
        neg.append(f"Aucune publication de résultats documentée sur les {th.earnings_event_max_sessions} dernières séances")

    # --- Volume & liquidité
    dv = t.get("dollar_vol_20")
    liq = min(100.0, 100 * dv / (5 * th.min_dollar_volume)) if dv else None
    vparts, vcov = _avg([p["updown"][sym], p["volr"][sym]], 2)
    volume = None if vparts is None and liq is None else 0.7 * (vparts if vparts is not None else 50) + 0.3 * (liq or 0)
    sub["volume_liquidity"] = {"score": volume, "coverage": vcov, "components": {
        "rang volume hausse/baisse 50 j": p["updown"][sym], "rang volume 20 j / 50 j": p["volr"][sym],
        "liquidité (volume $ 20 j)": liq}}
    ud = t.get("updown_vol_50")
    if ud is not None:
        if ud > 1.2:
            pos.append(f"Accumulation : volume des séances de hausse = {ud:.2f}× celui des baisses (50 j)".replace(".", ","))
        elif ud < 0.8:
            neg.append(f"Distribution : volume des séances de hausse = {ud:.2f}× celui des baisses (50 j)".replace(".", ","))

    # --- Marché & secteur
    sec = ctx.sectors.get(s.sector_etf or "")
    sec_score = 100 * (1 - (sec["rank"] - 1) / max(1, sec["n"] - 1)) if sec else None
    ms = 0.5 * ctx.regime["score"] + 0.5 * (sec_score if sec_score is not None else 50)
    sub["market_sector"] = {"score": ms, "coverage": 1.0 if sec else 0.5, "components": {
        "régime de marché": ctx.regime["score"], f"rang secteur {s.sector_etf or 'N/D'}": sec_score}}
    if sec:
        txt = f"Secteur {s.sector_etf} classé {sec['rank']}/{sec['n']} en force relative (1-3 mois)"
        if sec["rank"] <= 3:
            pos.append(txt)
        elif sec["rank"] > sec["n"] - 3:
            neg.append(txt)
    rs_sec = t.get("rs_sector_3m")
    if rs_sec is not None and abs(rs_sec) > 0.03:
        (pos if rs_sec > 0 else neg).append(f"{fmt_pct(rs_sec)} vs son ETF sectoriel sur 3 mois")

    # --- Risque & qualité des données (100 = risque faible)
    risk, rnotes = 100.0, {}
    if p["atr"][sym] is not None:
        risk -= 0.3 * p["atr"][sym]
        rnotes["volatilité (rang ATR)"] = -round(0.3 * p["atr"][sym], 1)
    ext = t.get("ext_sma50")
    if ext is not None and ext > th.max_extension_sma50:
        pen = 25 if ext > 0.20 else 12
        risk -= pen
        rnotes["extension vs MM50"] = -pen
        neg.append(f"Étendu : {fmt_pct(ext)} au-dessus de la moyenne 50 jours (risque de poursuite)")
    if ext is not None and 0.08 < ext <= th.max_extension_sma50:
        neg.append(f"Déjà {fmt_pct(ext)} au-dessus de la moyenne 50 jours : proche de la limite d'extension "
                   f"({fmt_pct(th.max_extension_sma50, False, 0)})")
    up = s.upcoming
    if up and up.get("sessions_until") is not None and up["sessions_until"] <= th.earnings_caution_sessions:
        risk -= 15
        rnotes["résultats imminents"] = -15
        neg.append(f"Résultats attendus le {up['date']} (dans {up['sessions_until']} séance(s)) : risque d'événement")
    if s.fund_lag:
        risk -= 10
        rnotes["fondamentaux en retard"] = -10
    if dh is not None and dh < -0.30:
        risk -= 15
        rnotes["tendance cassée (>30 % sous le plus haut)"] = -15
    sub["risk_quality"] = {"score": max(0.0, min(100.0, risk)), "coverage": 1.0, "components": rnotes}

    # --- Agrégation
    total, conf = 0.0, 0.0
    for k, wt in w.items():
        sc = sub[k]["score"]
        total += wt * (sc if sc is not None else 50.0)
        conf += wt * (sub[k]["coverage"] if sc is not None else 0.0)
        sub[k]["label"] = SUBSCORE_LABELS[k]
        sub[k]["weight"] = wt
        if sc is not None:
            sub[k]["score"] = round(sc, 1)
    total = round(total, 1)
    conf = round(conf, 2)

    strategies = detect_strategies(s, ctx, th)
    status, reason = decide_status(s, total, conf, strategies, ctx, th)
    metrics = {k: t.get(k) for k in ("close", "price_date", "ret_1m", "ret_3m", "ret_12m", "rs_spy_3m", "rs_sector_3m",
                                     "dist_52w_high", "ext_sma50", "atr14_pct", "dollar_vol_20", "squeeze_pct",
                                     "breakout_date", "breakout_level", "sma50", "sma200")}
    metrics.update({k: f.get(k) for k in ("last_quarter_end", "last_filed", "revenue_yoy_q", "revenue_accel",
                                          "op_margin_ttm", "op_margin_delta", "net_income_yoy_q", "eps_yoy_q", "roe_ttm")})
    metrics.update({k: v.get(k) for k in ("market_cap", "pe_ttm", "ps_ttm", "fcf_yield")})
    return {"total": total, "confidence": conf, "subscores": sub, "strategies": strategies,
            "reasons_pos": pos, "reasons_neg": neg, "status": status, "status_reason": reason,
            "metrics": metrics, "sources": s.sources}


# ----------------------------------------------------------------------------- stratégies
def detect_strategies(s: StockInput, ctx: Context, th) -> dict[str, dict[str, Any]]:
    """Familles de signaux indépendantes. Stade RESEARCH : aucune n'est validée historiquement."""
    t, f = s.tech, s.fund
    out: dict[str, dict[str, Any]] = {}

    # A. Early momentum : force relative en hausse + tendance saine + configuration de départ, sans extension
    setup = []
    if (t.get("squeeze_pct") is not None and t["squeeze_pct"] <= 0.25):
        setup.append("contraction de volatilité")
    if t.get("constructive_base"):
        setup.append("base constructive près des plus hauts")
    if t.get("breakout_holding"):
        setup.append(f"breakout tenu ({t.get('breakout_date')})")
    cond = [("RS 3 m > 0", (t.get("rs_spy_3m") or -1) > 0), ("ligne de RS en hausse", bool(t.get("rs_line_rising"))),
            ("cours > MM50", bool(t.get("above_sma50"))), ("MM50 > MM200", bool(t.get("sma50_above_sma200"))),
            ("configuration", bool(setup)),
            ("pas d'extension excessive", (t.get("ext_sma50") or 0) <= th.max_extension_sma50)]
    out["A"] = _strat("Early momentum", cond, ", ".join(setup))

    # B. Earnings momentum : réaction positive aux résultats confirmée par la dérive post-publication
    e = s.earnings or {}
    g = f.get("eps_yoy_q") if f.get("eps_yoy_q") is not None else f.get("net_income_yoy_q")
    cond = [("8-K résultats récent", e.get("reaction_excess") is not None),
            ("réaction > +3 % vs SPY", (e.get("reaction_excess") or -1) > 0.03),
            ("dérive post-résultats ≥ 0", (e.get("drift_excess") if e.get("drift_excess") is not None else -1) >= 0),
            ("bénéfice en croissance", g is None or g > 0)]
    out["B"] = _strat("Earnings momentum (PEAD)", cond)

    # C. Growth acceleration
    cond = [("CA > +10 % a/a", (f.get("revenue_yoy_q") or -1) > 0.10),
            ("CA en accélération", (f.get("revenue_accel") or -1) > 0),
            ("marge op. en hausse", (f.get("op_margin_delta") or -1) > 0),
            ("fondamentaux à jour", not s.fund_lag)]
    out["C"] = _strat("Growth acceleration", cond)

    # D. Sector rotation
    sec = ctx.sectors.get(s.sector_etf or "")
    cond = [("secteur dans le top 3", bool(sec and sec["rank"] <= 3)),
            ("surperforme son secteur sur 3 m", (t.get("rs_sector_3m") or -1) > 0)]
    out["D"] = _strat("Sector rotation", cond, f"{s.sector_etf} {sec['rank']}/{sec['n']}" if sec else "")

    out["E"] = {"label": "Catalyst investing", "active": False, "implemented": False,
                "detail": "prévue en phase 6 : analyse sourcée des 8-K, contrats, décisions réglementaires"}
    out["F"] = {"label": "High potential / spéculatif", "active": False, "implemented": False,
                "detail": "non applicable : l'univers actuel ne contient que des grandes capitalisations liquides"}
    return out


def _strat(label: str, cond: list[tuple[str, bool]], detail: str = "") -> dict[str, Any]:
    ok = all(c for _, c in cond)
    return {"label": label, "active": ok, "implemented": True, "detail": detail,
            "conditions": {name: bool(c) for name, c in cond},
            "missing": [name for name, c in cond if not c]}


def decide_status(s: StockInput, total: float, conf: float, strategies: dict, ctx: Context, th) -> tuple[str, str]:
    t = s.tech
    if s.price_stale:
        return REJECTED, f"cours non à jour (dernière séance {t.get('price_date')})"
    if (t.get("dollar_vol_20") or 0) < th.min_dollar_volume:
        return REJECTED, "liquidité insuffisante"
    if conf < th.min_confidence:
        return REJECTED, f"confiance dans les données insuffisante ({conf:.2f})"
    if total < th.reject_below:
        return REJECTED, f"score {total} < {th.reject_below}"
    active = [k for k, v in strategies.items() if v.get("active")]
    if total >= th.developing_at and active:
        blockers = []
        if total < th.ready_at:
            blockers.append(f"score < {th.ready_at}")
        if not (t.get("breakout_holding") or strategies["B"]["active"]):
            blockers.append("pas de déclencheur confirmé (breakout tenu ou réaction aux résultats)")
        if ctx.regime["label"] == "RISK-OFF":
            blockers.append("marché en RISK-OFF")
        if (t.get("ext_sma50") or 0) > th.max_extension_sma50:
            blockers.append("trop étendu au-dessus de la MM50")
        if (t.get("gap_last") or 0) > th.max_gap_last:
            blockers.append("gap d'ouverture excessif à la dernière séance")
        up = s.upcoming
        if up and up.get("sessions_until") is not None and up["sessions_until"] <= th.earnings_blackout_sessions:
            blockers.append("résultats imminents")
        if not blockers:
            return READY, f"stratégie(s) {', '.join(active)} actives, déclencheur confirmé, aucun blocage"
        return DEVELOPING, f"stratégie(s) {', '.join(active)} actives ; en attente : " + " ; ".join(blockers)
    if not active:
        return WATCHLIST, "aucune famille de signaux active"
    return WATCHLIST, f"stratégie(s) {', '.join(active)} actives mais score {total} < {th.developing_at}"
