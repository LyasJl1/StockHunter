"""Historique mondial hors PostgreSQL (3.2) : Parquet compressé (zstd), segmenté place / année / séance.

    var/world/bars/<place>/<année>/<séance>.parquet     une séance d'une place (toutes ses lignes)
    var/world/fx/ecb.parquet                             taux de référence BCE (date, devise, taux)
    var/world/manifest.json                              séances stockées (source, lignes, horodatage),
                                                         séances absentes chez la source, consommation

Principes : incrémental et reprenable (une séance stockée n'est jamais retéléchargée ni réécrite), titres radiés
conservés (rien n'est supprimé), budget disque contrôlé AVANT écriture. La base PostgreSQL (500 Mo) ne reçoit que
les résultats (référentiel, sélections, suivi), jamais les cours.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from stockhunter.world.providers import BAR_COLUMNS

DEFAULT_BUDGET_MB = 2_000.0        # plafond du stockage mondial dans le cache Actions (limite GitHub : 10 Go au total)


class StorageBudgetExceeded(RuntimeError):
    pass


class WorldStore:
    def __init__(self, root: str | Path = "var/world", budget_mb: float = DEFAULT_BUDGET_MB):
        self.root = Path(root)
        self.budget_mb = budget_mb
        self._manifest: dict | None = None

    # ------------------------------------------------------------------ manifeste
    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def manifest(self) -> dict:
        if self._manifest is None:
            self._manifest = (json.loads(self.manifest_path.read_text()) if self.manifest_path.exists()
                              else {"sessions": {}, "missing": {}, "fx": None})
        return self._manifest

    def _save_manifest(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=1, sort_keys=True))
        os.replace(tmp, self.manifest_path)

    # ------------------------------------------------------------------ séances
    def path(self, market: str, d: date) -> Path:
        return self.root / "bars" / market / f"{d:%Y}" / f"{d.isoformat()}.parquet"

    def has(self, market: str, d: date) -> bool:
        return str(d) in self.manifest["sessions"].get(market, {}) and self.path(market, d).exists()

    def is_missing(self, market: str, d: date) -> bool:
        return str(d) in self.manifest["missing"].get(market, {})

    def stored_sessions(self, market: str) -> list[date]:
        return sorted(date.fromisoformat(k) for k in self.manifest["sessions"].get(market, {}))

    def usage_mb(self) -> float:
        if not self.root.exists():
            return 0.0
        return sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file()) / 1e6

    def check_budget(self, extra_mb: float = 1.0) -> None:
        used = self.usage_mb()
        if used + extra_mb > self.budget_mb:
            raise StorageBudgetExceeded(f"stockage mondial {used:.0f} Mo + {extra_mb:.0f} Mo > budget "
                                        f"{self.budget_mb:.0f} Mo : collecte arrêtée (aucune suppression)")

    def write_session(self, market: str, d: date, df: pd.DataFrame, provider: str, meta: dict | None = None) -> bool:
        """Écrit une séance. Déjà stockée -> rien (immuable). Renvoie True si écrite."""
        if self.has(market, d):
            return False
        self.check_budget()
        out = df[df["market"] == market][BAR_COLUMNS].reset_index(drop=True)
        p = self.path(market, d)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        out.to_parquet(tmp, compression="zstd", index=False)
        os.replace(tmp, p)
        self.manifest["sessions"].setdefault(market, {})[str(d)] = {
            "provider": provider, "rows": int(len(out)), "traded": int(out["traded"].sum()),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **(meta or {})}
        self.manifest["missing"].get(market, {}).pop(str(d), None)
        self._save_manifest()
        return True

    def mark_missing(self, market: str, d: date, reason: str) -> None:
        self.manifest["missing"].setdefault(market, {})[str(d)] = reason
        self._save_manifest()

    def load(self, market: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        days = [d for d in self.stored_sessions(market) if (start is None or d >= start) and (end is None or d <= end)]
        frames = [pd.read_parquet(self.path(market, d)) for d in days if self.path(market, d).exists()]
        if not frames:
            return pd.DataFrame(columns=BAR_COLUMNS)
        df = pd.concat(frames, ignore_index=True)
        df["date"] = pd.to_datetime(df["date"])
        return df

    # ------------------------------------------------------------------ change
    @property
    def fx_path(self) -> Path:
        return self.root / "fx" / "ecb.parquet"

    def save_fx(self, df: pd.DataFrame) -> int:
        old = self.load_fx()
        merged = (pd.concat([old, df], ignore_index=True).drop_duplicates(["date", "currency"], keep="first")
                  .sort_values(["date", "currency"]).reset_index(drop=True))
        added = len(merged) - len(old)
        if added:
            self.check_budget()
            self.fx_path.parent.mkdir(parents=True, exist_ok=True)
            merged.to_parquet(self.fx_path, compression="zstd", index=False)
        self.manifest["fx"] = {"provider": "ecb", "rows": int(len(merged)),
                               "last": str(merged["date"].max()) if len(merged) else None,
                               "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self._save_manifest()
        return added

    def load_fx(self) -> pd.DataFrame:
        if not self.fx_path.exists():
            return pd.DataFrame(columns=["date", "currency", "rate"])
        df = pd.read_parquet(self.fx_path)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    def status(self) -> dict:
        m = self.manifest
        return {"racine": str(self.root), "occupation_mo": round(self.usage_mb(), 1), "budget_mo": self.budget_mb,
                "places": {k: {"seances": len(v), "premiere": min(v) if v else None, "derniere": max(v) if v else None,
                               "absentes": len(m["missing"].get(k, {}))} for k, v in sorted(m["sessions"].items())},
                "change": m.get("fx")}
