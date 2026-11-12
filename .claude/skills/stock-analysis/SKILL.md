---
name: stock-analysis
description: Produire ou relire l'analyse d'une action StockHunter (pourquoi elle ressort, risques, catalyseurs, plan simulé) uniquement à partir des données datées de la base, avec sources. À utiliser quand Ilias demande « analyse X » ou pour vérifier une synthèse.
---

# Analyse d'une action

1. `stockhunter analyse SYMBOLE` (ou `--as-of AAAA-MM-JJ` pour une date passée) : synthèse sourcée par règles.
2. Vérifier la fraîcheur : séance de la clôture, date du dernier 10-Q, date des 8-K et articles. Une donnée
   ancienne se dit ancienne ; aucun cours n'est « en direct ».
3. Lire les sources citées (liens SEC, articles) avant de résumer un catalyseur ; ne jamais extrapoler un titre.
4. Toute rédaction libre (LLM) reste une reformulation : aucun chiffre, date ou fait absent de la base.
5. Toujours rappeler : stratégie non validée (voir docs/validation.md), score ≠ probabilité, plan = simulation.

Sortie attendue : situation, points forts, vigilance, catalyseurs datés, prochain événement, ce qui invaliderait
la configuration, liste des sources.
