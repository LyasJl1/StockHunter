"""Fournisseurs de données mondiales gratuits (3.2) : interface commune, analyse pure, appels réseau séparés.

Chaque fournisseur déclare ses capacités (listes, OHLCV, opérations sur titres, informations de cotation, change,
fondamentaux), sa licence et ses quotas. Les fonctions `parse_*` sont pures et testées sur des fixtures
synthétiques ; les fonctions `fetch_*` font UN appel HTTP par fichier, à faible cadence, avec un User-Agent
identifié, sans contourner aucune protection : une page de vérification anti-robot (JavaScript, captcha) est
traitée comme « source non accessible », jamais contournée.

Sources vérifiées par sondage le 14/11/2026 (docs/v3-2/sources.md) :
- Euronext : instantané officiel de fin de séance de toutes les actions (Paris, Amsterdam, Bruxelles, Lisbonne,
  Dublin, Milan, Oslo) ; pas d'historique.
- NSE et BSE (Inde) : bhavcopy officiel quotidien au format UDiFF, archives publiques.
- BCE : taux de change de référence quotidiens (≈ 30 devises), historique complet.
"""

from __future__ import annotations

import csv
import io
import logging
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable

import pandas as pd

log = logging.getLogger(__name__)

USER_AGENT = "StockHunter-research/3.2 (usage personnel, non commercial)"

# colonnes normalisées d'un instantané de séance (une ligne par listing et par séance)
BAR_COLUMNS = ["date", "market", "ticker", "isin", "name", "currency", "segment", "open", "high", "low", "close",
               "volume", "turnover", "prev_close_adj", "lot_size", "traded"]


class SourceUnavailable(RuntimeError):
    """Source inaccessible (réseau, protection anti-robot, format inattendu) : jamais contournée."""


class DataCorrupted(SourceUnavailable):
    """Fichier reçu mais inexploitable (format inattendu, date incohérente) : ÉCHEC CRITIQUE, pas une indisponibilité
    temporaire. Rend la chaîne rouge (3.2.1)."""


class NoSession(RuntimeError):
    """Le fichier de la séance n'existe pas chez la source (férié non prévu par le calendrier, retard)."""


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    markets: tuple[str, ...]
    capabilities: tuple[str, ...]          # listings | ohlcv | corporate_actions | listing_info | fx | fundamentals
    license: str
    redistribution: bool
    quota: str
    history: str
    url: str
    status: str = "vérifié"                # vérifié | identifié (non intégré) | inaccessible | quota insuffisant


