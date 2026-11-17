# StockHunter AI 3.4 — optimisation du paper trading et validation des stratégies

Branche `stockhunter-v3-4-paper-optimization`, sans fusion ni déploiement. Budget : 0 €. Aucun ordre réel.
Aucune donnée supprimée en production, aucune position existante modifiée, aucun paramètre de stratégie changé.

## 1. Audit de production (lecture seule, 17/11/2026)
`stockhunter paper audit` (workflow « Diagnostic de la base », exécution 44161275980) recalcule tout depuis le
journal immuable des événements et le compare à la table `paper_trades` et au tableau de bord.

| Grandeur | Tableau de bord | Recalcul depuis le journal |
|---|---|---|
| Capital | 5 000 $ | 5 000 $ (réglage `paper_config`) |
| Valeur | 5 011,18 $ | 5 011,18 $ |
| Réalisé net | −0,51 $ | −0,51 $ (uniquement les frais d'entrée de PG) |
| Latent | +11,69 $ | +11,69 $ (3 × (173,02 − 169,12) au 16/11) |
| Investi / trésorerie | — | 507,36 $ / 4 492,12 $ |

PG : 3 actions à 169,12 $ le 12/11 ; risque par action 6,68 $ → 3 actions (budget 25 $). Stop relevé le 16/11 à
163,00 $ = plus haute clôture 173,02 − 3 × ATR 3,34 : stop suiveur conforme, jamais abaissé. TP1 179,15, TP2 189,17.

Le journal, la table et le tableau de bord concordent. Les anomalies relevées :

1. **« 8 trades »** : le tableau de bord additionnait plans actifs (2), positions (1) et plans annulés (5).
   Un plan n'est pas un trade : il y avait une seule exécution (l'achat de PG).
2. **Refus répétitifs** : MA a été refusée 3 fois et LMT 2 fois. Le motif était chaque fois « risque par position
   trop faible pour une action », alors que le risque par action atteignait 33,8 à 44 $ pour un budget de 25 $.
   Cause : le risque n'était contrôlé qu'à l'exécution, et un nouveau plan était recréé à chaque signal dès que le
   précédent avait été annulé.
3. **Taille hors frais** : la quantité ignorait les frais et le glissement. Le risque réel pouvait donc dépasser
   0,5 % de quelques centimes.
4. **Telegram** : une alerte partait pour chaque événement, y compris pour la création puis l'annulation du même
   plan à l'identique (14 alertes « trade »), et 3 textes d'alerte étaient en double.

## 2. Corrections
- **Une seule formule de taille** (`trading/rules.py`), appliquée au plan, à l'exécution et au laboratoire :
  - `risque_par_action = écart du stop + frais d'entrée + (frais + glissement) à la sortie au stop` ;
  - `quantité_max_risque = floor(budget_risque / risque_par_action_tous_frais_compris)` ;
  - `quantité_exécutable = min(quantité_max_risque, quantité_max_achat, limites)`.

  La quantité ne dépasse jamais le budget de risque. Pas de levier.
- **Éligibilité au moment du plan** (`paper.evaluate`). Les contrôles se font dans cet ordre :
  1. risque ;
  2. nombre de positions et de plans actifs ;
  3. secteur ;
  4. corrélation sur 60 séances jusqu'au signal inclus ;
  5. capital disponible, net de ce que réservent les plans actifs.

  Statuts possibles : ÉLIGIBLE, NON ÉLIGIBLE — RISQUE / CAPITAL / EXPOSITION / CORRÉLATION, EN ATTENTE et
  DONNÉES INSUFFISANTES. Le classement du scanner n'est pas modifié : l'éligibilité est un filtre de faisabilité.
- **Journal immuable `paper_decisions`** (migration 0014, additive) :
  - une ligne par couple (scan, action) ;
  - une empreinte sur symbole, stratégie, statut, cours, risque par action, budget, capital, paramètres et
    composition du portefeuille, à 5 % près ;
  - `repeat_of` pour un refus identique, qui ne crée alors ni nouveau plan ni nouvelle alerte ;
  - une réévaluation automatique dès qu'une de ces grandeurs change.

  Une opportunité vraiment nouvelle n'est jamais bloquée par un ancien refus.
- **TP partiel avec 1 à 3 actions** : la quantité vendue est un nombre entier (`floor(n/2)`). Avec une seule
  action, la règle annoncée dès le plan est la vente totale à TP1, et le journal l'écrit explicitement. Aucune
  fraction d'action n'est jamais simulée.
