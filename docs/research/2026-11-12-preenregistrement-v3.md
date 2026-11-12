# Pré-enregistrement — programme de recherche StockHunter 3.0 (12/11/2026)

Écrit et commité **avant** toute exécution sur les données. Le code du laboratoire (`stockhunter/lab/`) applique
ce texte ; toute modification après lecture des résultats = nouvelle étude, nouvelle entrée du registre.

## 1. Ce que l'on sait déjà (et qui reste acquis)
- v2.0.0 (42 grandes capitalisations) : aucune stratégie validée ; période réservée **consommée**.
- Étude univers large du 12/11/2026 (H1–H7, composite C) : aucun facteur retenu sur la période de développement
  (≤ 28/02/2026). Sa période réservée (≥ 01/04/2026) n'a **pas** été consultée.
- Ces 8 tests comptent dans la famille des tests multiples : la période de développement n'est plus vierge pour
  eux. Ils ne sont **pas** re-testés ni « retournés ».

## 2. Données admissibles
- Barres journalières Massive de tout le marché US, 12/11/2024 → 11/11/2026 (≈ 501 séances), radiés compris.
- Développement : dates de signal ≤ 28/02/2026 (embargo d'un mois avant la période réservée, inchangé).
- **Limite assumée** : deux ans d'historique, un seul régime de marché (haussier). Un facteur exigeant 252 séances
  n'a qu'une douzaine de dates de signal. La puissance statistique est faible ; un résultat négatif signifie
  « non démontré », pas « inexistant ». Aucune preuve suffisante ne sera revendiquée sur cette seule base.
- Pas de fondamentaux, de secteurs ni de capitalisations fiables pour l'univers large (non fournis par le plan
  gratuit) : value, quality, croissance et rotation sectorielle ne sont **pas testables** à cette échelle
  aujourd'hui. La liquidité (volume en dollars) sert de proxy de taille.

## 3. Univers (recalculé à chaque date avec les seules données antérieures)
Actions ordinaires NYSE / Nasdaq / NYSE American (radiées comprises), hors OTC ; cours brut ≥ 5 $ ; médiane 20 j du
volume en dollars ≥ 2 M$ ; ≥ 130 séances d'historique ; cotée au moins 15 des 20 dernières séances (risque de
suspension) ; aucune variation journalière > 80 % non expliquée par un split sur 20 séances (qualité des données).
Tranches de liquidité : tiers du volume médian en dollars à la date (proxy grandes / moyennes / petites valeurs).

## 4. Hypothèses nouvelles (famille v3)
Rendement mesuré : entrée à l'ouverture de J+1, sortie à la clôture de J+20 (principal) et J+5 ; signal tous les
5 jours de bourse ; frais 25 pb par côté ; titre radié = dernière clôture connue.

| # | Hypothèse | Fondement économique | Sens | Fréquence | Risque de sur-ajustement |
|---|---|---|---|---|---|
| V1 | Momentum 12-1 : rendement de J−252 à J−21 | Sous-réaction à l'information (Jegadeesh & Titman 1993) | + | permanent | faible (définition standard) ; **≈ 12 dates seulement** |
| V2 | Tendance ajustée du risque : rendement 126 j ÷ (volatilité 60 j × √126) | Momentum moins exposé aux titres « loterie » (Barroso & Santa-Clara 2015) | + | permanent | faible |
| V3 | Momentum résiduel 6-1 : somme ÷ écart-type des résidus de la régression sur le marché équipondéré, J−126 à J−21 | Momentum propre au titre, moins sensible aux retournements de marché (Blitz, Huij & Martens 2011) | + | permanent | faible |
| V4 | Sortie de consolidation : clôture au-dessus du plus haut des 60 séances précédentes, après contraction (amplitude 20 j < 80 % de l'amplitude 120 j), volume du jour ≥ 1,5 × moyenne 60 j | Reprise de tendance après accumulation ; ancrage sur les plus hauts (George & Hwang 2004) | + (événement) | rare | moyen (3 seuils, fixés ici) |
| V9 | Effet « MAX » : plus forte hausse journalière des 21 dernières séances | Préférence pour les titres loterie, surévalués (Bali, Cakici & Whitelaw 2011) | − | permanent | faible |
| MF | Composite : moyenne des rangs de V2, V3, −V9, −H4 (volatilité 60 j) | Combinaison de signaux complémentaires tendance + faible risque | + | permanent | moyen (composition fixée ici) |

Facteurs écartés d'emblée : momentum 6-1, retournement, plus haut 52 s, volatilité, volume anormal, illiquidité,
choc d'information (déjà testés) ; conditionnement par régime (un seul régime observé : non testable).

## 5. Machine learning (phase E)
- Cible : rang du rendement à 20 j dans l'univers. Variables : rangs de H1–H7, V2, V3, V9 (10 variables).
- Modèles : régression Ridge (α = 10) ; gradient boosting (HistGradientBoosting, profondeur 3, 100 arbres,
  taux 0,05). Hyperparamètres fixés ici, non optimisés.
- Référence simple obligatoire : composite équipondéré des rangs signés.
- Validation : walk-forward **purgé** sur le développement — entraînement sur les dates dont la fenêtre de
  rendement est entièrement terminée avant la date testée (purge de 20 séances + 5), au moins 12 dates
  d'entraînement, ré-entraînement à chaque date.
- Un modèle n'est retenu que si son IC hors entraînement atteint t ≥ 3,0 **et** dépasse la référence simple avec
  t ≥ 2,0 sur la différence appariée. Sinon : non intégré.

## 6. Règle de décision (fixée avant les données)
Famille : 8 tests antérieurs + 6 hypothèses + 2 modèles = 16. Seuil corrigé (Bonferroni, unilatéral 5 %, petits
échantillons) : **t de Newey-West ≥ 3,0** sur l'IC à 20 j dans le sens attendu (événement V4 : sur l'écart moyen
vs univers). P-valeurs ajustées (Holm, Benjamini-Hochberg) publiées pour toute la famille.

Un facteur est **CANDIDAT** seulement si, sur le développement : (1) t ≥ 3,0 ; (2) décile favorable net de frais
> univers ; (3) IC moyen positif dans chacune des deux moitiés de la période ; (4) borne basse de l'intervalle
bootstrap par blocs à 95 % > 0. Moins de 20 dates : **NON TESTABLE**.

Un CANDIDAT n'est pas « validé ». Il passe (a) une consultation **unique** de la période réservée (≥ 01/04/2026),
consignée au registre, exigeant t ≥ 1,0 et un écart net positif ; puis (b) le test prospectif (paniers figés
chaque semaine à partir du 13/11/2026). Statuts : RESEARCH → CANDIDAT → CONFIRMÉ HORS ÉCHANTILLON → PAPER →
VALIDÉ (≥ 6 mois de prospectif positif). Sans candidat, la période réservée reste intacte.

Diagnostics publiés pour chaque facteur (descriptifs, sans effet sur la décision) : sensibilité aux frais
(10 / 25 / 50 pb), stabilité par semestre, par tranche de liquidité, capacité (volume médian du décile),
comparaison au SPY et à l'univers équipondéré.

## 7. Simulation de portefeuille (descriptive)
Les règles de trading unifiées (`stockhunter/trading/rules.py`, identiques au paper trading) sont rejouées sur
les 20 meilleurs titres du composite MF à chaque date de signal. Résultat publié à titre descriptif : il ne
valide rien et ne sert pas à régler les règles.
