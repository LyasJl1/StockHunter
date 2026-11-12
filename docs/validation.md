# Protocole de validation (phase 3)

Commande : `stockhunter validate [--out rapport.md] [--signals-cache f.pkl] [--holdout]`
Workflow GitHub : **Actions → Validation → Run workflow** (rapport dans le résumé du run et en artefact).

## Principe
Le moteur de scoring **réel** (le même code que le scan quotidien, vérifié par `test_replay_matches_live_scan`)
est rejoué chaque semaine sur l'historique, en ne voyant que les données connues à chaque date
(`DataStore`, vérifié par `test_no_look_ahead`). On mesure ensuite ce qui s'est passé après chaque signal.

## Règles
| Point | Règle |
|---|---|
| Exécution | signal à la clôture J, entrée à l'ouverture J+1 |
| Prix | ajustés dividendes et splits (rendement total) |
| Frais | 10 pb par côté ; sensibilité 0 / 10 / 25 pb |
| Sorties | stop 2×ATR, stop suiveur 3×ATR, 40 séances max ; gap sous le stop exécuté à l'ouverture ; trades non terminés exclus |
| Références | SPY, **univers équipondéré** (même biais de sélection), règle simple momentum 12-1 |
| Statistiques | moyennes par date, t de Newey-West (fenêtres qui se chevauchent) |
| Stabilité | résultats par année (walk-forward sans réglage : aucune optimisation n'est faite) |
| Période réservée | 12/11/2024 → aujourd'hui, zone tampon de 90 jours ; évaluée **une fois** par version, méthode figée ; chaque consultation est tracée et une 2e est refusée sauf `--allow-holdout-again` (signalé dans le rapport) |

## Critères de décision (fixés avant les résultats)
Une stratégie devient **éligible au paper trading** seulement si :
1. au moins 100 trades simulés ;
2. espérance nette de frais > 0 ;
3. surperformance de l'univers équipondéré à 20 jours avec t ≥ 2 ;
4. surperformance positive au moins 60 % des années ;
5. surperformance confirmée sur la période réservée.
Une stratégie dont la sous-performance est significative (t ≤ −2) est **rejetée**.

## Limites
- **Biais du survivant** : l'univers est choisi parmi les grandes capitalisations d'aujourd'hui. Les rendements
  absolus sont surestimés (univers équipondéré : ~25 %/an simulés) ; seule la comparaison à l'univers est pertinente.
- 42 actions : faible diversité, résultats sensibles à quelques titres (NVDA, LLY…).
- Pas de spread mesuré (estimé) ; pas de données intrajournalières.

## Résultats v2.0.0 — période de développement (rapport du 12/11/2026)
Voir `docs/reports/2026-11-12-validation-v2.0.0-developpement.md`. Historique 8-K incomplet (71 %) au moment du calcul.
- Le **score global n'a pas de pouvoir prédictif démontré** (IC ≈ 0 à 5, 20 et 60 jours).
- READY, Top 5, B et D : ne battent pas l'univers équipondéré → restent en RESEARCH.
- **A Early momentum : rejetée** (t = −2,1, négative chaque année).
- **C Growth acceleration : la plus prometteuse** (+0,67 % / 20 j vs univers, positive 7 années sur 8) mais t = 1,69 < 2.
- La règle simple « top 5 momentum 12-1 » fait aussi bien que C (t = 1,5) : le moteur ne bat pas encore une règle simple.
