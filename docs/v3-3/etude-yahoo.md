# Yahoo Finance comme source d'historiques internationaux — étude (14/11/2026)

## Conditions d'utilisation (vérifiées sur les textes officiels)
- [Yahoo Terms of Service](https://legal.yahoo.com/xw/en/yahoo/terms/otos/index.html), §2(d)(ix) : interdiction
  d'« access or collect data, or attempt to access or collect data, from our Services using any automated means,
  including but not limited to robots, spiders, scrapers, data mining tools ». §2(e) : pas d'accès « using a method
  other than the interface and the instructions that we provide ». §2(h) : pas de reproduction ni de distribution
  sans autorisation écrite. Yahoo Finance est fourni par Yahoo Finance LLC.
- [Aide Yahoo — SLN2311](https://help.yahoo.com/kb/SLN2311.html) : « Downloading historical data is available with
  a Yahoo Finance Gold subscription » ; option absente pour certains instruments (licences) ; pas d'historique
  avant 1970.
- `yfinance` : bibliothèque non officielle qui interroge des points d'accès internes de Yahoo ; une licence open
  source du CODE ne donne aucun droit sur les DONNÉES. Son usage automatisé relève du §2(d)(ix).

## Faisabilité technique (sans aucun appel à Yahoo)
| Aspect | Constat | Traitement dans StockHunter |
|---|---|---|
| OHLCV | Date, Open, High, Low, Close, Adj Close, Volume | lus tels quels |
| Ajustements | « Close » ajusté des divisions ; « Adj Close » ajusté des divisions et dividendes | « Close » utilisé (comme pour les US), « Adj Close » non utilisé pour les signaux |
| Symboles | suffixes de place (`.PA`, `.DE`, `.L`, `.NS`, `.T`, `.HK`…) ; classes US avec tiret (`BRK-B`) | table `YAHOO_SUFFIX` → places du registre ; suffixe inconnu refusé |
| Devises | Londres en pence (GBp), Johannesburg en cents (ZAc) | ramenés à l'unité (÷ 100) |
| Calendriers | lignes possibles hors séance | écartées et comptées (calendrier exchange_calendars) |
| Radiés | titres radiés en général absents | **biais de survivance** signalé sur toute place importée |
| ISIN | absent des exports | clé « YF:<symbole> » ; doublons inter-places non résolus sauf ISIN fourni dans un fichier long |
| Casablanca | pas de suffixe Yahoo connu pour la Bourse de Casablanca | non couverte |

## Décision
- **Mode A (import)** : développé et testé sur fichiers d'exemple. Utilisable avec des fichiers obtenus
  légitimement par l'utilisateur (export Yahoo Finance Gold si abonnement existant, export d'un courtier, autre
  source autorisée). À 0 €, Yahoo ne fournit pas d'export CSV.
- **Mode B (actualisation automatique depuis Yahoo)** : **impossible** dans le respect des conditions. Source
  « yahoo » désactivée automatiquement ; aucune requête n'est envoyée à Yahoo par StockHunter.

## Utilisation
```bash
stockhunter world import AIR.PA.csv SAP.DE.csv --source yahoo-gold --rights "export Yahoo Finance Gold, usage personnel"
stockhunter world sources            # sources activées / désactivées et pourquoi
stockhunter world scan               # places importées scannées en mode recherche
stockhunter world ranking --mode recherche --top 20
stockhunter world replay --markets XETR   # protocole pré-enregistré, biais de survivance signalé
```
