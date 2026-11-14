"""Messages Telegram 3.2 : classement mondial, par région, par pays ; résumé quotidien mondial ; portefeuille.

Courts et sans répétition : au plus 10 lignes par message, chaque ligne avec sa place, sa devise et la date de
SA séance. Le score est une position relative, jamais une probabilité."""

from __future__ import annotations

from html import escape as e

from sqlalchemy.engine import Engine

from stockhunter.world import markets as mk
from stockhunter.world import run as wr

MAX_LINES = 10


def _fx(engine: Engine):
    from stockhunter.config import get_settings
    from stockhunter.world.store import WorldStore
    fx = wr.fx_for_display(engine, WorldStore(get_settings().world_dir))
    return None if fx.wide.empty else fx


def ranking_message(engine: Engine, region: str | None = None, country: str | None = None, title: str = "") -> str:
    from stockhunter.world.ranking import global_ranking
    res = global_ranking(engine, _fx(engine), top=MAX_LINES, region=region, country=country)
    lines = []
    for r in res["lignes"]:
        eur = f" ≈ {r.close_eur:.2f} €" if r.close_eur and r.currency != "EUR" else ""
        lines.append(f"{r.rank}. <b>{e(r.ticker)}</b> {e((r.name or '')[:24])} · {e(r.market)} · "
                     f"{r.close:.2f} {e(r.currency)}{eur} · {e(', '.join(r.categories))} · score {r.norm:.2f} · "
                     f"risque {e(r.risk)} (séance {r.as_of})")
    body = "\n".join(lines) or "Aucune sélection : places sans opportunité ou historique en constitution."
    seances = ", ".join(f"{m} {d}" for m, d in res["seances"]) or "—"
    return (f"<b>{e(title or 'Classement mondial')}</b> — {res['apres_filtres']} sélection(s)\n{body}\n\n"
            f"<i>Séances : {e(seances)}. Clôtures, pas de cours en direct. Score = position relative, pas une "
            f"probabilité. Aucune stratégie validée.</i>")


def country_message(engine: Engine, code: str | None) -> str:
    if not code:
        return "Usage : /country FR (code pays ISO à 2 lettres)"
    code = code.upper()[:2]
    places = [m for m in mk.MARKETS.values() if m.country == code]
    if not places:
        return f"Pays {e(code)} inconnu du registre des places."
    if not any(m.data == mk.PRICES for m in places):
        return (f"<b>{e(places[0].country_name)}</b> : pas de cours gratuits fiables ({e(places[0].data)}). "
                f"{e(places[0].note)}")
    return ranking_message(engine, country=code, title=f"Classement — {places[0].country_name}")


def portfolio_message(engine: Engine) -> str:
    p = wr.kv_get(engine, f"{wr.KV_PORTFOLIO}_EUR")
    if not p or not p.get("resume") or "valeur_finale" not in p["resume"]:
        return "<b>Portefeuille mondial simulé</b> : pas encore calculé."
    s = p["resume"]
    pct = lambda x: "N/D" if x is None else f"{x * 100:+.1f} %"
    refs = " · ".join(f"{e(k.split(' (')[0])} {pct(v)}" for k, v in (p.get("references") or {}).items() if v is not None)
    return (f"<b>Portefeuille mondial simulé (EUR)</b>\nValeur {s['valeur_finale']:,.0f} € ({pct(s['rendement_total'])}, "
            f"drawdown max {pct(s['drawdown_max'])}) · {s['trades_clos']} trades clos, {s['positions_ouvertes']} ouverts"
            f" · frais {s['frais']['courtage'] + s['frais']['change']:,.0f} €"
            + (f" · profit factor {s['profit_factor']:.2f}" if s.get("profit_factor") else "")
            + f"\nRéférences, même période en EUR : "
            f"{refs or 'N/D'}\n<i>{e(s['lecture'])} · simulation, jamais mélangée au portefeuille réel.</i>"
            ).replace(",", " ")


