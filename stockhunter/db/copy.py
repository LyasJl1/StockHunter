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


# État technique que le serveur web écrit dès son démarrage (curseur des commandes Telegram, réponse à
# une commande). Ce ne sont pas des données de marché : s'il n'y a QUE cela dans la cible, on le remplace
# par l'état de la base source. Toute autre donnée présente bloque la copie.
RUNTIME_TABLES = {"kv_store", "notifications"}


def _counts(conn) -> dict[str, int]:
    return {t.name: conn.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}


def copy_database(src: Engine, dst: Engine, progress=print) -> dict[str, int]:
    copied = {}
    with src.connect() as s, dst.begin() as d:
        if dst.dialect.name == "postgresql":
            # Le serveur web tourne pendant la copie : ses écritures attendent la fin de la transaction.
            d.execute(text(f"LOCK TABLE {', '.join(sorted(RUNTIME_TABLES))} IN ACCESS EXCLUSIVE MODE"))
        existing = {k: v for k, v in _counts(d).items() if v}
        blocking = {k: v for k, v in existing.items() if k not in RUNTIME_TABLES}
        if blocking:
            raise RuntimeError(f"la base cible n'est pas vide ({existing}) : copie refusée")
        for name in sorted(existing):
            table = Base.metadata.tables[name]
            d.execute(table.delete())
            progress(f"  état technique du serveur remplacé : {name} ({existing[name]} ligne(s))")
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


WAL_SETTINGS = {"max_wal_size": "64MB", "min_wal_size": "32MB"}


def tune(engine: Engine, progress=print) -> dict:
    """Entretien PostgreSQL pour un petit volume (0,5 Go) : plafonne les journaux WAL (1 Go par défaut, ce qui a
    saturé le disque le 13/11/2026), force un point de contrôle pour libérer l'espace et nettoie les tables.
    Réversible (`ALTER SYSTEM RESET ...`). Aucune donnée n'est modifiée. Sans effet sur SQLite."""
    if engine.dialect.name != "postgresql":
        progress("Base SQLite : aucun réglage nécessaire.")
        return {}
    out = {}
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        def sizes(tag):
            out[f"base_{tag}_mo"] = round(c.execute(text("SELECT pg_database_size(current_database())")).scalar() / 1048576, 1)
            try:
                out[f"wal_{tag}_mo"] = round((c.execute(text("SELECT COALESCE(sum(size), 0) FROM pg_ls_waldir()")).scalar() or 0) / 1048576, 1)
            except Exception:                               # droit absent : information facultative
                out[f"wal_{tag}_mo"] = None
        sizes("avant")
        for k, v in WAL_SETTINGS.items():
            if c.execute(text(f"SHOW {k}")).scalar() != v:
                c.execute(text(f"ALTER SYSTEM SET {k} = '{v}'"))
        c.execute(text("SELECT pg_reload_conf()"))
        c.execute(text("CHECKPOINT"))
        c.execute(text("CHECKPOINT"))                       # le second recycle les segments libérés par le premier
        c.execute(text("VACUUM (ANALYZE)"))
        sizes("apres")
    progress(f"Entretien PostgreSQL : {out}")
    return out
