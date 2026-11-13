# Programme de recherche v3 — résultats (exécution unique du 12/11/2026)

> Protocole : `docs/research/2026-11-12-preenregistrement-v3.md` (commité avant l'exécution).
> Rendements simulés sur données historiques ; aucune garantie pour l'avenir.

- Données : 501 séances (2024-11-12 → 2026-11-11), 6485 actions ordinaires (radiées comprises), empreinte `5ce349e7dea5dd9f` ; code `b21f7e9d6d25`.
- Univers dynamique : 2722 titres en moyenne par date ; 3628 titres y sont passés, dont 344 radiés depuis. Dernière date : 2738 ({'faible': 912, 'moyenne': 913, 'forte': 913}).
- Dates de signal : 65 en développement ; 32 réservées.

## Facteurs (développement, horizon 20 séances)

| # | Hypothèse | Sens | Dates | Stat. | t (NW) | IC bootstrap 95 % | Moitié 1 / 2 | Décile net 25 pb vs univers | vs SPY | Décision |
|---|---|---|---:|---:|---:|---|---|---:|---:|---|
| V1 | Momentum 12-1 | + | 14 | +0,032 | +0,96 | [+0,005 ; +0,093] | +0,011 / +0,052 | +0,76 % | +1,85 % | **NON TESTABLE** |
| V2 | Tendance ajustée du risque (126 j) | + | 39 | +0,039 | +1,49 | [-0,008 ; +0,099] | +0,035 / +0,042 | +0,73 % | +1,02 % | **NON RETENU** |
| V3 | Momentum résiduel 6-1 | + | 37 | +0,047 | +1,65 | [-0,002 ; +0,109] | +0,057 / +0,039 | +1,12 % | +1,43 % | **NON RETENU** |
| V4 | Sortie de consolidation | + | 39 | +0,011 | +1,44 | [-0,003 ; +0,027] | +0,020 / +0,002 | +0,60 % | +0,88 % | **NON RETENU** |
| V9 | Effet MAX (plus forte hausse 21 j) | − | 39 | -0,021 | -0,78 | [-0,071 ; +0,034] | -0,084 / +0,039 | -1,48 % | -1,20 % | **NON RETENU** |
| MF | Composite tendance + faible risque | + | 37 | +0,022 | +0,64 | [-0,031 ; +0,091] | -0,026 / +0,067 | -1,04 % | -0,72 % | **NON RETENU** |

Stat. = IC de rang moyen (facteurs continus) ou écart moyen vs univers (événement V4). Seuil : t ≥ 3,0 (16 tests dans la famille).

### Diagnostics

- **V1** — frais 10/25/50 pb : +1,06 % / +0,76 % / +0,26 % ; semestres : 2025-S2 -0,007, 2026-S1 +0,061 ; IC par liquidité (faible/moyenne/forte) : +0,049 / +0,036 / +0,013 ; volume médian du groupe : 45.2 M$/j.
- **V2** — frais 10/25/50 pb : +1,03 % / +0,73 % / +0,23 % ; semestres : 2025-S1 -0,079, 2025-S2 +0,050, 2026-S1 +0,091 ; IC par liquidité (faible/moyenne/forte) : +0,030 / +0,045 / +0,043 ; volume médian du groupe : 59.8 M$/j.
- **V3** — frais 10/25/50 pb : +1,42 % / +1,12 % / +0,62 % ; semestres : 2025-S1 -0,089, 2025-S2 +0,054, 2026-S1 +0,094 ; IC par liquidité (faible/moyenne/forte) : +0,046 / +0,050 / +0,049 ; volume médian du groupe : 60.9 M$/j.
- **V4** — frais 10/25/50 pb : +0,90 % / +0,60 % / +0,10 % ; semestres : 2025-S1 +0,008, 2025-S2 +0,010, 2026-S1 +0,017 ; volume médian du groupe : 63.1 M$/j.
- **V9** — frais 10/25/50 pb : -1,18 % / -1,48 % / -1,98 % ; semestres : 2025-S1 -0,134, 2025-S2 -0,029, 2026-S1 +0,089 ; IC par liquidité (faible/moyenne/forte) : -0,000 / -0,020 / -0,044 ; volume médian du groupe : 67.1 M$/j.
- **MF** — frais 10/25/50 pb : -0,74 % / -1,04 % / -1,54 % ; semestres : 2025-S1 -0,185, 2025-S2 +0,011, 2026-S1 +0,158 ; IC par liquidité (faible/moyenne/forte) : +0,028 / +0,027 / +0,012 ; volume médian du groupe : 86.7 M$/j.

## Tests multiples (toute la famille, registre)

| Test | p unilatérale | Holm | Benjamini-Hochberg |
|---|---:|---:|---:|
| v3-V3 | 0.054 | 0.869 | 0.317 |
| v3-V2 | 0.072 | 1.000 | 0.317 |
| broad-H2 | 0.076 | 1.000 | 0.317 |
| v3-V4 | 0.079 | 1.000 | 0.317 |
| broad-H3 | 0.111 | 1.000 | 0.357 |
| broad-H1 | 0.155 | 1.000 | 0.357 |
| broad-H4 | 0.174 | 1.000 | 0.357 |
| v3-V1 | 0.178 | 1.000 | 0.357 |
| v3-MF | 0.262 | 1.000 | 0.466 |
| broad-H6 | 0.300 | 1.000 | 0.479 |
| broad-C | 0.567 | 1.000 | 0.825 |
| broad-H7 | 0.658 | 1.000 | 0.877 |
| v3-ML-gbm | 0.763 | 1.000 | 0.893 |
| v3-V9 | 0.781 | 1.000 | 0.893 |
| v3-ML-ridge | 0.921 | 1.000 | 0.949 |
| broad-H5 | 0.949 | 1.000 | 0.949 |

## Machine learning (walk-forward purgé, hors entraînement)

22 dates testées (2025-09-24 → 2026-02-25).

| Modèle | IC | t | % IC > 0 | Décile net vs univers | IC − référence | t différence | Retenu |
|---|---:|---:|---:|---:|---:|---:|---|
| reference | +0,059 | +1,65 | 68% | -0,12 % | N/D | N/D | — |
| ridge | -0,048 | -1,46 | 45% | -1,34 % | -0,107 | -1,69 | non |
| gbm | -0,021 | -0,73 | 41% | -0,51 % | -0,080 | -1,30 | non |

Coefficients Ridge (moyenne ± écart-type entre ré-entraînements ; stabilité des variables) : H1 -0.105±0.024, H2 -0.001±0.012, H3 -0.020±0.046, H4 +0.102±0.023, H5 -0.034±0.005, H6 -0.013±0.009, H7 +0.001±0.007, V2 +0.033±0.029, V3 +0.100±0.035, V9 -0.019±0.006

## Simulation de portefeuille (descriptive, règles de trading partagées, composite MF)

Période 2025-06-06 → 2026-03-31 ; 32 trades clos, 624 plans annulés, exposition moyenne 89%, gagnants 44%, R moyen -0,47.

| | Rendement | Sharpe | Drawdown max |
|---|---:|---:|---:|
| Portefeuille simulé | -3,87 % | -0,83 | -5,63 % |
| SPY (achat-conservation) | +8,55 % | +0,91 | -9,13 % |
| Univers équipondéré | +13,06 % | +0,99 | -9,57 % |

Ce tableau ne valide rien : il décrit le comportement des règles sur un signal non validé.

## Conclusion (sans embellissement)
- **Aucun facteur n'atteint le seuil pré-enregistré** (t ≥ 3,0). Le meilleur, le momentum résiduel V3, est à t = 1,65
  (p ajustée de Holm 0,87). Aucun ne serait significatif même sans correction des tests multiples.
- **Machine learning : non retenu.** Ridge et gradient boosting font moins bien que la référence simple hors
  entraînement (IC négatifs). Avec 22 dates de test et un seul régime de marché, ils apprennent surtout du bruit.
  Aucun modèle n'est intégré à la plateforme.
- **Simulation de portefeuille** (descriptive) : −3,9 % contre +8,6 % pour le SPY sur la même période. Les règles
  de trading appliquées à un signal sans avantage démontré ne créent pas de performance.
- **Période réservée (≥ 01/04/2026) : toujours intacte** — aucun candidat ne justifiait de la consulter.
- **Ce que cela signifie** : avec deux ans de données journalières et un marché haussier, la puissance statistique
  est insuffisante pour démontrer un avantage modeste. Les signes de V2, V3 et V4 sont conformes à la littérature
  (IC ≈ +0,04, décile net ≈ +0,7 à +1,1 % par mois) mais instables : négatifs au premier semestre 2025, positifs
  ensuite. Ils ne sont **pas** exploitables en l'état. Ils sont inscrits au test prospectif, seule preuve
  recevable désormais.
- Coût de calcul : 83 secondes et 1,6 Go de mémoire pour 6 485 titres × 501 séances (runner GitHub gratuit).

## Suite (expériences prospectives, sans ré-optimisation)
1. Paniers hebdomadaires figés pour V2, V3, V4, V9 et MF, ajoutés au test prospectif existant (H1–H7, C).
2. Réévaluation à 26 et 52 semaines de données prospectives, avec le même seuil.
3. Historique long (≥ 10 ans) pour quelques centaines de titres via le plan gratuit Tiingo (limites à vérifier) : à décider par Ilias (voir le rapport
   v3, « prochaines expériences »). C'est le seul moyen gratuit d'obtenir plusieurs régimes de marché.
