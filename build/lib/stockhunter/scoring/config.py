"""Paramètres du scoring. Toute modification de ce fichier = nouvelle VERSION (le hash est vérifié à
l'enregistrement : un même numéro de version ne peut pas désigner deux paramétrages différents).

Statut : RESEARCH. Les poids et seuils sont des hypothèses de départ, NON validées par backtest.
Ils seront évalués en phase 3 (walk-forward, hors échantillon) avant toute promotion.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

SCORING_NAME = "stockhunter-multifactor"
SCORING_VERSION = "2.0.0"
STAGE = "RESEARCH"


@dataclass(frozen=True)
class Weights:
    technical: float = 0.25
    fundamental: float = 0.20
    catalyst: float = 0.20
    volume_liquidity: float = 0.15
    market_sector: float = 0.10
    risk_quality: float = 0.10


@dataclass(frozen=True)
class Thresholds:
    # Liquidité minimale (volume moyen 20 séances, en dollars)
    min_dollar_volume: float = 20_000_000
    # Confiance minimale dans les données pour être classé
    min_confidence: float = 0.5
    reject_below: float = 40.0
    developing_at: float = 60.0
    ready_at: float = 70.0
    # Ne pas poursuivre un titre trop étendu au-dessus de sa moyenne 50 jours, ni un gap excessif
    max_extension_sma50: float = 0.12
    max_gap_last: float = 0.04
    # Résultats imminents : risque d'événement
    earnings_blackout_sessions: int = 3
    earnings_caution_sessions: int = 5
    # Fenêtre de l'événement résultats (8-K item 2.02) pris en compte
    earnings_event_max_sessions: int = 40


@dataclass(frozen=True)
class ScoringConfig:
    name: str = SCORING_NAME
    version: str = SCORING_VERSION
    stage: str = STAGE
    weights: Weights = field(default_factory=Weights)
    thresholds: Thresholds = field(default_factory=Thresholds)

    def as_dict(self) -> dict:
        return asdict(self)

    def params_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True)

    def params_hash(self) -> str:
        return hashlib.sha256(self.params_json().encode()).hexdigest()


DEFAULT_CONFIG = ScoringConfig()
assert abs(sum(asdict(DEFAULT_CONFIG.weights).values()) - 1.0) < 1e-9

SUBSCORE_LABELS = {
    "technical": "Technique & momentum",
    "fundamental": "Fondamentaux & valorisation",
    "catalyst": "Catalyseurs documentés",
    "volume_liquidity": "Volume & liquidité",
    "market_sector": "Marché & secteur",
    "risk_quality": "Risque & qualité des données",
}
