# Validation historique — stockhunter-multifactor 2.0.0 (run #2)

> Résultats **simulés** sur données passées, frais inclus. Ils ne garantissent aucun résultat futur.
> Un score n'est pas une probabilité de hausse.

## Verdicts

| Stratégie / groupe | Décision | Critères |
|---|---|---|
| READY | **RESTE EN RESEARCH** | ✅ ≥ 100 trades (222) ; ✅ espérance nette > 0 (après frais) (0.0031) ; ❌ bat l'univers EW à 20 j (t ≥ 2.0) (-0.204) ; ❌ positif ≥ 60% des années (0.5) ; ❌ holdout : bat l'univers EW (-0.0001) |
| DEVELOPING ou READY | **RESTE EN RESEARCH** | ✅ ≥ 100 trades (1144) ; ✅ espérance nette > 0 (après frais) (0.0149) ; ❌ bat l'univers EW à 20 j (t ≥ 2.0) (-0.7194) ; ❌ positif ≥ 60% des années (0.375) ; ❌ holdout : bat l'univers EW (-0.004) |
| Top 5 du score | **RESTE EN RESEARCH** | ✅ ≥ 100 trades (540) ; ✅ espérance nette > 0 (après frais) (0.0197) ; ❌ bat l'univers EW à 20 j (t ≥ 2.0) (0.4721) ; ✅ positif ≥ 60% des années (0.625) ; ❌ holdout : bat l'univers EW (-0.0027) |
| A Early momentum | **REJETÉE (sous-performe significativement)** | ✅ ≥ 100 trades (798) ; ✅ espérance nette > 0 (après frais) (0.009) ; ❌ bat l'univers EW à 20 j (t ≥ 2.0) (-2.1222) ; ❌ positif ≥ 60% des années (0.0) ; ❌ holdout : bat l'univers EW (-0.0029) |
| B Earnings momentum | **RESTE EN RESEARCH** | ✅ ≥ 100 trades (319) ; ✅ espérance nette > 0 (après frais) (0.0156) ; ❌ bat l'univers EW à 20 j (t ≥ 2.0) (-0.7104) ; ❌ positif ≥ 60% des années (0.375) ; ✅ holdout : bat l'univers EW (0.0067) |
| C Growth acceleration | **RESTE EN RESEARCH** | ✅ ≥ 100 trades (484) ; ✅ espérance nette > 0 (après frais) (0.0235) ; ❌ bat l'univers EW à 20 j (t ≥ 2.0) (1.4942) ; ✅ positif ≥ 60% des années (0.75) ; ❌ holdout : bat l'univers EW (-0.007) |
| D Sector rotation | **RESTE EN RESEARCH** | ✅ ≥ 100 trades (827) ; ✅ espérance nette > 0 (après frais) (0.018) ; ❌ bat l'univers EW à 20 j (t ≥ 2.0) (-0.9512) ; ❌ positif ≥ 60% des années (0.375) ; ❌ holdout : bat l'univers EW (-0.0019) |

## Périmètre et limites

- Rejeu du moteur de scoring réel tous les 5 jours de bourse, du 2017-11-27 au 2026-11-10 (451 dates, 42 actions).
- Période de développement : jusqu'au 2024-08-09 (zone tampon de 90 jours avant la période réservée qui commence le 2024-11-12).
- Période réservée (holdout) : **évaluée dans ce rapport**
- Entrée à l'ouverture du lendemain du signal ; frais 10 pb par côté (sensibilité 0, 10, 25 pb) ; prix ajustés (dividendes, splits).
- Sorties testées : stop initial 2×ATR, stop suiveur 3×ATR sous le plus haut, durée max 40 séances ; un gap sous le stop est exécuté à l'ouverture.
- t-stats : moyennes par date, écart-type de Newey-West (fenêtres qui se chevauchent).
- ⚠️ **Biais du survivant** : l'univers = 42 sociétés qui sont de grandes capitalisations aujourd'hui. Leur rendement passé est donc surestimé ; la référence pertinente est l'**univers équipondéré**, qui porte le même biais (colonne « vs univers »).
- Historique des publications de résultats (8-K) complet sur 96,4 % des couples action/date en moyenne ; la stratégie B n'est évaluée que là où il l'est.

## Rendements après signal — developpement

