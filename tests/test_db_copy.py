"""Copie de base (migration SQLite -> PostgreSQL). Données SYNTHÉTIQUES (symboles ZZ*)."""

import pytest

from stockhunter.db.copy import copy_database, row_counts
from stockhunter.db.models import Base, Instrument, KeyValue, NotificationLog
from stockhunter.db.session import get_engine, session_scope


@pytest.fixture
def source(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path / 'src.db'}")
    Base.metadata.create_all(eng)
    with session_scope(eng) as s:
        s.add(Instrument(symbol="ZZA"))
        s.add(KeyValue(key="telegram_offset", value="100"))
        s.add(NotificationLog(dedup_key="src", kind="report_evening", text="t", status="SENT"))
    return eng


def test_copy_replaces_only_web_server_runtime_state(source, engine):
    with session_scope(engine) as s:   # ce que le serveur web écrit au démarrage
        s.add(KeyValue(key="telegram_offset", value="5"))
        s.add(NotificationLog(dedup_key="cmd", kind="command", text="/status", status="SENT"))
    msgs = []
    copied = copy_database(source, engine, progress=msgs.append)
    assert copied["instruments"] == 1 and row_counts(engine)["notifications"] == 1
    with session_scope(engine) as s:
        assert s.get(KeyValue, "telegram_offset").value == "100"   # état de la source
    assert any("état technique" in m for m in msgs)


def test_copy_refuses_target_with_real_data(source, engine):
    with session_scope(engine) as s:
        s.add(Instrument(symbol="ZZB"))
        s.add(KeyValue(key="telegram_offset", value="5"))
    with pytest.raises(RuntimeError, match="pas vide"):
        copy_database(source, engine, progress=lambda m: None)
    assert row_counts(engine)["kv_store"] == 1   # rien n'a été modifié


def test_upsert_does_not_rewrite_unchanged_rows(engine):
    """Non-régression de l'incident du 13/11/2026 : une ligne identique n'est pas réécrite (pas de WAL, pas de
    ligne morte) ; l'horodatage de collecte seul ne déclenche pas de réécriture ; une vraie valeur nouvelle, si."""
    from datetime import date, datetime, timezone
    from sqlalchemy import select
    from stockhunter.data.ingestion import ensure_sources
    from stockhunter.db.models import PriceBar
    from stockhunter.db.upsert import upsert
    src = ensure_sources(engine)["tiingo"]
    with session_scope(engine) as s:
        inst = Instrument(symbol="ZZA")
        s.add(inst)
        s.flush()
        iid = inst.id
    utc = lambda d: d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    t1, t2 = datetime(2026, 11, 12, tzinfo=timezone.utc), datetime(2026, 11, 13, tzinfo=timezone.utc)
    row = dict(instrument_id=iid, source_id=src, bar_date=date(2026, 11, 11), open=1.0, high=2.0, low=0.5, close=1.5,
               volume=100.0, fetched_at=t1)
    keys = ["instrument_id", "source_id", "bar_date"]
    with session_scope(engine) as s:
        upsert(s, PriceBar, [row], keys)
    with session_scope(engine) as s:
        upsert(s, PriceBar, [{**row, "fetched_at": t2}], keys)                 # mêmes valeurs, collecte plus récente
    with session_scope(engine) as s:
        b = s.scalars(select(PriceBar)).one()
        assert b.close == 1.5 and utc(b.fetched_at) == t1   # ligne NON réécrite
    with session_scope(engine) as s:
        upsert(s, PriceBar, [{**row, "close": 1.6, "fetched_at": t2}], keys)   # valeur corrigée par la source
    with session_scope(engine) as s:
        b = s.scalars(select(PriceBar)).one()
        assert b.close == 1.6 and utc(b.fetched_at) == t2


def test_tune_is_safe_on_any_database(engine):
    from stockhunter.db.copy import tune
    out = tune(engine, progress=lambda m: None)
    if engine.dialect.name == "postgresql":
        assert out["apres"]["repertoire_mo"] > out["apres"]["bases_mo"] > 0
        assert out["apres"]["methode"] == "répertoire de données mesuré"
        assert out["wal"]["segments_apres"] <= max(out["wal"]["segments_avant"], 4)
        with engine.connect() as c:
            from sqlalchemy import text
            assert c.execute(text("SHOW max_wal_size")).scalar() == "64MB"
    else:
        assert out == {}


def test_storage_levels(engine, monkeypatch):
    from stockhunter.db import copy as dbc
    if engine.dialect.name != "postgresql":
        assert dbc.storage(engine)["niveau"] == "ok"
        return
    used = dbc.storage(engine)["utilise_mo"]
    assert dbc.storage(engine, volume_mb=used / 0.5)["niveau"] == "ok"
    assert dbc.storage(engine, volume_mb=used / 0.85)["niveau"] == "attention"
    assert dbc.storage(engine, volume_mb=used / 0.92)["niveau"] == "alerte"
    assert dbc.storage(engine, volume_mb=used / 0.97)["niveau"] == "critique"
    # VACUUM demandé mais marge insuffisante : non lancé (il a lui-même besoin de place)
    monkeypatch.setenv("SH_DB_VOLUME_MB", str(used / 0.99))
    out = dbc.tune(engine, progress=lambda m: None, vacuum=True)
    assert out["vacuum"].startswith("non fait")


def test_reclaim_wal_never_deletes_files_itself_and_shrinks_preallocated_segments(engine):
    """Reproduit l'incident : beaucoup de segments WAL préalloués ; après plafonnement, `reclaim_wal` les rend
    au disque par pg_switch_wal + CHECKPOINT (aucune suppression manuelle)."""
    from sqlalchemy import text
    from stockhunter.db import copy as dbc
    if engine.dialect.name != "postgresql":
        return
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text("ALTER SYSTEM SET max_wal_size = '1GB'"))
        c.execute(text("ALTER SYSTEM SET min_wal_size = '320MB'"))
        c.execute(text("SELECT pg_reload_conf()"))
        c.execute(text("CREATE TABLE IF NOT EXISTS zz_wal_load (x text)"))
        for _ in range(14):                                      # ≈ 200 Mo de WAL
            c.execute(text("INSERT INTO zz_wal_load SELECT repeat('x', 1000) FROM generate_series(1, 15000)"))
        c.execute(text("CHECKPOINT"))                            # segments gardés, recyclés d'avance (min_wal_size)
        c.execute(text("DROP TABLE zz_wal_load"))
        before = int(c.execute(text("SELECT count(*) FROM pg_ls_waldir() WHERE name ~ '^[0-9A-F]{24}$'")).scalar())
    try:
        out = dbc.tune(engine, progress=lambda m: None)
        assert before >= 10 and out["wal"]["segments_avant"] >= 10
        assert out["wal"]["segments_apres"] <= 6 and out["wal"]["libere_mo"] >= 64
    finally:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text("ALTER SYSTEM RESET max_wal_size"))
            c.execute(text("ALTER SYSTEM RESET min_wal_size"))
            c.execute(text("SELECT pg_reload_conf()"))
