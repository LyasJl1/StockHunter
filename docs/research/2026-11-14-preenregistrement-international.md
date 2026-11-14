# Pré-enregistrement — réplication internationale des catégories du scanner (3.2, 14/11/2026)

Écrit et commité **avant** toute exécution sur des données historiques non américaines. Toute modification après
lecture des résultats = nouvelle étude, nouvelle entrée du registre (`stockhunter/lab/registry.json`).

## Question
Les cinq catégories du moteur de découverte (version 3.0.0, règles figées le 13/11/2026 sur le seul marché
américain) produisent-elles, **sans aucun réglage**, un rendement supérieur à l'univers de la place sur un marché
où elles n'ont jamais été développées ?

## Données admissibles
- Inde, NSE (listing principal), bhavcopy officiel, 300 séances jusqu'au 13/11/2026, ajusté des opérations sur
  titres par le cours de référence publié. Univers point-in-time identique à celui des États-Unis (conventions
  3.2 : ≥ 130 séances, ≥ 2 M USD de montant médian échangé converti au taux BCE du jour, cours ≥ 1 USD).
- Toutes ces données sont **hors échantillon** pour les règles (jamais vues lors de leur conception) : aucune
  période réservée supplémentaire n'est découpée ; le suivi prospectif (à partir de la fusion de la 3.2) servira
  de confirmation.
- BSE : listing secondaire (doublons du NSE) — non testé. Euronext : 1 séance d'historique — **non testable**.
- Limite assumée : moins de 9 mois de dates de signal, un seul régime de marché ; puissance faible ; un résultat
  négatif signifie « non démontré ».

## Mesure (fixée ici)
- Date de signal toutes les 5 séances, de la première date où l'univers existe jusqu'à 20 séances avant la fin.
- Pour chaque catégorie et chaque date : moyenne, sur les titres sélectionnés, du rendement « ouverture J+1 →
  clôture J+20 » moins la moyenne de l'univers, **nette de 50 pb** (25 pb par côté).
- t de Newey-West (retard 4, chevauchement 20/5) sur la série des dates ayant au moins une sélection.
- Descriptif : R moyen des trades simulés avec les règles partagées (frais et glissement), effet de change en EUR,
  moitiés de période.

## Règle de décision
Famille : 17 tests antérieurs + 5 catégories = **22**. Seuil : **t ≥ 3,0** et écart net moyen positif dans
chacune des deux moitiés → « CANDIDAT INTERNATIONAL » (ni validé ni promu ; suivi prospectif requis). Moins de 20
dates avec sélection : « NON TESTABLE ». Sinon : « NON RETENU ». P-valeurs ajustées (Holm, BH) sur la famille.
Aucune règle n'est modifiée au vu des résultats ; aucune stratégie n'est déclarée gagnante.

## Portefeuille (descriptif)
Les sélections rejouées alimentent le portefeuille mondial simulé (conventions 3.2, base EUR) ; résultat publié à
titre descriptif avec l'indice équipondéré de la place sur la même période et dans la même devise.
