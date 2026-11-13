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


def diagnose(engine: Engine) -> dict:
    """Diagnostic EN LECTURE SEULE de l'espace PostgreSQL : tables (données, index, TOAST), lignes mortes, WAL,
    slots de réplication (qui retiennent le WAL), fichiers temporaires, réglages. Aucune écriture."""
    if engine.dialect.name != "postgresql":
        return {"dialect": engine.dialect.name}
    out: dict = {"dialect": "postgresql"}

    def q(sql, default=None):
        try:
            with engine.connect() as c:
                return c.execute(text(sql)).all()
        except Exception as exc:                      # droit absent : on note et on continue
            return default if default is not None else [("ERREUR", str(exc).splitlines()[0][:160])]

    mb = lambda b: None if b is None else round(b / 1048576, 1)
    out["base_mo"] = mb(q("SELECT pg_database_size(current_database())")[0][0])
    out["toutes_bases_mo"] = {n: mb(s) for n, s in q("SELECT datname, pg_database_size(datname) FROM pg_database")}
    out["tables"] = [
        {"table": r[0], "total_mo": mb(r[1]), "donnees_mo": mb(r[2]), "index_mo": mb(r[3]), "toast_mo": mb(r[4]),
         "lignes": r[5], "mortes": r[6], "dernier_autovacuum": str(r[7])[:19] if r[7] else None}
        for r in q("""SELECT relname, pg_total_relation_size(relid), pg_relation_size(relid),
                             pg_indexes_size(relid),
                             pg_total_relation_size(relid) - pg_relation_size(relid) - pg_indexes_size(relid),
                             n_live_tup, n_dead_tup, last_autovacuum
                      FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC""", [])]
    out["index_les_plus_gros"] = [{"index": n, "table": t, "mo": mb(s), "utilisations": u} for n, t, s, u in q(
        """SELECT indexrelname, relname, pg_relation_size(indexrelid), idx_scan FROM pg_stat_user_indexes
           ORDER BY pg_relation_size(indexrelid) DESC LIMIT 15""", [])]
    w = q("SELECT count(*), COALESCE(sum(size), 0) FROM pg_ls_waldir()")
    out["wal"] = {"fichiers": w[0][0], "mo": mb(w[0][1])} if w and w[0][0] != "ERREUR" else {"erreur": w[0][1]}
    t = q("SELECT count(*), COALESCE(sum(size), 0) FROM pg_ls_tmpdir()")
    out["temporaires"] = {"fichiers": t[0][0], "mo": mb(t[0][1])} if t and t[0][0] != "ERREUR" else {"erreur": t[0][1]}
    lg = q("SELECT count(*), COALESCE(sum(size), 0) FROM pg_ls_logdir()")
    out["journaux_serveur"] = {"fichiers": lg[0][0], "mo": mb(lg[0][1])} if lg and lg[0][0] != "ERREUR" else {"erreur": lg[0][1]}
    out["slots_replication"] = [{"slot": s, "actif": a, "wal_retenu_mo": mb(r)} for s, a, r in q(
        """SELECT slot_name, active, pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) FROM pg_replication_slots""", [])]
    out["reglages"] = {k: v for k, v in q(
        """SELECT name, setting || COALESCE(' ' || unit, '') FROM pg_settings WHERE name IN
           ('max_wal_size','min_wal_size','wal_keep_size','max_slot_wal_keep_size','archive_mode','archive_command',
            'checkpoint_timeout','wal_level','autovacuum','shared_buffers','work_mem','temp_file_limit',
            'data_directory','log_destination','logging_collector','server_version')""", [])}
    out["config_auto"] = [r[0] for r in q("SELECT name || ' = ' || setting FROM pg_file_settings WHERE sourcefile LIKE '%auto.conf'", [])]
    out["fichiers_temporaires_cumules"] = {n: {"fichiers": f, "mo": mb(b)} for n, f, b in q(
        "SELECT datname, temp_files, temp_bytes FROM pg_stat_database WHERE datname IS NOT NULL", [])}
    top = q("""SELECT d, sum(s) FROM (SELECT split_part(f, '/', 1) d, (pg_stat_file(f)).size s
               FROM (SELECT 'base/' || x f FROM pg_ls_dir('base') x) z) y GROUP BY d""", [])
    out["repertoire_base_mo"] = mb(sum(int(s or 0) for _d, s in top)) if top and top[0][0] != "ERREUR" else None
    return out