PROVIDERS: dict[str, Provider] = {p.name: p for p in [
    Provider("massive", "Massive (ex-Polygon), plan gratuit", ("US",), ("listings", "ohlcv", "corporate_actions",
             "listing_info"), "Usage personnel ; redistribution interdite.", False, "5 appels/min",
             "2 ans, radiés compris", "https://massive.com"),
    Provider("tiingo", "Tiingo, plan gratuit", ("US", "XSHG", "XSHE"), ("ohlcv", "corporate_actions"),
             "Usage personnel ; redistribution interdite.", False, "50 req/h, 1 000/j, 500 titres/mois",
             "long (30 ans US)", "https://www.tiingo.com",
             "vérifié (Chine : 600519 répond, mais quota de 500 titres/mois insuffisant pour le marché)"),
    Provider("euronext", "Euronext — liste et cours de fin de séance des actions", ("XPAR", "XAMS", "XBRU", "XLIS",
             "XDUB", "XMIL", "XOSL"), ("listings", "ohlcv", "listing_info"),
             "Données de marché Euronext publiées sur live.euronext.com : consultation personnelle ; "
             "aucune redistribution (rien n'est publié dans le dépôt ni dans des artefacts).", False,
             "1 fichier par séance (≈ 450 Ko)", "aucune (constitution jour après jour)",
             "https://live.euronext.com/en/products/equities/list"),
    Provider("nse", "NSE India — bhavcopy UDiFF", ("XNSE",), ("listings", "ohlcv", "corporate_actions", "listing_info"),
             "Collecte et simulation suspendues par défaut : conditions NSE §8–9, autorisation requise.", False,
             "1 fichier par séance (≈ 200 Ko), cadence prudente", "archives (vérifié au 13/11/2024)",
             "https://www.nseindia.com/all-reports"),
    Provider("bse", "BSE India — bhavcopy UDiFF", ("XBOM",), ("listings", "ohlcv", "corporate_actions", "listing_info"),
             "Collecte suspendue par défaut : droits BSE applicables non confirmés.", False,
             "1 fichier par séance (≈ 900 Ko), cadence prudente", "archives au format UDiFF",
             "https://www.bseindia.com/markets/MarketInfo/BhavCopy.aspx"),
    Provider("ecb", "BCE — taux de change de référence", (), ("fx",),
             "Réutilisation libre avec mention de la source (BCE).", True, "sans quota publié ; 1 appel par jour",
             "depuis 1999", "https://data.ecb.europa.eu"),
    Provider("jpx", "JPX — liste des titres cotés", ("XTKS",), ("listings",), "Consultation ; pas de cours.", False,
             "—", "—", "https://www.jpx.co.jp/english/markets/statistics-equities/misc/01.html", "identifié (liste seule)"),
    Provider("asx", "ASX — liste des sociétés cotées", ("XASX",), ("listings",), "Consultation ; pas de cours.", False,
             "—", "—", "https://www.asx.com.au", "identifié (liste seule)"),
    Provider("stooq", "Stooq", (), ("ohlcv",), "Clé API obligatoire depuis 2026 ; page de vérification anti-robot.",
             False, "non publié", "—", "https://stooq.com", "inaccessible (vérification JavaScript : non contournée)"),
]}


# ============================================================================ téléchargement (un appel, prudent)
Getter = Callable[[str], tuple[int, bytes, str]]


def http_get(url: str, timeout: float = 60.0) -> tuple[int, bytes, str]:
    import httpx
    with httpx.Client(headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, timeout=timeout,
                      follow_redirects=True) as c:
        r = c.get(url)
        return r.status_code, r.content, r.headers.get("content-type", "")


@dataclass
class Fetcher:
    """Un appel par URL, au plus un toutes les `min_interval_s` secondes, 2 nouvelles tentatives sur erreur réseau
    ou 5xx. 404 -> NoSession ; page HTML à la place d'un fichier -> SourceUnavailable (anti-robot, maintenance)."""

    get: Getter = http_get
    min_interval_s: float = 1.5
    retries: int = 2
    sleep: Callable[[float], None] = time.sleep
    calls: int = 0
    _last: float = field(default=0.0, init=False)

    def __call__(self, url: str, expect: str) -> bytes:
        for attempt in range(self.retries + 1):
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            try:
                status, body, ctype = self.get(url)
            except Exception as exc:                       # réseau
                err = SourceUnavailable(f"erreur réseau : {type(exc).__name__}")
            else:
                if status == 404:
                    raise NoSession(f"404 : fichier absent chez la source")
                if status < 400:
                    head = body[:200].lstrip().lower()
                    if expect != "html" and (head.startswith(b"<!doctype html") or head.startswith(b"<html")):
                        raise SourceUnavailable("page HTML reçue au lieu du fichier (vérification anti-robot ou "
                                                "maintenance) : non contournée")
                    return body
                err = SourceUnavailable(f"HTTP {status}")
                if status < 500 and status != 429:
                    raise err
            if attempt < self.retries:
                self.sleep(5.0 * (attempt + 1))
        raise err


# ============================================================================ Euronext
EURONEXT_URL = "https://live.euronext.com/en/pd_es/data/stocks/download?mics=dm_all_stock"
_EURONEXT_CITY = {"paris": "XPAR", "amsterdam": "XAMS", "brussels": "XBRU", "lisbon": "XLIS", "dublin": "XDUB",
                  "milan": "XMIL", "oslo": "XOSL"}
