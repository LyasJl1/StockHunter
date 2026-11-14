"""Sauvegarde durable de l'historique mondial (3.2.1), indépendante du cache GitHub Actions.

Le cache Actions est une copie de TRAVAIL : GitHub peut l'effacer (7 jours sans accès, limite de 10 Go). Les
séances Euronext ne sont pas reconstituables (aucun historique gratuit) : chacune est archivée, telle qu'elle a été
stockée (Parquet zstd), dans la base PostgreSQL privée du projet (Railway), avec son empreinte SHA-256.

- Aucune redistribution : base privée, jamais d'artefact ni de dépôt public. Les cours US Tiingo sont déjà dans
  cette base : le statut de licence ne change pas.
- Incrémental et reprenable : seules les séances absentes de l'archive sont envoyées ; une séance archivée n'est
  jamais modifiée ni supprimée (rétention illimitée).
- Volume contrôlé AVANT écriture : budget de l'archive (défaut 60 Mo) et occupation du volume PostgreSQL (refus
  au-delà de 85 %). Euronext ≈ 0,1 Mo par séance, soit ≈ 30 Mo par an.
- Intégrité : `verify` recalcule l'empreinte de chaque fichier local et de chaque copie archivée.
- Restauration : `restore` recrée les fichiers manquants (jamais d'écrasement d'un fichier différent) et le
  manifeste, puis vérifie les empreintes. Lancée automatiquement en début de collecte si le cache a disparu.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from stockhunter.db.models import WorldArchive
from stockhunter.db.session import session_scope
from stockhunter.world.store import WorldStore

NON_RECONSTRUCTIBLE = ("XPAR", "XAMS", "XBRU", "XLIS", "XDUB", "XMIL", "XOSL")   # Euronext : pas d'historique gratuit
BUDGET_MB = 60.0
MAX_DB_SHARE = 0.85


class ArchiveRefused(RuntimeError):
    pass


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@dataclass
class ArchiveReport:
    archived: int = 0
    already: int = 0
    restored: int = 0
    verified: int = 0
    mismatches: list[str] = field(default_factory=list)
    missing_local: list[str] = field(default_factory=list)
    missing_archive: list[str] = field(default_factory=list)
    refused: str | None = None
    size_mb: float = 0.0


def archive_size_mb(engine: Engine) -> float:
    with session_scope(engine) as s:
        return float(s.scalar(select(func.coalesce(func.sum(WorldArchive.nbytes), 0))) or 0) / 1e6


def _db_share(engine: Engine) -> float | None:
    from stockhunter.db.copy import storage
    st = storage(engine)
    return st.get("part")


def push(engine: Engine, store: WorldStore, markets: tuple[str, ...] = NON_RECONSTRUCTIBLE,
         budget_mb: float = BUDGET_MB) -> ArchiveReport:
    rep = ArchiveReport()
    with session_scope(engine) as s:
        have = {(m, d) for m, d in s.execute(select(WorldArchive.market, WorldArchive.session)
                                             .where(WorldArchive.market.in_(markets)))}
    todo = [(m, d) for m in markets for d in store.stored_sessions(m) if (m, d) not in have]
    rep.already = sum(1 for m in markets for d in store.stored_sessions(m) if (m, d) in have)
    if not todo:
        rep.size_mb = archive_size_mb(engine)
        return rep
    share = _db_share(engine)
    if share is not None and share >= MAX_DB_SHARE:
        rep.refused = f"volume PostgreSQL à {share:.0%} (≥ {MAX_DB_SHARE:.0%}) : archivage suspendu, rien n'est supprimé"
        return rep
    used = archive_size_mb(engine)
    for m, d in todo:
        path = store.path(m, d)
        if not path.exists():
            rep.missing_local.append(f"{m} {d}")
            continue
        blob = path.read_bytes()
        if used + len(blob) / 1e6 > budget_mb:
            rep.refused = f"budget de l'archive atteint ({used:.1f} / {budget_mb:.0f} Mo)"
            break
        meta = store.manifest["sessions"].get(m, {}).get(str(d), {})
        with session_scope(engine) as s:
            s.add(WorldArchive(market=m, session=d, provider=meta.get("provider", "?"), rows=int(meta.get("rows", 0)),
                               nbytes=len(blob), sha256=sha256(blob), manifest_json=json.dumps(meta, ensure_ascii=False),
                               blob=blob))
        used += len(blob) / 1e6
        rep.archived += 1
    rep.size_mb = round(used, 2)
    return rep


def verify(engine: Engine, store: WorldStore, markets: tuple[str, ...] = NON_RECONSTRUCTIBLE) -> ArchiveReport:
    """Empreintes : copie archivée (recalculée) et fichier local doivent concorder ; séances non archivées listées."""
    rep = ArchiveReport()
    with session_scope(engine) as s:
        rows = list(s.execute(select(WorldArchive.market, WorldArchive.session, WorldArchive.sha256, WorldArchive.blob)
                              .where(WorldArchive.market.in_(markets))))
    arch = {}
    for m, d, h, blob in rows:
        arch[(m, d)] = h
        if sha256(blob) != h:
            rep.mismatches.append(f"{m} {d} : copie archivée altérée")
            continue
        p = store.path(m, d)
        if p.exists():
            if sha256(p.read_bytes()) != h:
                rep.mismatches.append(f"{m} {d} : fichier local différent de l'archive")
            else:
                rep.verified += 1
        else:
            rep.missing_local.append(f"{m} {d}")
    for m in markets:
        for d in store.stored_sessions(m):
            if (m, d) not in arch:
                rep.missing_archive.append(f"{m} {d}")
    rep.size_mb = archive_size_mb(engine)
    return rep


def restore(engine: Engine, store: WorldStore, markets: tuple[str, ...] = NON_RECONSTRUCTIBLE) -> ArchiveReport:
    """Recrée les séances absentes du stockage local depuis l'archive (empreinte vérifiée). Un fichier local
    présent n'est jamais écrasé ; une divergence est signalée."""
    rep = ArchiveReport()
    with session_scope(engine) as s:
        rows = list(s.execute(select(WorldArchive.market, WorldArchive.session, WorldArchive.sha256, WorldArchive.blob,
                                     WorldArchive.manifest_json).where(WorldArchive.market.in_(markets))
                              .order_by(WorldArchive.market, WorldArchive.session)))
    for m, d, h, blob, meta in rows:
        if sha256(blob) != h:
            rep.mismatches.append(f"{m} {d} : copie archivée altérée (non restaurée)")
            continue
        p = store.path(m, d)
        if p.exists():
            if sha256(p.read_bytes()) != h:
                rep.mismatches.append(f"{m} {d} : fichier local différent (conservé, non écrasé)")
            elif str(d) not in store.manifest["sessions"].get(m, {}):
                store.manifest["sessions"].setdefault(m, {})[str(d)] = {**json.loads(meta), "restaure_de": "archive"}
                rep.restored += 1
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, p)
        if sha256(p.read_bytes()) != h:
            rep.mismatches.append(f"{m} {d} : écriture incorrecte")
            continue
        store.manifest["sessions"].setdefault(m, {})[str(d)] = {**json.loads(meta), "restaure_de": "archive"}
        rep.restored += 1
    if rep.restored:
        store._save_manifest()
    return rep


def status(engine: Engine) -> dict:
    with session_scope(engine) as s:
        rows = s.execute(select(WorldArchive.market, func.count(), func.min(WorldArchive.session),
                                func.max(WorldArchive.session), func.sum(WorldArchive.nbytes))
                         .group_by(WorldArchive.market)).all()
    return {"places": {m: {"seances": n, "premiere": str(a), "derniere": str(b), "mo": round((nb or 0) / 1e6, 2)}
                       for m, n, a, b, nb in rows},
            "total_mo": round(sum((r[4] or 0) for r in rows) / 1e6, 2), "budget_mo": BUDGET_MB}
