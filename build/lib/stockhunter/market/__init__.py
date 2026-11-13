"""Univers large : toutes les actions US, une barre journalière par titre, via Massive (plan gratuit).

Les barres sont stockées NON ajustées (un fichier Parquet par séance, immuable) ; l'ajustement des splits
est appliqué à la lecture à partir de la table market_splits. Ainsi un split annoncé après la collecte
d'une séance n'introduit jamais de rupture dans la série.
"""
