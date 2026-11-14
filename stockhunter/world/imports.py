"""Importation d'historiques fournis par l'utilisateur (3.3, mode « recherche ») et interface commune des sources.

Pourquoi un import et pas une collecte Yahoo : les conditions d'utilisation de Yahoo (legal.yahoo.com, §2(d)(ix),
vérifiées le 14/11/2026) interdisent d'« access or collect data … using any automated means, including … robots,
spiders, scrapers ». `yfinance` n'est pas une API officielle. Le téléchargement CSV de Yahoo Finance est réservé à
l'abonnement Gold (help.yahoo.com/kb/SLN2311). StockHunter ne se connecte donc JAMAIS à Yahoo : il importe les
fichiers que l'utilisateur a obtenus lui-même, par un moyen autorisé, et consigne la déclaration de droits fournie.

Formats acceptés (détection automatique) :
- export « Historical Data » de Yahoo Finance : Date, Open, High, Low, Close, Adj Close, Volume (un symbole par
  fichier, symbole tiré du nom de fichier, ex. `AIR.PA.csv`) ;
- CSV produit par `yfinance` (un ou plusieurs symboles ; en-têtes multiples « Price / Ticker / Date ») ;
- format long générique CSV / Parquet : date, symbol (ou ticker), open, high, low, close, volume, et facultatif
  adj_close, isin, currency, name.

Normalisation : place et devise déduites du suffixe Yahoo (`.PA` -> Euronext Paris, `.NS` -> NSE…) ; cotations en
pence (Londres, « GBp ») ou en cents (Johannesburg, « ZAc ») ramenées à l'unité ; prix Yahoo « Close » déjà ajustés
des divisions (pas des dividendes) — conservés comme tels, « Adj Close » gardé à part. Contrôles qualité consignés,
lignes invalides écartées (jamais corrigées par invention).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

from stockhunter.world import markets as mk
from stockhunter.world.providers import BAR_COLUMNS
from stockhunter.world.store import WorldStore

# ============================================================================ interface commune des sources
CAPABILITIES = ("listings", "ohlcv", "currency", "exchange", "isin", "data_date", "adjustments", "provenance")


@dataclass(frozen=True)
class SourceStatus:
    name: str
    enabled: bool
    mode: str                       # « collecte automatique » | « import de fichiers » | « désactivée »
    reason: str
    markets: tuple[str, ...] = ()


class MarketDataProvider(Protocol):
    """Contrat commun : toute source AUTORISÉE fournit des barres normalisées (`BAR_COLUMNS`) datées, avec
    devise, place, identifiant (ISIN si connu), ajustements et provenance. Une source non autorisée ou
    indisponible se déclare désactivée (`status().enabled = False`) sans bloquer les autres."""

    name: str

    def status(self) -> SourceStatus: ...

    def securities(self, market: str) -> pd.DataFrame: ...

    def history(self, market: str, start: date | None = None, end: date | None = None) -> pd.DataFrame: ...

    def provenance(self) -> dict[str, Any]: ...


def sources_status() -> list[SourceStatus]:
    """État de chaque source : activée ou désactivée automatiquement, et pourquoi."""
    out = []
    out.append(SourceStatus("massive", True, "collecte automatique", "API officielle, plan gratuit (US)", ("US",)))
    out.append(SourceStatus("tiingo", True, "collecte automatique", "API officielle, plan gratuit (univers US suivi)", ("US",)))
    out.append(SourceStatus("nse", True, "collecte automatique", "fichiers publics des archives NSE (conditions à "
                            "confirmer par l'utilisateur)", ("XNSE",)))
    out.append(SourceStatus("bse", True, "collecte automatique", "fichiers publics BSE (conditions à confirmer)", ("XBOM",)))
    out.append(SourceStatus("ecb", True, "collecte automatique", "réutilisation libre avec mention de la source", ()))
    eur = mk._euronext_enabled()
    out.append(SourceStatus("euronext", eur, "collecte automatique" if eur else "désactivée",
                            "autorisation écrite déclarée" if eur else "conditions d'euronext.com : accès automatisé "
                            "et extraction systématique interdits sans autorisation écrite",
                            tuple(c for c, m in mk.MARKETS.items() if "euronext" in m.providers)))
    out.append(SourceStatus("yahoo", False, "désactivée", "conditions de Yahoo §2(d)(ix) : collecte automatisée "
                            "interdite ; yfinance non officiel ; aucun appel effectué", ()))
    out.append(SourceStatus("import", True, "import de fichiers", "fichiers fournis par l'utilisateur avec déclaration "
                            "de droits (Yahoo Finance Gold, export de courtier, autre source autorisée)", ()))
    return out


# ============================================================================ symboles Yahoo
# suffixe -> (place du registre, devise de cotation Yahoo, diviseur vers l'unité monétaire)
YAHOO_SUFFIX: dict[str, tuple[str, str, float]] = {
    "": ("US", "USD", 1.0), "PA": ("XPAR", "EUR", 1.0), "AS": ("XAMS", "EUR", 1.0), "BR": ("XBRU", "EUR", 1.0),
    "LS": ("XLIS", "EUR", 1.0), "IR": ("XDUB", "EUR", 1.0), "MI": ("XMIL", "EUR", 1.0), "OL": ("XOSL", "NOK", 1.0),
    "DE": ("XETR", "EUR", 1.0), "F": ("XETR", "EUR", 1.0), "L": ("XLON", "GBp", 100.0), "IL": ("XLON", "USD", 1.0),
    "MC": ("XMAD", "EUR", 1.0), "SW": ("XSWX", "CHF", 1.0), "ST": ("XSTO", "SEK", 1.0), "TO": ("XTSE", "CAD", 1.0),
    "T": ("XTKS", "JPY", 1.0), "SS": ("XSHG", "CNY", 1.0), "SZ": ("XSHE", "CNY", 1.0), "HK": ("XHKG", "HKD", 1.0),
    "NS": ("XNSE", "INR", 1.0), "BO": ("XBOM", "INR", 1.0), "KS": ("XKRX", "KRW", 1.0), "KQ": ("XKRX", "KRW", 1.0),
    "AX": ("XASX", "AUD", 1.0), "SA": ("BVMF", "BRL", 1.0), "MX": ("XMEX", "MXN", 1.0), "JO": ("XJSE", "ZAc", 100.0),
    "SR": ("XSAU", "SAR", 1.0),
}
MINOR_UNITS = {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}


class ImportError_(ValueError):
    pass


def parse_symbol(symbol: str) -> tuple[str, str, str, float]:
    """« AIR.PA » -> (« AIR.PA », « XPAR », « EUR », 1.0). Suffixe inconnu -> ImportError_ (jamais deviné).
    Les suffixes de type indice (« ^ ») et change (« =X ») sont refusés : seules les actions sont importées."""
    s = symbol.strip().upper()
    if not s or s.startswith("^") or s.endswith("=X") or s.endswith("=F"):
        raise ImportError_(f"{symbol} : indice, change ou contrat à terme, pas une action")
    # Yahoo note les classes d'actions US avec un tiret (BRK-B) : tout « .X » final est un suffixe de place
    m = re.fullmatch(r"([A-Z0-9&\-]+)(?:\.([A-Z]{1,2}))?", s)
    if not m:
        raise ImportError_(f"{symbol} : symbole Yahoo illisible")
    base, suf = m.group(1), m.group(2) or ""
    if suf not in YAHOO_SUFFIX:
        raise ImportError_(f"{symbol} : suffixe « .{suf} » non reconnu (place inconnue du registre)")
    market, ccy, div = YAHOO_SUFFIX[suf]
    return s, market, ccy, div


# ============================================================================ lecture des fichiers
@dataclass
class ParsedFile:
    path: str
    fmt: str
    frame: pd.DataFrame                                   # colonnes : date, symbol, open…volume, adj_close, isin…
    sha256: str
    warnings: list[str] = field(default_factory=list)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _norm_cols(cols) -> list[str]:
    return [str(c).strip().lower().replace(" ", "_") for c in cols]


def read_file(path: str | Path, symbol: str | None = None) -> ParsedFile:
    p = Path(path)
    raw = p.read_bytes()
    sha = _sha(raw)
    if p.suffix.lower() == ".parquet":
        df = pd.read_parquet(io.BytesIO(raw))
        df.columns = _norm_cols(df.columns)
        return ParsedFile(str(p), "parquet long", _long(df, symbol), sha)
    text = raw.decode("utf-8-sig", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ImportError_(f"{p.name} : fichier vide")
    head = _norm_cols(next(csv.reader([lines[0]])))
    # CSV yfinance multi-niveaux : « Price,Close,High,… » puis « Ticker,AAPL,… » puis « Date,,, »
    if head[0] == "price" and len(lines) > 2 and lines[1].lower().startswith("ticker"):
        fields = next(csv.reader([lines[0]]))[1:]
        tickers = next(csv.reader([lines[1]]))[1:]
        skip = 3 if lines[2].lower().startswith("date") else 2
        rows = list(csv.reader(lines[skip:]))
        recs = []
        for r in rows:
            for j, (fld, tk) in enumerate(zip(fields, tickers)):
                recs.append({"date": r[0], "symbol": tk, "field": _norm_cols([fld])[0], "value": r[j + 1] if j + 1 < len(r) else ""})
        lg = pd.DataFrame(recs)
        wide = lg.pivot_table(index=["date", "symbol"], columns="field", values="value", aggfunc="first").reset_index()
        wide.columns = _norm_cols(wide.columns)
        return ParsedFile(str(p), "yfinance (plusieurs symboles)", _long(wide, None), sha)
    df = pd.read_csv(io.StringIO(text))
    df.columns = _norm_cols(df.columns)
    if {"date", "open", "high", "low", "close", "volume"} <= set(df.columns) and not ({"symbol", "ticker"} & set(df.columns)):
        sym = symbol or p.stem
        fmt = "export Yahoo Finance (Historical Data)" if "adj_close" in df.columns else "CSV yfinance (un symbole)"
        return ParsedFile(str(p), fmt, _long(df.assign(symbol=sym), None), sha)
    return ParsedFile(str(p), "CSV long générique", _long(df, symbol), sha)


def _long(df: pd.DataFrame, symbol: str | None) -> pd.DataFrame:
    df = df.rename(columns={"ticker": "symbol", "adjclose": "adj_close", "adj._close": "adj_close"})
    if "symbol" not in df.columns:
        if not symbol:
            raise ImportError_("colonne symbol/ticker absente et aucun symbole fourni")
        df = df.assign(symbol=symbol)
    need = {"date", "open", "high", "low", "close", "volume"}
    miss = need - set(df.columns)
    if miss:
        raise ImportError_(f"colonnes manquantes : {', '.join(sorted(miss))}")
    out = pd.DataFrame({"date": pd.to_datetime(df["date"], errors="coerce", utc=True).dt.tz_convert(None).dt.normalize(),
                        "symbol": df["symbol"].astype(str).str.strip()})
    for c in ("open", "high", "low", "close", "volume", "adj_close"):
        out[c] = pd.to_numeric(df[c], errors="coerce") if c in df.columns else np.nan
    for c in ("isin", "currency", "name"):
        out[c] = df[c].astype(str).str.strip().replace({"nan": None, "": None}) if c in df.columns else None
    return out


# ============================================================================ contrôles qualité
@dataclass
class Quality:
    rows_in: int = 0
    rows_kept: int = 0
    issues: dict[str, int] = field(default_factory=dict)
    symbols: dict[str, dict] = field(default_factory=dict)

    def add(self, key: str, n: int = 1) -> None:
        if n:
            self.issues[key] = self.issues.get(key, 0) + int(n)


def normalize(df: pd.DataFrame, q: Quality, today: date | None = None) -> pd.DataFrame:
    """Barres normalisées (`BAR_COLUMNS` + adj_close) ; chaque écart consigné dans `q`."""
    today = today or datetime.now(timezone.utc).date()
    q.rows_in += len(df)
    rows = []
    for sym, g in df.groupby("symbol", sort=True):
        try:
            s, market, ccy, div = parse_symbol(sym)
        except ImportError_ as exc:
            q.add(f"symbole refusé : {exc}", len(g))
            continue
        g = g.dropna(subset=["date"]).sort_values("date")
        n0 = len(g)
        g = g.drop_duplicates("date", keep="first")
        q.add("dates en double (première gardée)", n0 - len(g))
        fut = g["date"].dt.date > today
        q.add("dates futures écartées", fut.sum())
        g = g[~fut]
        bad = (g[["open", "high", "low", "close"]].isna().any(axis=1) | (g[["open", "high", "low", "close"]] <= 0).any(axis=1)
               | (g["high"] < g[["open", "close", "low"]].max(axis=1) - 1e-9)
               | (g["low"] > g[["open", "close", "high"]].min(axis=1) + 1e-9))
        q.add("OHLC incohérents ou non positifs écartés", bad.sum())
        g = g[~bad]
        m = mk.get(market)
        sess = {x.session for x in mk.sessions(m, g["date"].min().date(), g["date"].max().date())} if len(g) else set()
        cal_ok = m.calendar is not None
        off = ~g["date"].dt.date.isin(sess) if cal_ok else pd.Series(False, index=g.index)
        q.add("lignes hors séance du calendrier de la place écartées", off.sum())
        g = g[~off]
        vol0 = (g["volume"].fillna(0) <= 0)
        q.add("séances sans volume (gardées, marquées non échangées)", vol0.sum())
        flat = (g["close"].diff() == 0) & (g["open"] == g["close"]) & (g["high"] == g["low"])
        q.add("séances figées (OHLC identiques, marquées non échangées)", flat.sum())
        jumps = (g["close"].pct_change().abs() > 0.45).sum()
        q.add("variations > 45 % (historique tronqué au scanner)", jumps)
        if len(sess):
            missing = len(sess) - len(set(g["date"].dt.date) & sess)
            q.add("séances du calendrier absentes du fichier", missing)
        if not len(g):
            continue
        fx_div = div
        real_ccy = MINOR_UNITS.get(ccy, ccy)
        given = g["currency"].dropna().unique() if "currency" in g else []
        if len(given) and given[0] not in (ccy, real_ccy):
            q.add(f"devise déclarée {given[0]} ≠ devise de la place ({real_ccy}) : symbole écarté", len(g))
            continue
        traded = ~(vol0 | flat).reindex(g.index, fill_value=False)
        px = {c: g[c] / fx_div for c in ("open", "high", "low", "close")}
        key = (g["isin"].dropna().iloc[0] if g["isin"].notna().any() else f"YF:{s}")[:40]
        q.symbols[s] = {"place": market, "devise": real_ccy, "premiere": str(g["date"].min().date()),
                        "derniere": str(g["date"].max().date()), "seances": int(len(g)), "cle": key}
        rows.append(pd.DataFrame({
            "date": g["date"].dt.date, "market": market, "ticker": s, "isin": key,
            "name": g["name"].iloc[-1] if "name" in g and g["name"].notna().any() else None,
            "currency": real_ccy, "segment": "import", "open": px["open"].where(traded), "high": px["high"].where(traded),
            "low": px["low"].where(traded), "close": px["close"].where(traded),
            "volume": g["volume"].where(traded), "turnover": (px["close"] * g["volume"]).where(traded),
            "prev_close_adj": np.nan, "lot_size": m.lot_size, "traded": traded.astype(bool)}))
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=BAR_COLUMNS)
    q.rows_kept += int(out["traded"].sum()) if len(out) else 0
    return out[BAR_COLUMNS]


# ============================================================================ stockage des imports
class ImportStore(WorldStore):
    """Même format que le stockage mondial, sous `var/world/imports` ; une séance importée peut être COMPLÉTÉE par
    de nouveaux symboles (jamais réécrite pour un symbole déjà présent : la première version importée fait foi)."""

    def __init__(self, root: str | Path = "var/world/imports", budget_mb: float = 2000.0):
        super().__init__(root, budget_mb)

    def merge_session(self, market: str, d: date, df: pd.DataFrame, provider: str, meta: dict) -> tuple[int, int]:
        part = df[df["market"] == market][BAR_COLUMNS]
        p = self.path(market, d)
        added, conflicts = len(part), 0
        if p.exists():
            old = pd.read_parquet(p)
            dup = part["isin"].isin(set(old["isin"]))
            conflicts = int(dup.sum())
            part = part[~dup]
            added = len(part)
            if not added:
                return 0, conflicts
            part = pd.concat([old, part], ignore_index=True)
        self.check_budget()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        part.reset_index(drop=True).to_parquet(tmp, compression="zstd", index=False)
        os.replace(tmp, p)
        self.manifest["sessions"].setdefault(market, {})[str(d)] = {
            "provider": provider, "rows": int(len(part)), "traded": int(part["traded"].sum()),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **meta}
        return added, conflicts

    def record_file(self, entry: dict) -> None:
        self.manifest.setdefault("fichiers", []).append(entry)
        self._save_manifest()

    def files(self) -> list[dict]:
        return list(self.manifest.get("fichiers", []))


class CombinedStore:
    """Vue unique « collecte + imports » pour le scanner : une séance collectée fait foi ; les imports complètent
    (autres titres, autres places, historique antérieur)."""

    def __init__(self, primary: WorldStore, imports: ImportStore | None):
        self.primary, self.imports = primary, imports
        self.root = primary.root

    def stored_sessions(self, market: str) -> list[date]:
        a = set(self.primary.stored_sessions(market))
        if self.imports is not None:
            a |= set(self.imports.stored_sessions(market))
        return sorted(a)

    def path(self, market: str, d: date):
        p = self.primary.path(market, d)
        if p.exists() or self.imports is None:
            return p
        return self.imports.path(market, d)

    def load(self, market: str, start=None, end=None) -> pd.DataFrame:
        a = self.primary.load(market, start, end)
        if self.imports is None:
            return a
        b = self.imports.load(market, start, end)
        if b.empty:
            return a
        if a.empty:
            return b
        both = pd.concat([a.assign(_p=0), b.assign(_p=1)], ignore_index=True)
        both = both.sort_values("_p").drop_duplicates(["date", "isin"], keep="first").drop(columns="_p")
        return both.reset_index(drop=True)

    def load_fx(self):
        return self.primary.load_fx()

    @property
    def manifest(self):
        return self.primary.manifest

    def has(self, market, d):
        return self.primary.has(market, d) or (self.imports is not None and self.imports.has(market, d))


@dataclass
class ImportReport:
    files: list[dict] = field(default_factory=list)
    quality: Quality = field(default_factory=Quality)
    sessions_written: int = 0
    rows_added: int = 0
    conflicts: int = 0
    markets: dict[str, int] = field(default_factory=dict)
    refused: list[str] = field(default_factory=list)


def import_files(store: ImportStore, paths: list[str], rights: str, source: str, engine=None,
                 today: date | None = None) -> ImportReport:
    """Importe des fichiers d'historique. `rights` : déclaration obligatoire de la provenance et du droit d'usage
    (ex. « export Yahoo Finance Gold, usage personnel »). Sans déclaration : refus."""
    rep = ImportReport()
    if not rights or len(rights.strip()) < 10:
        raise ImportError_("déclaration de droits obligatoire (--rights) : provenance et droit d'usage du fichier")
    known = {f["sha256"] for f in store.files()}
    for path in paths:
        try:
            pf = read_file(path)
        except (ImportError_, ValueError, OSError) as exc:
            rep.refused.append(f"{Path(path).name} : {exc}")
            continue
        if pf.sha256 in known:
            rep.refused.append(f"{Path(path).name} : déjà importé (même empreinte)")
            continue
        q = Quality()
        bars = normalize(pf.frame, q, today)
        for k, v in q.issues.items():
            rep.quality.add(k, v)
        rep.quality.rows_in += q.rows_in
        rep.quality.rows_kept += q.rows_kept
        rep.quality.symbols.update(q.symbols)
        meta = {"fichier": Path(path).name, "sha256": pf.sha256, "source": source}
        for (market, d), g in bars.groupby(["market", "date"]):
            added, conf = store.merge_session(market, d, g, source, meta)
            rep.rows_added += added
            rep.conflicts += conf
            rep.sessions_written += int(added > 0)
            rep.markets[market] = rep.markets.get(market, 0) + added
        entry = {"fichier": Path(path).name, "format": pf.fmt, "sha256": pf.sha256, "source": source,
                 "droits": rights.strip(), "importe_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "lignes": int(len(pf.frame)), "gardees": int(q.rows_kept), "symboles": sorted(q.symbols),
                 "qualite": q.issues}
        store.record_file(entry)
        known.add(pf.sha256)
        rep.files.append(entry)
        if engine is not None and len(bars):
            from stockhunter.world.sync import update_master
            for market, g in bars.groupby("market"):
                last = g[g["date"] == g["date"].max()]
                update_master(engine, last.assign(date=pd.to_datetime(last["date"])), f"import:{source}"[:20])
    store._save_manifest()
    return rep
