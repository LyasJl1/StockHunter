---
name: data-quality
description: Diagnostiquer les données StockHunter manquantes, incohérentes ou périmées (prix EOD, fondamentaux SEC, macro FRED) et proposer des corrections sans jamais inventer de valeur. À utiliser après une ingestion, avant un backtest ou quand un rapport semble incohérent.
---

# Diagnostic qualité des données

## Étapes
1. `stockhunter status` — noter la couverture réelle, la dernière séance attendue, les jobs en échec.
2. `stockhunter quality` — lister les anomalies par sévérité (critical > warning > info).
3. Pour chaque anomalie `critical` :
   - `price_missing` / `price_stale` : vérifier le dernier `job_runs` d'ingestion (`detail`), les quotas
     (`api_calls` des dernières 24 h), puis relancer `stockhunter ingest prices --symbols X`.
   - Si la source n'a réellement pas de donnée : le documenter, ne pas combler.
4. Pour `price_extreme_move` : comparer prix bruts / ajustés et `split_factor` ; vérifier un 8-K autour de la date
   (`filings`). Un vrai mouvement reste ; une erreur de source est signalée, jamais corrigée à la main en base.
5. Pour `price_gaps` : distinguer fermeture exceptionnelle de marché (ignorer) et trou de source (`--full`).
6. Pour `fund_no_cik` : le ticker a peut-être changé → mettre à jour l'univers.

## Requêtes utiles (SQLite)
```sql
SELECT check_name, severity, detail FROM data_anomalies WHERE resolved_at IS NULL ORDER BY severity;
SELECT provider, COUNT(*) FROM api_calls WHERE called_at > datetime('now','-1 day') AND from_cache=0 GROUP BY 1;
SELECT job_name, status, items_failed, substr(detail,1,200) FROM job_runs ORDER BY id DESC LIMIT 10;
```

## Interdits
- Remplacer un NULL par 0, interpoler un prix, recopier la veille.
- Présenter une clôture comme un cours en temps réel.
- Affirmer qu'une source fonctionne sans `stockhunter verify-sources` réussi.

## Sortie attendue
Tableau : anomalie | cause probable | action | vérifiée (oui/non).