| Groupe | Signaux | Rdt moyen 20 j | % gagnants | vs SPY 20 j | vs univers 20 j | t | vs univers 60 j | t |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| READY | 451 | +1,05 % | 58,5 % | +0,82 % | -0,00 % | -0,20 | +0,19 % | 0,31 |
| DEVELOPING ou READY | 4078 | +1,80 % | 60,4 % | +0,72 % | -0,13 % | -0,72 | +0,22 % | 0,01 |
| Top 5 du score | 1690 | +2,06 % | 61,6 % | +0,99 % | +0,16 % | 0,47 | +0,47 % | 0,58 |
| A Early momentum | 2132 | +0,86 % | 57,2 % | +0,22 % | -0,63 % | -2,12 | -1,15 % | -1,33 |
| B Earnings momentum | 1043 | +1,75 % | 61,7 % | +0,77 % | -0,14 % | -0,71 | +0,74 % | 0,98 |
| C Growth acceleration | 2128 | +2,65 % | 62,3 % | +1,34 % | +0,61 % | 1,49 | +1,36 % | 1,21 |
| D Sector rotation | 2554 | +2,10 % | 60,6 % | +0,85 % | -0,05 % | -0,95 | +1,14 % | 0,44 |
| Règle simple : top 5 momentum 12-1 | 1690 | +2,65 % | 59,6 % | +1,58 % | +0,74 % | 1,52 | +2,27 % | 1,46 |
| Référence : toutes les actions (univers EW) | 14196 | +1,90 % | 59,6 % | +0,84 % | -0,00 % | N/D | -0,00 % | N/D |

Pouvoir prédictif du score (corrélation de rang score / rendement futur, par date) :

| Horizon | IC moyen | t | % de dates IC > 0 |
|---:|---:|---:|---:|
| 5 j | 0,005 | 0,36 | 51,5 % |
| 20 j | 0,003 | 0,15 | 53,8 % |
| 60 j | 0,013 | 0,37 | 54,4 % |

### Trades simulés (developpement, frais 10 pb/côté)

| Groupe | Trades | % gagnants | Rdt net moyen | Profit factor | Espérance (R) | Durée moy. | Rdt net à 0 / 25 pb |
|---|---:|---:|---:|---:|---:|---:|---|
| READY | 222 | 40,5 % | +0,31 % | 1,12 | 0,12 | 19,6 | +0,51 % / +0,01 % |
| DEVELOPING ou READY | 1144 | 42,3 % | +1,49 % | 1,57 | 0,28 | 20,7 | +1,69 % / +1,19 % |
| Top 5 du score | 540 | 44,8 % | +1,97 % | 1,75 | 0,34 | 21,8 | +2,17 % / +1,67 % |
| A Early momentum | 798 | 38,4 % | +0,90 % | 1,40 | 0,22 | 19,4 | +1,10 % / +0,60 % |
| B Earnings momentum | 319 | 43,6 % | +1,56 % | 1,52 | 0,26 | 22,4 | +1,76 % / +1,26 % |
| C Growth acceleration | 484 | 44,0 % | +2,35 % | 1,78 | 0,37 | 21,6 | +2,55 % / +2,05 % |
| D Sector rotation | 827 | 43,4 % | +1,80 % | 1,67 | 0,32 | 20,6 | +2,00 % / +1,50 % |
| Règle simple : top 5 momentum 12-1 | 479 | 41,1 % | +2,18 % | 1,64 | 0,29 | 20,3 | +2,38 % / +1,88 % |

### Portefeuille simulé (developpement) : max 10 positions de 10%

| Portefeuille | Rdt annualisé | Drawdown max | Volatilité | Sharpe | Sortino |
|---|---:|---:|---:|---:|---:|
| READY | +0,79 % | -14,49 % | 5,8 % | 0,17 | 0,18 |
| DEVELOPING ou READY | +14,05 % | -21,08 % | 16,6 % | 0,88 | 1,10 |
| Top 5 du score | +14,52 % | -19,44 % | 13,7 % | 1,06 | 1,41 |
| A Early momentum | +9,47 % | -17,79 % | 10,9 % | 0,88 | 1,16 |
| B Earnings momentum | +6,06 % | -23,36 % | 11,3 % | 0,58 | 0,69 |
| C Growth acceleration | +15,58 % | -21,62 % | 14,7 % | 1,06 | 1,38 |
| D Sector rotation | +14,48 % | -25,80 % | 16,1 % | 0,92 | 1,23 |
| Règle simple : top 5 momentum 12-1 | +13,50 % | -24,30 % | 16,2 % | 0,86 | 1,19 |
| Référence : SPY | +14,55 % | -33,70 % | 19,5 % | 0,80 | 0,97 |
| Référence : Univers équipondéré | +25,19 % | -31,93 % | 20,9 % | 1,18 | 1,44 |

## Rendements après signal — holdout

