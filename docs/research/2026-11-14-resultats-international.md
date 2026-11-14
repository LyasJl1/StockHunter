# Résultats — réplication internationale des catégories du scanner (14/11/2026)

Protocole : `docs/research/2026-11-14-preenregistrement-international.md` (commit baace64, **avant** exécution).
Exécution : workflow « Monde — vérification réelle », run 43598324437, commit bbd9fc7. Données : NSE, 300 séances
(bhavcopy officiel), 30 dates de signal du 05/03/2026 au 07/10/2026. Registre : entrées `intl-XNSE-*`.

| Catégorie | Dates | Écart net moyen à 20 séances | t (Newey-West) | Moitiés | p (Holm) | Décision |
|---|---|---|---|---|---|---|
| Momentum | 27 | +2,12 % | +1,45 | −0,23 % / +4,30 % | 1,00 | NON RETENU |
| Sortie de consolidation | 30 | +1,35 % | +0,95 | +0,97 % / +1,74 % | 1,00 | NON RETENU |
| Tendance régulière | 27 | +0,16 % | +0,14 | −1,54 % / +1,73 % | 1,00 | NON RETENU |
| Choc d'information | 29 | +0,36 % | +0,18 | +3,21 % / −2,29 % | 1,00 | NON RETENU |
| À surveiller | 30 | +0,27 % | +0,36 | −0,33 % / +0,87 % | 1,00 | NON RETENU |

Lecture : aucune catégorie n'atteint le seuil t ≥ 3,0 ; aucune n'est donc « candidate ». Les écarts moyens positifs
(momentum, sortie de consolidation) ne sont pas statistiquement distinguables de zéro sur 7 mois et un seul
régime. **Aucune stratégie n'est déclarée gagnante.**

Portefeuille mondial simulé alimenté par ces sélections rejouées (descriptif ; run 43599040783, commit 045e0f5 ;
05/03/2026 → 13/11/2026, même période et même devise pour la référence) :

| Base | Portefeuille simulé | Indice équipondéré NSE (StockHunter) | Drawdown max | Trades clos | Gagnants | R moyen | Frais (courtage / change) | Entrées refusées |
|---|---|---|---|---|---|---|---|---|
| EUR | −3,4 % | +17,1 % | −4,3 % | 33 | 36 % | −0,14 | 476 / 1 190 € | 99 |
| USD | −3,5 % | +11,4 % | −4,8 % | 33 | 36 % | −0,14 | 476 / 1 190 $ | 99 |

Lecture : nettement sous la référence passive. Une seule place étant disponible sur la période, les plafonds
(40 % par pays, 50 % par devise étrangère) limitent l'exposition et laissent l'essentiel en trésorerie : la
comparaison mesure surtout cette contrainte et les frais de change. Descriptif, non validé, sans effet sur les règles.

Écart au protocole relevé : le pré-enregistrement annonçait une famille de 22 tests (17 + 5) ; le registre
comptait 16 tests (17 entrées dont une non-test) : la famille réelle est de **21**. Le seuil t ≥ 3,0, fixé
indépendamment, et les décisions sont inchangés.
