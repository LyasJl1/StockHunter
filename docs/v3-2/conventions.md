# Conventions de calcul — StockHunter 3.2 (marchés mondiaux)

## Séances, fuseaux, fraîcheur
- Chaque place a son calendrier (exchange_calendars ; demi-séances et fériés locaux). Au-delà de la plage couverte
  par la bibliothèque (ex. Inde après 2026) ou sans calendrier (Casablanca, Dubaï) : jours ouvrés, **signalé**
  (« fériés inconnus ») ; un fichier absent chez la source est alors consigné comme absent, jamais inventé.
- Une séance est **terminée** quand sa clôture **plus le délai de publication** du fournisseur est passée
  (US 3 h, Euronext 1 h, Inde 3 h). Conséquence : la séance US du jour est traitée dès 19 h à New York (correctif
  3.2 ; auparavant elle n'était comptée que le lendemain), et une clôture de Tokyo du jour n'est jamais associée
  à une clôture américaine à venir.
- La fraîcheur se mesure en **séances de la place** (et non en jours calendaires).
- Instantané Euronext refusé pendant une séance (non définitif) ; une place dont la dernière séance diffère de
  celle de Paris (férié local) est écartée ce jour-là (lignes non datables avec certitude).
- La chaîne nocturne est « déjà faite » seulement si la séance attendue de **chaque** place a été traitée : un
  férié américain n'empêche plus la collecte européenne ou indienne.

## Prix et opérations sur titres
- Cours = clôtures de fin de séance (jamais « en direct »). Une ligne sans échange ce jour-là est gardée sans
  cours (`traded = False`) : un cours de référence repris de la veille n'est pas une clôture.
- Inde : facteur d'ajustement déduit du cours de référence ajusté publié par la bourse
  (`PrvsClsgPric(t) ÷ clôture(t−1)`), écarts < 2 % ignorés (dividendes ordinaires).
- Toute place : l'historique d'un titre est **tronqué** à sa dernière variation journalière > 45 % non expliquée.
- Dividendes non inclus (rendements de prix), comme pour les États-Unis.

## Univers et normalisation
- Même moteur de découverte et mêmes catégories que pour les États-Unis ; mêmes exigences (≥ 130 séances
  d'historique, ≥ 15 cotations sur 20, 2 M USD de montant médian échangé sur 20 séances) ; cours minimal = 1 USD
  converti au taux du jour. Montants convertis en USD au taux BCE de chaque date (point-in-time).
- Référence de marché d'une place = indice **équipondéré** de ses titres liquides, construit à partir des données
  (pas un indice officiel ; les indices officiels ne sont pas disponibles gratuitement).
- Score mondial = position relative dans la catégorie et la place : `1 − (rang − 1) ÷ admis`. Ce n'est pas une
  probabilité. Classement déterministe (score, nombre de catégories, confiance, place, symbole).
- Déduplication : même ISIN sur plusieurs places = une société (listing principal selon la priorité de la place) ;
  un ADR n'est rapproché d'une action locale que par un lien explicite, jamais par le nom. Les ADR sont exclus du
  scanner US (actions ordinaires seulement).

## Change
- BCE : unités de devise pour 1 EUR, publiées vers 16 h CET. Taux d'une date J = dernier taux publié de date ≤ J
  (jamais un taux futur ; avant la première publication : N/D). USD et autres bases : taux croisés via l'EUR.
- Devise absente des taux BCE (SAR, AED…) : aucune conversion, N/D.
- Rendement converti = (1 + local) × (1 + effet de change) − 1 ; l'effet de change est publié séparément.
- Frais de conversion : 25 pb par conversion (achat et vente) quand la devise diffère de la base.

## Suivi des sélections
- Suivi unitaire : plan figé au signal, rejoué avec les règles partagées (`trading/rules.py`), en devise locale ;
  notionnel de risque de 1 000 000 (convention d'arrondi, sans effet sur les multiples de R publiés — testé).
  Rendement à 20 séances local et converti en EUR ; écart à l'univers équipondéré de la place.
- Résultat final jamais recalculé ; signal jamais réécrit.

## Portefeuille mondial simulé
- Devise de base EUR ou USD ; capital 100 000 ; risque 0,5 % du capital par position (distance du stop convertie) ;
  15 positions au plus ; 15 % au plus par ligne ; 40 % par pays ; 50 % par devise étrangère ; 30 % par secteur
  (quand le secteur est connu — N/D hors États-Unis) ; corrélation ≤ 0,80 (rendements journaliers en base, ≥ 40
  dates communes) ; 5 nouvelles entrées au plus par place et par jour ; pas de levier.
- Ordre : jour par jour, places dans l'ordre de leur clôture UTC, entrées avant gestion. Entrée à l'ouverture
  suivant le signal ; plan annulé sans cotation pendant 5 séances ; position sans cotation pendant 10 séances
  clôturée au dernier cours (suspension, radiation).
- Frais : courtage 10 pb et glissement 5 pb (règles partagées) + change 25 pb. Quotité de la source (Inde) ou 1.
- Courbe de capital = trésorerie + valeur de marché en devise de base, à chaque séance.
- Références (même période, même devise) : SPY, URTH (MSCI World), VGK (Europe développée, proxy du STOXX 600 : le
  STOXX 600 n'est pas disponible gratuitement), INDA (MSCI India), indices équipondérés StockHunter de chaque place.
- **Simulation** : jamais mélangée au portefeuille réel ni présentée comme une performance réelle.
