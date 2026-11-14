# Rapport StockHunter 3.2.1 — correction critique, fiabilisation, validation (14/11/2026)

Branche `stockhunter-v3-2-1-fixes`, **non fusionnée**. Niveaux de preuve utilisés ci-dessous :
**codé** · **testé** (tests automatisés hors réseau, SQLite et PostgreSQL 16) · **réel** (exécution sur données
réelles dans GitHub Actions, base jetable) · **production** (base Railway / chaîne nocturne de `main`).

## Audit initial
- `main` = 9f09960 (fusion de la 3.2) ; déploiement Railway 2707b5f8 **SUCCESS** (13 h 39 UTC, migrations au
  démarrage). Aucune chaîne nocturne n'a encore tourné depuis la fusion : **aucune donnée mondiale en production**
  (prochaine : 23 h 17 UTC).
- PostgreSQL (diagnostic en lecture seule, run 43614674470) : 353,9 Mo / 500 Mo (70,8 %), WAL 64 Mo ; plus gros
  postes : `fundamental_facts` 68 Mo, `filings` 34 Mo, `price_bars` 34 Mo.
- Problèmes de l'audit **confirmés sur le code** : décisions d'entrée à l'ouverture fondées sur la clôture du jour
  (valeur du portefeuille, exposition, corrélation) ; taux BCE du jour utilisé pour un ordre de Mumbai à 3 h 45 UTC ;
  étapes `monde_*` non critiques (un plantage = « partiel », job vert) ; `continue-on-error` dans `world-verify.yml`.
- Problèmes **supplémentaires** trouvés : look-ahead dans le **rejeu** (troncature des discontinuités sur tout
  l'historique, seuil de change de la dernière date) ; **conditions d'utilisation d'Euronext** incompatibles avec la
  collecte automatique (voir ci-dessous).

## Tableau de synthèse

| Élément | Avant (3.2) | Après (3.2.1) | Preuve |
|---|---|---|---|
| Look-ahead du portefeuille | clôture du jour, expositions et corrélations du jour, change de l'après-midi utilisés à l'ouverture | aucun : moteur événementiel horodaté | tests de perturbation (`tests/test_world_portfolio.py`) ; le simulateur 3.2 (copie figée) **échoue** aux mêmes tests — testé |
| Horodatage des décisions | aucun | journal par décision : instant UTC, capital connu, taux et date BCE utilisés | `Result.decisions` ; test de chronologie Inde → Europe → US — testé |
| Change intrajournalier | taux BCE de la date | taux connu à l'instant (publication 16 h Francfort, heure d'été comprise) ; conversions du scanner au taux connu à la clôture de la place | tests `ecb_known_date`, DST — testé, réel |
| Cohérence capital / positions | trésorerie débitée après coup | débit à la décision ; contrôle d'une trésorerie jamais négative (exception sinon) | tests de trésorerie, frais, gap, données manquantes — testé, réel |
| Pipeline mondial | étapes non critiques, `continue-on-error` | 4 états (succès, aucune opportunité, partiel, échec critique) ; étapes critiques ; panne de toutes les places étrangères = rouge | tests sur les vrais YAML et le pipeline — testé ; run 43616802932 — réel |
| Détection des erreurs | annotations | + résumé GitHub par place, page Monde (places en échec), alertes Telegram regroupées et dédupliquées, données périmées exclues du classement | tests dashboard, alertes, classement — testé |
| Sauvegarde des historiques | cache Actions seul | archive durable en base privée (Parquet + SHA-256), budget 60 Mo, refus à 85 % du volume ; restauration automatique si le cache disparaît | tests aller-retour, budget, reprise — testé |
| Restauration testée | non | 300 séances NSE réelles archivées (55,8 Mo), cache supprimé, restaurées, **identiques octet pour octet**, 300 empreintes vérifiées | run 43618799710 — réel |
| Historique européen disponible | « en constitution » (collecte quotidienne) | **indisponible** : collecte Euronext contraire aux conditions du site, désactivée ; aucune source gratuite autorisée trouvée | `docs/v3-2-1/sources-europe.md` |
| Marchés opérationnels | 12 places annoncées | **5 places réellement opérationnelles** : NYSE, Nasdaq, NYSE American (production) ; NSE, BSE (réel, pas encore en production) | tableau ci-dessous |
| Actions analysées | US 2 725 ; NSE 487 ; BSE 1 | identique (US en production le 13/11 ; Inde en vérification réelle) | runs 43616802932, 43618799710 |
| Portefeuille corrigé | biaisé | moteur 3.2.1 sur rejeu point-in-time | run 43618799710 — réel |
| Rendement recalculé (rejeu Inde, EUR, 05/03 → 13/11/2026) | −3,4 % (biaisé), réf. +17,1 % | **−6,7 %**, 35 trades, R moyen −0,34 ; indice équipondéré NSE +17,7 % | `docs/research/2026-11-14-resultats-international.md` |
| Tests réussis | 225 | **250** sur SQLite et **250** sur PostgreSQL 16 ; migrations 0011–0012 appliquées sur PostgreSQL vierge, `alembic check` sans écart | local + CI |
| PostgreSQL | 70,8 % | inchangé ; migration 0012 additive (`world_archive`) ; archive Euronext ≈ 0,1 Mo par séance si la collecte est réautorisée | diagnostic 43614674470 |
| Coût mensuel | 0 € | **0 €** | — |