# lieux d'exécution qui ne sont pas des marchés de cotation principale (titres étrangers, après séance, obligations)
_EURONEXT_EXCLUDED = ("global equity", "after hours", "eurotlx", "expert market")


def _num(x: str | None) -> float | None:
    if x is None:
        return None
    x = x.strip().replace(" ", "").replace(",", "")
    if x in ("", "-", "--"):
        return None
    try:
        return float(x)
    except ValueError:
        return None


def euronext_market(market_label: str) -> tuple[str | None, str]:
    """« Euronext Growth Paris » -> (« XPAR », « Growth ») ; lieux exclus -> (None, motif)."""
    low = market_label.lower()
    if any(k in low for k in _EURONEXT_EXCLUDED):
        return None, "lieu d'exécution secondaire"
    for city, mic in _EURONEXT_CITY.items():
        if city in low:
            seg = ("Growth" if "growth" in low else "Access" if "access" in low else
                   "Expand" if "expand" in low else "Réglementé")
            return mic, seg
    return None, "marché non reconnu"


def parse_euronext(body: bytes, session: date) -> tuple[pd.DataFrame, dict]:
    """Instantané Euronext (CSV « ; », 4 lignes d'en-tête) -> barres normalisées de `session`.

    Une ligne sans échange ce jour-là (volume nul ou absent, ouverture « - ») est gardée avec `traded = False` et
    SANS cours : un cours de référence repris de la veille n'est pas une clôture du jour."""
    text = body.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    if not lines or not lines[0].lstrip('"').startswith("Name"):
        raise DataCorrupted("format Euronext inattendu (en-tête absent)")
    header = next(csv.reader([lines[0]], delimiter=";"))
    stamp = None
    for ln in lines[1:4]:
        try:
            stamp = datetime.strptime(ln.strip().strip('"'), "%d %b %Y").date()
        except ValueError:
            continue
    if stamp is not None and stamp < session:
        raise SourceUnavailable(f"instantané daté du {stamp}, antérieur à la séance attendue {session}")
    rows, skipped = [], {"lieu secondaire": 0, "non reconnu": 0, "sans ISIN": 0}
    for r in csv.reader(lines[4:], delimiter=";"):
        if len(r) < len(header) - 1:
            continue
        rec = dict(zip(header, r))
        mic, seg = euronext_market(rec.get("Market", ""))
        if mic is None:
            skipped["lieu secondaire" if seg == "lieu d'exécution secondaire" else "non reconnu"] += 1
            continue
        isin = (rec.get("ISIN") or "").strip()
        if len(isin) != 12:
            skipped["sans ISIN"] += 1
            continue
        vol = _num(rec.get("Volume"))
        o, h, lo, c = (_num(rec.get(k)) for k in ("Open Price", "High Price", "low Price", "Closing Price"))
        if c is None:
            c = _num(rec.get("last Price")) if vol else None
        traded = bool(vol and vol > 0 and c is not None and o is not None)
        rows.append({"date": session, "market": mic, "ticker": rec.get("Symbol", "").strip(), "isin": isin,
                     "name": rec.get("Name", "").strip(), "currency": rec.get("Currency", "").strip(), "segment": seg,
                     "open": o if traded else None, "high": h if traded else None, "low": lo if traded else None,
                     "close": c if traded else None, "volume": vol if traded else None,
                     "turnover": _num(rec.get("Turnover")) if traded else None, "prev_close_adj": None,
                     "lot_size": 1, "traded": traded})
    df = pd.DataFrame(rows, columns=BAR_COLUMNS)
    return df, {"lignes": len(df), "ecartees": skipped, "horodatage_source": str(stamp) if stamp else None}


# ============================================================================ Inde : bhavcopy UDiFF (NSE et BSE)
def nse_url(d: date) -> str:
    return f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"


def bse_url(d: date) -> str:
    return f"https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV"


