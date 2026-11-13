"""Notifications Telegram (API simulée, aucun envoi réel)."""

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select

from stockhunter.db.models import NotificationLog
from stockhunter.db.session import session_scope
from stockhunter.notifications import engine as ne
from stockhunter.notifications.telegram import TelegramClient, TelegramError, split_message
from stockhunter.portfolio import paper
from tests.test_paper import bars, setup

TOKEN, CHAT = "123:SECRET-TOKEN", "987654"
API = f"https://api.telegram.org/bot{TOKEN}"


def client():
    return TelegramClient(TOKEN, CHAT, sleep=lambda s: None)


def ok(result=True):
    return httpx.Response(200, json={"ok": True, "result": result})


def test_split_message():
    parts = split_message("\n".join(["x" * 50] * 200), limit=1000)
    assert all(len(p) <= 1000 for p in parts) and "".join(parts).count("x") == 50 * 200


@respx.mock
def test_retry_after_429_and_token_redacted():
    route = respx.post(f"{API}/sendMessage").mock(side_effect=[
        httpx.Response(429, json={"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 3}}),
        ok()])
    client().send("bonjour")
    assert route.call_count == 2
    respx.post(f"{API}/sendMessage").mock(return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))
    with pytest.raises(TelegramError) as ei:
        client().send("x")
    assert "SECRET-TOKEN" not in str(ei.value)


@respx.mock
def test_dedup_idempotence_and_trade_alerts(engine, settings):
    settings.telegram_bot_token, settings.telegram_chat_id = SecretStr(TOKEN), SecretStr(CHAT)
    src, ids, run = setup(engine)
    paper.plan_from_scan(engine, run)
    bars(engine, src, ids["ZZA"], [("2026-11-03", 100, 101, 99, 100.5)])
    paper.process(engine)
    sent = respx.post(f"{API}/sendMessage").mock(return_value=ok())
    respx.post(f"{API}/getUpdates").mock(return_value=ok([]))
    now = datetime(2026, 11, 4, 0, 30, tzinfo=timezone.utc)
    out1 = ne.run_notifications(engine, settings, "evening", now=now)
    n1 = sent.call_count
    out2 = ne.run_notifications(engine, settings, "evening", now=now)   # relance du workflow
    assert out1["envoyes"] == 3 and out1["alertes_trading"] == 2       # rapport + plan + entrée
    assert out2["rapport"] == "déjà envoyé (dédupliqué)" and sent.call_count == n1   # rien n'est renvoyé
    texts = [json.loads(c.request.content)["text"] for c in sent.calls]
    assert any("ENTRÉE SIMULÉE" in t and "NON validée" in t for t in texts)


def test_not_configured_messages_are_skipped_not_queued_forever(engine, settings):
    src, ids, run = setup(engine)
    out = ne.run_notifications(engine, settings, "evening", now=datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc))
    assert out["ignores"] >= 1 and out["envoyes"] == 0
    with session_scope(engine) as s:
        assert {n.status for n in s.scalars(select(NotificationLog))} == {"SKIPPED"}


@pytest.mark.parametrize("utc_hour,month,expected", [(7, 1, True), (6, 1, False), (6, 7, True), (5, 7, False),
                                                     (9, 1, True), (11, 1, False), (10, 7, False)])   # retard GitHub toléré jusqu'à midi
def test_morning_report_follows_paris_time_and_dst(engine, settings, utc_hour, month, expected):
    setup(engine)
    now = datetime(2026, month, 14 if month == 1 else 15, utc_hour, 0, tzinfo=timezone.utc)   # mercredis
    out = ne.run_notifications(engine, settings, "morning", now=now)
    assert (out["rapport"] == "mis en file") is expected


