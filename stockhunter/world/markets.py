"""Registre des places boursières (3.2) : identité, fuseau, devise, calendrier, horaires, source de données.

Ajouter une place = ajouter une ligne `Market` ici (et, si elle a des cours, un fournisseur dans `providers.py`).
Rien d'autre n'est à réécrire : collecte, stockage, scanner, classement et tableau de bord lisent ce registre.

Couverture HONNÊTE : `data` indique ce qui est réellement disponible gratuitement, légalement et de façon vérifiée
(sondages du 14/11/2026, docs/v3-2/sources.md). Une place sans source fiable reste « indisponible » : elle n'est
ni scannée ni comptée comme couverte.

Séance « terminée » : une séance n'est attendue qu'une fois sa clôture passée **plus** le délai de publication du
fournisseur. Ainsi une clôture de Tokyo du jour n'est jamais mêlée à une clôture américaine encore à venir, et
la séance US du jour est traitée dès que ses données sont publiées (et non plus le lendemain matin).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd

PRICES, LIST_ONLY, NONE, QUOTA = "cours quotidiens", "liste des titres seulement", "aucune source gratuite fiable", \
    "cours disponibles mais quota insuffisant"


@dataclass(frozen=True)
class Market:
    code: str                      # identifiant interne (MIC de la place, ou « US » pour NYSE + Nasdaq + NYSE American)
    name: str
    country: str                   # ISO 3166-1 alpha-2
    country_name: str
    region: str                    # Amérique du Nord | Europe | Asie-Pacifique | Amérique latine | Afrique | Moyen-Orient
    mics: tuple[str, ...]
    calendar: str | None           # nom exchange_calendars (None : non disponible -> jours ouvrés, signalé)
    tz: str
    currency: str                  # devise de cotation principale (ISO 4217)
    close_local: time              # clôture habituelle (repli si le calendrier ne couvre pas la date)
    delay_min: int                 # délai de publication des cours de clôture par le fournisseur
    data: str = NONE
    providers: tuple[str, ...] = ()
    note: str = ""
    lot_size: int = 1              # quotité de négociation par défaut (surchargée par titre si la source la donne)
    priority: int = 9              # départage du listing principal d'un même ISIN (petit = prioritaire)
    open_local: time | None = None # ouverture habituelle (repli) ; None = 9 h locales

    @property
    def continent(self) -> str:
        return {"Amérique du Nord": "Amériques", "Amérique latine": "Amériques"}.get(self.region, self.region)


EURONEXT_NOTE = ("Instantané officiel de fin de séance (toutes les actions Euronext, OHLC, volume, montant échangé). "
                 "Pas d'historique gratuit : l'historique se constitue jour après jour à partir de la première collecte.")
INDIA_NOTE = ("Bhavcopy officiel quotidien (OHLC, volume, montant, cours de référence ajusté des opérations sur titres) ; "
              "archives publiques permettant de reconstituer l'historique.")

MARKETS: dict[str, Market] = {m.code: m for m in [
    # ------------------------------------------------------------------ Amérique du Nord
    Market("US", "États-Unis (NYSE, Nasdaq, NYSE American)", "US", "États-Unis", "Amérique du Nord",
           ("XNYS", "XNAS", "XASE"), "XNYS", "America/New_York", "USD", time(16, 0), 180, PRICES, ("massive", "tiingo"),
           "Massive (barres de tout le marché, radiés compris, 2 ans) ; Tiingo pour l'univers suivi.", priority=1,
           open_local=time(9, 30)),
    Market("XTSE", "Toronto Stock Exchange", "CA", "Canada", "Amérique du Nord", ("XTSE",), "XTSE", "America/Toronto",
           "CAD", time(16, 0), 120, NONE, (), "Aucune source gratuite de cours de tout le marché identifiée."),
    # ------------------------------------------------------------------ Europe
    Market("XPAR", "Euronext Paris", "FR", "France", "Europe", ("XPAR", "ALXP", "XMLI"), "XPAR", "Europe/Paris", "EUR",
           time(17, 30), 60, PRICES, ("euronext",), EURONEXT_NOTE, priority=2),
    Market("XAMS", "Euronext Amsterdam", "NL", "Pays-Bas", "Europe", ("XAMS",), "XAMS", "Europe/Amsterdam", "EUR",
           time(17, 30), 60, PRICES, ("euronext",), EURONEXT_NOTE, priority=2),
    Market("XBRU", "Euronext Bruxelles", "BE", "Belgique", "Europe", ("XBRU",), "XBRU", "Europe/Brussels", "EUR",
           time(17, 30), 60, PRICES, ("euronext",), EURONEXT_NOTE, priority=2),
    Market("XLIS", "Euronext Lisbonne", "PT", "Portugal", "Europe", ("XLIS",), "XLIS", "Europe/Lisbon", "EUR",
           time(16, 30), 60, PRICES, ("euronext",), EURONEXT_NOTE, priority=2),
    Market("XDUB", "Euronext Dublin", "IE", "Irlande", "Europe", ("XDUB",), "XDUB", "Europe/Dublin", "EUR",
           time(16, 30), 60, PRICES, ("euronext",), EURONEXT_NOTE, priority=2),
    Market("XMIL", "Euronext Milan (Borsa Italiana)", "IT", "Italie", "Europe", ("XMIL",), "XMIL", "Europe/Rome", "EUR",
           time(17, 30), 60, PRICES, ("euronext",), EURONEXT_NOTE, priority=2),
    Market("XOSL", "Euronext Oslo Børs", "NO", "Norvège", "Europe", ("XOSL",), "XOSL", "Europe/Oslo", "NOK",
           time(16, 20), 60, PRICES, ("euronext",), EURONEXT_NOTE, priority=2),
    Market("XETR", "Xetra / Francfort", "DE", "Allemagne", "Europe", ("XETR", "XFRA"), "XETR", "Europe/Berlin", "EUR",
           time(17, 30), 60, NONE, (), "Liste des instruments Xetra non accessible au sondage (504) ; pas de cours "
           "gratuits de tout le marché (le jeu de données public Deutsche Börse n'est plus publié)."),
    Market("XLON", "London Stock Exchange", "GB", "Royaume-Uni", "Europe", ("XLON",), "XLON", "Europe/London", "GBP",
           time(16, 30), 60, NONE, (), "Données LSE payantes ; aucune source gratuite fiable de tout le marché."),
    Market("XMAD", "Bolsa de Madrid", "ES", "Espagne", "Europe", ("XMAD",), "XMAD", "Europe/Madrid", "EUR",
           time(17, 30), 60, NONE, (), "Aucune source gratuite fiable identifiée."),
    Market("XSWX", "SIX Swiss Exchange", "CH", "Suisse", "Europe", ("XSWX",), "XSWX", "Europe/Zurich", "CHF",
           time(17, 30), 60, NONE, (), "Aucune source gratuite fiable identifiée."),
    Market("XSTO", "Nasdaq Stockholm", "SE", "Suède", "Europe", ("XSTO",), "XSTO", "Europe/Stockholm", "SEK",
           time(17, 30), 60, NONE, (), "Aucune source gratuite fiable identifiée."),
    # ------------------------------------------------------------------ Asie-Pacifique
    Market("XTKS", "Tokyo Stock Exchange", "JP", "Japon", "Asie-Pacifique", ("XTKS",), "XTKS", "Asia/Tokyo", "JPY",
           time(15, 30), 120, LIST_ONLY, ("jpx",), "Liste officielle JPX disponible (xlsx) ; cours non gratuits.",
           lot_size=100),
    Market("XSHG", "Bourse de Shanghai", "CN", "Chine", "Asie-Pacifique", ("XSHG",), "XSHG", "Asia/Shanghai", "CNY",
           time(15, 0), 240, QUOTA, ("tiingo",), "Tiingo couvre ≈ 2 800 actions actives, mais le plan gratuit est "
           "limité à 500 titres par mois (déjà utilisés en partie pour les États-Unis) : couverture du marché "
           "impossible sans plan payant.", lot_size=100),
    Market("XSHE", "Bourse de Shenzhen", "CN", "Chine", "Asie-Pacifique", ("XSHE",), "XSHG", "Asia/Shanghai", "CNY",
           time(15, 0), 240, QUOTA, ("tiingo",), "Comme Shanghai : ≈ 3 200 actions chez Tiingo, quota de 500 titres "
           "par mois. Calendrier de Shanghai utilisé (identique).", lot_size=100),
    Market("XHKG", "Hong Kong Exchanges", "HK", "Hong Kong", "Asie-Pacifique", ("XHKG",), "XHKG", "Asia/Hong_Kong",
           "HKD", time(16, 0), 120, NONE, (), "Cotations quotidiennes officielles HKEX accessibles (page de 23 Mo, "
           "sans archive longue) : source identifiée, non intégrée à ce stade."),
    Market("XNSE", "National Stock Exchange of India", "IN", "Inde", "Asie-Pacifique", ("XNSE",), "XBOM",
           "Asia/Kolkata", "INR", time(15, 30), 180, PRICES, ("nse",), INDIA_NOTE + " Calendrier BSE (mêmes fériés).",
           priority=3, open_local=time(9, 15)),
    Market("XBOM", "BSE (Bombay Stock Exchange)", "IN", "Inde", "Asie-Pacifique", ("XBOM",), "XBOM", "Asia/Kolkata",
           "INR", time(15, 30), 180, PRICES, ("bse",), INDIA_NOTE + " Titres déjà cotés au NSE : listing secondaire.",
           priority=4, open_local=time(9, 15)),
    Market("XKRX", "Korea Exchange", "KR", "Corée du Sud", "Asie-Pacifique", ("XKRX",), "XKRX", "Asia/Seoul", "KRW",
           time(15, 30), 120, NONE, (), "Aucune source gratuite fiable identifiée."),
    Market("XASX", "Australian Securities Exchange", "AU", "Australie", "Asie-Pacifique", ("XASX",), "XASX",
           "Australia/Sydney", "AUD", time(16, 0), 120, LIST_ONLY, ("asx",),
           "Liste officielle des sociétés cotées (CSV) ; cours non gratuits."),
    # ------------------------------------------------------------------ Amérique latine
    Market("BVMF", "B3 (Brésil)", "BR", "Brésil", "Amérique latine", ("BVMF",), "BVMF", "America/Sao_Paulo", "BRL",
           time(17, 0), 120, NONE, (), "Aucune source gratuite fiable identifiée."),
    Market("XMEX", "Bolsa Mexicana de Valores", "MX", "Mexique", "Amérique latine", ("XMEX",), "XMEX",
           "America/Mexico_City", "MXN", time(15, 0), 120, NONE, (), "Aucune source gratuite fiable identifiée."),
    # ------------------------------------------------------------------ Afrique et Moyen-Orient
    Market("XJSE", "Johannesburg Stock Exchange", "ZA", "Afrique du Sud", "Afrique", ("XJSE",), "XJSE",
           "Africa/Johannesburg", "ZAR", time(17, 0), 120, NONE, (), "Aucune source gratuite fiable identifiée."),
    Market("XCAS", "Bourse de Casablanca", "MA", "Maroc", "Afrique", ("XCAS",), None, "Africa/Casablanca", "MAD",
           time(15, 30), 120, NONE, (), "Ni source gratuite fiable ni calendrier exchange_calendars."),
    Market("XSAU", "Saudi Exchange (Tadawul)", "SA", "Arabie saoudite", "Moyen-Orient", ("XSAU",), "XSAU",
           "Asia/Riyadh", "SAR", time(15, 0), 120, NONE, (), "Aucune source gratuite fiable ; SAR absent des taux BCE."),
    Market("XDFM", "Dubai Financial Market", "AE", "Émirats arabes unis", "Moyen-Orient", ("XDFM", "XADS"), None,
           "Asia/Dubai", "AED", time(15, 0), 120, NONE, (), "Aucune source gratuite fiable ; AED absent des taux BCE."),
]}

REGIONS = ("Amérique du Nord", "Europe", "Asie-Pacifique", "Amérique latine", "Afrique", "Moyen-Orient")
# raccourcis des commandes Telegram et des filtres
REGION_ALIASES = {"europe": ("Europe",), "usa": ("Amérique du Nord",), "us": ("Amérique du Nord",),
                  "asia": ("Asie-Pacifique",), "asie": ("Asie-Pacifique",), "global": REGIONS}


def get(code: str) -> Market:
    try:
        return MARKETS[code]
    except KeyError:
        raise KeyError(f"place inconnue : {code} (connues : {', '.join(MARKETS)})") from None


def with_prices() -> list[Market]:
    return [m for m in MARKETS.values() if m.data == PRICES]


# ============================================================================ calendrier et séances
@lru_cache(maxsize=None)
def _cal(name: str):
    import exchange_calendars as xcals
    return xcals.get_calendar(name)


@dataclass(frozen=True)
class SessionInfo:
    session: date
    close_utc: datetime
    source: str                     # « exchange_calendars » | « jours ouvrés (calendrier non disponible) »
    open_utc: datetime | None = None


def sessions(m: Market, start: date, end: date) -> list[SessionInfo]:
    """Séances de la place entre deux dates incluses, avec l'heure de clôture en UTC (demi-séances comprises).
    Au-delà de la plage couverte par exchange_calendars (ou sans calendrier) : jours ouvrés, signalé."""
    out: list[SessionInfo] = []
    tz = ZoneInfo(m.tz)
    cal = None
    if m.calendar:
        cal = _cal(m.calendar)
    cal_end = cal.last_session.date() if cal is not None else None
    cal_start = cal.first_session.date() if cal is not None else None
    if cal is not None and start <= cal_end:
        a, b = max(start, cal_start), min(end, cal_end)
        if a <= b:
            idx = cal.sessions_in_range(pd.Timestamp(a), pd.Timestamp(b))
            closes, opens = cal.closes.loc[idx], cal.opens.loc[idx]
            out += [SessionInfo(s.date(), c.to_pydatetime().astimezone(timezone.utc), "exchange_calendars",
                                o.to_pydatetime().astimezone(timezone.utc))
                    for s, c, o in zip(idx, closes, opens)]
    fb_start = start if cal is None else max(start, cal_end + timedelta(days=1))
    for d in pd.bdate_range(fb_start, end) if fb_start <= end else []:
        d = d.date()
        close = datetime.combine(d, m.close_local, tz).astimezone(timezone.utc)
        opn = datetime.combine(d, m.open_local or time(9, 0), tz).astimezone(timezone.utc)
        out.append(SessionInfo(d, close, "jours ouvrés (calendrier non disponible : fériés inconnus)", opn))
    return out


def is_session(m: Market, d: date) -> bool:
    return any(s.session == d for s in sessions(m, d, d))


def last_completed_session(m: Market, now: datetime | None = None) -> date:
    """Dernière séance dont la clôture ET la publication des cours (délai du fournisseur) sont passées."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ss = sessions(m, (now - timedelta(days=21)).date(), (now + timedelta(days=1)).date())
    done = [s.session for s in ss if s.close_utc + timedelta(minutes=m.delay_min) <= now]
    if not done:
        raise RuntimeError(f"{m.code} : aucune séance terminée sur les 21 derniers jours (calendrier ?)")
    return done[-1]


