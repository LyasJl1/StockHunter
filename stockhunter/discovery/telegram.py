"""Messages Telegram du moteur de découverte : résumé du jour, origine et résultat d'un signal."""

from __future__ import annotations

import html
import json

from sqlalchemy import select
from sqlalchemy.engine import Engine

from stockhunter.db.models import DiscoveryCandidate, DiscoveryOutcome, DiscoveryRun
from stockhunter.db.session import session_scope

STATUS_FR = {"PLANNED": "plan en attente d'entrée", "OPEN": "position simulée ouverte", "CLOSED": "clos", "CANCELLED": "annulé"}


def e(x) -> str:
    return html.escape(str(x), quote=False)


def summary_lines(engine: Engine, top: int = 3) -> list[str]:
    """Bloc du rapport quotidien : marché large, catégories, nouvelles détections. Vide si aucun classement."""
    with session_scope(engine) as s:
        runs = list(s.scalars(select(DiscoveryRun).order_by(DiscoveryRun.as_of.desc()).limit(2)))
        if not runs:
            return []
        run = runs[0]
        ov = json.loads(run.overview_json)
        cur = s.execute(select(DiscoveryCandidate.category, DiscoveryCandidate.ticker, DiscoveryCandidate.rank)
                        .where(DiscoveryCandidate.run_id == run.id).order_by(DiscoveryCandidate.rank)).all()
        prev = set()
        if len(runs) > 1:
            prev = set(s.execute(select(DiscoveryCandidate.category, DiscoveryCandidate.ticker)
                                 .where(DiscoveryCandidate.run_id == runs[1].id)).all())
    m = ov["marche"]
    L = [f"Univers large ({run.universe_size} actions, clôture du {run.as_of:%d/%m/%Y}) : marché <b>{e(m['regime'])}</b> "
         f"— {e(m['motif_regime'])}."]
    for key, c in ov["categories"].items():
        names = [t for cat, t, _r in cur if cat == key]
        if c.get("suspendue"):
            L.append(f"• {e(c['label'])} : suspendue (marché défavorable)")
        elif names:
            new = [t for t in names if prev and (key, t) not in prev]
            L.append(f"• {e(c['label'])} ({len(names)}) : {e(', '.join(names[:top]))}"
                     + (f" · nouveaux : {e(', '.join(new[:top]))}" if new else ""))
        else:
            L.append(f"• {e(c['label'])} : aucune")
    L.append("<i>Sélections exploratoires (stratégies non validées), suivies en simulation. Détail : /scanner, "
             "/signal SYMBOLE.</i>")
    return L


def scanner_message(engine: Engine) -> str:
    lines = summary_lines(engine, top=10)
    return "<b>Scanner — univers large</b>\n" + "\n".join(lines) if lines else "Aucun classement enregistré pour l'instant."


def signal_message(engine: Engine, ticker: str) -> str:
    """Origine (catégorie, date, raisons, plan) et résultat de chaque signal émis sur un titre."""
    t = ticker.upper()[:12]
    with session_scope(engine) as s:
        rows = s.execute(select(DiscoveryCandidate, DiscoveryOutcome)
                         .join(DiscoveryOutcome, DiscoveryOutcome.candidate_id == DiscoveryCandidate.id, isouter=True)
                         .where(DiscoveryCandidate.ticker == t)
                         .order_by(DiscoveryCandidate.as_of.desc(), DiscoveryCandidate.category).limit(8)).all()
        if not rows:
            return f"{e(t)} n'a jamais été relevée par le scanner."
        c0 = rows[0][0]
        card = json.loads(c0.card_json)
        pl = card["plan"]
        L = [f"<b>Signal {e(t)}</b>" + (f" — {e(c0.name)}" if c0.name else ""),
             f"Dernière détection : {c0.as_of:%d/%m/%Y}, catégorie {e(c0.category)}, rang {c0.rank} · clôture {c0.close:.2f} $ "
             f"(fin de journée)", "Pourquoi : " + e(" ; ".join(card["pos"][:3]) or "N/D")]
        if card["neg"]:
            L.append("Prudence : " + e(" ; ".join(card["neg"][:2])))
        L.append(f"Plan simulé : entrée {pl['entry_min']:.2f}–{pl['entry_max']:.2f} $ à l'ouverture suivante, stop "
                 f"{pl['stop_si_entree_au_cours']:.2f} $, objectifs {pl['tp1']:.2f} puis {pl['tp2']:.2f} $, "
                 f"{pl['horizon_seances']} séances max.")
        L.append("\n<b>Historique et résultats</b>")
        for c, o in rows:
            res = "pas encore suivi"
            if o is not None:
                res = STATUS_FR.get(o.status, o.status)
                if o.r_multiple is not None:
                    res += f", {o.r_multiple:+.2f} R"
                if o.excess_20 is not None:
                    res += f", {o.excess_20 * 100:+.1f} % vs univers à 20 séances"
                if o.detail and o.status in ("CLOSED", "CANCELLED"):
                    res += f" ({e(o.detail)})"
            L.append(f"• {c.as_of:%d/%m} {e(c.category)} #{c.rank} : {res}")
        L.append("\n<i>Stratégie non validée : information, pas une recommandation. Résultats simulés (frais inclus).</i>")
        return "\n".join(L)
