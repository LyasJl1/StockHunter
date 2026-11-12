# Audit final — phase 8 (12/11/2026)

Périmètre : code (commit 19f0a74 et suivants), workflows GitHub Actions, déploiement Railway, règles du cahier des
charges. Tests : 106 hors réseau (SQLite et PostgreSQL 16), 5 tests réels des API (CI « live-sources »).

## 1. Sécurité
| Constat | Gravité | Statut |
|---|---|---|
| Dépôt **public** : les artefacts `stockhunter-db` / `stockhunter-market` (base SQLite, cours Tiingo, données Massive) étaient téléchargeables par tout compte GitHub. Contraire aux licences (redistribution interdite). | Important | Corrigé dans les workflows (plus d'artefact de données). **Artefacts existants à supprimer** (action d'Ilias). |
| Dépôt public : résumé du run « Générer les accès dashboard » contenant le hachage scrypt du mot de passe. | Important | **À supprimer** (action d'Ilias). Le hachage ne donne pas le mot de passe mais permet une attaque hors ligne. |
| Dépôt public : une PR venant d'un fork peut, après approbation, restaurer le cache Actions (`var/`). | Moyen | Risque résiduel : **passer le dépôt en privé** le supprime (Actions reste gratuit, ~2 000 min/mois, usage estimé < 400). |
| CSP autorisait les scripts inline (`'unsafe-inline'`). | Moyen | Corrigé : nonce par requête, aucun gestionnaire inline. |
| Limitation des tentatives de connexion par IP du proxy Railway (toutes les connexions partageaient la même IP). | Moyen | Corrigé : IP réelle (dernière entrée X-Forwarded-For) + plafond global 30 échecs / 15 min. |
| En-têtes : HSTS et `Cache-Control: no-store` absents. | Mineur | Corrigé. |
| PostgreSQL exposé sur Internet (proxy TCP Railway) pour les jobs GitHub. | Moyen | Accepté : mot de passe aléatoire long, TLS ; nécessaire tant que les jobs tournent sur GitHub. |
| Secrets dans l'historique git, le code, les journaux | — | Aucun trouvé (recherche sur tout l'historique). Masquage des clés dans `data/http.py` testé. |
| Commandes Telegram | — | Acceptées uniquement depuis le chat configuré ; HTML échappé (testé). |
| Dépendances non figées (`>=`) | Mineur | Risque résiduel ; à figer si le projet se stabilise. |

## 2. Honnêteté des données et rigueur
| Règle | Vérification | Statut |
|---|---|---|
| Score ≠ probabilité | Recherche des mots « chance / probabilité » ; avertissement dans le rapport, le dashboard, `/analyse` | OK |
| Jamais une donnée ancienne présentée comme à jour | Ajout d'un contrôle de retard (séance attendue à New York) : bandeau dashboard + ligne dans le rapport | Corrigé |
| Pas de look-ahead | `test_no_look_ahead` (scan) ; synthèse : 8-K, articles, calendrier, plans et anomalies filtrés à la date (testé) | OK, renforcé |
| Signaux passés jamais modifiés | Aucune écriture sur `scores`, `status_changes`, `forward_signals` hors insertion | OK |
| Réel et simulé jamais mélangés | Tables séparées (`paper_trades` / `manual_transactions`), pages et calculs séparés | OK |
| Fixtures jamais en production | Aucun symbole `ZZ*` ni `FIXTURE_*` dans le paquet | OK |
| Couverture honnête | 42 actions suivies ; univers large utilisé seulement en recherche ; dit partout | OK |
| Aucune stratégie annoncée comme validée | v2.0.0 : aucune stratégie validée (holdout consommé) ; affiché sur chaque classement | OK |

## 3. Robustesse
- Ingestion idempotente, échec par symbole isolé (statut `partial`), quotas persistés en base, arrêt propre sur quota.
- Verrou de job en base ; alerte Telegram si un job échoue sans succès ultérieur dans les 24 h.
- File Telegram persistante : déduplication, réessais, limite par exécution.
- Base : `pool_pre_ping` (reconnexion PostgreSQL) ; migration vérifiée table par table.
- Railway gratuit : crédit de 1 $ épuisé vers la mi-mois au rythme actuel (≈ 1,9 $/mois). Choix d'Ilias : laisser tel
  quel. Les jobs GitHub continuent ; le dashboard et les réponses instantanées s'arrêtent jusqu'au mois suivant.
- Deux lecteurs des commandes Telegram (serveur Railway et jobs GitHub) : avertissements « 409 Conflict » sans
  conséquence ; conservé pour que les commandes restent traitées si Railway est arrêté.

## 4. Conformité au cahier des charges
| Exigence | Statut |
|---|---|
| Collecte de données gratuite, sourcée, datée | Fait (Tiingo, SEC, FRED, Finnhub, Massive) |
| Stratégies A-F | A-D faits ; E informative (hors score, non validée faute d'historique d'actualités) ; F sans objet (grandes capitalisations) |
| Score 0-100 explicable (25/20/20/15/10/10) | Fait |
| Analyse approfondie par action | Fait (synthèse sourcée, phase 6) |
| Moteur de décision : entrée, sortie, risque | Fait en simulation (paper trading) |
| Validation scientifique | Faite ; résultat négatif documenté |
| Rapports Telegram matin et soir, alertes, commandes | Fait (+ `/analyse`) ; à confirmer sur la première nuit en base Railway |
| Dashboard sécurisé (5 pages) | Fait |
| Agents et skills Claude, CLAUDE.md | Fait (3 skills, 3 agents) |
| Déploiement | Railway (dashboard + PostgreSQL) + GitHub Actions (jobs) |
| Aucun ordre réel, aucun courtier | Vérifié (aucun code de courtier) |
| Critère réel de la phase 4 : un signal suivi jusqu'à sa clôture | En attente (premiers plans simulés cette nuit) |

## 5. Actions restantes pour Ilias
1. Passer le dépôt en privé (Settings → General → Danger Zone → Change visibility), ou à défaut supprimer les
   artefacts `stockhunter-db` / `stockhunter-market`.
2. Supprimer le run réussi « Générer les accès dashboard » (Actions → run → ⋯ → Delete workflow run).
