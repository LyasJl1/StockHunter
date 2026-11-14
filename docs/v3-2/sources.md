# Sources de données mondiales — StockHunter 3.2 (vérifiées le 14/11/2026)

Méthode : deux sondages en lecture seule (workflow « Sondage des sources mondiales », un appel par URL, statut HTTP,
type, taille, premières lignes ; aucune donnée conservée ni publiée), puis collecte réelle dans le workflow
« Monde — vérification réelle ». Aucun fournisseur payant, aucun contournement de protection, aucun coût.

## Sources retenues (gratuites, légales, vérifiées)

| Source | Places | Contenu | Historique | Licence / usage | Quota retenu |
|---|---|---|---|---|---|
| Euronext — téléchargement « Equities » de live.euronext.com | Paris, Amsterdam, Bruxelles, Lisbonne, Dublin, Milan, Oslo | 3 883 lignes : nom, ISIN, symbole, marché, devise, OHLC, volume, montant échangé, clôture de la dernière séance | **aucun** : constitué jour après jour | consultation personnelle ; pas de redistribution | 1 fichier par séance |
| NSE India — bhavcopy UDiFF (nsearchives) | NSE | OHLC, volume, montant, cours de référence ajusté (opérations sur titres), quotité, ISIN | archives publiques (vérifié au 13/11/2024) | usage personnel non commercial | ≤ 40 fichiers / passage, 1 toutes les 1,5 s |
| BSE India — bhavcopy UDiFF | BSE | même format que le NSE | archives au format UDiFF | usage personnel non commercial | idem |
| BCE — taux de référence (data-api.ecb.europa.eu) | change | ≈ 30 devises contre EUR, quotidien, publié vers 16 h CET | depuis 1999 | réutilisation libre avec mention « source : BCE » | 1 appel / jour |
| Massive (existant) | États-Unis | toutes les actions, radiées comprises | 2 ans | usage personnel | 5 appels / min |

Filtres : Euronext — lieux secondaires exclus (« Global Equity Market » = titres étrangers, « Trading After
Hours », EuroTLX, Expert Market) ; Inde — ISIN « INE… » uniquement (actions d'émetteurs indiens ; obligations
souveraines « IN00… » et fonds « INF… » écartés).

## Sources examinées et non retenues

| Source | Résultat du sondage | Décision |
|---|---|---|
| Stooq | page de vérification JavaScript (anti-robot) ; clé API obligatoire depuis 2026 | **non contournée** : indisponible |
| Xetra (liste des instruments) | HTTP 504 | indisponible ; aucun cours gratuit de tout le marché |
| JPX (Tokyo) | liste officielle (xlsx) accessible ; pas de cours | liste seulement |
| ASX | liste officielle (CSV) ; pas de cours | liste seulement |
| HKEX — cotations quotidiennes | page HTML de 23 Mo, sans archive longue | source identifiée, **non intégrée** en 3.2 |
| Tiingo — Chine | 600519 répond ; ≈ 2 834 actions actives à Shanghai, 3 187 à Shenzhen | quota de 500 titres / mois (déjà en partie utilisé pour les US) : couverture du marché impossible sans plan payant |
| Twelve Data (gratuit) | États-Unis seulement, usage interne | non pertinent |
| Alpha Vantage (gratuit) | 25 requêtes / jour | trop limité |
| EODHD (gratuit) | 20 requêtes / jour | trop limité |
| Yahoo Finance | conditions d'utilisation interdisant ce type d'usage | écarté |

Places sans source gratuite fiable (affichées « indisponible », jamais comptées comme couvertes) : Toronto, Xetra /
Francfort, Londres, Madrid, Zurich, Stockholm, Tokyo (cours), Shanghai / Shenzhen (quota), Hong Kong (non intégré),
Corée, Australie (cours), Brésil, Mexique, Johannesburg, Casablanca, places du Golfe.

## Ajouter une place

1. Une ligne `Market` dans `stockhunter/world/markets.py` (MIC, pays, région, fuseau, devise, clôture, délai de
   publication, calendrier exchange_calendars).
2. Si des cours gratuits et légaux existent : une fonction `parse_*` pure + un test sur fixture synthétique dans
   `stockhunter/world/providers.py`, une collecte dans `sync.py` (idempotente, reprenable, plafonnée).
3. Ajouter le code de la place à `SCANNED` (`world/run.py`). Le scanner, le classement, le portefeuille, la page
   Monde et Telegram l'utilisent sans autre modification.
4. Test réel : workflow « Monde — vérification réelle » (comptes par place, scanner, rendu).