def session_in_progress(m: Market, now: datetime | None = None) -> bool:
    """Vrai entre l'ouverture d'une séance du jour et la fin de son délai de publication : un instantané pris à ce
    moment n'est pas définitif et ne doit pas être enregistré comme une clôture."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local = now.astimezone(ZoneInfo(m.tz)).date()
    for s in sessions(m, local - timedelta(days=1), local + timedelta(days=1)):
        opens = s.close_utc - timedelta(hours=10)          # borne large : aucune place ne dépasse 10 h de séance
        if opens <= now < s.close_utc + timedelta(minutes=m.delay_min):
            return True
    return False


def expected_sessions(now: datetime | None = None, codes: tuple[str, ...] | None = None) -> dict[str, str]:
    """Séance attendue pour chaque place dotée de cours (sert à décider si la chaîne nocturne a tout traité)."""
    ms = [get(c) for c in codes] if codes else with_prices()
    return {m.code: str(last_completed_session(m, now)) for m in ms}


@dataclass
class Freshness:
    expected: date
    last: date | None
    lag: int | None
    stale: bool
    detail: str = field(default="")


def freshness(m: Market, last: date | None, now: datetime | None = None) -> Freshness:
    """Retard en séances de la place (et non en jours calendaires) : 0 = à jour."""
    exp = last_completed_session(m, now)
    if last is None:
        return Freshness(exp, None, None, True, "aucune donnée")
    lag = len([s for s in sessions(m, last + timedelta(days=1), exp)]) if last < exp else 0
    return Freshness(exp, last, lag, lag >= 1, "à jour" if lag == 0 else f"{lag} séance(s) de retard")
