# Résultats — étude pré-enregistrée univers large (12/11/2026)

Protocole : `2026-11-12-preenregistrement-univers-large.md` (commit 762717b, écrit avant les données).
Exécution : workflow « Recherche univers large », 12/11/2026. Période de développement uniquement.

- Séances : 501 (12/11/2024 → 11/11/2026) ; titres éligibles (actions ordinaires NYSE/Nasdaq/NYSE American,
  radiés compris) : 6 485 ; univers moyen par date : 2 025.

| # | Facteur | Sens attendu | Dates | IC 20 j | t | % IC > 0 | Décile sup. vs univers (net, 20 j) | t | Décision |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| H1 | Momentum 6-1 | + | 39 | +0,030 | +1,03 | 67 % | +1,24 % | +0,84 | NON RETENU |
| H2 | Retournement court terme | − | 39 | −0,018 | −1,46 | 36 % | −0,57 % | −1,11 | NON RETENU |
| H3 | Proximité du plus haut 52 s | + | 14 | +0,071 | +1,28 | 64 % | +0,38 % | +0,42 | NON RETENU |
| H4 | Volatilité 60 j | − | 39 | −0,036 | −0,95 | 38 % | −1,73 % | −2,37 | NON RETENU |
| H5 | Volume anormal 5/60 j | + | 39 | −0,020 | −1,68 | 36 % | −0,86 % | −2,39 | NON RETENU |
| H6 | Illiquidité (Amihud) | + | 39 | +0,007 | +0,53 | 46 % | −0,12 % | −0,17 | NON RETENU |
| H7 | Choc d'information | + | 39 | −0,004 | −0,41 | 38 % | −0,90 % | −2,49 | NON RETENU |
| C | Composite | + | 39 | −0,004 | −0,17 | 56 % | −0,55 % | −1,20 | NON RETENU |

## Conclusion
- Aucun facteur ne remplit la règle de décision (t ≥ 2,7 dans le sens attendu) ; aucun n'atteint même t ≥ 2.
- Période réservée **non évaluée** : le protocole ne la prévoit que pour confirmer un facteur prometteur.
  Elle reste intacte.
- Les écarts de sens contraire (H4, H5, H7 : décile supérieur à t ≈ −2,4) sont sous le seuil corrigé et
  cohérents avec un marché haussier favorable aux titres les plus volatils. Ils ne sont **pas** retournés
  pour être exploités (ce serait une hypothèse nouvelle, à tester sur des données postérieures au 12/11/2026).
- Faible puissance : 39 dates (14 pour H3). Un effet modeste ne peut pas être exclu, seulement non démontré.
