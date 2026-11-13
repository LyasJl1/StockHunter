"""Classement déterministe des catalyseurs : rubriques des 8-K (SEC) et titres d'articles.

- 8-K : les rubriques (« items ») sont définies par la SEC ; le libellé et l'importance sont une table fixe.
  L'importance (1 faible, 2 moyenne, 3 forte) reflète la nature de l'événement, PAS son effet sur le cours.
- Articles : classement par mots-clés sur le titre. C'est indicatif ; aucune analyse de sentiment,
  aucun avis « positif / négatif » n'est déduit d'un titre.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# rubrique -> (libellé, importance, point de vigilance)
SEC_8K_ITEMS: dict[str, tuple[str, int, bool]] = {
    "1.01": ("Accord important conclu", 3, False),
    "1.02": ("Fin d'un accord important", 2, True),
    "1.03": ("Faillite ou mise sous administration", 3, True),
    "1.04": ("Sécurité des mines", 1, False),
    "1.05": ("Incident de cybersécurité important", 3, True),
    "2.01": ("Acquisition ou cession réalisée", 3, False),
    "2.02": ("Publication de résultats", 3, False),
    "2.03": ("Nouvelle obligation financière (dette)", 2, False),
    "2.04": ("Événement accélérant une dette", 3, True),
    "2.05": ("Restructuration (coûts de sortie)", 3, True),
    "2.06": ("Dépréciation d'actifs", 3, True),
    "3.01": ("Avis de radiation ou non-conformité de cotation", 3, True),
    "3.02": ("Émission d'actions non enregistrée", 2, True),
    "3.03": ("Modification des droits des actionnaires", 2, False),
    "4.01": ("Changement d'auditeur", 2, True),
    "4.02": ("Comptes antérieurs non fiables (retraitement)", 3, True),
    "5.01": ("Changement de contrôle", 3, False),
    "5.02": ("Départ ou nomination de dirigeants / administrateurs", 2, False),
    "5.03": ("Modification des statuts ou de l'exercice", 1, False),
    "5.05": ("Code de déontologie", 1, False),
    "5.07": ("Vote des actionnaires (assemblée)", 1, False),
    "5.08": ("Désignation d'administrateurs par les actionnaires", 1, False),
    "7.01": ("Communication Reg FD (présentation, communiqué)", 1, False),
    "8.01": ("Autre événement déclaré", 2, False),
    "9.01": ("Pièces jointes (états financiers, documents)", 1, False),
}


@dataclass(frozen=True)
class FilingCatalyst:
    item: str
    label: str
    importance: int
    watch: bool          # point de vigilance (événement typiquement défavorable ou à vérifier)


def classify_8k(items: str | None) -> list[FilingCatalyst]:
    """Rubriques d'un 8-K (« 2.02,9.01 ») -> catalyseurs, du plus important au moins important."""
    out = []
    for raw in (items or "").replace(";", ",").split(","):
        code = raw.strip()
        if not code:
            continue
        label, imp, watch = SEC_8K_ITEMS.get(code, (f"Rubrique {code}", 1, False))
        out.append(FilingCatalyst(code, label, imp, watch))
    return sorted(out, key=lambda c: (-c.importance, c.item))


# catégorie -> (libellé, importance, expressions (anglais : titres de la presse US))
NEWS_RULES: list[tuple[str, str, int, tuple[str, ...]]] = [
    ("legal", "Justice / enquête", 3, (r"lawsuit", r"\bsue[sd]?\b", r"class action", r"settle(ment|s)?",
                                       r"investigation", r"probe", r"subpoena", r"indict", r"fraud", r"antitrust")),
    ("regulatory", "Réglementaire", 3, (r"\bFDA\b", r"\bapproval\b", r"regulator", r"clearance", r"\bEMA\b", r"recall",
                                        r"\bFTC\b", r"\bDOJ\b", r"\bSEC (charges|fines|probe)", r"tariff", r"sanction")),
    ("mna", "Fusion / acquisition", 3, (r"\bacquir(e|es|ed|ing|ition)", r"\bmerger?s?\b", r"takeover",
                                        r"\bbuyout\b", r"to buy\b", r"\bdeal to\b", r"spin-?off", r"divest")),
    ("earnings", "Résultats", 2, (r"earnings", r"\bresults\b", r"\bQ[1-4]\b", r"revenue",
                                  r"\bprofit\b", r"\bEPS\b", r"beats?\b", r"miss(es|ed)?\b")),
    ("guidance", "Prévisions", 3, (r"guidance", r"outlook", r"forecast", r"raises? (its )?(full-year|annual)",
                                   r"cuts? (its )?(full-year|annual|outlook|forecast)")),
    ("analyst", "Avis d'analyste", 1, (r"upgrade", r"downgrade", r"price target", r"initiat(es|ed) coverage",
                                       r"\boverweight\b", r"\bunderweight\b", r"\boutperform\b")),
    ("management", "Direction", 2, (r"\bCEO\b", r"\bCFO\b", r"chief executive", r"resign", r"steps? down",
                                    r"appoint", r"names? .* (chief|president)")),
    ("capital", "Capital / dividende", 2, (r"buyback", r"repurchase", r"dividend", r"stock split",
                                           r"(share|stock|debt|bond) offering", r"\bnotes? offering")),
    ("business", "Contrat / produit", 2, (r"contract", r"partnership", r"agreement", r"launch", r"unveil",
                                          r"order[s]? (for|from)", r"award")),
]
_COMPILED = [(cat, lab, imp, re.compile("|".join(pats), re.IGNORECASE)) for cat, lab, imp, pats in NEWS_RULES]
NEWS_LABELS = {cat: (lab, imp) for cat, lab, imp, _ in NEWS_RULES} | {"other": ("Autre", 1)}


def classify_headline(headline: str | None) -> str:
    """Première règle qui correspond (l'ordre de NEWS_RULES fixe la priorité) ; sinon « other »."""
    text = headline or ""
    for cat, _lab, _imp, rx in _COMPILED:
        if rx.search(text):
            return cat
    return "other"
