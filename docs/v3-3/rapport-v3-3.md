# Rapport StockHunter 3.3 — Global Stock Intelligence (14/11/2026)

Branche `stockhunter-v3-3-yahoo`, construite **sur la PR #6 (3.2.1, non fusionnée)** : toutes ses corrections sont
conservées. À fusionner après la PR #6. Aucune fusion ni déploiement effectué.

Niveaux de preuve : **codé** · **testé sur fichiers d'exemple** · **réel** (données réelles, base jetable,
GitHub Actions) · **production**.

## État vérifié au départ
- `main` = 9f09960 (3.2), déployé sur Railway (SUCCESS) ; PR #6 ouverte, mergeable, CI verte.
- Collecte US : dernière chaîne nocturne réussie le 14/11 à 12 h 30 UTC (avant la fusion de la 3.2).
- PostgreSQL : 70,8 % (diagnostic 43614674470) ; sauvegarde hebdomadaire réussie (run 43620236702).

## Synthèse

| Indicateur | Résultat vérifié |
|---|---|
| Fournisseurs utilisables | automatiques : Massive, Tiingo (US), BCE (change) ; NSE/BSE désactivés par défaut en attente d’autorisation ; import de fichiers fournis par l'utilisateur. Désactivés automatiquement : Euronext, Yahoo |
| Yahoo : faisabilité et conditions | **collecte automatique interdite** (conditions §2(d)(ix)) ; `yfinance` non officiel ; export CSV réservé à l'abonnement Gold. Importateur développé (exports Yahoo, CSV yfinance, format long) et testé sur fichiers d'exemple ; **aucun appel à Yahoo** |
| Pays disponibles | **automatique** : États-Unis (production) ; Inde testée historiquement, mais collecte suspendue. **Import** : tout pays du registre doté d'un suffixe Yahoo (testé sur exemples : Allemagne, France, Royaume-Uni, Inde) |
| Bourses disponibles | NYSE, Nasdaq, NYSE American (production) ; NSE/BSE testés historiquement, mais suspendus ; autres places par import |
| Actions référencées | US : univers Massive ; NSE 4 055 ; BSE 7 919 (4 795 hors doublons NSE) |
| Actions réellement analysables | US 2 725 ; NSE 487 / BSE 1 dans les anciens tests, non exploités automatiquement |
| Profondeur de l'historique | US 2 ans ; Inde 300 séances (depuis le 25/08/2025) ; imports : celle des fichiers |
| Mode recherche opérationnel | **oui, sur données réelles** (Inde, run 43629035593) ; import → scan en mode recherche → classement « recherche » vérifié de bout en bout sur un fichier d'exemple |
| Mode surveillance opérationnel | US : **production** ; Inde suspendue par défaut après fusion ; pas d’autre place en surveillance automatique |
| Nombre de stratégies testées | 21 tests au registre (16 US + 5 catégories sur le NSE) |
| Stratégies statistiquement validées | **aucune** (NSE : t ≤ 1,01 pour un seuil de 3,0) |
| Portefeuille simulé | moteur événementiel 3.2.1 conservé (tests anti-look-ahead) ; ajouts : profit factor, rendement moyen par trade, résultats par stratégie et par pays ; rejeu NSE : −6,7 % en EUR contre +17,7 % pour l'indice équipondéré |
| Tests réussis | **262** sur SQLite et **262** sur PostgreSQL 16 ; migrations 0011 → 0013 sur une base PostgreSQL vierge, `alembic check` sans écart |
| GitHub Actions | vérifications réelles : 43626488229 (échec : défaut trouvé et corrigé), 43628514261 (échec : réimport non idempotent, corrigé), **43629035593 (succès)** |
| Railway | inchangé (3.2 déployée) ; rien de déployé par cette branche |
| Coût supplémentaire | **0 €** |

## Ce qui a été fait
1. **Interface commune** `MarketDataProvider` et `sources_status()` : chaque source déclare son mode (collecte /
   import / désactivée) et le motif ; une source non autorisée est désactivée sans bloquer les autres
   (`stockhunter world sources`, page Monde).
2. **Import d'historiques** (`stockhunter world import fichiers… --source … --rights "…"`) :
   - formats : export Yahoo, CSV yfinance (un ou plusieurs symboles), format long CSV / Parquet avec ISIN facultatif ;
   - normalisation : suffixes → places du registre, pence et cents → unité ;
   - contrôles qualité : doublons, dates futures, OHLC incohérents, lignes hors séance, séances figées ou sans volume, sauts > 45 %, devise contradictoire ;
   - déclaration de droits obligatoire, empreinte SHA-256, réimport idempotent ;
   - la première version importée fait foi (aucune réécriture).
3. **Branchement au scanner** : `CombinedStore` (collecte prioritaire, imports en complément). Places importées
   scannées en **mode recherche** : dernière séance connue, jamais présentées comme à jour, sans fausse alerte de
   fraîcheur. Classement en deux modes : **surveillance** (données à jour seulement) et **recherche** (retard affiché).
4. **Classement** : justification de chaque sélection, filtre de risque, clé « YF:<symbole> » signalée (ISIN inconnu :
   doublons inter-places non résolus), dédoublonnage par ISIN quand il est fourni.
5. **Recherche** : pré-enregistrement de l'extension aux places importées (même protocole, 5 tests par place,
   **biais de survivance** signalé pour Yahoo) ; le rejeu affiche ce biais.
6. **Telegram** :
   - nouvelles sélections par place (clôture datée « pas un cours en direct », plan simulé, justification) ;
   - entrées et résultats du portefeuille simulé ;
   - une alerte par événement, dédupliquée.
7. **Dashboard** : section « Données » (sources et droits, fichiers importés, qualité), mode recherche / surveillance,
   justification et retard affichés.
8. **Migration 0013** non destructive : identifiants mondiaux élargis à 40 caractères.

## Limitations
- Sans fichier légitimement obtenu, aucune place hors US / Inde n'est couverte : Yahoo ne fournit pas d'export
  gratuit et interdit la collecte automatique ; Euronext l'interdit également.
- Imports Yahoo : biais de survivance (radiés absents), pas d'ISIN, pas de secteur ni de capitalisation.
- NSE : conditions §8–9 consultées, collecte et simulation suspendues par défaut. BSE : droits non établis, collecte suspendue.
- Bourse de Casablanca : aucun suffixe Yahoo connu, aucune source autorisée.

## Commits
`791f07d` (import, sources, scanner, classement, portefeuille, Telegram, migration), `8564711` (correctif trouvé en
vérification réelle), `1eb3d31` (réimport idempotent), puis documentation.
