# Mise en production de StockHunter 3.0

Rien n'est déployé tant que les PR ne sont pas fusionnées : Railway redéploie automatiquement à chaque fusion sur
`main`, et les workflows planifiés utilisent `main`.

## Ordre conseillé
1. **PR #2 « Correctif production »** (disque, alertes) — à fusionner d'abord, elle répare l'incident du 13/11.
   Puis Actions → **Entretien de la base** → Run workflow (libère ≈ 170 Mo tout de suite).
2. **PR « StockHunter 3.0 »** (`stockhunter-v3` → `main`), qui contient déjà le correctif.

## Ce qui se passe à la fusion de la PR 3.0
- Railway redémarre le site ; `stockhunter init-db` applique les migrations 0008 et 0009 :
  **additives uniquement** (2 colonnes, 4 tables). Aucune donnée supprimée, aucune table modifiée autrement.
  Les positions simulées déjà ouvertes sont reprises (séances détenues et risque initial recalculés).
- Le workflow nocturne passe à `stockhunter pipeline nightly`. Un échec critique rend le job rouge (GitHub envoie
  un e-mail) et part en alerte Telegram.
- Première nuit : le scanner large s'exécute (45 s), les paniers prospectifs v3 sont figés, la page Santé se remplit.

## Vérifications après fusion
| Quoi | Où | Attendu |
|---|---|---|
| Site | `/health` puis connexion | pages Marché, Scanner, Laboratoire, Santé accessibles |
| Migrations | journaux Railway | « Base migrée. » |
| Chaîne | Actions → Ingestion quotidienne | étape « Chaîne nocturne » avec le bilan des 11 étapes |
| Santé | dashboard → Santé | chaîne OK ou partielle, base < 60 % du volume |
| Telegram | `/scanner`, `/signal SYMBOLE`, `/status` | réponse en quelques secondes ; plus d'erreur 409 dans les journaux |

## Retour arrière
- Code : annuler la fusion (`Revert` sur GitHub) ; Railway redéploie l'ancienne version. Les colonnes et tables
  ajoutées restent en place sans gêner l'ancien code.
- Réglage WAL : `ALTER SYSTEM RESET max_wal_size; ALTER SYSTEM RESET min_wal_size; SELECT pg_reload_conf();`

## Coûts et limites
- Aucun service payant ajouté. Données : plans gratuits (≈ 60 appels/nuit pour la découverte : SEC et Finnhub).
- GitHub Actions : 4 créneaux nocturnes × ≈ 10 min. Dépôt public : illimité. Dépôt privé : ≈ 1 100 min/mois sur
  2 000 gratuites.
- Railway : volume 0,5 Go, crédit 1 $/mois (site arrêté une fois le crédit épuisé).

## À faire par Ilias (non automatisable ou irréversible)
1. Fusionner les deux PR dans l'ordre ci-dessus.
2. Passer le dépôt en privé, ou m'autoriser à supprimer les 8 anciens artefacts de données.
3. Décider de l'expérience « historique long » (rapport v2 → v3, prochaines expériences, point 3).
