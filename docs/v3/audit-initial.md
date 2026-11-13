# StockHunter 3.0 — audit indépendant du système 2.0 (phase A)

Audit mené sur `main` (commit b1d8337) les 12 et 13/11/2026. Chaque défaut « confirmé » a été reproduit par un
test ou observé en production ; chaque correctif a un test de non-régression.

## Défauts confirmés et corrigés
| # | Défaut | Preuve | Correctif | Test |
|---|---|---|---|---|
| 1 | **Disque PostgreSQL saturé** (production, 13/11 08 h 05) : journaux WAL ≈ 220 Mo sur un volume de 500 Mo + réécriture de toutes les lignes à chaque collecte | journaux Railway (`No space left on device`, redémarrage), scan du 12/11 non exécuté | upsert qui ne réécrit que les lignes modifiées ; `stockhunter db tune` (WAL plafonnés à 64 Mo, point de contrôle, nettoyage) en tête de chaîne | `test_upsert_does_not_rewrite_unchanged_rows`, `test_tune_is_safe_on_any_database` |
| 2 | **41 alertes Telegram en une nuit** (classement des articles par mots-clés trop large) | journal du 13/11 : « alertes_catalyseurs : 41 » | alertes sur dépôts SEC uniquement, 5 max par exécution, expiration à 24 h ; la presse reste dans le rapport | `test_alerts_never_come_from_press_are_capped_and_expire` |
| 3 | **Workflows qui masquent les échecs** : chaque étape finissait par `\|\| echo warning` ; le job du 13/11 est resté vert malgré le plantage de la base | run 43382598527 : « success » avec `OperationalError` | `stockhunter pipeline nightly` : tout est consigné, sortie en erreur si une étape critique échoue, job rouge, alerte le soir même | `tests/test_pipeline.py` (5 tests) |
| 4 | **Conflits HTTP 409 Telegram** : deux lecteurs `getUpdates` (serveur Railway + jobs GitHub, et deux conteneurs pendant un redéploiement) | journaux Railway du 12/11 | bail exclusif en base (un seul lecteur ; reprise à expiration) | `test_single_telegram_reader_lease` |
| 5 | **Splits dans la courbe de capital** : après un split, le P&L historique était calculé avec le prix d'entrée ajusté et la quantité d'origine (gain ou perte fictif) | lecture de `equity_curve` ; reproduit | événement `SPLIT` rejoué dans la courbe | `test_sector_limit_and_split_adjustment` (courbe) |
| 6 | **ATR non ajusté au split** (stop suiveur trop large après un split) et split ignoré avant l'entrée | lecture de `process` | tous les niveaux par action ajustés, y compris pour un plan en attente | `test_rules_split_while_planned_and_open` |
| 7 | **Backtest ≠ paper trading** : le backtest v2 n'avait ni zone d'entrée, ni TP1/TP2, ni taille au risque ; il ne testait pas ce que le paper trading exécute | comparaison `validation/backtest.py` / `portfolio/paper.py` | une seule machine à états (`trading/rules.py`) utilisée par les deux | `test_backtest_and_paper_trading_produce_identical_trades` |
| 8 | **Hypothèse intrajournalière** : le jour du TP1, le stop passait aussitôt au prix d'entrée, ce qui suppose de connaître l'ordre des cours dans la séance | lecture | stop au prix d'entrée seulement à la séance suivante ; stop prioritaire si stop et objectif le même jour | `test_rules_same_day_tp1_does_not_assume_intraday_order` |
| 9 | **Aucun glissement** sur les ordres au marché et les stops | lecture | 5 pb défavorables (paramétrable) | `test_rules_slippage_and_costs_are_adverse` |
| 10 | **Rapport du matin jamais envoyé si GitHub est en retard** (il n'agissait qu'à 8 h pile ; le 13/11 la planification a eu 4 h de retard) | runs du 13/11 | envoi entre 8 h et 12 h, une fois par jour ; créneaux multiples | test paramétré mis à jour |
| 11 | **Limite de corrélation absente** du portefeuille simulé | lecture | refus motivé si corrélation 60 j > 0,80 avec une position ouverte | `test_correlated_position_is_refused` |

## Points vérifiés sans défaut
Absence de look-ahead du scan v2 (test existant), immutabilité des scans, séparation réel / simulé, secrets
(aucun dans l'historique git), authentification du dashboard (corrigée le 12/11 : nonce CSP, limitation réelle).

## Risques résiduels
- **Dépôt public** : 8 artefacts anciens contenant la base et les cours sont encore téléchargeables, ainsi que le
  hachage du mot de passe du dashboard dans un résumé de run. Suppression = action irréversible, laissée à Ilias
  (passer le dépôt en privé règle les deux).
- **Railway gratuit** : 1 $ de crédit pour ≈ 1,9 $ de consommation mensuelle → arrêt du site en cours de mois ;
  volume de 0,5 Go (≈ 0,27 Go utilisés après correctif). La collecte et Telegram (GitHub Actions) continuent.
- **Planification GitHub** : retards de plusieurs heures constatés ; atténués par des créneaux multiples, pas
  supprimés.
- **Ordre d'entrée** : la borne basse de la zone d'entrée (annulation si l'ouverture est trop basse) n'est pas
  exécutable avec un seul ordre réel ; sans conséquence tant qu'aucun ordre réel n'existe.
- **Dépendances non figées** (`>=`).
