# StockHunter AI

Plateforme personnelle de recherche, d'analyse, de validation et de suivi d'opportunités actions.
**Analyse et alertes uniquement : aucun ordre réel, aucune connexion courtier, pas de levier.** Un score n'est
jamais une probabilité ; les cours sont des clôtures datées, jamais des cours en direct.

## État (3.2 — 14/11/2026)
| Domaine | Couverture réelle |
|---|---|
| États-Unis | toutes les actions ordinaires NYSE / Nasdaq / NYSE American (Massive, radiées comprises, 2 ans) ; univers suivi détaillé (Tiingo, SEC, FRED, Finnhub) |
| Europe (Euronext) | Paris, Amsterdam, Bruxelles, Lisbonne, Dublin, Milan, Oslo : instantané officiel de fin de séance ; **historique en constitution** depuis la première collecte (scanner actif à 130 séances) |
| Inde | NSE et BSE : bhavcopy officiel, historique reconstitué depuis les archives publiques ; scanner actif |
| Change | taux de référence BCE (≈ 30 devises), point-in-time |
| Autres places | registrées mais **indisponibles** (pas de source gratuite fiable) : voir `docs/v3-2/sources.md` |

Fonctions : scanner multi-catégories par place, classement mondial dédupliqué et filtrable, suivi de chaque
sélection avec des règles de trading uniques, paper trading US, portefeuille mondial simulé (EUR / USD),
laboratoire de recherche pré-enregistré, tableau de bord web (Railway), Telegram.

## Documentation
- `CLAUDE.md` — règles non négociables et architecture.
- `docs/v3/` — StockHunter 3.0 / 3.1 (audit, architecture, mise en production, rapports).
- `docs/v3-2/` — marchés mondiaux : sources et licences, conventions de calcul, rapport 3.2.
- `docs/research/` — pré-enregistrements et résultats ; `stockhunter/lab/registry.json` — registre des tests.
- `docs/ops/` — exploitation (stockage, sauvegardes, diagnostics).

## Démarrage
```bash
pip install -e ".[dev,postgres]"
stockhunter init-db
pytest -m "not live"                         # tests hors réseau (SQLite) ; SH_TEST_DATABASE_URL=postgresql://… pour PostgreSQL
stockhunter world markets                    # registre des places et couverture
stockhunter world sync --markets XNSE --backfill 20   # collecte réelle (réseau requis)
stockhunter world scan && stockhunter world ranking --top 20
stockhunter dashboard serve --insecure-cookies
```
En production, la chaîne nocturne (`stockhunter pipeline nightly`, workflow GitHub Actions) fait tout ; les secrets
passent uniquement par des variables d'environnement `SH_*`.
