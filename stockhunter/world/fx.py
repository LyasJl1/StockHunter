"""Change (3.2) : taux de référence BCE point-in-time, conversion EUR / USD, décomposition des rendements.

Conventions (docs/v3-2/conventions.md) :
- La BCE publie vers 16 h (Francfort) le taux du jour J, exprimé en unités de devise pour 1 EUR. Le taux utilisé
  pour une date J est le dernier taux publié de date ≤ J (week-ends et fériés BCE : dernier taux connu). La chaîne
  nocturne tourne après 16 h, heure de Francfort : aucune décision n'utilise un taux publié après elle.
- Une conversion vers l'USD passe par l'EUR (taux croisé USD/EUR ÷ devise/EUR), comme la BCE le recommande.
- Une devise absente des taux BCE (ex. SAR, AED) n'est JAMAIS convertie avec un taux inventé : N/D.
- Rendement converti = (1 + rendement local) × (1 + effet de change) − 1 ; effet de change = variation de la
  valeur, dans la devise de base, d'une unité de devise locale.
- Coût de conversion : `fx_cost_bps` (défaut 25 pb) prélevé à chaque conversion (achat puis vente).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

BASES = ("EUR", "USD")


class FxUnavailable(KeyError):
    pass


@dataclass
class FxTable:
    """Taux BCE : unités de devise pour 1 EUR, indexés par date (ffill, jamais bfill)."""

    wide: pd.DataFrame                    # index = dates (Timestamp), colonnes = devises

    @classmethod
    def from_long(cls, df: pd.DataFrame) -> "FxTable":
        if df.empty:
            return cls(pd.DataFrame())
        w = df.pivot_table(index="date", columns="currency", values="rate", aggfunc="last")
        w.index = pd.to_datetime(w.index)
        w["EUR"] = 1.0
        return cls(w.sort_index())

    @property
    def currencies(self) -> list[str]:
        return sorted(self.wide.columns)

    @property
    def last_date(self) -> date | None:
        return None if self.wide.empty else self.wide.index[-1].date()

    def per_eur(self, ccy: str, d) -> float:
        """Unités de `ccy` pour 1 EUR au dernier taux publié ≤ d."""
        if ccy == "EUR":
            return 1.0
        if ccy not in self.wide.columns:
            raise FxUnavailable(f"{ccy} : devise absente des taux de référence BCE")
        s = self.wide[ccy].loc[:pd.Timestamp(d)].dropna()
        if s.empty:
            raise FxUnavailable(f"{ccy} : aucun taux BCE publié au plus tard le {d}")
        return float(s.iloc[-1])

    def rate(self, ccy: str, base: str, d) -> float:
        """Valeur, dans la devise `base`, d'UNE unité de `ccy` à la date d (point-in-time)."""
        if ccy == base:
            return 1.0
        return self.per_eur(base, d) / self.per_eur(ccy, d)

    def series(self, ccy: str, base: str, dates: pd.DatetimeIndex) -> pd.Series:
        """Valeur en `base` d'une unité de `ccy` pour chaque date (dernier taux ≤ date ; NaN avant le premier)."""
        if ccy == base:
            return pd.Series(1.0, index=dates)

        def col(c):
            if c == "EUR":
                return pd.Series(1.0, index=dates)
            if c not in self.wide.columns:
                raise FxUnavailable(f"{c} : devise absente des taux de référence BCE")
            return self.wide[c].dropna().reindex(self.wide.index.union(dates)).ffill().reindex(dates)
        return col(base) / col(ccy)


def convert(amount: float, ccy: str, base: str, fx: FxTable, d) -> float:
    return amount * fx.rate(ccy, base, d)


def decompose(p0: float, p1: float, x0: float, x1: float) -> dict[str, float]:
    """Prix local p0 -> p1, valeur d'une unité locale en base x0 -> x1."""
    local = p1 / p0 - 1
    fxe = x1 / x0 - 1
    conv = (1 + local) * (1 + fxe) - 1
    return {"local": local, "change": fxe, "converti": conv, "effet_change": conv - local}


def conversion_cost(amount_base: float, bps: float = 25.0) -> float:
    return abs(amount_base) * bps / 1e4


def to_usd_frame(values: pd.DataFrame, ccy: str, fx: FxTable) -> pd.DataFrame:
    """Montants locaux (dates × titres) convertis en USD, date par date (point-in-time). Devise inconnue -> NaN."""
    try:
        r = fx.series(ccy, "USD", values.index)
    except FxUnavailable:
        return values * np.nan
    return values.mul(r, axis=0)
