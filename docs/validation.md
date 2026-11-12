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

## Résultats v2.0.0 — verdict final (12/11/2026)
Rapport complet : `docs/reports/2026-11-12-validation-v2.0.0-final.md` (historique 8-K complet, période réservée
évaluée une seule fois, méthode figée).

**Aucune stratégie n'est validée. Le score global n'a pas de pouvoir prédictif démontré.**

| Groupe | Développement : vs univers 20 j (t) | Période réservée : vs univers 20 j | Verdict |
|---|---|---|---|
| READY | −0,00 % (−0,20) | −0,01 % | reste en RESEARCH |
| Top 5 du score | +0,16 % (0,47) | −0,27 % | reste en RESEARCH |
| A Early momentum | −0,63 % (−2,12) | −0,29 % | **rejetée** |
| B Earnings momentum | −0,14 % (−0,71) | +0,67 % | reste en RESEARCH |
| C Growth acceleration | +0,61 % (1,49) | −0,70 % | reste en RESEARCH (échoue sur la période réservée) |
| D Sector rotation | −0,05 % (−0,95) | −0,19 % | reste en RESEARCH |
| Règle simple momentum 12-1 | +0,74 % (1,52) | +1,05 % | référence |

- IC du score à 20 j : 0,003 (développement), 0,001 (période réservée).
- Diagnostic facteur par facteur : aucun ingrédient n'a |t| ≥ 2. Les moins faibles (volatilité élevée, P/E élevé)
  traduisent un effet bêta du marché haussier 2017-2024, pas un avantage reproductible : ils ne sont **pas**
  retenus pour une v2.1 (ce serait du sur-ajustement).
- Conséquence : chaque classement affiche « AUCUNE stratégie validée — informatif, pas un signal fiable ».
