# Rapport StockHunter 3.2 — Global Stock Hunter (14/11/2026)

Branche `stockhunter-v3-2`, **non fusionnée** (fusion et mise en production soumises à validation). Toutes les
mesures ci-dessous viennent d'exécutions réelles (workflows « Sondage des sources mondiales » et « Monde —
vérification réelle », runs 43594372623, 43596558340, 43598324437, 43599040783) sur une base SQLite jetable :
**rien n'a été écrit dans la base de production**.

## Tableau de synthèse

| Élément | Résultat vérifié |
|---|---|
| Pays couverts (cours quotidiens) | **9** : États-Unis, France, Pays-Bas, Belgique, Portugal, Irlande, Italie, Norvège, Inde |
| Places couvertes | **12** : NYSE, Nasdaq, NYSE American ; Euronext Paris, Amsterdam, Bruxelles, Lisbonne, Dublin, Milan, Oslo ; NSE ; BSE |
| Places du registre sans source gratuite fiable | 17 (Toronto, Xetra, Londres, Madrid, Zurich, Stockholm, Tokyo*, Shanghai**, Shenzhen**, Hong Kong***, Corée, Australie*, Brésil, Mexique, Johannesburg, Casablanca, Golfe) |
| Actions analysables (référentiel, listing principal) | Euronext 1 789 sociétés ; NSE 4 055 ; BSE (hors doublons NSE) 4 795 ; États-Unis : univers Massive inchangé |
| Actions réellement analysées (univers négociable du 13/11) | États-Unis 2 725 ; NSE 487 ; BSE 1 ; Euronext 0 (historique : 1 séance sur 130 requises) |
| Fournisseurs fonctionnels | Massive, Tiingo (existants) ; Euronext, NSE, BSE, BCE (nouveaux) — tous gratuits |
| Scanner mondial | opérationnel par place (même moteur, mêmes catégories) ; Inde : 50 sélections le 13/11 |
| Classement mondial | opérationnel : 49 sociétés distinctes (dédupliquées par ISIN), filtres continent / pays / bourse / devise / catégorie / taille, 5-10-20-50-toutes |
| Sélections distinctes | 49 (Inde) + États-Unis (dernier scanner de production) ; Europe : aucune tant que l'historique se constitue |
| Gestion des devises | BCE point-in-time (29 447 taux depuis 2023), EUR et USD, effet de change séparé, 25 pb par conversion |
| Portefeuille mondial | simulateur opérationnel EUR / USD (plafonds pays, devise, secteur, corrélation, quotités, frais de change) |
| Performance historique (rejeu Inde, descriptif) | −3,4 % en EUR du 05/03 au 13/11/2026, 33 trades, R moyen −0,14 |
| Références | indice équipondéré NSE +17,1 % (EUR, même période) ; SPY, URTH, VGK, INDA branchés (cours US Massive) |
| Stratégies validées | **aucune** (réplication Inde : 5 catégories NON RETENU, t de 0,14 à 1,45 < 3,0 ; famille de 21 tests) |
| Tests | **225** réussis sur SQLite et sur PostgreSQL 16 (dont 34 tests mondiaux couvrant les 18 cas exigés) |
| Espace PostgreSQL | inchangé par les cours (Parquet hors base) ; tables 3.2 estimées < 1 Mo + ≈ 150 Ko par séance de sélections |
| Stockage mondial (cache Actions) | 137,5 Mo pour 300 séances NSE + 300 BSE + Euronext + change (budget 2 Go) |
| Coût mensuel | **0 €** supplémentaire |

\* liste officielle seulement, pas de cours gratuits. \*\* cours chez Tiingo mais 500 titres / mois au plus.
\*\*\* source officielle identifiée (cotations quotidiennes HKEX), non intégrée.

## Commits
- `106134a`, `d58812b` — sondages en lecture seule des sources mondiales.
- `d565a1b` — cœur 3.2 : registre des places, fournisseurs, stockage Parquet, change, référentiel, scanners,
  classement, portefeuille, correctifs (séance attendue, visibilité des échecs, notionnel), page Monde, Telegram.
- `baace64` — pré-enregistrement de la réplication internationale (avant exécution) ; suppression de `build/`.
- `bbd9fc7` — rejeu pré-enregistré, affichage hébergé, documentation.
- `045e0f5` et suivants — résultats au registre, BSE sans doublons, courbe depuis le premier signal, rapport.

