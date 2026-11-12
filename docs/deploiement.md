# Déploiement Railway (plan gratuit, 1 $/mois)

Architecture (validée par Ilias le 12/11/2026, plan gratuit) :
- **Railway** : service web (dashboard + lecteur de commandes Telegram) et base **PostgreSQL**.
- **GitHub Actions** (gratuit) : tous les traitements planifiés, qui écrivent dans la base Railway.
- Les données de marché brutes (Parquet, ~150 Mo) restent dans le cache GitHub Actions (recherche uniquement).

Contraintes du plan gratuit (railway.com/pricing, relevé le 12/11/2026) : 1 projet, 0,5 Go de mémoire par
service, 0,5 Go de stockage. Taille mesurée de la base dans PostgreSQL : ~120 Mo (copie de test du 12/11/2026).

## Étapes (une seule fois)
1. **Railway → New Project → Deploy from GitHub repo → LyasJl1/StockHunter.** Railway lit `railway.json`
   (Dockerfile, commande de démarrage, contrôle de santé `/health`).
2. Dans le projet : **+ New → Database → PostgreSQL**.
3. Service web → **Variables** :
   - `SH_DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (référence interne Railway, pas de trafic facturé)
   - `SH_DASHBOARD_PASSWORD_HASH` et `SH_DASHBOARD_SECRET` : produits par `stockhunter dashboard hash-password`
     (ou par le workflow GitHub « Générer les accès dashboard »)
   - `SH_TELEGRAM_BOT_TOKEN`, `SH_TELEGRAM_CHAT_ID` (mêmes valeurs que dans GitHub)
4. Service web → **Settings → Networking → Generate Domain** : adresse HTTPS du dashboard.
5. Service PostgreSQL → **Variables** : copier `DATABASE_PUBLIC_URL`.
6. GitHub → Environments → Variables : secret **`SH_POSTGRES_URL`** = cette valeur, puis lancer
   **Actions → « Migration vers PostgreSQL »** (copie vérifiée table par table ; refusée si la cible n'est pas vide).
7. Une fois la migration réussie : secret **`SH_DATABASE_URL`** = la même valeur. Tous les workflows écrivent
   alors dans PostgreSQL (sans ce secret, ils restent sur SQLite).

## Vérifications
- `https://<domaine>/health` répond `{"status": "ok", ...}`.
- Connexion au dashboard avec le mot de passe choisi.
- Envoyer `/status` au bot Telegram : réponse en quelques secondes.
- Railway → Usage : consommation réelle (à comparer à l'estimation).

## Coûts
- Railway : plan gratuit, 1 $/mois après l'essai (aucune autre ressource créée).
- GitHub Actions, Massive, Tiingo, SEC, FRED, Finnhub, Telegram : 0 €.