| Groupe | Signaux | Rdt moyen 20 j | % gagnants | vs SPY 20 j | vs univers 20 j | t | vs univers 60 j | t |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| READY | 123 | +0,38 % | 53,9 % | +0,26 % | -0,01 % | -0,16 | -1,06 % | -1,50 |
| DEVELOPING ou READY | 1141 | +0,48 % | 51,7 % | -0,19 % | -0,40 % | -0,95 | -1,88 % | -2,64 |
| Top 5 du score | 500 | +1,02 % | 52,1 % | -0,08 % | -0,27 % | -0,49 | -2,23 % | -1,87 |
| A Early momentum | 468 | +0,52 % | 54,0 % | -0,18 % | -0,29 % | -2,24 | -1,01 % | -1,54 |
| B Earnings momentum | 265 | +1,13 % | 54,6 % | +0,77 % | +0,67 % | 0,50 | -2,07 % | -0,54 |
| C Growth acceleration | 902 | +0,61 % | 49,4 % | -0,46 % | -0,70 % | -0,45 | -1,13 % | 0,03 |
| D Sector rotation | 739 | +1,00 % | 52,5 % | +0,06 % | -0,19 % | -0,10 | -1,18 % | -1,21 |
| Règle simple : top 5 momentum 12-1 | 500 | +2,34 % | 51,9 % | +1,24 % | +1,05 % | 0,83 | +3,53 % | 0,96 |
| Référence : toutes les actions (univers EW) | 4200 | +1,29 % | 53,6 % | +0,19 % | -0,00 % | N/D | +0,00 % | N/D |

Pouvoir prédictif du score (corrélation de rang score / rendement futur, par date) :

| Horizon | IC moyen | t | % de dates IC > 0 |
|---:|---:|---:|---:|
| 5 j | 0,006 | 0,25 | 47,5 % |
| 20 j | 0,001 | 0,02 | 51,0 % |
| 60 j | -0,057 | -1,37 | 39,8 % |

### Trades simulés (holdout, frais 10 pb/côté)

| Groupe | Trades | % gagnants | Rdt net moyen | Profit factor | Espérance (R) | Durée moy. | Rdt net à 0 / 25 pb |
|---|---:|---:|---:|---:|---:|---:|---|
| READY | 56 | 41,1 % | +0,00 % | 1,00 | 0,07 | 19,8 | +0,20 % / -0,30 % |
| DEVELOPING ou READY | 331 | 34,7 % | -0,13 % | 0,96 | 0,03 | 18,0 | +0,07 % / -0,43 % |
| Top 5 du score | 156 | 42,3 % | +0,90 % | 1,31 | 0,13 | 19,9 | +1,10 % / +0,60 % |
| A Early momentum | 187 | 36,4 % | +0,27 % | 1,11 | 0,11 | 18,5 | +0,47 % / -0,03 % |
| B Earnings momentum | 78 | 33,3 % | -0,06 % | 0,98 | 0,00 | 19,9 | +0,14 % / -0,36 % |
| C Growth acceleration | 222 | 32,4 % | +0,36 % | 1,09 | 0,01 | 18,5 | +0,56 % / +0,06 % |
| D Sector rotation | 229 | 37,5 % | +0,56 % | 1,17 | 0,12 | 18,1 | +0,76 % / +0,26 % |
| Règle simple : top 5 momentum 12-1 | 155 | 33,6 % | +0,89 % | 1,20 | 0,11 | 17,8 | +1,09 % / +0,59 % |

### Portefeuille simulé (holdout) : max 10 positions de 10%

| Portefeuille | Rdt annualisé | Drawdown max | Volatilité | Sharpe | Sortino |
|---|---:|---:|---:|---:|---:|
| READY | -0,10 % | -9,11 % | 4,7 % | 0,00 | 0,00 |
| DEVELOPING ou READY | -7,73 % | -21,85 % | 14,8 % | -0,47 | -0,70 |
| Top 5 du score | +6,54 % | -12,71 % | 11,2 % | 0,63 | 0,89 |
| A Early momentum | +2,99 % | -10,48 % | 9,8 % | 0,35 | 0,48 |
| B Earnings momentum | -0,53 % | -10,25 % | 8,0 % | -0,03 | -0,03 |
| C Growth acceleration | +3,38 % | -18,32 % | 16,6 % | 0,28 | 0,45 |
| D Sector rotation | +4,45 % | -12,66 % | 14,6 % | 0,37 | 0,53 |
| Règle simple : top 5 momentum 12-1 | +5,23 % | -14,62 % | 16,2 % | 0,40 | 0,64 |
| Référence : SPY | +15,13 % | -18,76 % | 16,9 % | 0,92 | 1,20 |
| Référence : Univers équipondéré | +18,53 % | -18,79 % | 15,7 % | 1,17 | 1,55 |

