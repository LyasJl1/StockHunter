"""Historique long (≥ 10 ans) séparé de la base opérationnelle : un fichier Parquet compressé par titre.

Pourquoi : l'univers large (Massive gratuit) ne couvre que 2 ans, un seul régime de marché. Tiingo (gratuit)
donne 30+ ans de clôtures ajustées (splits ET dividendes) mais limite le nombre de titres DISTINCTS par mois
(500 selon la documentation tierce consultée le 13/11/2026 — à vérifier sur le compte) et les requêtes (50/h,
1000/jour). D'où une collecte étalée, plafonnée, reprenable, et un stockage hors PostgreSQL (volume 0,5 Go).

Limites assumées et affichées : échantillon de titres, pas « tout le marché » ; les titres radiés AVANT 2024 ne
sont pas dans le référentiel Massive : biais du survivant résiduel sur la partie ancienne de l'historique.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume", "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume",
           "div_cash", "split_factor"]


@dataclass
class HistoryStore:
    root: Path

    def __post_init__(self):
        self.root = Path(self.root)
        (self.root / "daily").mkdir(parents=True, exist_ok=True)

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def manifest(self) -> dict:
        try:
            return json.loads(self.manifest_path.read_text())
        except FileNotFoundError:
            return {"titres": {}, "symboles_du_mois": {}}

    def save_manifest(self, m: dict) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1, default=str))
        tmp.replace(self.manifest_path)                       # écriture atomique : reprise sûre après coupure

    def path(self, ticker: str) -> Path:
        return self.root / "daily" / f"{ticker.upper()}.parquet"

    def write(self, ticker: str, rows: list[dict], source: str) -> int:
        df = pd.DataFrame(rows)
        if df.empty:
            return 0
        df["date"] = pd.to_datetime(df.pop("bar_date"))
        df = df.set_index("date").sort_index()[[c for c in COLUMNS if c in df.columns]]
        df = df[~df.index.duplicated(keep="last")]
        tmp = self.path(ticker).with_suffix(".tmp")
        df.to_parquet(tmp, compression="zstd")
        tmp.replace(self.path(ticker))
        m = self.manifest()
        m["titres"][ticker.upper()] = {"source": source, "premiere": str(df.index[0].date()),
                                       "derniere": str(df.index[-1].date()), "seances": int(len(df)),
                                       "collecte": datetime.now(timezone.utc).isoformat()[:19]}
        self.save_manifest(m)
        return len(df)

    def read(self, ticker: str) -> pd.DataFrame:
        return pd.read_parquet(self.path(ticker))

    def tickers(self) -> list[str]:
        return sorted(p.stem for p in (self.root / "daily").glob("*.parquet"))

    def size_mb(self) -> float:
        return round(sum(p.stat().st_size for p in (self.root / "daily").glob("*.parquet")) / 1048576, 2)


def month_key(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def fetch(store: HistoryStore, provider, tickers: list[str], start: date, max_new_symbols: int,
          already_used: set[str] = frozenset(), today: date | None = None, progress=print) -> dict:
    """Collecte reprenable. N'ajoute au plus que `max_new_symbols` titres DISTINCTS nouveaux ce mois-ci (les titres
    déjà interrogés ce mois-ci — dont les 58 suivis chaque soir, `already_used` — ne consomment rien de plus)."""
    from stockhunter.data.http import QuotaExceeded
    today = today or date.today()
    m = store.manifest()
    used = set(m["symboles_du_mois"].get(month_key(today), [])) | {t.upper() for t in already_used}
    out = {"collectes": [], "ignores_deja_presents": 0, "plafond_atteint": False, "erreurs": {}}
    new_count = len(m["symboles_du_mois"].get(month_key(today), []))     # budget déjà consommé ce mois-ci
    for t in [x.upper() for x in tickers]:
        if t in m["titres"]:
            out["ignores_deja_presents"] += 1
            continue
        is_new = t not in used
        if is_new and new_count >= max_new_symbols:
            out["plafond_atteint"] = True
            break
        try:
            rows = provider.fetch_prices(t, start, today)
        except QuotaExceeded as exc:
            out["erreurs"][t] = f"quota : {exc}"
            break
        except Exception as exc:
            out["erreurs"][t] = f"{type(exc).__name__} : {str(exc)[:120]}"
            continue
        finally:
            if is_new:
                new_count += 1
                used.add(t)
                m = store.manifest()
                m["symboles_du_mois"][month_key(today)] = sorted(used - {x.upper() for x in already_used} |
                                                                  set(m["symboles_du_mois"].get(month_key(today), [])))
                store.save_manifest(m)
        n = store.write(t, rows, provider.NAME)
        out["collectes"].append((t, n))
        progress(f"  {t} : {n} séances")
        m = store.manifest()
    out["nouveaux_symboles_ce_mois"] = len(m["symboles_du_mois"].get(month_key(today), []))
    return out


def candidate_tickers(engine, market_dir: str, n: int, as_of: date = date(2024, 11, 12)) -> list[str]:
    """Échantillon reproductible : les `n` actions ordinaires les plus liquides (volume médian en dollars sur les
    20 premières séances Massive), radiées depuis comprises. Ordre fixe -> collecte reprenable mois après mois."""
    from stockhunter.lab.panel import load_panel
    from stockhunter.market.store import MarketStore
    p = load_panel(engine, MarketStore(market_dir))
    first = p.dollar_vol.iloc[:20].median()
    price_ok = p.raw_close.iloc[:20].median() >= 5
    return list(first[price_ok].dropna().sort_values(ascending=False).head(n).index)
