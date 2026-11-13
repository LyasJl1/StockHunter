# StockHunter 2.0 → 3.0 : rapport de comparaison (13/11/2026)

Aucun chiffre n'est estimé : « non mesuré » signifie qu'aucune mesure n'existe.

## Tableau de comparaison
| Indicateur | 2.0 | 3.0 |
|---|---|---|
| Actions analysables | 42 grandes capitalisations (+ 16 ETF) | **≈ 2 700 négociables par jour**, tirées de 6 485 actions ordinaires US (3 628 sont passées par l'univers, dont 344 radiées depuis) ; les 42 gardent leur score détaillé |
| Couverture historique | ≈ 10 ans (Tiingo) sur 42 titres ; biais du survivant | 501 séances (2 ans, Massive) sur tout le marché, radiés compris ; **limite : un seul régime de marché** |
| Durée du scan | non mesurée (42 titres) | **45 s** pour 2 738 titres, 1,6 Go de mémoire (mesuré sur runner GitHub) ; programme de recherche complet : 83 s |
| Stratégies testées | 13 (score, A-D, H1-H7, C) | 13 + 6 hypothèses nouvelles + 2 modèles ML = **21, toutes au registre** |
| Stratégies validées | 0 | **0** |
| Qualité prédictive hors échantillon | aucune (IC ≈ 0 ; C échoue sur la période réservée) | aucune démontrée : meilleur facteur t = 1,65 (seuil 3,0) ; ML hors entraînement : IC −0,05 (Ridge) et −0,02 (GBM), sous la référence simple (+0,06, t = 1,65) |
| Rendement simulé | voir rapport v2.0.0 (non comparable : règles différentes du paper trading) | composite MF, règles partagées, 06/2025 → 03/2026 : **−3,9 %** (descriptif, signal non validé) |
| Surperformance vs référence | aucune | **−12,4 points** vs SPY (+8,5 %), −16,9 points vs univers équipondéré (+13,1 %) |
| Drawdown maximal | voir rapport v2.0.0 | −5,6 % (SPY −9,1 %) |
| Sharpe | voir rapport v2.0.0 | −0,83 (SPY 0,91) |
| Coût | 0 € de données ; Railway 1 $/mois de crédit | inchangé ; aucun service payant ajouté |
| Fiabilité des jobs | 1 nuit observée sur la base Railway (13/11) : **échec masqué** (job vert, scan non fait) | chaîne testée (5 tests) ; **non mesurée en production** tant que la PR n'est pas fusionnée |
| Tests automatisés | 106 | **141** (SQLite et PostgreSQL) |

## Ce que ces résultats veulent dire
- **Aucun avantage statistique robuste n'a été découvert.** Ni les facteurs classiques (momentum 12-1, tendance
  ajustée du risque, momentum résiduel, sortie de consolidation, effet MAX), ni leur combinaison, ni le machine
  learning ne passent le seuil fixé à l'avance.
- La cause principale est la **puissance statistique** : 39 dates de signal sur un seul régime de marché. Les
  signes de V2, V3 et V4 sont ceux de la littérature (IC ≈ +0,04) mais changent de signe d'un semestre à l'autre.
- 3.0 n'est donc pas « un système qui gagne » : c'est un système qui **cherche, mesure, explique et suit**
  correctement, et qui dit quand il ne sait pas. Le scanner affiche des configurations exploratoires, chacune avec
  le verdict de sa stratégie, et chaque signal est suivi jusqu'à son résultat.

## Réalisé
| Phase | Livré |
|---|---|
| A audit | 11 défauts confirmés et corrigés (dont l'incident de production du 13/11), voir `audit-initial.md` |
| B univers large | univers dynamique point-in-time, filtres paramétrables, tranches de liquidité, présélection rapide + analyse approfondie des meilleurs |
| C recherche | 6 hypothèses documentées (fondement, données, limites, fréquence, sur-ajustement, coûts, méthode), pré-enregistrées avant exécution |
| D laboratoire | IC, Newey-West, bootstrap par blocs, Holm/BH, coûts 10/25/50 pb, capacité, semestres, SPY et univers équipondéré, registre versionné |
| E machine learning | Ridge et gradient boosting en walk-forward purgé, comparés à une référence simple → **non retenus** |
| F sélection | classement quotidien en 5 catégories, fiche complète (raisons, prudence, mesures, niveau de validation, plan simulé), aucune sélection forcée, filtre de marché |
| G paper trading | règles uniques backtest/paper, splits, glissement, corrélation, journal de décision, suivi unitaire de chaque signal |
| H dashboard | Marché, Scanner, Fiche approfondie, Laboratoire, Portefeuilles, Santé du système |
| I Telegram | lecteur unique (fin des 409), alertes dédupliquées et plafonnées, résumé de découverte, `/scanner`, `/signal` |
| J automatisation | chaîne à 8 étapes séparées, bilan en base, échecs non masqués, upsert incrémental, entretien de la base |
| K tests | 141 tests, revue indépendante du moteur quantitatif (6 défauts corrigés, aucun look-ahead), migrations vérifiées sur PostgreSQL, essai à blanc sur données réelles |

## Non réalisé (et pourquoi)
- **Fondamentaux, secteurs et capitalisations pour tout l'univers large** : non fournis par les plans gratuits.
  Value, quality, croissance et rotation sectorielle restent limités aux 42 titres suivis ; les meilleurs candidats
  du scanner reçoivent un résumé SEC à la demande.
- **Catégories Croissance, Value/Quality, Opportunités sectorielles, Retournement dans le scanner large** : pas de
  données (trois premières) ; « uniquement si validé » (retournement : H2 non retenu).
- **Conditionnement par régime de marché** : un seul régime observé, non testable.
- **Paper trading de portefeuille sur les signaux de l'univers large** : ces signaux sont suivis un par un
  (résultat en R et vs univers) mais n'entrent pas dans le portefeuille simulé, réservé aux 42 titres suivis.
- **Révisions d'analystes, PEAD sur l'univers large** : pas de source gratuite fiable (H7 en est le proxy, non retenu).
- **Mesure de fiabilité en production de la chaîne v3** : nécessite la fusion.
- **Suppression des anciens artefacts publics** : action irréversible laissée à Ilias.

## Risques
1. Disque Railway (0,5 Go) : ≈ 0,27 Go après correctif ; la page Santé affiche l'occupation.
2. Crédit Railway : site arrêté en cours de mois (choix d'Ilias) ; collecte et Telegram non touchés.
3. Retards de planification GitHub (jusqu'à 4 h constatés).
4. Classement exploratoire lu comme un conseil : chaque page et message rappelle le niveau de validation.
5. Dépôt public (artefacts anciens, hachage du mot de passe).

## Prochaines expériences prospectives (sans ré-optimisation)
1. **Paniers figés** chaque semaine pour H1-H7, C, V2, V3, V4, V9, MF — en place depuis le 13/11/2026 ; lecture à
   26 puis 52 semaines avec le seuil inchangé.
2. **Suivi unitaire des signaux du scanner** par catégorie (R moyen, excès à 20 séances) — en place.
3. **Historique long** : ≈ 440 titres supplémentaires sur 10 ans via le plan gratuit Tiingo (limite mensuelle de symboles distincts à vérifier sur leur page de tarifs avant de lancer),
   choisis parmi les plus liquides de fin 2024, radiés compris. Donnerait plusieurs régimes de marché. Coût : 0 €,
   environ 10 h de collecte étalées. **À décider par Ilias** (consomme le quota mensuel de symboles).
4. Période réservée de l'univers large (≥ 01/04/2026) : toujours intacte, à ne consulter que pour un candidat.
