"""Collecte mondiale (3.2) : change BCE, instantanés Euronext, bhavcopy NSE / BSE, référentiel.

Chaque collecte est idempotente et reprenable : une séance stockée n'est jamais retéléchargée ; une séance
absente chez la source est consignée (avec le motif) au lieu d'être inventée. Les échecs d'une région n'arrêtent
pas les autres : chaque place renvoie son propre bilan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from stockhunter.db.models import WorldListing, WorldTickerChange
from stockhunter.db.session import session_scope
from stockhunter.world import markets as mk
from stockhunter.world import providers as pv
from stockhunter.world.store import StorageBudgetExceeded, WorldStore

log = logging.getLogger(__name__)
EURONEXT_MARKETS = ("XPAR", "XAMS", "XBRU", "XLIS", "XDUB", "XMIL", "XOSL")
INACTIVE_AFTER_SESSIONS = 10


@dataclass
class Report:
    market: str
    status: str = "ok"                    # ok | rien à faire | partiel | échec
    critical: bool = False                # 3.2.1 : données corrompues / exception = échec critique (chaîne rouge)
    written: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    detail: str = ""

    def line(self) -> str:
        return (f"{self.market:6} {self.status:12} écrites {len(self.written):3}  absentes {len(self.missing):3}  "
                f"erreurs {len(self.errors):2}  {self.detail}" + (f"  [{self.errors[0]}]" if self.errors else ""))


# ============================================================================ change
def sync_fx(store: WorldStore, fetch: pv.Fetcher, start: date = date(2023, 1, 1)) -> Report:
    rep = Report("FX-BCE")
    fx = store.load_fx()
    since = start if fx.empty else max(fx["date"]) - timedelta(days=7)
    try:
        df = pv.parse_ecb(fetch(pv.ECB_URL.format(start=since.isoformat()), "csv"))
    except (pv.SourceUnavailable, pv.NoSession) as exc:
        rep.status, rep.errors = "échec", [str(exc)]
        rep.critical = isinstance(exc, pv.DataCorrupted)
        return rep
    added = store.save_fx(df)
    rep.status = "ok" if added else "rien à faire"
    rep.detail = f"{added} taux ajoutés ; dernier taux du {store.manifest['fx']['last']}"
    return rep


# ============================================================================ Euronext
def sync_euronext(store: WorldStore, engine: Engine | None, fetch: pv.Fetcher, now: datetime | None = None
                  ) -> list[Report]:
    """Un téléchargement couvre les 7 places. Refusé pendant une séance (instantané non définitif). Une place dont
    la dernière séance terminée diffère de celle de Paris (férié local) est écartée ce jour-là : l'instantané ne
    permettrait pas de dater ses lignes avec certitude."""
    now = now or datetime.now(timezone.utc)
    reps = {c: Report(c) for c in EURONEXT_MARKETS}
    paris = mk.get("XPAR")
    if any(mk.session_in_progress(mk.get(c), now) for c in EURONEXT_MARKETS):
        for r in reps.values():
            r.status, r.detail = "rien à faire", "séance en cours ou cours pas encore publiés : instantané non définitif"
        return list(reps.values())
    session = mk.last_completed_session(paris, now)
    todo = [c for c in EURONEXT_MARKETS if not store.has(c, session)]
    if not todo:
        for r in reps.values():
            r.status, r.detail = "rien à faire", f"séance du {session} déjà stockée"
        return list(reps.values())
    try:
        df, meta = pv.parse_euronext(fetch(pv.EURONEXT_URL, "csv"), session)
    except (pv.SourceUnavailable, pv.NoSession) as exc:
        for c in todo:
            reps[c].status, reps[c].errors = "échec", [str(exc)]
            reps[c].critical = isinstance(exc, pv.DataCorrupted)
        return list(reps.values())
    for c in todo:
        m, r = mk.get(c), reps[c]
        if mk.last_completed_session(m, now) != session:
            r.status, r.detail = "rien à faire", f"dernière séance de la place ≠ {session} (Paris) : non datable"
            continue
        part = df[df["market"] == c]
        if part.empty:
            r.status, r.errors = "échec", ["aucune ligne pour cette place dans l'instantané"]
            continue
        try:
            if store.write_session(c, session, part, "euronext", {"source_stamp": meta["horodatage_source"]}):
                r.written.append(str(session))
        except StorageBudgetExceeded as exc:
            r.status, r.errors = "échec", [str(exc)]
            continue
        r.detail = f"séance du {session} : {len(part)} titres, {int(part['traded'].sum())} échangés"
        if engine is not None:
            update_master(engine, part, "euronext")
    return list(reps.values())


# ============================================================================ Inde
def india_targets(m: mk.Market, store: WorldStore, now: datetime, backfill_sessions: int) -> list[date]:
    """Séances à collecter, de la plus récente à la plus ancienne, sur `backfill_sessions` séances."""
    exp = mk.last_completed_session(m, now)
    ss = [s.session for s in mk.sessions(m, exp - timedelta(days=int(backfill_sessions * 1.6) + 10), exp)]
    ss = ss[-backfill_sessions:] if backfill_sessions > 0 else ss[-1:]
    return [d for d in reversed(ss) if not store.has(m.code, d) and not store.is_missing(m.code, d)]


def sync_india(store: WorldStore, engine: Engine | None, fetch: pv.Fetcher, code: str, now: datetime | None = None,
               backfill_sessions: int = 5, max_files: int = 40) -> Report:
    now = now or datetime.now(timezone.utc)
    m, rep = mk.get(code), Report(code)
    if not mk._india_enabled(code):
        rep.status = "rien à faire"
        rep.detail = ("collecte NSE/BSE suspendue : droits de collecte, stockage et simulation non vérifiés ; "
                      "aucune requête envoyée")
        return rep
    url = pv.nse_url if code == "XNSE" else pv.bse_url
    targets = india_targets(m, store, now, backfill_sessions)
    if not targets:
        rep.status, rep.detail = "rien à faire", "séances déjà stockées"
        return rep
    expected = mk.last_completed_session(m, now)
    consecutive_fail = 0
    for d in targets[:max_files]:
        try:
            df, meta = pv.parse_bhavcopy(fetch(url(d), "csv"), d, code)
        except pv.NoSession:
            if d == expected and now - datetime.combine(d, datetime.min.time(), timezone.utc) < timedelta(days=2):
                rep.errors.append(f"{d} : pas encore publié")      # réessayé au prochain passage
            else:
                store.mark_missing(code, d, "absent chez la source (férié non prévu au calendrier ?)")
                rep.missing.append(str(d))
            continue
        except pv.DataCorrupted as exc:                 # fichier reçu mais inexploitable : critique
            rep.errors.append(f"{d} : {exc}")
            rep.critical = True
            continue
        except pv.SourceUnavailable as exc:
            rep.errors.append(f"{d} : {exc}")
            consecutive_fail += 1
            if consecutive_fail >= 3:                 # source bloquée : on s'arrête, on ne martèle pas
                rep.errors.append("3 échecs consécutifs : collecte interrompue")
                break
            continue
        consecutive_fail = 0
        if df.empty:
            store.mark_missing(code, d, "fichier sans action")
            rep.missing.append(str(d))
            continue
        try:
            store.write_session(code, d, df, m.providers[0], meta)
        except StorageBudgetExceeded as exc:
            rep.errors.append(str(exc))
            break
        rep.written.append(str(d))
        if engine is not None:
            update_master(engine, df, m.providers[0])
    rest = len(targets) - min(len(targets), max_files)
    rep.status = "échec" if rep.errors and not rep.written else ("partiel" if rep.errors else "ok")
    if rep.errors and all("pas encore publié" in e for e in rep.errors):
        rep.status = "partiel" if rep.written else "en attente"   # publication retardée : ni panne ni succès
    rep.detail = f"{len(rep.written)} séance(s) écrite(s), {rest} restante(s) à reconstituer"
    return rep


# ============================================================================ référentiel
def update_master(engine: Engine, df: pd.DataFrame, source: str) -> dict[str, int]:
    """Insère / met à jour les listings vus dans une séance ; consigne les changements de symbole ; passe en
    INACTIF les listings non vus depuis `INACTIVE_AFTER_SESSIONS` séances (jamais supprimés)."""
    out = {"nouveaux": 0, "maj": 0, "changements_symbole": 0, "inactifs": 0}
    if df.empty:
        return out
    market = df["market"].iloc[0]
    m = mk.get(market)
    d = pd.Timestamp(df["date"].iloc[0]).date()
    with session_scope(engine) as s:
        existing = {x.isin: x for x in s.scalars(select(WorldListing).where(WorldListing.market == market))}
        for r in df.itertuples(index=False):
            x = existing.get(r.isin)
            if x is None:
                s.add(WorldListing(market=market, isin=r.isin, ticker=r.ticker, name=r.name or None, country=m.country,
                                   issuer_country=r.isin[:2], currency=r.currency or m.currency, segment=r.segment or None,
                                   lot_size=int(r.lot_size or 1), status="ACTIF", first_seen=d, last_seen=d,
                                   source=source))
                out["nouveaux"] += 1
                continue
            if d < x.last_seen:                         # séance ancienne (reconstitution) : seul first_seen bouge
                x.first_seen = min(x.first_seen, d)
                continue
            if r.ticker and r.ticker != x.ticker:
                s.add(WorldTickerChange(market=market, isin=r.isin, old_ticker=x.ticker, new_ticker=r.ticker,
                                        changed_on=d))
                x.ticker = r.ticker
                out["changements_symbole"] += 1
            x.name, x.segment, x.lot_size = r.name or x.name, r.segment or x.segment, int(r.lot_size or x.lot_size)
            x.last_seen, x.status = d, "ACTIF"
            out["maj"] += 1
        seen = set(df["isin"])
        recent = [ss.session for ss in mk.sessions(m, d - timedelta(days=40), d)]
        cutoff = recent[-INACTIVE_AFTER_SESSIONS] if len(recent) >= INACTIVE_AFTER_SESSIONS else None
        for isin, x in existing.items():
            if isin not in seen and cutoff and x.last_seen < cutoff and x.status == "ACTIF":
                x.status = "INACTIF"
                out["inactifs"] += 1
    return out


def primary_listings(engine: Engine) -> dict[str, str]:
    """ISIN -> place du listing principal : priorité de la place (NSE avant BSE, etc.), puis ancienneté.
    Sert à ne pas compter deux fois une société cotée sur deux places."""
    with session_scope(engine) as s:
        rows = s.execute(select(WorldListing.isin, WorldListing.market, WorldListing.first_seen)
                         .where(WorldListing.status == "ACTIF")).all()
    best: dict[str, tuple] = {}
    for isin, market, first in rows:
        key = (mk.get(market).priority, first, market)
        if isin not in best or key < best[isin][0]:
            best[isin] = (key, market)
    return {i: v[1] for i, v in best.items()}


def counts(engine: Engine) -> dict[str, dict[str, int]]:
    """Listings par place : actifs, inactifs, sociétés distinctes (ISIN) dont c'est le listing principal."""
    prim = primary_listings(engine)
    out: dict[str, dict[str, int]] = {}
    with session_scope(engine) as s:
        for market, isin, status in s.execute(select(WorldListing.market, WorldListing.isin, WorldListing.status)):
            c = out.setdefault(market, {"actifs": 0, "inactifs": 0, "principal": 0})
            c["actifs" if status == "ACTIF" else "inactifs"] += 1
            c["principal"] += int(prim.get(isin) == market)
    return out
