# Validation historique — stockhunter-multifactor 2.0.0 (run #1)

> Résultats **simulés** sur données passées, frais inclus. Ils ne garantissent aucun résultat futur.
> Un score n'est pas une probabilité de hausse.

## Verdicts

| Stratégie / groupe | Décision | Critères |
|---|---|---|
| READY | **RESTE EN RESEARCH** | ✅ ≥ 100 trades (162) ; ✅ espérance nette > 0 (après frais) (0.0103) ; ❌ bat l'univers EW à 20 j (t ≥ 2.0) (-0.2586) ; ❌ positif ≥ 60% des années (0.4286) |
| DEVELOPING ou READY | **RESTE EN RESEARCH** | ✅ ≥ 100 trades (1096) ; ✅ espérance nette > 0 (après frais) (0.0148) ; ❌ bat l'univers EW à 20 j (t ≥ 2.0) (-0.9451) ; ❌ positif ≥ 60% des années (0.375) |
| Top 5 du score | **RESTE EN RESEARCH** | ✅ ≥ 100 trades (544) ; ✅ espérance nette > 0 (après frais) (0.0199) ; ❌ bat l'univers EW à 20 j (t ≥ 2.0) (0.192) ; ❌ positif ≥ 60% des années (0.5) |
| A Early momentum | **REJETÉE (sous-performe significativement)** | ✅ ≥ 100 trades (798) ; ✅ espérance nette > 0 (après frais) (0.009) ; ❌ bat l'univers EW à 20 j (t ≥ 2.0) (-2.1222) ; ❌ positif ≥ 60% des années (0.0) |
| B Earnings momentum | **RESTE EN RESEARCH** | ✅ ≥ 100 trades (204) ; ✅ espérance nette > 0 (après frais) (0.0128) ; ❌ bat l'univers EW à 20 j (t ≥ 2.0) (-0.4713) ; ❌ positif ≥ 60% des années (0.1429) |
| C Growth acceleration | **RESTE EN RESEARCH** | ✅ ≥ 100 trades (464) ; ✅ espérance nette > 0 (après frais) (0.0234) ; ❌ bat l'univers EW à 20 j (t ≥ 2.0) (1.6872) ; ✅ positif ≥ 60% des années (0.875) |
| D Sector rotation | **RESTE EN RESEARCH** | ✅ ≥ 100 trades (827) ; ✅ espérance nette > 0 (après frais) (0.018) ; ❌ bat l'univers EW à 20 j (t ≥ 2.0) (-0.9512) ; ❌ positif ≥ 60% des années (0.375) |

## Périmètre et limites

- Rejeu du moteur de scoring réel tous les 5 jours de bourse, du 2017-11-27 au 2026-11-10 (451 dates, 42 actions).
- Période de développement : jusqu'au 2024-08-09 (zone tampon de 90 jours avant la période réservée qui commence le 2024-11-12).
- Période réservée (holdout) : NON évaluée (réservée pour une méthode figée)
- Entrée à l'ouverture du lendemain du signal ; frais 10 pb par côté (sensibilité 0, 10, 25 pb) ; prix ajustés (dividendes, splits).
- Sorties testées : stop initial 2×ATR, stop suiveur 3×ATR sous le plus haut, durée max 40 séances ; un gap sous le stop est exécuté à l'ouverture.
- t-stats : moyennes par date, écart-type de Newey-West (fenêtres qui se chevauchent).
- ⚠️ **Biais du survivant** : l'univers = 42 sociétés qui sont de grandes capitalisations aujourd'hui. Leur rendement passé est donc surestimé ; la référence pertinente est l'**univers équipondéré**, qui porte le même biais (colonne « vs univers »).
- Historique des publications de résultats (8-K) complet sur 71,2 % des couples action/date en moyenne ; la stratégie B n'est évaluée que là où il l'est.

## Rendements après signal — developpement

| Groupe | Signaux | Rdt moyen 20 j | % gagnants | vs SPY 20 j | vs univers 20 j | t | vs univers 60 j | t |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| READY | 328 | +1,23 % | 59,5 % | +0,63 % | -0,08 % | -0,26 | +0,04 % | 0,05 |
| DEVELOPING ou READY | 3912 | +1,69 % | 59,9 % | +0,65 % | -0,17 % | -0,95 | +0,11 % | -0,18 |
| Top 5 du score | 1690 | +1,97 % | 60,5 % | +0,91 % | +0,07 % | 0,19 | +0,59 % | 0,64 |
| A Early momentum | 2132 | +0,86 % | 57,2 % | +0,22 % | -0,63 % | -2,12 | -1,15 % | -1,33 |
| B Earnings momentum | 697 | +1,56 % | 61,4 % | +0,45 % | -0,34 % | -0,47 | +0,12 % | 0,48 |
| C Growth acceleration | 2061 | +2,66 % | 62,5 % | +1,40 % | +0,67 % | 1,69 | +1,53 % | 1,48 |
| D Sector rotation | 2554 | +2,10 % | 60,6 % | +0,85 % | -0,05 % | -0,95 | +1,14 % | 0,44 |
| Règle simple : top 5 momentum 12-1 | 1690 | +2,65 % | 59,6 % | +1,58 % | +0,74 % | 1,52 | +2,27 % | 1,46 |
| Référence : toutes les actions (univers EW) | 14196 | +1,90 % | 59,6 % | +0,84 % | -0,00 % | N/D | -0,00 % | N/D |

