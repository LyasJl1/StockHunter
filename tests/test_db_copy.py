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
        assert out["base_apres_mo"] > 0
        with engine.connect() as c:
            from sqlalchemy import text
            assert c.execute(text("SHOW max_wal_size")).scalar() == "64MB"
    else:
        assert out == {}
