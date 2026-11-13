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
VOLUME_MB_DEFAULT = 500          # volume du plan gratuit Railway
THRESHOLDS = (0.80, 0.90, 0.95)


DATA_DIR_SQL = """
WITH RECURSIVE f(path, isdir) AS (
    SELECT name, (pg_stat_file(name, true)).isdir FROM pg_ls_dir('.', true, false) AS name
  UNION ALL
    SELECT f.path || '/' || c, (pg_stat_file(f.path || '/' || c, true)).isdir
    FROM f, LATERAL pg_ls_dir(f.path, true, false) AS c WHERE f.isdir
)
SELECT COALESCE(sum((pg_stat_file(path, true)).size) FILTER (WHERE NOT isdir), 0) FROM f
"""


def storage(engine: Engine, volume_mb: float | None = None) -> dict:
    """Occupation du volume PostgreSQL. Mesure réelle du répertoire de données (tous les fichiers, WAL compris)
    quand le rôle le permet ; sinon estimation bases + WAL. S'y ajoute la réserve du système de fichiers
    (SH_DB_FS_OVERHEAD_MB, mesurée sur Railway). Niveau : ok | attention (≥ 80 %) | alerte (≥ 90 %) |
    critique (≥ 95 %). Lecture seule."""
    if engine.dialect.name != "postgresql":
        return {"dialect": engine.dialect.name, "niveau": "ok"}
    from stockhunter.config import Settings
    st = Settings()
    volume_mb = volume_mb or st.db_volume_mb or VOLUME_MB_DEFAULT
    with engine.connect() as c:
        dbs = float(c.execute(text("SELECT COALESCE(sum(pg_database_size(datname)), 0) FROM pg_database")).scalar())
        try:
            wal = float(c.execute(text("SELECT COALESCE(sum(size), 0) FROM pg_ls_waldir()")).scalar())
        except Exception:
            wal = None
        try:
            ddir = float(c.execute(text(DATA_DIR_SQL)).scalar())
            method = "répertoire de données mesuré"
        except Exception:
            ddir, method = dbs + (wal or 0), "estimation bases + WAL"
        max_wal = c.execute(text("SHOW max_wal_size")).scalar()
    used = ddir / 1048576 + st.db_fs_overhead_mb
    share = used / volume_mb
    level = ("critique" if share >= THRESHOLDS[2] else "alerte" if share >= THRESHOLDS[1]
             else "attention" if share >= THRESHOLDS[0] else "ok")
    return {"dialect": "postgresql", "bases_mo": round(dbs / 1048576, 1),
            "wal_mo": None if wal is None else round(wal / 1048576, 1), "repertoire_mo": round(ddir / 1048576, 1),
            "reserve_fs_mo": st.db_fs_overhead_mb, "utilise_mo": round(used, 1), "volume_mo": volume_mb,
            "part": round(share, 4), "niveau": level, "methode": method, "max_wal_size": max_wal}


def reclaim_wal(engine: Engine, progress=print, target_files: int = 4, max_rounds: int = 24) -> dict:
    """Rend au disque les segments WAL préalloués devenus inutiles, SANS supprimer de fichier à la main.

    PostgreSQL ne supprime un segment que lorsqu'il est derrière le point de reprise d'un point de contrôle ; les
    segments « recyclés » d'avance (240 Mo le 13/11/2026) ne le deviennent qu'une fois traversés. `pg_switch_wal()`
    fait passer au segment suivant sans écrire 16 Mo ; le CHECKPOINT qui suit laisse alors PostgreSQL supprimer ce
    qui dépasse le plafond `max_wal_size`. Opérations standard, sans perte de données."""
    rounds = []
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        count = lambda: int(c.execute(text("SELECT count(*) FROM pg_ls_waldir() WHERE name ~ '^[0-9A-F]{24}$'")).scalar())
        start = count()
        for _ in range(max_rounds):
            n = count()
            rounds.append(n)
            if n <= target_files:
                break
            c.execute(text("SELECT pg_switch_wal()"))
            c.execute(text("CHECKPOINT"))
        end = count()
    out = {"segments_avant": start, "segments_apres": end, "tours": len(rounds) - 1,
           "libere_mo": (start - end) * 16}
    progress(f"Récupération WAL : {out}")
    return out


def tune(engine: Engine, progress=print, vacuum: bool = False) -> dict:
    """Entretien PostgreSQL pour un petit volume (0,5 Go) : plafonne les journaux WAL (1 Go par défaut, ce qui a
    saturé le disque le 13/11/2026), puis rend les segments inutiles au disque (`reclaim_wal`). Aucun fichier n'est
    supprimé à la main, aucune donnée n'est modifiée ; réversible (`ALTER SYSTEM RESET ...`). Le nettoyage des
    tables est laissé à l'autovacuum (le VACUUM manuel du 13/11 a échoué faute de place : il n'est lancé que sur
    demande et avec de la marge). Sans effet sur SQLite."""
    if engine.dialect.name != "postgresql":
        progress("Base SQLite : aucun réglage nécessaire.")
        return {}
    out = {"avant": storage(engine)}
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        changed = []
        for k, v in WAL_SETTINGS.items():
            if c.execute(text(f"SHOW {k}")).scalar().replace(" ", "") != v:
                c.execute(text(f"ALTER SYSTEM SET {k} = '{v}'"))
                changed.append(k)
        if changed:
            c.execute(text("SELECT pg_reload_conf()"))
        out["reglages_modifies"] = changed
    out["wal"] = reclaim_wal(engine, progress)
    out["apres_wal"] = storage(engine)
    if vacuum and out["apres_wal"]["part"] < THRESHOLDS[0]:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text("VACUUM (ANALYZE, PARALLEL 0)"))
        out["vacuum"] = "fait"
    else:
        out["vacuum"] = "laissé à l'autovacuum" if not vacuum else "non fait (marge insuffisante, < 20 % libre)"
    out["apres"] = storage(engine)
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
    r = q(DATA_DIR_SQL)
    out["repertoire_donnees_mo"] = mb(float(r[0][0])) if r and r[0][0] != "ERREUR" else r[0][1]
    out["stockage"] = storage(engine)
    return out
