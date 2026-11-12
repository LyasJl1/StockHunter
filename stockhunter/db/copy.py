"""Copie intégrale d'une base vers une autre (ex. SQLite du cache GitHub -> PostgreSQL Railway).

- La cible doit être migrée (`init-db`) et VIDE : on refuse d'écraser des données existantes.
- Les tables sont copiées dans l'ordre des clés étrangères, par lots, dans une seule transaction.
- Les séquences PostgreSQL sont recalées sur les identifiants copiés.
- Contrôle final : nombre de lignes identique table par table.
"""

from __future__ import annotations

from sqlalchemy import func, insert, select, text
from sqlalchemy.engine import Engine

from stockhunter.db.models import Base

BATCH = 2000


def row_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as c:
        return {t.name: c.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}


def copy_database(src: Engine, dst: Engine, progress=print) -> dict[str, int]:
    existing = {k: v for k, v in row_counts(dst).items() if v}
    if existing:
        raise RuntimeError(f"la base cible n'est pas vide ({existing}) : copie refusée")
    copied = {}
    with src.connect() as s, dst.begin() as d:
        for table in Base.metadata.sorted_tables:
            n, batch = 0, []
            for row in s.execute(select(table)).mappings():
                batch.append(dict(row))
                if len(batch) >= BATCH:
                    d.execute(insert(table), batch)
                    n += len(batch)
                    batch = []
            if batch:
                d.execute(insert(table), batch)
                n += len(batch)
            copied[table.name] = n
            if n:
                progress(f"  {table.name:24} {n:>8} lignes")
        if dst.dialect.name == "postgresql":
            for table in Base.metadata.sorted_tables:
                pk = list(table.primary_key.columns)
                if len(pk) == 1 and pk[0].autoincrement in (True, "auto") and pk[0].type.python_type is int:
                    d.execute(text(f"SELECT setval(pg_get_serial_sequence('{table.name}', '{pk[0].name}'), "
                                   f"COALESCE((SELECT MAX({pk[0].name}) FROM {table.name}), 0) + 1, false)"))
    after = row_counts(dst)
    diff = {k: (copied[k], after[k]) for k in copied if copied[k] != after[k]}
    if diff:
        raise RuntimeError(f"contrôle échoué, lignes copiées vs présentes : {diff}")
    return copied
