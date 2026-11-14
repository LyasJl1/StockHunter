# Historique européen : sources examinées (3.2.1, 14/11/2026)

Objectif : rendre le scanner européen opérationnel (130 séances d'historique requises) sans payer, sans
contourner de protection et dans le respect des conditions des fournisseurs.

## Constat principal : la collecte Euronext de la 3.2 n'est pas autorisée
Les conditions d'utilisation d'euronext.com ([Terms of Use](https://euronext.com/en/terms-use)) interdisent, sauf
autorisation écrite préalable, l'usage de « spiders, robots, avatars, agents, tools or other devices » pour
naviguer sur le site, et la copie ou le téléchargement de contenus « through framing or systematic retrieval to
create collections, compilations, databases or directories ». Seule est permise « a single, unaltered, permanent
copy » pour un usage personnel non commercial. La collecte quotidienne automatique (3.2) est donc contraire à ces
conditions : **désactivée par défaut en 3.2.1** (`SH_WORLD_EURONEXT_ENABLED`, à n'activer qu'avec une autorisation
écrite d'Euronext). La séance déjà collectée (13/11/2026) est conservée, non redistribuée. Le téléchargement
historique par titre du site (bouton « Download » de `popout-page/getHistoricalPrice`) relève des mêmes conditions :
**non intégré**. L'historique officiel d'Euronext est un produit payant ([Historical Data](https://euronext.com/data/historical-data)).

## Comparatif (vérifié sur la documentation publique accessible ; aucun appel massif)

| Source | Places | Historique | OHLCV / OST | Quota gratuit | Conditions | Intégrable gratuitement ? |
|---|---|---|---|---|---|---|
| Euronext (site) | 7 places Euronext | séance du jour ; par titre via le site | OHLCV ; non ajusté / ajusté | — | automatisation interdite sans accord écrit | **non** (sauf autorisation écrite) |
| Euronext Historical Data | Euronext | profond | complet | payant | licence | non (payant) |
| EODHD, plan gratuit ([plans](https://eodhd.com/help/plans)) | annonce toutes les places, à confirmer pour le plan gratuit | 1 an | EOD, divisions, dividendes | 20 appels / jour | API prévue pour l'accès automatisé ; conditions à valider à l'inscription | **partiel** : ≈ 20 titres / jour en reconstitution, **aucune mise à jour quotidienne possible** pour un marché entier (1 appel par titre) |
| Alpha Vantage, clé gratuite ([doc](https://www.alphavantage.co/documentation/)) | quelques places européennes par suffixe | jusqu'à « full » selon la doc tierce | EOD | 25 requêtes / jour | API officielle | **non** pour un marché entier (25 titres / jour) |
| Marketstack gratuit | multi-places | 1 an | EOD | 100 requêtes / **mois** | API | non |
| Stooq | Europe large | long | EOD | clé obligatoire, page anti-robot | non contournée | non |
| Xetra / Deutsche Börse (jeu public) | Allemagne | — | — | — | jeu de données public arrêté ; liste des instruments : HTTP 504 au sondage | non |
| LSE, BME (Madrid), SIX, Nasdaq Nordic | RU, Espagne, Suisse, pays nordiques | — | — | — | données payantes ou sites aux conditions équivalentes | non |
| Yahoo Finance | partout | long | complet | non officiel | conditions d'utilisation contraires | écarté |

## Conclusion
Il n'existe pas, à ce jour, de source **gratuite, autorisée et capable de couvrir chaque jour un marché européen
entier**. Le scanner européen reste donc indisponible ; rien n'est simulé ni estimé à sa place.

Options, toutes soumises à ton accord :
1. **Demander à Euronext une autorisation écrite** d'usage personnel automatisé (gratuit si accordé ; délai inconnu).
   Avec elle, la collecte quotidienne se réactive par une variable, et l'historique se constitue en ≈ 6 mois.
2. **EODHD « EOD Historical Data — All World »** (offre payante, ordre de grandeur 20 €/mois selon leur grille
   publique, à vérifier) : historique de toutes les places européennes et API de masse par place (1 appel par place et
   par jour). Connecteur à écrire après accord ; aucun abonnement n'a été pris.
3. **Clés gratuites EODHD / Alpha Vantage** pour un panier restreint (≈ 20 grandes valeurs) : couverture très
   partielle, à présenter comme telle ; nécessite que tu crées les clés (conditions à accepter par toi).
