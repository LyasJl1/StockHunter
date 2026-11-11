# StockHunter AI

Plateforme de recherche et de suivi d'opportunités sur actions US. **Outil d'analyse : aucun ordre réel, aucun conseil
garanti, aucun score présenté comme une probabilité.**

## État : phase 1 — infrastructure et données
| Composant | État |
|---|---|
| Configuration par variables d'environnement | ✅ |
| Schéma SQL + migrations Alembic (SQLite / PostgreSQL) | ✅ |
| Client HTTP : rate limit, quotas persistés, cache, retries, masquage des secrets | ✅ testé |
| Tiingo EOD (prix bruts/ajustés, dividendes, splits) | ✅ code + tests simulés · ⏳ test réel |
| SEC EDGAR (fondamentaux XBRL point-in-time, dépôts 8-K/10-K/10-Q/Form 4) | ✅ code + tests simulés · ⏳ test réel |
| FRED (taux, CPI, VIX, courbe) | ✅ code + tests simulés · ⏳ test réel |
| Finnhub (calendrier des résultats) | ✅ code + tests simulés · ⏳ test réel |
| Contrôles qualité (fraîcheur, trous, OHLC, sauts, volumes nuls, macro) | ✅ testé |
| Verrou anti-concurrence + journal des jobs | ✅ testé |

## Démarrage
```bash
cp .env.example .env        # renseigner les clés gratuites (Tiingo, FRED, e-mail SEC)
pip install -e ".[dev]"
stockhunter init-db
stockhunter verify-sources  # prouve que chaque API répond réellement
stockhunter sync-universe
stockhunter ingest all
stockhunter quality
stockhunter status
```

Voir `CLAUDE.md` (règles et architecture) et `docs/data-sources.md` (sources, quotas, coûts, limites).