def daily_summary(engine: Engine) -> str:
    """Résumé mondial du soir : état de chaque place couverte + 5 premières sélections mondiales."""
    st = (wr.kv_get(engine, wr.KV_STATE) or {}).get("scanners", {}).get("places", {})
    lines = []
    for code, r in st.items():
        lines.append(f"• {e(code)} : {e(r.get('statut', '—'))}"
                     + (f", {r['selections']} sélection(s) sur {r['univers']} titres" if r.get("univers") else
                        f" ({r.get('seances') or 0} séances stockées)"))
    head = "<b>Marchés mondiaux</b>\n" + ("\n".join(lines) or "Aucun scanner mondial exécuté.")
    return head + "\n\n" + ranking_message(engine, title="Top mondial")


def opportunity_message(code: str, res: dict, max_items: int = 3) -> str | None:
    """Nouvelles sélections d'une place (une fois par séance) : place, devise, justification, clôture DATÉE, plan
    simulé (zone d'entrée, stop, objectifs). Jamais présenté comme un cours en direct."""
    m = mk.get(code)
    cands = sorted((c for k, v in (res.get("categories") or {}).items() for c in v.get("candidats", [])),
                   key=lambda c: (-(c.get("norm_score") or 0), c.get("ticker", "")))
    seen, picks = set(), []
    for c in cands:
        if c.get("isin") in seen:
            continue
        seen.add(c.get("isin"))
        picks.append(c)
    if not picks:
        return None
    lines = [f"<b>Nouvelles sélections — {e(m.name)}</b> (séance du {e(str(res.get('as_of')))}, devise {e(m.currency)})"]
    for c in picks[:max_items]:
        pl = c["plan"]
        why = " ; ".join(str(x) for x in (c.get("pos") or [])[:2])
        lines.append(f"• <b>{e(c['ticker'])}</b> {e((c.get('name') or '')[:28])} — {e(c['category'])}\n"
                     f"  clôture du {e(str(res.get('as_of')))} : {c['close']:.2f} {e(m.currency)} (fin de séance, pas un cours en direct)\n"
                     f"  entrée simulée {pl['entry_min']:.2f}–{pl['entry_max']:.2f}, stop ≈ {pl['stop_si_entree_au_cours']:.2f}, "
                     f"TP1 {pl['tp1']:.2f}, TP2 {pl['tp2']:.2f}\n  {e(why)}")
    if len(picks) > max_items:
        lines.append(f"… et {len(picks) - max_items} autre(s) : /global")
    lines.append("<i>Simulation et recherche : aucune stratégie validée, aucun ordre réel. Score ≠ probabilité.</i>")
    return "\n".join(lines)


def trade_message(t: dict, base: str) -> str:
    res = "N/D" if t.get("rendement") is None else f"{t['rendement'] * 100:+.1f} %"
    r = "N/D" if t.get("r") is None else f"{t['r']:+.2f} R"
    pnl = f"{t['pnl_base']:+,.0f}".replace(",", " ")
    return (f"<b>Portefeuille mondial simulé — position clôturée</b>\n{e(t['ticker'])} ({e(t['place'])}, {e(t['devise'])}) "
            f"· {e(t['categorie'])}\nentrée {e(t['entree'])}, sortie {e(t['sortie'])} : {e(t.get('motif') or '')}\n"
            f"résultat {res} ({pnl} {e(base)}, {r}), frais inclus\n<i>Simulation : jamais un résultat réel.</i>")


def opened_message(x: dict) -> str:
    return (f"<b>Portefeuille mondial simulé — entrée simulée</b>\n{e(x['ticker'])} ({e(x['place'])}) à l'ouverture du "
            f"{e(x['seance'])} : {x['quantite']:g} titres, capital connu {x['capital_connu']:,.0f}, taux BCE du "
            f"{e(x['taux_bce_du'])}\n<i>Simulation.</i>").replace(",", " ")
