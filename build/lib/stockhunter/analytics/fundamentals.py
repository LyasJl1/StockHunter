"""Fondamentaux point-in-time à partir des faits XBRL SEC.

Règles (vérifiées sur les données réelles du 12/11/2026) :
- Seuls les faits issus de rapports périodiques (10-Q, 10-K, 20-F, 40-F et amendements) déposés
  au plus tard à la date d'analyse sont utilisés (`filed_date <= as_of`) : pas de look-ahead.
- Une même période est souvent redéclarée dans les dépôts suivants (comparatifs) : on retient la valeur
  du dépôt le plus récent connu à la date d'analyse (prise en compte des retraitements connus à cette date).
- Les flux (CA, résultat, cash-flow) sont publiés soit sur 3 mois, soit en cumul depuis le début de
  l'exercice (6 mois, 9 mois, 12 mois). Les trimestres manquants sont reconstitués par différence de
  cumuls de même début d'exercice (ex. T4 = exercice - 9 mois).
- Le chiffre d'affaires change parfois de concept XBRL (ex. COST en 2026) : par période, on retient la
  plus grande valeur parmi les concepts de CA (le total englobe ses composantes).
- Une valeur introuvable reste None.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Iterable

PERIODIC_FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
REVENUE_CONCEPTS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                    "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
                    "RevenuesNetOfInterestExpense")
DURATION_CONCEPTS = {
    "revenue": REVENUE_CONCEPTS,
    "op_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),  # ProfitLoss (incl. minoritaires) en repli
    "cfo": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"),
    "eps": ("EarningsPerShareDiluted",),
}
NON_ADDITIVE = {"eps"}  # un BPA ne se reconstitue pas par différence de cumuls
INSTANT_CONCEPTS = {
    "cash": ("CashAndCashEquivalentsAtCarryingValue",),
    "equity": ("StockholdersEquity",),
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
    "debt_total": ("LongTermDebt",),
    "debt_noncurrent": ("LongTermDebtNoncurrent",),
    "debt_current": ("LongTermDebtCurrent",),
    "shares": ("EntityCommonStockSharesOutstanding",),  # repli si pas d'actions diluées
}
QUARTER_DAYS = (60, 120)  # trimestres fiscaux de 12 à 16 semaines (ex. COST : T4 de 112 jours)
YEAR_TOLERANCE = 20
INSTANT_MAX_AGE_DAYS = 200  # un poste de bilan plus ancien que cela (vs dernier trimestre) est ignoré


def _days(a: date, b: date) -> int:
    return (b - a).days


def _known_values(facts: Iterable[dict], as_of: date) -> dict[tuple, tuple[float, date]]:
    """(concept, start, end) -> (valeur, date de dépôt) du dépôt le plus récent <= as_of."""
    best: dict[tuple, tuple[float, date]] = {}
    for f in facts:
        if f["form"] not in PERIODIC_FORMS or f["filed_date"] > as_of:
            continue
        key = (f["concept"], f["period_start"], f["period_end"])
        cur = best.get(key)
        if cur is None or f["filed_date"] > cur[1]:
            best[key] = (f["value"], f["filed_date"])
    return best


def _quarters(periods: dict[tuple[date, date], float], additive: bool) -> dict[date, tuple[date, float]]:
    """Périodes (début, fin) -> trimestres discrets {fin: (début, valeur)}."""
    q: dict[date, tuple[date, float]] = {}
    by_start: dict[date, list[tuple[date, float]]] = defaultdict(list)
    for (start, end), val in periods.items():
        if start is None:
            continue
        d = _days(start, end)
        if QUARTER_DAYS[0] <= d <= QUARTER_DAYS[1]:
            q[end] = (start, val)
        if additive:
            by_start[start].append((end, val))
    if additive:
        for start, series in by_start.items():
            series.sort()
            for (e1, v1), (e2, v2) in zip(series, series[1:]):
                if e2 not in q and QUARTER_DAYS[0] <= _days(e1, e2) <= QUARTER_DAYS[1]:
                    q[e2] = (e1 + timedelta(days=1), v2 - v1)
    return q


def _ttm(q: dict[date, tuple[date, float]], end: date) -> float | None:
    """Somme de 4 trimestres consécutifs se terminant à `end` (contiguïté vérifiée à 7 jours près)."""
    if end not in q:
        return None
    total, cur = 0.0, end
    for i in range(4):
        if cur not in q:
            return None
        start, val = q[cur]
        total += val
        prev_end = start - timedelta(days=1)
        if i < 3:
            match = [e for e in q if abs(_days(e, prev_end)) <= 7]
            if not match:
                return None
            cur = match[0]
    return total


def _year_ago(q: dict[date, Any], end: date) -> date | None:
    target = end - timedelta(days=365)
    cands = [e for e in q if abs(_days(e, target)) <= YEAR_TOLERANCE]
    return min(cands, key=lambda e: abs(_days(e, target))) if cands else None


def _growth(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev <= 0:
        return None  # croissance non définie sur une base nulle ou négative
    return cur / prev - 1


def fundamental_snapshot(facts: list[dict], as_of: date) -> dict[str, Any]:
    known = _known_values(facts, as_of)
    out: dict[str, Any] = {}
    if not known:
        out["no_data"] = True
        return out
    out["last_filed"] = max(v[1] for v in known.values()).isoformat()

    series: dict[str, dict[date, tuple[date, float]]] = {}
    for name, concepts in DURATION_CONCEPTS.items():
        periods: dict[tuple[date, date], float] = {}
        rank: dict[tuple[date, date], int] = {}
        for (concept, start, end), (val, _) in known.items():
            if concept not in concepts or start is None:
                continue
            k, r = (start, end), concepts.index(concept)
            if name == "revenue":
                periods[k] = max(val, periods.get(k, val))
            elif k not in rank or r < rank[k]:  # concept prioritaire = premier de la liste
                periods[k], rank[k] = val, r
        series[name] = _quarters(periods, additive=name not in NON_ADDITIVE)

    rev = series["revenue"]
    # Ancre = dernier trimestre publié par la société, tous flux confondus. Un indicateur dont la série
    # ne couvre pas ce trimestre (concept abandonné, ex. "Revenues" chez JPM depuis 2014) reste None.
    ends = [max(series[n]) for n in ("revenue", "net_income", "op_income", "cfo") if series[n]]
    if not ends:
        out["no_quarters"] = True
    else:
        last_q = max(ends)
        out["last_quarter_end"] = last_q.isoformat()
        ya = next((y for y in (_year_ago(series[n], last_q) for n in ("revenue", "net_income")) if y), None)

        def ttm(name: str, end: date | None) -> float | None:
            return _ttm(series[name], end) if end else None

        out["revenue_ttm"] = ttm("revenue", last_q)
        out["net_income_ttm"] = ttm("net_income", last_q)
        out["op_income_ttm"] = ttm("op_income", last_q)
        cfo, capex = ttm("cfo", last_q), ttm("capex", last_q)
        out["cfo_ttm"] = cfo
        out["fcf_ttm"] = cfo - capex if cfo is not None and capex is not None else None

        def q_yoy(name: str, end: date) -> float | None:
            s = series[name]
            if end not in s:
                return None
            prev = _year_ago(s, end)
            return _growth(s[end][1], s[prev][1]) if prev else None

        out["revenue_yoy_q"] = q_yoy("revenue", last_q)
        prev_q = max((e for e in rev if e < last_q - timedelta(days=50)), default=None) if last_q in rev else None
        out["revenue_yoy_prev_q"] = q_yoy("revenue", prev_q) if prev_q else None
        if out["revenue_yoy_q"] is not None and out["revenue_yoy_prev_q"] is not None:
            out["revenue_accel"] = out["revenue_yoy_q"] - out["revenue_yoy_prev_q"]
        out["net_income_yoy_q"] = q_yoy("net_income", last_q)
        out["eps_yoy_q"] = q_yoy("eps", last_q)

        rv, oi = out["revenue_ttm"], out["op_income_ttm"]
        out["op_margin_ttm"] = oi / rv if rv and oi is not None and rv > 0 else None
        if ya:
            rv0, oi0 = ttm("revenue", ya), ttm("op_income", ya)
            out["op_margin_ttm_prev"] = oi0 / rv0 if rv0 and oi0 is not None and rv0 > 0 else None
            if out["op_margin_ttm"] is not None and out["op_margin_ttm_prev"] is not None:
                out["op_margin_delta"] = out["op_margin_ttm"] - out["op_margin_ttm_prev"]
            ni0 = ttm("net_income", ya)
            out["net_income_ttm_prev"] = ni0
            if ni0 is not None and out["net_income_ttm"] is not None:
                out["emerging_profitability"] = ni0 <= 0 < out["net_income_ttm"]
            out["revenue_ttm_growth"] = _growth(out["revenue_ttm"], rv0)

    ref = date.fromisoformat(out["last_quarter_end"]) if out.get("last_quarter_end") else as_of
    for name, concepts in INSTANT_CONCEPTS.items():
        cands = [(end, val) for (concept, _s, end), (val, _f) in known.items() if concept in concepts]
        if cands:
            end, val = max(cands)
            if _days(end, ref) > INSTANT_MAX_AGE_DAYS:
                out[f"{name}_stale_date"] = end.isoformat()  # ex. V : nombre d'actions dei de 2010
                continue
            out[name] = val
            out[f"{name}_date"] = end.isoformat()
    # Nombre d'actions : moyenne pondérée diluée du dernier trimestre (cohérente avec le BPA, et valable
    # pour les sociétés à plusieurs classes d'actions, où le chiffre dei est déclaré classe par classe).
    dil = [(end, -_days(start, end), val) for (concept, start, end), (val, _f) in known.items()
           if concept == "WeightedAverageNumberOfDilutedSharesOutstanding" and start is not None]
    if dil:
        end, _neg, val = max(dil)
        if _days(end, ref) <= INSTANT_MAX_AGE_DAYS:
            out["shares"] = val
            out["shares_date"] = end.isoformat()
            out["shares_source"] = "diluées pondérées"
    elif "shares" in out:
        out["shares_source"] = "dei (en circulation)"
    debt = out.get("debt_total")
    if debt is None and (out.get("debt_noncurrent") is not None or out.get("debt_current") is not None):
        debt = (out.get("debt_noncurrent") or 0.0) + (out.get("debt_current") or 0.0)
    out["debt"] = debt
    if out.get("net_income_ttm") is not None and out.get("equity") and out["equity"] > 0:
        out["roe_ttm"] = out["net_income_ttm"] / out["equity"]
    return out


def valuation(fund: dict[str, Any], price: float | None) -> dict[str, Any]:
    """Valorisation à partir du cours brut daté et du nombre d'actions déclaré (dei) le plus récent."""
    shares = fund.get("shares")
    if not price or not shares:
        return {}
    mcap = price * shares
    out: dict[str, Any] = {"market_cap": mcap}
    ni, rv, fcf = fund.get("net_income_ttm"), fund.get("revenue_ttm"), fund.get("fcf_ttm")
    out["pe_ttm"] = mcap / ni if ni and ni > 0 else None
    out["ps_ttm"] = mcap / rv if rv and rv > 0 else None
    out["fcf_yield"] = fcf / mcap if fcf is not None else None
    g = fund.get("revenue_yoy_q")
    out["ps_to_growth"] = out["ps_ttm"] / (g * 100) if out["ps_ttm"] and g and g > 0.01 else None
    return out
