"""Upsert portable SQLite/PostgreSQL : garantit la déduplication à l'ingestion."""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from sqlalchemy.orm import Session


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
            stmt = stmt.on_conflict_do_update(
                index_elements=list(conflict_cols),
                set_={c: stmt.excluded[c] for c in update_cols}  # [] : 'items' est aussi une méthode,
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict_cols))
        session.execute(stmt)
        total += len(part)
    return total
