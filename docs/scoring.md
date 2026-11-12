# Méthode de scoring — version 2.0.0 (stade RESEARCH)

> Le score est un **classement relatif** au sein de l'univers suivi (42 actions), pas une probabilité de hausse.
> Poids, seuils et règles sont des **hypothèses de départ non validées**. La phase 3 les testera
> (walk-forward, hors échantillon, coûts) avant toute promotion. Toute modification = nouvelle version.

## Point-in-time (pas de look-ahead)
Un scan à la date J n'utilise que :
- les clôtures `bar_date <= J` ;
- les faits XBRL et dépôts SEC `filed_date <= J`, en retenant pour chaque période la valeur du dépôt le plus
  récent connu à J (retraitements connus à cette date) ;
- la dernière observation macro `obs_date <= J`.
Le calendrier des résultats (Finnhub) n'est daté que de sa collecte : il n'est **pas** utilisé pour un scan passé.
Un test automatique (`test_no_look_ahead`) vérifie qu'un scan passé est identique avec ou sans les données postérieures.

## Score global = somme pondérée de 6 sous-scores (0-100)
| Sous-score | Poids | Composantes |
|---|---|---|
| Technique & momentum | 25 % | rangs centiles : excès de rendement vs SPY 3 m et 6 m, momentum 12-1, proximité du plus haut 52 s ; tendance (cours > MM50, > MM200, MM50 > MM200, MM50 croissante) ; +10 si breakout tenu |
| Fondamentaux & valorisation | 20 % | rangs : croissance CA trimestrielle a/a, accélération, évolution de la marge op. TTM, croissance du BPA (ou du résultat net), ROE, rendement FCF, P/S ÷ croissance (bas = mieux). Au moins 3 composantes requises |
| Catalyseurs documentés | 20 % | réaction du marché au dernier 8-K « item 2.02 » (résultats) des 40 dernières séances, en excès du SPY, ajustée de la dérive depuis. Sans résultat récent : 40 |
| Volume & liquidité | 15 % | rangs : volume des séances de hausse / baisse (50 j), volume 20 j / 50 j ; liquidité (volume en $) |
| Marché & secteur | 10 % | régime de marché (50 %) ; rang de l'ETF sectoriel GICS en force relative 1-3 m (50 %) |
| Risque & qualité | 10 % | 100 moins pénalités : volatilité (ATR), extension > 12 % au-dessus de la MM50, résultats sous 5 séances, fondamentaux en retard, cours > 30 % sous le plus haut |

Un sous-score sans aucune donnée vaut 50 (neutre) ; la **confiance** (0-1) = part pondérée des données disponibles.

## Régime de marché (règles)
+1/−1 par critère : SPY vs MM200, MM50 vs MM200, SPY sur 3 mois (±2 %), VIX (< 18 / > 25), part des actions
au-dessus de leur MM50 (> 55 % / < 40 %). Moyenne ≥ 0,5 : RISK-ON ; ≤ −0,5 : RISK-OFF ; sinon NEUTRE.

## Familles de signaux (indépendantes du score)
- **A Early momentum** : RS 3 m > 0, ligne de RS en hausse, cours > MM50 > MM200, configuration (contraction de
  volatilité, base < 15 % près des plus hauts, ou breakout tenu), extension ≤ 12 %.
- **B Earnings momentum (PEAD)** : 8-K résultats récent, réaction > +3 % vs SPY, dérive depuis ≥ 0, bénéfice en croissance.
- **C Growth acceleration** : CA > +10 % a/a, en accélération, marge op. en hausse, fondamentaux à jour.
- **D Sector rotation** : secteur dans le top 3, surperformance vs son ETF sectoriel sur 3 m.
- **E Catalyst (informatif, hors score)** : 8-K de forte importance hors résultats déposé depuis moins de 14 jours
  (accord important, acquisition, restructuration, dépréciation, changement de contrôle…) ou article de presse
  « justice / réglementaire / fusion / prévisions » publié depuis moins de 7 jours. Ne modifie ni le score ni le statut
  (une telle règle devrait d'abord passer la validation historique, impossible faute d'historique d'actualités
  fiable). Calcul : `stockhunter/analysis/synthesis.py`. **F High potential** : non applicable (grandes capitalisations).

## Synthèse sourcée (phase 6)
`stockhunter analyse SYMBOLE`, page action du dashboard, commande Telegram `/analyse SYMBOLE`.
Texte produit par règles, sans IA générative : chaque phrase reprend une donnée datée de la base avec sa source
(dépôt SEC, article, clôture Tiingo, scan). Point-in-time : à la date J, seuls les 8-K déposés au plus tard à J et
les articles publiés avant la fin de J (heure de New York) sont utilisés.
- Rubriques 8-K : table fixe `catalysts/classify.py` (libellé, importance 1-3, point de vigilance).
- Articles : classement par mots-clés du titre (justice, réglementaire, fusion, résultats, prévisions, analystes,
  direction, capital, contrat/produit). Indicatif : **aucune analyse de sentiment**, aucun « positif / négatif ».
- Alertes Telegram : catalyseur important (hors simple publication de résultats) touchant une action READY,
  DEVELOPING ou en position simulée ; une seule alerte par événement.

## Statuts
| Statut | Règle |
|---|---|
| REJECTED | cours non à jour, volume < 20 M$/j, confiance < 0,5 ou score < 40 |
| WATCHLIST | par défaut |
| DEVELOPING | score ≥ 60 et au moins une famille active |
| READY | DEVELOPING + score ≥ 70 + déclencheur confirmé (breakout tenu ou B) + marché non RISK-OFF + extension ≤ 12 % + gap < 4 % + pas de résultats sous 3 séances |

ACTIVE PAPER TRADE, CLOSED et INVALIDATED arrivent en phase 4. Chaque changement est enregistré (`status_changes`).

## Limites connues
- Univers restreint : les rangs centiles dépendent de sa composition.
- Pas d'estimations d'analystes (non disponibles gratuitement) : pas de « surprise » vs consensus, seulement la
  réaction du marché et la croissance publiée.
- Formulaires 4 (initiés) : seules les métadonnées sont collectées, le sens (achat/vente) n'est pas encore exploité.
- Banques et sociétés à plusieurs classes d'actions : certaines valorisations sont N/D (concepts XBRL absents).
- Biais du survivant de l'univers : à traiter en phase 3.
