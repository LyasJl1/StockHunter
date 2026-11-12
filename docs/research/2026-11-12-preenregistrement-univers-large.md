# Pré-enregistrement — recherche sur l'univers large (12/11/2026)

> Écrit et commité **avant** la réception des données de marché (aucune séance Massive n'est encore stockée).
> Toute analyse qui s'écarte de ce document sera signalée comme exploratoire dans les rapports.

## Données
- Massive, plan gratuit : barres journalières brutes de toutes les actions US, ~504 séances (≈ nov. 2024 → nov. 2026),
  ajustées des splits à la lecture. **Dividendes non inclus** (rendements de prix ; biais faible et commun à tous).
- Référentiel incluant les titres retirés de la cote : un titre est présent tant qu'il a des barres.

## Univers (recalculé à chaque date, sans information future)
- Type `CS` (action ordinaire), place principale NYSE, Nasdaq ou NYSE American (`XNYS`, `XNAS`, `XASE`), hors OTC.
- Cours brut de clôture ≥ 5 $ ; volume en dollars médian sur 20 séances ≥ 2 M$ ; au moins 130 séances d'historique.

## Hypothèses (sens attendu, d'après la littérature)
| # | Facteur | Définition (t = date du signal, clôture) | Sens attendu |
|---|---|---|---|
| H1 | Momentum 6-1 | rendement de t−126 à t−21 | + |
| H2 | Retournement court terme | rendement de t−21 à t | − |
| H3 | Proximité du plus haut 52 s | clôture / plus haut des 252 séances (seulement si 252 séances disponibles) | + |
| H4 | Faible volatilité | écart-type des rendements quotidiens sur 60 séances | − |
| H5 | Volume anormal | volume moyen 5 séances / volume moyen 60 séances | + |
| H6 | Illiquidité (Amihud) | moyenne sur 60 séances de \|rendement\| / volume en dollars | + |
| H7 | Choc d'information | sur 20 séances, la séance au plus fort gap d'ouverture absolu avec volume ≥ 3× la moyenne 60 j : signe × taille du gap (0 si aucune) | + |
| C | Composite | moyenne des rangs centiles de H1, −H2, H5, H7 | + |

## Mesures
- Dates de signal hebdomadaires (une séance sur 5). Entrée à l'ouverture de t+1, sortie à la clôture de t+h, h = 5 et 20.
- Titre retiré de la cote avant t+h : sortie à sa dernière clôture disponible (limite : la perte éventuelle de radiation n'est pas comptée).
- **IC** : corrélation de rang (Spearman) facteur / rendement futur, par date ; t de Newey-West (retard = h/5).
- **Décile supérieur** (dans le sens attendu) vs univers équipondéré, mêmes dates ; frais 25 pb par côté.
- Monotonie : rendement moyen par décile.

## Découpage
- Développement : du premier jour testable au 28/02/2026. Zone tampon de 30 jours.
- Période réservée : du 01/04/2026 à la dernière date dont l'horizon de 20 j est observable. Évaluée **une seule fois**.

## Règles de décision (fixées ici)
- 8 tests : seuil corrigé des tests multiples |t| ≥ 2,7 (≈ Bonferroni à 5 %).
- Un facteur est **retenu** si : IC à 20 j de développement dans le sens attendu avec t ≥ 2,7, ET décile supérieur net de
  frais > univers en développement, ET même signe d'IC sur la période réservée avec t ≥ 1.
- Un facteur dont l'IC de développement est de sens contraire avec |t| ≥ 2,7 est noté « inversé » mais **pas**
  retenu dans ce sens (ce serait une hypothèse nouvelle, à tester sur des données futures).
- Aucun paramètre (fenêtres, seuils) n'est ajusté au vu des résultats. Un réglage = nouvelle hypothèse, testée sur
  les données postérieures au 12/11/2026 (paper trading).

## Limites connues d'avance
- Deux ans seulement, dont ~1 an testable par facteur à 6 mois ; un seul régime de marché possible.
- H3 n'a que ~12 mois de dates testables.
- Pas de fondamentaux dans cette première étude (la SEC sera ajoutée pour les sociétés de l'univers retenu).
