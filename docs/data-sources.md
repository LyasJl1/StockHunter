# Sources de données — vérification du 12/11/2026

Vérification documentaire (pages officielles et documentations tierces). Les quotas doivent être
**reconfirmés en conditions réelles** avec `stockhunter verify-sources` : l'environnement de développement
n'avait pas d'accès réseau sortant vers ces API au moment de la phase 1.

| Besoin | Source retenue | Coût | Limites connues | Statut |
|---|---|---|---|---|
| OHLCV EOD ajustés, dividendes, splits | **Tiingo** (Free) | 0 € | 50 req/h, 1000 req/j, ~500 symboles uniques/mois ; usage personnel, redistribution interdite | implémenté, non testé en réel |
| Fondamentaux point-in-time (XBRL) | **SEC EDGAR** companyfacts | 0 € | ≤10 req/s, User-Agent avec e-mail obligatoire | implémenté, non testé en réel |
| Dépôts 8-K, 10-K, 10-Q, Form 4, 13D/G, S-1/S-3 | **SEC EDGAR** submissions | 0 € | idem ; 1000 dépôts récents par fichier | implémenté (métadonnées) |
| Taux, inflation, VIX, courbe | **FRED** | 0 € | clé gratuite ; ~120 req/min | implémenté, non testé en réel |
| Calendrier des résultats | **Finnhub** (Free) | 0 € | 60 req/min (30 configurés par prudence) ; usage personnel ; **chandeliers payants** | implémenté, optionnel |
| Actualités (titres, éditeur, lien) | **Finnhub** `/company-news` (Free) | 0 € | 1 appel par action et par soir ; on stocke titre, éditeur, lien et date de publication, jamais le texte intégral des articles | implémenté (phase 6) |

## Choix écartés
- **Finnhub pour l'historique de prix** : les chandeliers actions sont réservés aux plans payants.
- **Alpha Vantage** : quota gratuit trop faible pour un univers de plusieurs dizaines de titres.
- **yfinance / scraping Yahoo** : non officiel, fragile, conditions d'utilisation incompatibles avec la consigne.

## Couverture réelle avec ces quotas
- Univers par défaut : 58 symboles (4 indices ETF, 12 ETF sectoriels, 42 actions liquides US).
- 1 seul appel Tiingo par symbole (historique complet en une requête), 58 appels pour tout l'univers.
- 50 req/h : le chargement initial et chaque mise à jour quotidienne se font en 2 passages espacés d'1 h
  (le job s'arrête net sur un 429, sans retry, et le passage suivant reprend par les séries les plus anciennes).
- Constaté au 1er run réel (12/11/2026) : Tiingo a renvoyé 429 avant notre compteur local de 50 —
  le quota côté Tiingo fait foi.
- Plafond logiciel : 400 symboles (marge sous la limite de 500 symboles/mois).

## Limites à traiter dans les phases suivantes
- **Biais du survivant** : l'univers est choisi aujourd'hui. La phase 3 devra utiliser un univers point-in-time
  (ex. composition historique d'indices) ou documenter explicitement le biais dans chaque backtest.
- **Prix ajustés rétroactifs** : un split/dividende modifie tout l'historique ajusté ; l'ingestion recharge alors
  la série complète. Les backtests doivent utiliser les prix ajustés stockés à la date d'exécution, ou les bruts + actions.
- Pas de données intraday ni de spread bid/ask gratuits : le slippage sera modélisé (phase 3), pas mesuré.
- Euronext Paris : aucune source gratuite fiable sélectionnée à ce stade (phase ultérieure).

## Estimation des coûts mensuels (phase 1)
| Poste | Coût |
|---|---|
| API de données (plans gratuits) | 0 € |
| GitHub + GitHub Actions (dépôt privé, quota gratuit) | 0 € |
| Railway (jobs + PostgreSQL) | à chiffrer avant création — **aucune ressource créée** |
