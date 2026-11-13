# StockHunter 3.1 — stabilisation (13/11/2026)

Branche `stockhunter-v3-1-fixes`, à partir de `main` @ 9bdbc32. Chaque constat de l'audit a été vérifié sur l'état réel
avant correction ; chaque correction a un test ou une exécution réelle comme preuve.

## Constats confirmés
1. **PostgreSQL à 99 %** : diagnostic en lecture seule (run 43429665721) — données 182 Mo (toutes bases), **WAL
   240 Mo (15 segments)**, aucun slot de réplication, aucun fichier temporaire ; `max_wal_size = 1024 MB` : le
   plafond du correctif du 13/11 n'avait jamais été appliqué (aucune collecte depuis sa fusion). Le volume compte
   aussi ≈ 89 Mo hors répertoire de données (système de fichiers).
2. **Workflow nocturne non branché** : la fusion de la PR #3 a conservé l'ancien `ingest.yml` (commandes séparées,
   `|| echo`) ; l'étape finale testait `steps.pipeline.outcome` alors qu'aucune étape n'avait l'id `pipeline` :
   **le job ne pouvait jamais échouer**.
3. Le jeton GitHub des workflows n'était pas restreint ; le workflow de génération du hachage publiait celui-ci
   dans un résumé lisible par tous (dépôt public).

## Corrections et preuves
| Élément | Avant | Après | Preuve |
|---|---|---|---|
| Espace PostgreSQL | 496 / 500 Mo (99 %), erreurs « No space left on device » | **≈ 350 / 500 Mo (70 %)** après une collecte complète ; WAL plafonné à 64 Mo | runs 43430847543 (sauvegarde vérifiée), 43433889186 (WAL 14 → 4 segments, 160 Mo rendus), 43436882699 (diagnostic après collecte) |
| Pipeline GitHub | ancienne chaîne, échecs masqués, condition d'échec morte | `stockhunter pipeline nightly`, étape `id: pipeline`, échec critique = job rouge ; séance déjà traitée = rien refait | `tests/test_workflows.py` (lit le YAML réel) ; run 43434452175 : 12 étapes OK |
| Scanner multi-actions | jamais exécuté en production | exécuté sur données réelles et enregistré en base de production | run 43434452175 : « Découverte du 2026-11-12 : univers 2722 titres » |
| Actions réellement analysées | 0 en production | **2 722** négociables (sur ≈ 6 500 actions ordinaires stockées) | idem |
| Signaux enregistrés | 0 | **50** (5 catégories, 109 à 195 titres admis par catégorie, 10 affichés au plus) | idem ; 12 approfondis (SEC, actualités) sans erreur |
| Suivi des résultats | — | 50 suivis, 0 finalisé (trop récents) ; mesures complètes (exécutés, annulés, R, drawdown en R, vs univers, vs SPY, par catégorie et par mois) | `test_performance_metrics_come_from_simulated_trades_and_are_consistent` |
| Telegram | 409 jusqu'au 12/11 ; 41 alertes le 13/11 | bail unique (aucun 409 dans les journaux Railway depuis le déploiement 3.0) ; 18 anciennes alertes presse retirées ; rapport envoyé | journaux Railway 12 h 35 → 12 h 40 ; run 43434452175 « envoyes = 3, echecs = 0, ignores = 18 » ; tests de relecture et de concurrence (4 lecteurs simultanés → 1 seul) |
| Tests réussis | 141 | **184** (SQLite et PostgreSQL 16) | `pytest` |
| Dashboard | page Santé sans état de fonctionnement | 10 contrôles explicites (service, base, disque, fraîcheur, collecte, chaîne, scanner, signal, notification, erreurs) ; mesures des signaux au Laboratoire | `test_v3_pages_render_with_discovery_data` |
| Coût mensuel estimé | Railway gratuit (1 $ de crédit, ≈ 1,9 $ consommés) ; données gratuites | **inchangé** ; GitHub Actions gratuit (dépôt public) ; historique long : 0 € | aucune ressource créée |
| Stratégies validées | 0 | **0** | `stockhunter/lab/registry.json` |

## Autres corrections
- Seuils de stockage **80 / 90 / 95 %** : étape critique « stockage » en tête de chaîne ; alerte Telegram dès 80 % ;
  à 95 % la chaîne s'arrête avant d'écrire (seule l'alerte part) — test `test_critical_storage_halts_writes_but_still_alerts`.
- Entretien sûr : plafond WAL + `pg_switch_wal()`/`CHECKPOINT` (PostgreSQL supprime lui-même les segments ; aucune
  suppression manuelle) ; VACUUM manuel retiré (il avait échoué faute de place) ; incident reproduit en test.
- Sauvegarde **vérifiée par restauration complète** (table par table) avant toute intervention, et chaque samedi ;
  conservée dans le cache Actions, jamais publiée.
- Rétention étendue aux seules données techniques (articles > 180 j, journal d'envois > 90 j, analyses
  approfondies obsolètes) ; test garantissant que prix, scans, signaux, trades et résultats ne sont jamais touchés.
- Créneaux de secours sans double collecte : une séance entièrement traitée n'est pas refaite ; une séance partielle
  (quota) est reprise au créneau suivant.
- Sécurité : `permissions: contents: read` sur tous les workflows ; hachage du mot de passe refusé sur dépôt public ;
  dépendances du projet auditées en environnement vierge (pip-audit) : aucune vulnérabilité connue.
- Scanner : tests de reproductibilité, de split (aucun faux signal), de radiation (signal finalisé au dernier cours).
- Historique long : module séparé (Parquet zstd, hors PostgreSQL), plafond de titres distincts par mois, reprise ;
  essai réel sur 3 titres déjà suivis (aucun quota consommé) : SPY, AAPL, JPM — 3 236 séances 2014 → 2026,
  dividendes et splits présents (run 43436883031).

## État
| Catégorie | Éléments |
|---|---|
| Corrigé et vérifié en production | espace PostgreSQL (70 %, WAL plafonné) ; chaîne complète exécutée sur la production depuis la branche ; scanner et signaux en base ; Telegram sans 409 ; sauvegarde vérifiée |
| Implémenté, pas encore vérifié en production | exécution **planifiée** de la chaîne (après fusion) ; seuils d'alerte de stockage (aucun dépassement depuis) ; page Santé 3.1 ; mesures des signaux (aucun signal finalisé avant ≈ 20 séances) ; rétention étendue |
| Partiellement corrigé | croissance de la base : ≈ +14 Mo pour la première collecte complète ; à mesurer sur plusieurs nuits (page Santé) |
| Bloqué par une autorisation | suppression des 8 artefacts publics (≈ 330 Mo de données sous licence) et du run contenant le hachage ; passage du dépôt en privé ; collecte de l'historique long au-delà de l'essai (quota mensuel Tiingo) |
| Non réalisé | fondamentaux et secteurs pour l'univers large (aucune source gratuite) ; nouvelles hypothèses de recherche exécutées (rien ne doit être testé avant un pré-enregistrement et un historique long) |

## Prochaines actions
1. Fusionner la PR 3.1 ; vérifier le lendemain la page Santé (10 contrôles verts) et le run « Ingestion ».
2. Autoriser (ou non) : suppression des artefacts publics / passage en privé ; plafond mensuel de l'historique long
   (ex. 300 nouveaux titres/mois → ≈ 1 200 titres sur 4 mois, ≈ 270 Mo de Parquet dans le cache, 0 €).
3. Recherche : pré-enregistrer les hypothèses sur l'historique long (plusieurs régimes, dividendes inclus) avant
   toute exécution.
