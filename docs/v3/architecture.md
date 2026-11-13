# StockHunter 3.0 — architecture

```
                 GitHub Actions (planifié, gratuit)                         Railway (gratuit)
 ┌──────────────────────────────────────────────────────────────┐   ┌───────────────────────────┐
 │ stockhunter pipeline nightly                                 │   │ PostgreSQL (0,5 Go)       │
 │ 0 base        db tune                                        │   │                           │
 │ 1 collecte    sync-universe · ingest all · market sync       │──▶│ tables                    │
 │ 2 qualité     quality                                        │   │                           │
 │ 3 sélection   scan (42 suivies) · discover run (univers      │   │ Dashboard FastAPI         │
 │               large : classement, analyse approfondie, suivi)│   │  Marché · Scanner · Fiche │
 │ 4 décision    paper run (règles partagées)                   │   │  Laboratoire · Portefeuille│
 │ 5 suivi       forward record (paniers figés)                 │   │  Santé · Réglages         │
 │ 6 maintenance prune                                          │   │ Lecteur Telegram (bail)   │
 │ 7 rapports    notify evening                                 │   └───────────────────────────┘
 └──────────────────────────────────────────────────────────────┘
   cache Actions : var/market (Parquet, tout le marché US, 2 ans)
```

## Modules
| Couche | Module | Rôle |
|---|---|---|
| Collecte | `data/`, `market/` | Tiingo (42 suivies, historique long), Massive (tout le marché, 2 ans, radiés compris), SEC, FRED, Finnhub ; quotas persistés ; upsert sans réécriture inutile |
| Qualité | `data/quality.py` | anomalies datées |
| Indicateurs | `analytics/`, `lab/factors.py` | calcul pur, point-in-time ; mêmes définitions en recherche et en production |
| Univers | `lab/panel.py` | univers négociable recalculé chaque jour (prix, liquidité, historique, suspension, données suspectes), tranches de liquidité |
| Recherche | `lab/` | évaluation (IC, Newey-West, bootstrap par blocs, Holm/BH, coûts, capacité, semestres), ML en walk-forward purgé, backtest de portefeuille, **registre versionné** (`lab/registry.json`) |
| Sélection | `scoring/` + `scan.py` (42 suivies, score détaillé) ; `discovery/` (univers large, catégories à règle absolue, filtre de marché) | présélection rapide de ~2 700 titres en 45 s, puis analyse approfondie des 3 premiers de chaque catégorie |
| Décision simulée | `trading/rules.py` | machine à états unique : entrée conditionnelle, stop, TP1/TP2, stop suiveur, durée, frais, glissement, splits |
| Suivi | `portfolio/paper.py` (portefeuille simulé, contraintes, journal), `discovery/store.py` (résultat de chaque signal), `research/forward.py` (paniers prospectifs) | tout signal est figé puis suivi jusqu'à son terme |
| Rapports | `reports/`, `notifications/`, `dashboard/` | rien n'est recalculé côté web : lecture de la base |
| Orchestration | `pipeline.py` | étapes séparées, bilan en base, échec critique non masqué |

## Garanties
- **Point-in-time** : tests de troncature (`test_no_look_ahead_*`) sur les facteurs, l'univers et le classement.
- **Immutabilité** : scans, classements de découverte et paniers prospectifs sont insérés une fois ; un résultat
  final de signal n'est plus recalculé.
- **Parité** : backtest et paper trading donnent les mêmes exécutions sur les mêmes barres (testé).
- **Honnêteté statistique** : tout test va au registre ; la famille entière sert à corriger les tests multiples ;
  une période réservée consultée y est consignée.
- **Aucun ordre réel**, aucun courtier ; réel et simulé dans des tables séparées.

## Niveaux de validation d'une stratégie
RESEARCH → CANDIDAT (t ≥ 3,0 + robustesse, sur développement) → CONFIRMÉ HORS ÉCHANTILLON (période réservée, une
fois) → PAPER → VALIDÉ (≥ 6 mois de prospectif positif). Aujourd'hui : **toutes en RESEARCH**.
