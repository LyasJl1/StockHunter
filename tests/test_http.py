from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select

from stockhunter.data.http import NotFound, ProviderClient, ProviderError, QuotaExceeded, QuotaWindow
from stockhunter.db.models import ApiCall
from stockhunter.db.session import session_scope


def make_client(engine, tmp_path, sleeps, **kw):
    return ProviderClient(name="t", base_url="https://api.test", engine=engine, cache_dir=tmp_path / "c",
                          auth_params={"token": "SUPERSECRET"}, sleep=sleeps.append, backoff_base_s=0.01, **kw)


@respx.mock
def test_retry_on_429_then_success(engine, tmp_path):
    sleeps = []
    route = respx.get("https://api.test/x").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json={"ok": 1})])
    assert make_client(engine, tmp_path, sleeps).get_json("/x") == {"ok": 1}
    assert route.call_count == 2 and sleeps == [2.0]


@respx.mock
def test_cache_avoids_second_call_and_is_logged(engine, tmp_path):
    route = respx.get("https://api.test/x").mock(return_value=httpx.Response(200, json=[1]))
    c = make_client(engine, tmp_path, [])
    c.get_json("/x", {"a": 1}, cache_ttl=timedelta(hours=1))
    c.get_json("/x", {"a": 1}, cache_ttl=timedelta(hours=1))
    assert route.call_count == 1
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(ApiCall).where(ApiCall.from_cache.is_(True))) == 1
    # le token ne figure ni dans le nom de fichier cache, ni dans le journal
    for p in (tmp_path / "c").rglob("*"):
        assert "SUPERSECRET" not in p.name
    with session_scope(engine) as s:
        assert all("SUPERSECRET" not in e for e in s.scalars(select(ApiCall.endpoint)))


@respx.mock
def test_quota_is_enforced_from_db(engine, tmp_path):
    respx.get("https://api.test/x").mock(return_value=httpx.Response(200, json={}))
    c = make_client(engine, tmp_path, [], quotas=[QuotaWindow(timedelta(hours=1), 2)])
    c.get_json("/x")
    c.get_json("/x")
    assert c.remaining_quota() == {"1:00:00": 0}
    with pytest.raises(QuotaExceeded):
        c.get_json("/x")
    # un nouveau client (redémarrage) voit toujours le quota consommé
    with pytest.raises(QuotaExceeded):
        make_client(engine, tmp_path, [], quotas=[QuotaWindow(timedelta(hours=1), 2)]).get_json("/x")


@respx.mock
def test_errors_redact_secret_and_404(engine, tmp_path):
    respx.get("https://api.test/bad").mock(return_value=httpx.Response(401, text="invalid token SUPERSECRET"))
    respx.get("https://api.test/missing").mock(return_value=httpx.Response(404))
    c = make_client(engine, tmp_path, [])
    with pytest.raises(ProviderError) as ei:
        c.get_json("/bad")
    assert "SUPERSECRET" not in str(ei.value) and "***" in str(ei.value)
    with pytest.raises(NotFound):
        c.get_json("/missing")


@respx.mock
def test_gives_up_after_max_retries(engine, tmp_path):
    respx.get("https://api.test/x").mock(return_value=httpx.Response(503))
    sleeps = []
    with pytest.raises(ProviderError):
        make_client(engine, tmp_path, sleeps, max_retries=2).get_json("/x")
    assert len(sleeps) == 2