@respx.mock
def test_commands_only_from_configured_chat(engine, settings):
    settings.telegram_bot_token, settings.telegram_chat_id = SecretStr(TOKEN), SecretStr(CHAT)
    setup(engine)
    respx.post(f"{API}/getUpdates").mock(return_value=ok([
        {"update_id": 10, "message": {"chat": {"id": int(CHAT)}, "text": "/portfolio"}},
        {"update_id": 11, "message": {"chat": {"id": 1}, "text": "/status"}},          # intrus : ignoré
    ]))
    sent = respx.post(f"{API}/sendMessage").mock(return_value=ok())
    assert ne.process_commands(engine, client()) == 1
    ne.flush(engine, client())
    assert "Portefeuille simulé" in json.loads(sent.calls[0].request.content)["text"]
    assert ne.process_commands(engine, client()) == 0   # même update relu : dédupliqué (offset + clé)


def test_repaired_failure_does_not_alert_but_open_failure_does(engine):
    from stockhunter.db.models import JobRun
    now = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
    with session_scope(engine) as s:   # cas réel du 12/11 : deux échecs puis un succès
        s.add_all([JobRun(job_name="market_sync", status="failed", started_at=datetime(2026, 11, 12, 12, 15)),
                   JobRun(job_name="market_sync", status="failed", started_at=datetime(2026, 11, 12, 12, 21)),
                   JobRun(job_name="market_sync", status="partial", started_at=datetime(2026, 11, 12, 13, 40)),
                   JobRun(job_name="ingest_prices", status="failed", started_at=datetime(2026, 11, 12, 14, 0),
                          detail="panne")])
    assert ne.enqueue_system_alerts(engine, now) == 1
    with session_scope(engine) as s:
        assert "ingest_prices" in s.scalar(select(NotificationLog.text))


def test_report_tells_the_truth_about_plans_and_validation(engine):
    from stockhunter.db.models import StrategyVersion
    from stockhunter.reports.daily import build_report
    setup(engine)
    with session_scope(engine) as s:   # version réellement consultée dans le registre versionné
        sv = s.scalar(select(StrategyVersion))
        sv.name, sv.version = "stockhunter-multifactor", "2.0.0"
    text, _ = build_report(engine, "morning")
    assert "pas encore créés" in text and "AUCUNE STRATÉGIE VALIDÉE" in text and "non testées" not in text


def test_web_worker_answers_commands_quickly(engine, settings, monkeypatch):
    import time as _t

    from stockhunter.notifications import worker
    setup(engine)
    sent = []

    class Fake:
        chat_id, poll_timeout = CHAT, 0
        def __init__(self):
            self.served = False
        def updates(self, offset):
            if self.served:
                return []
            self.served = True
            return [{"update_id": 99, "message": {"chat": {"id": int(CHAT)}, "text": "/status"}}]
        def send(self, text):
            sent.append(text)

    monkeypatch.setattr(ne, "client_from", lambda s: Fake())
    worker.start(engine, settings, interval_s=0.05)
    for _ in range(100):
        if sent:
            break
        _t.sleep(0.05)
    assert sent and "État du système" in sent[0]


def test_single_telegram_reader_lease(engine, settings):
    """Un seul lecteur getUpdates à la fois (sinon HTTP 409) : bail exclusif, repris seulement à expiration."""
    from datetime import datetime, timedelta, timezone
    from stockhunter.notifications import engine as ne
    t0 = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)
    assert ne.acquire_reader(engine, "worker-a", 90, t0)
    assert ne.acquire_reader(engine, "worker-a", 90, t0 + timedelta(seconds=30))          # renouvellement
    assert not ne.acquire_reader(engine, "batch", 60, t0 + timedelta(seconds=60))         # job planifié : s'abstient
    assert not ne.acquire_reader(engine, "worker-b", 90, t0 + timedelta(seconds=100))     # nouveau conteneur : attend
    assert ne.acquire_reader(engine, "worker-b", 90, t0 + timedelta(seconds=121))         # bail expiré : reprise
    assert not ne.acquire_reader(engine, "worker-a", 90, t0 + timedelta(seconds=125))

    class Boom:
        chat_id = "1"
        def updates(self, offset):
            raise AssertionError("getUpdates ne doit pas être appelé sans le bail")
    with session_scope(engine) as s:
        from stockhunter.db.models import KeyValue
        import json
        s.get(KeyValue, ne.READER_KEY).value = json.dumps(
            {"owner": "worker-b", "until": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()})
    assert ne.process_commands(engine, Boom()) == 0
