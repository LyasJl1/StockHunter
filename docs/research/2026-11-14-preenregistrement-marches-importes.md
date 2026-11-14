# Pré-enregistrement — extension de la réplication internationale aux places nouvellement disponibles (3.3)

Écrit et commité le 14/11/2026 **avant** toute exécution sur une place autre que le NSE. Complète
`2026-11-14-preenregistrement-international.md` (mesure, seuils et règle de décision inchangés).

## Champ
Toute place dont l'historique devient disponible — par collecte autorisée ou par **import de fichiers fournis par
l'utilisateur** (`stockhunter world import`) — peut être testée avec le MÊME protocole : 5 catégories du moteur 3.0.0
figé, dates de signal toutes les 5 séances, écart net (50 pb) à l'univers de la place à 20 séances, t de Newey-West
(retard 4), seuil **t ≥ 3,0** et deux moitiés positives, moins de 20 dates = NON TESTABLE.

## Règles supplémentaires
- Chaque place ajoute 5 tests à la famille (correction de Holm / BH sur la famille entière, registre).
- Une place n'est testée qu'une fois par jeu de données ; un nouvel import de la même période ne crée pas de nouveau
  test (même identifiant `intl-<place>-<catégorie>`, résultat précédent conservé en historique).
- **Biais de survivance** : un historique importé depuis Yahoo Finance ne contient en général que des titres encore
  cotés. Tout résultat sur une place importée est marqué « biais de survivance possible » et ne peut, à lui seul,
  conduire à une validation ; il ne vaut qu'en recherche, jusqu'à un suivi prospectif sur données autorisées.
- Aucun réglage de paramètre au vu des résultats ; une catégorie candidate n'est jamais « validée » sans suivi
  prospectif.