## Stabilité par année (developpement) — rendement 20 j vs univers

| Groupe | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| READY | +2,78 % | +0,50 % | -0,69 % | +0,84 % | -1,25 % | +2,45 % | -0,77 % | -0,18 % |
| DEVELOPING ou READY | -0,43 % | +0,04 % | -0,16 % | +0,91 % | -0,60 % | -0,46 % | -0,65 % | +0,08 % |
| Top 5 du score | +1,61 % | +0,78 % | -0,08 % | +0,98 % | -1,26 % | +0,53 % | -0,30 % | +0,44 % |
| A Early momentum | -0,98 % | -0,15 % | -0,14 % | -0,46 % | -0,53 % | -1,08 % | -2,04 % | -0,45 % |
| B Earnings momentum | +0,69 % | +1,53 % | -0,31 % | +0,08 % | -0,58 % | -0,64 % | -0,48 % | -1,27 % |
| C Growth acceleration | +3,02 % | +0,78 % | -0,08 % | +2,42 % | +0,00 % | +0,17 % | +0,87 % | +0,70 % |
| D Sector rotation | -0,40 % | -0,59 % | -0,52 % | +0,80 % | +0,08 % | -0,81 % | +0,64 % | -0,94 % |
| Règle simple : top 5 momentum 12-1 | +3,49 % | -0,58 % | +0,34 % | +4,49 % | -2,36 % | +0,97 % | +0,76 % | +1,81 % |

## Diagnostic facteur par facteur (developpement) — IC à 20 j

Chaque ingrédient pris isolément. Avec ~25 facteurs testés, 1 ou 2 |t| > 2 peuvent apparaître par hasard.

| Facteur | IC moyen | t | % de dates IC > 0 |
|---|---:|---:|---:|
| atr14_pct | 0,042 | 1,43 | 54,4 % |
| pe_ttm | 0,040 | 1,39 | 55,6 % |
| revenue_yoy_q | 0,019 | 0,79 | 51,5 % |
| updown_vol_50 | 0,012 | 0,77 | 56,8 % |
| rs_sector_3m | 0,015 | 0,75 | 52,1 % |
| mom_12_1 | 0,018 | 0,70 | 53,8 % |
| earnings_reaction | 0,012 | 0,69 | 55,3 % |
| rs_spy_3m | 0,015 | 0,62 | 52,4 % |
| squeeze_pct | 0,009 | 0,62 | 55,6 % |
| net_income_yoy_q | 0,012 | 0,62 | 53,0 % |
| rs_spy_6m | 0,012 | 0,46 | 52,7 % |
| revenue_accel | 0,007 | 0,45 | 55,3 % |
| sous-score technical | 0,009 | 0,34 | 56,2 % |
| sous-score fundamental | 0,002 | 0,10 | 50,3 % |
| sous-score market_sector | -0,002 | -0,09 | 51,5 % |
| sous-score catalyst | -0,004 | -0,26 | 48,5 % |
| roe_ttm | -0,007 | -0,32 | 47,0 % |
| ps_to_growth | -0,007 | -0,36 | 47,3 % |
| sous-score volume_liquidity | -0,005 | -0,36 | 51,8 % |
| ext_sma50 | -0,009 | -0,43 | 49,4 % |
| fcf_yield | -0,015 | -0,53 | 48,5 % |
| earnings_drift | -0,018 | -0,85 | 49,1 % |
| op_margin_delta | -0,021 | -1,13 | 45,9 % |
| vol_ratio_20_50 | -0,015 | -1,21 | 46,2 % |
| dist_52w_high | -0,034 | -1,34 | 48,2 % |
| sous-score risk_quality | -0,042 | -1,47 | 46,2 % |

## Par régime de marché (developpement) — rendement 20 j

| Groupe | Régime | Signaux | Rdt moyen | vs univers | % gagnants |
|---|---|---:|---:|---:|---:|
| READY | NEUTRE | 102 | +0,12 % | +0,22 % | 54,9 % |
| READY | RISK-ON | 349 | +1,32 % | -0,07 % | 59,6 % |
| Top 5 du score | NEUTRE | 350 | +1,98 % | +0,54 % | 63,1 % |
| Top 5 du score | RISK-OFF | 220 | +3,00 % | -1,38 % | 63,6 % |
| Top 5 du score | RISK-ON | 1120 | +1,90 % | +0,34 % | 60,7 % |
