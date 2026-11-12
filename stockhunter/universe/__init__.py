"""Univers suivi. La couverture réelle = cet univers, pas « tout le marché ».

Limite : Tiingo Free ≈ 500 symboles uniques/mois. On plafonne l'univers à 400 pour garder une marge.
Attention biais du survivant : un univers choisi aujourd'hui ne contient pas les sociétés disparues ;
les backtests de la phase 3 devront en tenir compte (univers point-in-time).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

DEFAULT_UNIVERSE = Path(__file__).with_name("us_seed.csv")
MAX_UNIVERSE_SIZE = 400


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    asset_type: str
    role: str
    sector: str | None
    name: str | None = None
    sector_etf: str | None = None    # ETF Select Sector correspondant (classification GICS)
    industry_etf: str | None = None  # ETF d'industrie optionnel (ex. SMH pour les semi-conducteurs)


def load_universe(path: Path | None = None) -> list[UniverseEntry]:
    path = path or DEFAULT_UNIVERSE
    entries: dict[str, UniverseEntry] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("symbol") or "").strip().upper()
            if not sym:
                continue
            entries[sym] = UniverseEntry(
                symbol=sym,
                asset_type=(row.get("asset_type") or "stock").strip(),
                role=(row.get("role") or "candidate").strip(),
                sector=(row.get("sector") or "").strip() or None,
                name=(row.get("name") or "").strip() or None,
                sector_etf=(row.get("sector_etf") or "").strip().upper() or None,
                industry_etf=(row.get("industry_etf") or "").strip().upper() or None,
            )
    known = set(entries)
    for e in entries.values():
        for ref in (e.sector_etf, e.industry_etf):
            if ref and ref not in known:
                raise ValueError(f"{e.symbol} : ETF de référence {ref} absent de l'univers")
    if len(entries) > MAX_UNIVERSE_SIZE:
        raise ValueError(
            f"Univers de {len(entries)} symboles > {MAX_UNIVERSE_SIZE} : incompatible avec le quota Tiingo Free."
        )
    return list(entries.values())
