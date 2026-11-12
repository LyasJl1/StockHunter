---
name: strategy-validation
description: Tester ou faire évoluer une règle de scoring ou une stratégie StockHunter sans biais (look-ahead, survivant, sur-ajustement). À utiliser avant toute modification de scoring/config.py, de scoring/engine.py ou avant d'annoncer qu'une stratégie « fonctionne ».
---

# Validation d'une stratégie

## Règles
- Une hypothèse s'écrit AVANT de regarder les résultats (docs/research/ : pré-enregistrement daté et commité).
- Changer un poids, un seuil ou une règle = nouvelle `SCORING_VERSION` ; les scans passés ne sont jamais réécrits.
- Période de développement pour chercher ; période réservée évaluée UNE seule fois (`validate --holdout`),
  consignée dans `stockhunter/validation/holdout_ledger.json`. Jamais de réglage après lecture du holdout.
- Statistiques : Newey-West (fenêtres qui se chevauchent), embargo de 90 jours avant le holdout, coûts inclus.
- Univers de 42 actions = biais du survivant : le signaler dans toute conclusion ; l'univers large (Massive, radiés
  inclus) sert de contrôle.

## Étapes
1. Écrire l'hypothèse, le critère de succès (t-stat ≥ 2,7, sens attendu, après coûts) et la période dans docs/research/.
2. `stockhunter validate --out rapport.md --signals-cache var/signals-cache.pkl` (développement seul).
3. Si et seulement si le critère est atteint : figer, puis `validate --holdout` une fois.
4. Promotion RESEARCH → PAPER uniquement si le holdout confirme ; sinon documenter l'échec (docs/reports/).

## Interdits
Présenter un score comme une probabilité, annoncer un gain attendu, retenir la meilleure de N variantes sans
correction, utiliser une donnée dont la date de publication est postérieure à la date simulée.