## Couverture réelle

| Marché | Titres référencés | Titres analysés | Historique | Dernière séance | État |
|---|---:|---:|---|---|---|
| USA | univers Massive (actions ordinaires) | 2 725 | 2 ans | 13/11/2026 | **opérationnel (production)** |
| France | 769 (collecte du 13/11, branche 3.2) | 0 | 1 séance | 13/11/2026 | **indisponible** : collecte non autorisée |
| Pays-Bas | 126 | 0 | 1 séance | 13/11/2026 | indisponible (idem) |
| Belgique | 106 | 0 | 1 séance | 13/11/2026 | indisponible (idem) |
| Italie | 435 | 0 | 1 séance | 13/11/2026 | indisponible (idem) |
| Portugal | 48 | 0 | 1 séance | 13/11/2026 | indisponible (idem) |
| Irlande | 25 | 0 | 1 séance | 13/11/2026 | indisponible (idem) |
| Norvège | 290 | 0 | 1 séance | 13/11/2026 | indisponible (idem) |
| Inde NSE | 4 055 | 487 | 300 séances | 13/11/2026 | **opérationnel en vérification réelle** ; en production après fusion |
| Inde BSE | 7 919 (4 795 hors doublons NSE) | 1 | 300 séances | 13/11/2026 | opérationnel en vérification réelle (aucune opportunité) |

Classement mondial vérifié : 43 sociétés distinctes (50 sélections NSE dédupliquées par ISIN et par catégorie),
aucune place périmée, aucun doublon BSE / NSE.

## Changements de résultats et explication
- **Catégories (réplication pré-enregistrée)** : les écarts ont changé (momentum +2,1 % → +6,9 %, tendance
  régulière +0,2 % → −2,3 %), car l'ancien rejeu écartait à des dates passées des titres qui varieraient fortement
  plus tard. Toutes restent « non retenues » (t ≤ 1,01). Les anciens chiffres sont conservés dans le registre,
  marqués invalides.
- **Portefeuille** : −3,4 % → −7,8 % (même simulateur, sélections corrigées) → −6,7 % (simulateur corrigé). Aucun
  paramètre n'a été modifié. 1 058 entrées refusées par le plafond de 40 % par pays (une seule place disponible).

## Commits
`fab7b26` (look-ahead), `0127c3a` (pipeline, sauvegarde), `e654d03` (Euronext désactivé, vérification de la
restauration), `674e081` (indice sans variations inexpliquées, comparaison avant / après), puis rapport.
Runs GitHub Actions : diagnostic 43614674470 ; vérifications réelles 43616802932 et 43618799710.

## Limitations restantes
- Pas de marché européen opérationnel sans autorisation écrite d'Euronext ou sans source payante.
- L'Inde n'est vérifiée que sur base jetable : la production l'aura après fusion (reconstitution ≈ 7 nuits).
- Secteurs N/D hors US : le plafond sectoriel ne s'applique pas ; dividendes non inclus.
- Les messages de `trading/rules.py` affichent « $ » quelle que soit la devise (texte seulement, calculs justes).
- Puissance statistique faible (7 mois, une place) ; aucune stratégie validée.
- Les conditions d'utilisation NSE / BSE n'ont pas pu être lues (page soumise à autorisation d'accès) : à vérifier.
- Volume PostgreSQL à 70,8 % avec la croissance US existante : à surveiller (seuils 80 / 90 / 95 %).

## Autorisations nécessaires
1. Fusion de la PR (migration additive 0012, chaîne mondiale corrigée, Euronext désactivé).
2. Euronext : demander une autorisation écrite, ou accepter une source payante (aucune prise) — ou laisser l'Europe
   indisponible.
3. NSE / BSE : confirmer leurs conditions d'utilisation (envoie-moi le lien pour que je les lise).
4. Toujours en attente : visibilité du dépôt / anciens artefacts publics.
