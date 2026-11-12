"""Stockage des barres de tout le marché : un fichier Parquet par séance (écrit une fois, jamais modifié)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

SCHEMA = pa.schema([("ticker", pa.string()), ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()),
                    ("close", pa.float64()), ("volume", pa.float64()), ("vwap", pa.float64()),
                    ("trades", pa.float64()), ("otc", pa.bool_())])


class MarketStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.daily = self.root / "daily"

    def path(self, session: date) -> Path:
        return self.daily / f"{session.year}" / f"{session.isoformat()}.parquet"

    def has(self, session: date) -> bool:
        return self.path(session).exists()

    def write(self, session: date, rows: list[dict]) -> int:
        """Écriture atomique (fichier temporaire puis renommage) : une coupure ne laisse pas de fichier tronqué.
        Une séance sans aucune barre est enregistrée vide (le fichier prouve que la séance a été demandée)."""
        p = self.path(session)
        p.parent.mkdir(parents=True, exist_ok=True)
        cols = {f.name: [r.get(f.name) for r in rows] for f in SCHEMA}
        table = pa.table(cols, schema=SCHEMA)
        tmp = p.with_suffix(".tmp")
        pq.write_table(table, tmp, compression="zstd")
        tmp.replace(p)
        return len(rows)

    def sessions(self) -> list[date]:
        if not self.daily.exists():
            return []
        return sorted(date.fromisoformat(f.stem) for f in self.daily.glob("*/*.parquet"))

    def load(self, start: date | None = None, end: date | None = None,
             tickers: set[str] | None = None) -> pd.DataFrame:
        frames = []
        for d in self.sessions():
            if (start and d < start) or (end and d > end):
                continue
            t = pq.read_table(self.path(d))
            df = t.to_pandas()
            if tickers is not None:
                df = df[df["ticker"].isin(tickers)]
            df["date"] = pd.Timestamp(d)
            frames.append(df)
        if not frames:
            return pd.DataFrame(columns=[f.name for f in SCHEMA] + ["date"])
        return pd.concat(frames, ignore_index=True)


def split_factors(splits: pd.DataFrame, dates: pd.Series, tickers: pd.Series) -> pd.Series:
    """Facteur multiplicatif à appliquer au prix brut d'une séance pour le rendre comparable aux prix
    actuels : produit de split_from/split_to de tous les splits exécutés APRÈS cette séance.
    (Un split le jour J s'applique aux barres antérieures à J ; la barre de J est déjà post-split.)
    Coût proportionnel aux seules lignes des titres concernés (≈ 5 M de lignes en quelques secondes)."""
    out = np.ones(len(dates))
    if splits is None or splits.empty:
        return pd.Series(out, index=dates.index)
    positions = pd.Series(np.arange(len(tickers)), index=tickers.to_numpy()).groupby(level=0).indices
    dvals = pd.to_datetime(dates).to_numpy()
    for tk, g in splits.groupby("ticker"):
        pos = positions.get(tk)
        if pos is None:
            continue
        pos = np.asarray(pos)
        for ex, ratio in zip(pd.to_datetime(g["execution_date"]).to_numpy(), (g["split_from"] / g["split_to"]).to_numpy()):
            out[pos[dvals[pos] < ex]] *= ratio
    return pd.Series(out, index=dates.index)
