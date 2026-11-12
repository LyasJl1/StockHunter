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