Pouvoir prédictif du score (corrélation de rang score / rendement futur, par date) :

| Horizon | IC moyen | t | % de dates IC > 0 |
|---:|---:|---:|---:|
| 5 j | 0,012 | 0,83 | 54,1 % |
| 20 j | -0,005 | -0,22 | 52,7 % |
| 60 j | 0,004 | 0,10 | 51,8 % |

### Trades simulés (developpement, frais 10 pb/côté)

| Groupe | Trades | % gagnants | Rdt net moyen | Profit factor | Espérance (R) | Durée moy. | Rdt net à 0 / 25 pb |
|---|---:|---:|---:|---:|---:|---:|---|
| READY | 162 | 43,2 % | +1,03 % | 1,44 | 0,24 | 20,4 | +1,23 % / +0,73 % |
| DEVELOPING ou READY | 1096 | 42,0 % | +1,48 % | 1,57 | 0,29 | 20,6 | +1,68 % / +1,18 % |
| Top 5 du score | 544 | 43,8 % | +1,99 % | 1,78 | 0,34 | 21,7 | +2,19 % / +1,69 % |
| A Early momentum | 798 | 38,4 % | +0,90 % | 1,40 | 0,22 | 19,4 | +1,10 % / +0,60 % |
| B Earnings momentum | 204 | 45,1 % | +1,28 % | 1,42 | 0,30 | 22,8 | +1,48 % / +0,98 % |
| C Growth acceleration | 464 | 43,5 % | +2,34 % | 1,79 | 0,37 | 21,7 | +2,54 % / +2,04 % |
| D Sector rotation | 827 | 43,4 % | +1,80 % | 1,67 | 0,32 | 20,6 | +2,00 % / +1,50 % |
| Règle simple : top 5 momentum 12-1 | 479 | 41,1 % | +2,18 % | 1,64 | 0,29 | 20,3 | +2,38 % / +1,88 % |

### Portefeuille simulé (developpement) : max 10 positions de 10%

| Portefeuille | Rdt annualisé | Drawdown max | Volatilité | Sharpe | Sortino |
|---|---:|---:|---:|---:|---:|
| READY | +2,30 % | -7,12 % | 4,5 % | 0,53 | 0,64 |
| DEVELOPING ou READY | +12,92 % | -19,97 % | 16,2 % | 0,83 | 1,07 |
| Top 5 du score | +14,35 % | -15,92 % | 13,0 % | 1,10 | 1,52 |
| A Early momentum | +9,47 % | -17,79 % | 10,9 % | 0,88 | 1,16 |
| B Earnings momentum | +3,53 % | -16,92 % | 7,4 % | 0,50 | 0,59 |
| C Growth acceleration | +14,62 % | -21,62 % | 14,2 % | 1,03 | 1,35 |
| D Sector rotation | +14,48 % | -25,80 % | 16,1 % | 0,92 | 1,23 |
| Règle simple : top 5 momentum 12-1 | +13,50 % | -24,30 % | 16,2 % | 0,86 | 1,19 |
| Référence : SPY | +14,55 % | -33,70 % | 19,5 % | 0,80 | 0,97 |
| Référence : Univers équipondéré | +25,19 % | -31,93 % | 20,9 % | 1,18 | 1,44 |

## Stabilité par année (developpement) — rendement 20 j vs univers

| Groupe | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| READY | +2,64 % | -0,00 % | -0,39 % | +0,94 % | -1,17 % | +2,69 % | -1,10 % | +0,39 % |
| DEVELOPING ou READY | +0,00 % | +0,07 % | -0,18 % | +0,47 % | -0,60 % | -0,68 % | -0,56 % | +0,28 % |
| Top 5 du score | +1,43 % | -0,32 % | -0,13 % | +1,26 % | -1,15 % | +0,90 % | -0,52 % | +0,51 % |
| A Early momentum | -0,98 % | -0,15 % | -0,14 % | -0,46 % | -0,53 % | -1,08 % | -2,04 % | -0,45 % |
| B Earnings momentum | +2,80 % | +3,65 % | -0,09 % | -1,15 % | -0,56 % | -1,03 % | -0,54 % | -1,28 % |
| C Growth acceleration | +3,02 % | +0,78 % | -0,08 % | +1,87 % | +0,11 % | +0,17 % | +1,07 % | +1,64 % |
| D Sector rotation | -0,40 % | -0,59 % | -0,52 % | +0,80 % | +0,08 % | -0,81 % | +0,64 % | -0,94 % |
| Règle simple : top 5 momentum 12-1 | +3,49 % | -0,58 % | +0,34 % | +4,49 % | -2,36 % | +0,97 % | +0,76 % | +1,81 % |

## Par régime de marché (developpement) — rendement 20 j

| Groupe | Régime | Signaux | Rdt moyen | vs univers | % gagnants |
|---|---|---:|---:|---:|---:|
| READY | NEUTRE | 73 | +0,44 % | +0,05 % | 58,9 % |
| READY | RISK-ON | 255 | +1,45 % | -0,12 % | 59,6 % |
| Top 5 du score | NEUTRE | 350 | +2,14 % | +0,70 % | 63,1 % |
| Top 5 du score | RISK-OFF | 220 | +2,61 % | -1,78 % | 62,7 % |
| Top 5 du score | RISK-ON | 1120 | +1,79 % | +0,24 % | 59,3 % |
