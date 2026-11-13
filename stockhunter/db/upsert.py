"""Upsert portable SQLite/PostgreSQL : garantit la déduplication à l'ingestion."""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from sqlalchemy import or_
from sqlalchemy.orm import Session

VOLATILE_COLS = {"fetched_at"}   # horodatage de collecte : ne justifie pas à lui seul une réécriture


def upsert(
    session: Session,
    model: type,
    rows: Iterable[dict[str, Any]],
    conflict_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
    chunk: int = 500,
) -> int:
    # déduplication intra-lot (PostgreSQL refuse de mettre à jour deux fois la même ligne)
    dedup: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        dedup[tuple(r[c] for c in conflict_cols)] = r
    rows = list(dedup.values())
    if not rows:
        return 0
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover
        raise NotImplementedError(dialect)

    if update_cols is None:
        update_cols = [k for k in rows[0] if k not in conflict_cols]
    total = 0
    for i in range(0, len(rows), chunk):
        part = rows[i : i + chunk]
        stmt = insert(model).values(part)
        if update_cols:
            # Ne réécrit une ligne que si une valeur a réellement changé (l'horodatage de collecte seul ne compte
            # pas) : sans cela, chaque nuit réécrivait toutes les lignes, ce qui gonflait les journaux WAL et les
            # tables de PostgreSQL jusqu'à saturer le disque (incident du 13/11/2026).
            table = model.__table__
            compared = [c for c in update_cols if c not in VOLATILE_COLS]
            changed = or_(*[table.c[c].is_distinct_from(stmt.excluded[c]) for c in compared]) if compared else None
            stmt = stmt.on_conflict_do_update(
                index_elements=list(conflict_cols),
                set_={c: stmt.excluded[c] for c in update_cols},  # [] : 'items' est aussi une méthode,
                where=changed,
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict_cols))
        session.execute(stmt)
        total += len(part)
    return total