- **Telegram** :
  - l'alerte de plan donne le dernier cours daté (« pas un cours en direct »), la zone d'entrée, le stop, TP1 et
    TP2, la quantité, le risque estimé frais compris, le statut d'éligibilité et la mention « stratégie NON
    validée » ;
  - une opportunité non éligible donne lieu à une seule alerte par jeu de conditions, donc des alertes
    continuent de partir même quand rien n'est éligible.
- **Tableau de bord** :
  - colonne « Éligible avec mon capital », avec la quantité exécutable, le risque maximal en dollars et le motif ;
  - cartes séparées pour les plans, les positions, les exécutions, les frais et les opportunités évaluées ;
  - table des décisions filtrable par date, action, stratégie, statut et motif ;
  - statistiques prospectives et leurs limites.

  Les rapports en ligne de commande séparent eux aussi les plans et les exécutions.
- **Validation scientifique** (`paper.stats`) :
  - indicateurs : trades clôturés, taux de réussite, gain moyen, perte moyenne, espérance en R, profit factor,
    drawdown maximal, rendement annualisé (seulement au-delà d'un an), durée moyenne, exposition moyenne et
    performance nette face à SPY ;
  - en dessous de 30 trades clôturés : « aucune conclusion possible ».

  Aucune stratégie n'est validée à ce jour. Aucun paramètre n'a été changé.

## 3. Avant / après sur les mêmes données
Workflow « Comparaison avant/après 3.4 (copie jetable) », exécution 44165467870 :
1. `pg_dump` de la production en lecture seule ;
2. restauration dans un PostgreSQL éphémère ;
3. rejeu chronologique des scans du 11, 12, 13 et 16/11 avec le code 3.4 (`process(until=scan)` puis
   `plan_from_scan`).

La production n'a pas été modifiée, et la commande refuse toute base qui n'est pas locale.

| Mesure | Avant (production) | Après (3.4, mêmes données) |
|---|---|---|
| Plans générés | 8 | 2 (PG exécuté, UNH actif) |
| Plans créés puis refusés ou annulés | 5 | 0 |
| Refus répétitifs (plans recréés pour rien) | 5 (MA ×3, LMT ×2) | 0 — supprimés |
| Évaluations tracées | 0 | 10 : 2 ÉLIGIBLE, 6 NON ÉLIGIBLE — RISQUE (dont 3 répétitions sans plan), 2 EN ATTENTE |
| Positions ouvertes | 1 (PG, 3 actions) | 1 (PG, 3 actions, identique) |
| Trades clôturés | 0 | 0 |
| Capital utilisé | 507,36 $ | 507,36 $ |
| Risque initial moyen par position | 20,05 $ | 20,05 $ |
| Frais | 0,51 $ | 0,51 $ |
| Résultat net réalisé / latent | −0,51 $ / +11,69 $ | −0,51 $ / +11,69 $ |
| Alertes Telegram « trade » | 14 | 6 |
| Stratégies validées | 0 | 0 |

Le résultat financier est identique. Cela confirme que la correction ne fabrique pas de performance : elle
supprime le bruit (plans voués au refus et alertes répétées) et rend chaque refus explicite et chiffré.

## 4. Tests
`tests/test_paper_v34.py` couvre les 13 cas exigés, plus le rejeu et `until` :
1. trop chère pour le risque ;
2. abordable et éligible ;
3. écart entre plan et exécution ;
4. stop déplacé ;
5. TP partiel avec 1, 2 et 3 actions ;
6. refus répétitifs ;
7. redevenue éligible après un changement de cours ou de capital ;
8. données manquantes ;
9. continuité des positions et des plans d'avant 3.4 ;
10. frais et capital réservé ;
11. cohérence journal / tableau de bord / Telegram ;
12. résultat identique après redémarrage ou rejeu ;
13. pas de look-ahead : des séances futures corrélées ne changent pas la décision.

Suite complète : **282 tests réussis sur SQLite et sur PostgreSQL 16** (`SH_TEST_DATABASE_URL`). Migrations
vérifiées sur une base PostgreSQL vierge, puis `alembic check` : aucune différence (même résultat sur SQLite).

## 5. Compatibilité et mise en production (après accord d'Ilias)
- La migration 0014 crée seulement la table `paper_decisions`. Elle ne touche aucune table existante.
- Les plans actifs créés avant 3.4 (MA n° 7, UNH n° 8) seront exécutés avec la nouvelle formule de taille. MA
  sera refusée à l'ouverture pour le même motif, désormais chiffré.
- PG reste suivie sans aucune réécriture.
- Railway : le déploiement actif est toujours `main` 8efc5c3 (PR n° 7, SUCCESS). Rien n'a été déployé.
- Avant de fusionner, faire une sauvegarde vérifiée (workflow « Sauvegarde de la base »).
