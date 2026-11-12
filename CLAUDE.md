# CLAUDE.md — StockHunter AI

Moteur autonome de recherche, d'analyse, de validation et de suivi d'opportunités actions (US d'abord).
**Analyse et alertes uniquement : aucun ordre réel, aucune connexion courtier, pas de levier.**

## Règles non négociables
1. Ne jamais inventer de données, de performances ni d'opportunités. Données manquantes = `NULL` / « N/D », jamais 0.
2. Toute donnée est **datée** (séance, période, date de dépôt) **et horodatée** (`fetched_at`), avec sa source.
3. Aucun cours stocké n'est « live » : ce sont des clôtures EOD. L'UI et les messages doivent l'afficher.
4. Point-in-time : une analyse au jour J n'utilise que `filed_date <= J` (fondamentaux) et `bar_date < J` si J n'est pas clôturé.
5. Un score n'est pas une probabilité. Ne jamais écrire « 85 % de chances ».
6. Couverture honnête : l'univers suivi = `stockhunter/universe/us_seed.csv` (limité par les quotas), pas « tout le marché ».
7. Les fixtures de test sont **synthétiques** (`tests/fixtures.py`, symboles `ZZ*`) et ne vont jamais en production.
8. Secrets uniquement via variables d'environnement `SH_*`. Jamais dans le code, les logs, les messages, le cache.
9. Aucune ressource payante sans accord explicite d'Ilias.
10. Calculs (indicateurs, scores, risques, backtests) = code déterministe. LLM réservé à la synthèse de texte.

## Architecture (phases 1 et 2 livrées)
```
stockhunter/
  config.py            Settings (env SH_*) + quotas documentés des plans gratuits
  db/models.py         schéma SQLAlchemy ; migrations dans migrations/ (Alembic)
  db/upsert.py         upsert dédupliquant SQLite/PostgreSQL
  data/http.py         client commun : rate limit, quotas persistés, cache, retries, masquage secrets
  data/providers.py    Tiingo (EOD), SEC EDGAR (XBRL + dépôts), FRED (macro), Finnhub (calendrier résultats)
  data/ingestion.py    jobs idempotents d'ingestion
  data/quality.py      contrôles qualité -> table data_anomalies
  data/calendar.py     calendrier NYSE
  jobs/runner.py       verrou en base + journal JobRun
  universe/            univers suivi (CSV) + ETF sectoriel GICS de chaque action
  analytics/           indicateurs techniques et fondamentaux point-in-time (calcul pur)
  scoring/config.py    poids, seuils, version (hash vérifié : changer un paramètre = nouvelle version)
  scoring/engine.py    régime, sous-scores, familles de signaux A-F, statuts (calcul pur, sans base)
  scan.py              chargement point-in-time, enregistrement immuable, historique des statuts
  report_cli.py        commandes scan / top / explain
  cli.py               point d'entrée `stockhunter`
```
Méthode détaillée : `docs/scoring.md`. Protocole de backtest et résultats : `docs/validation.md`.
  validation/          rejeu du moteur réel, trades, portefeuille, verdicts, rapport
  market/              univers large : Massive -> Parquet par séance (brut), référentiel avec radiés, splits
  research/            études pré-enregistrées (protocole écrit avant les données, voir docs/research/)
  portfolio/paper.py   Trade Decision Engine simulé : plan (R = 2×ATR, TP1 1,5 R, TP2 3 R), suivi, événements
  reports/daily.py     rapport quotidien (12 sections), uniquement à partir de la base, sans LLM
  notifications/       Telegram : file persistante, déduplication, retries, anti-spam, commandes
Phases suivantes (à créer) : `dashboard/` (phase 5), actualités (6), déploiement (7). Ancien plan : `risk/ portfolio/ notifications/ reports/ dashboard/`.
Cycle d'une stratégie : RESEARCH → BACKTEST → OUT-OF-SAMPLE → PAPER → PROMOTED.

## Commandes
```bash
pip install -e ".[dev]"
stockhunter init-db                 # migrations
stockhunter sync-universe
stockhunter ingest all              # ou prices | fundamentals | macro | earnings [--symbols A,B] [--full]
stockhunter quality
stockhunter status
stockhunter prices AAPL -n 10
stockhunter verify-sources          # appels réels
stockhunter scan [--as-of AAAA-MM-JJ] [--if-new] [--require-fresh]
stockhunter top [-n 10] [--all]
stockhunter explain NVDA
stockhunter validate --out rapport.md --signals-cache var/signals-cache.pkl   # backtest (3 min)
stockhunter market sync|status      # univers large : toutes les actions US (Massive, gratuit, 2 ans)
stockhunter research broad [--out f.md]   # étude pré-enregistrée (docs/research/)
stockhunter paper run|status        # paper trading : plans des signaux READY, suivi, clôtures (simulation)
stockhunter forward record|report   # test prospectif des facteurs pré-enregistrés (depuis le 13/11/2026)
stockhunter notify evening|morning [--preview] [--force]   # rapport quotidien + alertes Telegram
pytest -m "not live"                # tests hors réseau
pytest -m live                      # tests réels (clés requises)
```

## Règles du scoring
- Un scan est inséré, jamais modifié. Ne jamais réécrire `scores` ou `status_changes`.
- Toute nouvelle donnée utilisée par le scoring doit être filtrée `<= as_of` ; ajouter un cas à `test_no_look_ahead`.
- Modifier `scoring/config.py` impose d'incrémenter `SCORING_VERSION` (le scan refuse sinon).
- Une étude de recherche suit son pré-enregistrement (docs/research/) : pas de réglage au vu des résultats.
- Aucune règle n'est réglée en regardant la période réservée. Chercher sur la période de développement, figer,
  puis évaluer la période réservée UNE fois (`validate --holdout`).

## Conventions
- Python 3.12+, typage, fonctions `parse_*` pures et testées séparément des appels réseau.
- Tout changement de schéma = nouvelle migration (`alembic revision --autogenerate`), puis `alembic check` en CI.
- Toute ingestion est idempotente (upsert) ; un échec par symbole ne fait pas échouer le job (statut `partial`).
- Nouveau fournisseur : quotas dans `ProviderLimits`, licence dans `SOURCE_REGISTRY`, tests avec fixtures `FIXTURE_*`.
- Français pour la documentation et les messages utilisateur.

## Procédure d'audit
Skill `/data-quality` pour les données ; agent `data-engineer` pour toute modification de l'ingestion.
Avant de dire qu'un service « fonctionne » : `stockhunter verify-sources` ou `pytest -m live` doivent l'avoir prouvé.

## Déploiement (cible : Railway, plan à valider par Ilias)
Image Docker = conteneur « job » (pas de service 24h/24). PostgreSQL en production via `SH_DATABASE_URL`.
Jobs planifiés prévus (heure UTC, à affiner en phase 7) : ingestion EOD après publication des clôtures US,
qualité juste après, macro 1×/jour, fondamentaux/dépôts SEC 1×/jour.
