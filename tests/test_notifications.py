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


@pytest.mark.parametrize("utc_hour,month,expected", [(7, 1, True), (6, 1, False), (6, 7, True), (7, 7, False)])
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