def parse_bhavcopy(body: bytes, session: date, market: str) -> tuple[pd.DataFrame, dict]:
    """Bhavcopy UDiFF (même format NSE et BSE ; zip ou CSV) -> barres normalisées.

    Seules les actions sont gardées : ISIN « INE… » (titres de capital d'émetteurs indiens), ce qui écarte
    obligations souveraines « IN00… », fonds et ETF « INF… ». `PrvsClsgPric` est le cours de référence publié par
    la bourse, AJUSTÉ des opérations sur titres (division, attribution gratuite) : il sert à détecter et corriger
    ces discontinuités sans source supplémentaire."""
    if body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            body = z.read(z.namelist()[0])
    text = body.decode("utf-8-sig", errors="replace")
    rd = csv.DictReader(io.StringIO(text))
    if not rd.fieldnames or "TckrSymb" not in rd.fieldnames:
        raise DataCorrupted("format bhavcopy inattendu")
    rows, other, wrong_date = [], 0, 0
    for r in rd:
        isin = (r.get("ISIN") or "").strip()
        if r.get("FinInstrmTp", "").strip() != "STK" or not isin.startswith("INE"):
            other += 1
            continue
        try:
            d = date.fromisoformat(r["TradDt"].strip())
        except (KeyError, ValueError):
            wrong_date += 1
            continue
        if d != session:
            wrong_date += 1
            continue
        vol = _num(r.get("TtlTradgVol"))
        o, h, lo, c = (_num(r.get(k)) for k in ("OpnPric", "HghPric", "LwPric", "ClsPric"))
        traded = bool(vol and vol > 0 and c and o)
        lot = _num(r.get("NewBrdLotQty"))
        rows.append({"date": session, "market": market, "ticker": r["TckrSymb"].strip(), "isin": isin,
                     "name": (r.get("FinInstrmNm") or "").strip(), "currency": "INR",
                     "segment": (r.get("SctySrs") or "").strip(), "open": o if traded else None,
                     "high": h if traded else None, "low": lo if traded else None, "close": c if traded else None,
                     "volume": vol if traded else None, "turnover": _num(r.get("TtlTrfVal")) if traded else None,
                     "prev_close_adj": _num(r.get("PrvsClsgPric")), "lot_size": int(lot) if lot and lot >= 1 else 1,
                     "traded": traded})
    if wrong_date and not rows:
        raise DataCorrupted(f"fichier ne correspondant pas à la séance {session}")
    df = pd.DataFrame(rows, columns=BAR_COLUMNS)
    # un même symbole peut apparaître sous plusieurs séries (ex. EQ et BL) : on garde la ligne la plus échangée
    if len(df):
        df = (df.assign(_t=df["turnover"].fillna(-1)).sort_values("_t", ascending=False)
              .drop_duplicates(["market", "isin"]).drop(columns="_t").sort_values("ticker").reset_index(drop=True))
    return df, {"lignes": len(df), "autres_instruments": other, "date_incoherente": wrong_date}


# ============================================================================ BCE : change
ECB_URL = ("https://data-api.ecb.europa.eu/service/data/EXR/D..EUR.SP00.A?format=csvdata&detail=dataonly"
           "&startPeriod={start}")


def parse_ecb(body: bytes) -> pd.DataFrame:
    """CSV BCE -> (date, currency, rate) ; rate = unités de devise pour 1 EUR, publié vers 16 h, heure de Francfort."""
    rd = csv.DictReader(io.StringIO(body.decode("utf-8-sig", errors="replace")))
    if not rd.fieldnames or "OBS_VALUE" not in rd.fieldnames:
        raise DataCorrupted("format BCE inattendu")
    rows = []
    for r in rd:
        v = _num(r.get("OBS_VALUE"))
        if v is None or v <= 0:
            continue
        rows.append({"date": date.fromisoformat(r["TIME_PERIOD"]), "currency": r["CURRENCY"], "rate": v})
    return pd.DataFrame(rows, columns=["date", "currency", "rate"])


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