## Correctifs (audit)
1. **Séance attendue** : la séance US du jour n'était comptée que le lendemain (collecte du soir repoussée à
   ≈ 07 h UTC). Désormais clôture + délai de publication, par place ; la chaîne n'est « faite » que si chaque place
   a traité sa séance (un férié US n'empêche plus la collecte européenne / indienne).
2. **Échecs de la découverte visibles** : étape devenue critique (panne = job rouge, annotation `::error`) ;
   données absentes ou séance attendue manquante = code 2, annotation `::warning`, réessai au créneau suivant ;
   « aucune opportunité » = succès avec annotation `::notice`. Résumé de la chaîne dans le résumé du job.
3. **Risque du suivi unitaire** (`discovery/store.py`) : la docstring annonçait 1 000 $, le code utilisait
   1 000 000 $. Le notionnel est désormais une constante documentée `UNIT_RISK` ; un test montre que les multiples
   de R publiés sont identiques (à 0,001 R près) avec un notionnel 1 000 fois plus grand : aucun résultat passé
   n'est modifié.
4. Dédoublonnage des signaux : par place et ISIN (listing principal), BSE scanné sans les sociétés du NSE.
5. `build/` (91 fichiers d'une ancienne construction) retiré du dépôt ; `.gitignore` complété.
6. Tests PostgreSQL : suite complète sur PostgreSQL 16 ; une colonne trop courte détectée et corrigée avant la
   première migration (`world_scans.status`).
7. Traçabilité : chaque séance stockée porte fournisseur, horodatage, nombre de lignes ; licences dans
   `docs/v3-2/sources.md` et dans la page Monde (santé par fournisseur).

## Places opérationnelles et données manquantes
- **Inde (NSE, BSE)** : opérationnelle, 300 séances reconstituées depuis les archives officielles, scanner actif.
- **Euronext (7 places)** : collecte quotidienne opérationnelle ; **pas d'historique gratuit** : le scanner
  s'activera après 130 séances (≈ fin mai 2027 si la collecte quotidienne démarre fin novembre 2026). Les séances
  manquées ne pourront pas être rattrapées.
- Secteurs et capitalisations : N/D hors États-Unis (aucune source gratuite) ; la liquidité sert de proxy de taille.
- Dividendes non inclus (rendements de prix), comme pour les États-Unis.

## Problèmes non résolus
- Places majeures sans source gratuite fiable (Londres, Xetra, Tokyo, Hong Kong, Chine, Canada, Corée, Australie,
  Brésil, Suisse…) : non couvertes.
- Puissance statistique faible (7 mois, une place, un régime) : aucune conclusion possible sur l'Inde.
- Le portefeuille mondial ne contient pour l'instant qu'une place avec historique : les plafonds pays / devise
  le maintiennent largement en trésorerie.
- Le stockage mondial vit dans le cache Actions (non sauvegardé ailleurs) : une purge du cache ferait perdre
  l'historique Euronext (non reconstituable) ; l'historique indien, lui, se reconstitue.

## Autorisations nécessaires
1. **Fusion de la PR** dans `main` (déclenche la migration additive 0011 en production et la collecte mondiale
   nocturne). Plus la fusion est tardive, plus l'historique Euronext démarre tard.
2. Facultatif : un plan payant (ex. EODHD, Tiingo supérieur) pour Londres, Xetra, Tokyo, Hong Kong ou la Chine —
   **non engagé**.
3. Toujours en attente (3.1) : dépôt privé ou suppression des anciens artefacts publics.

## Mise en production (après validation)
1. Fusionner la PR (aucun secret nouveau, aucune variable obligatoire ; réglages optionnels `SH_WORLD_*`).
2. La chaîne nocturne applique la migration 0011, collecte Euronext / NSE / BSE / BCE (40 fichiers au plus par
   place et par nuit : l'historique indien de 260 séances est reconstitué en ≈ 7 nuits), lance les scanners et le
   portefeuille mondial ; le tableau de bord Railway affiche la page **Monde** au déploiement suivant.
3. Facultatif, pour accélérer la reconstitution indienne : définir une fois la variable `SH_WORLD_MAX_FILES=320`
   pour une nuit (≈ 15 min de collecte, ≈ 140 Mo), puis la retirer.
4. Vérifier le lendemain : page Monde (fraîcheur par place), résumé Telegram « Marchés mondiaux », annotations
   du job.
