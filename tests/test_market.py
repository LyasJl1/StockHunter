"""Univers large Massive (réponses SYNTHÉTIQUES, symboles ZZ*)."""

from datetime import date, datetime, timezone

import httpx
import pandas as pd
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import func, select

from stockhunter.data.http import ProviderError
from stockhunter.db.models import MarketSplit, MarketTicker
from stockhunter.db.session import session_scope
from stockhunter.jobs.runner import JobContext
from stockhunter.market.provider import MassiveProvider, parse_grouped, parse_splits, parse_tickers
from stockhunter.market.store import MarketStore, split_factors
from stockhunter.market.sync import missing_sessions, sync_reference, sync_sessions, sync_splits

NOSLEEP = {"sleep": lambda s: None}


def ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc).timestamp() * 1000)


def grouped_payload(d: date, n=3):
    return {"status": "OK", "adjusted": False, "resultsCount": n, "results": [
        {"T": f"ZZ{i}", "o": 10 + i, "h": 11 + i, "l": 9 + i, "c": 10.5 + i, "v": 1000 * (i + 1), "vw": 10.2 + i,
         "t": ms(d), "n": 50} for i in range(n)]}


@pytest.fixture
def mv(settings, engine):
    settings.massive_api_key = SecretStr("MASSIVE_SECRET")
    return MassiveProvider(settings, engine, **NOSLEEP)


def test_parsers():
    rows = parse_grouped(grouped_payload(date(2026, 11, 11)), date(2026, 11, 11))
    assert [r["ticker"] for r in rows] == ["ZZ0", "ZZ1", "ZZ2"] and rows[0]["otc"] is False
    with pytest.raises(ProviderError, match="datée"):
        parse_grouped(grouped_payload(date(2026, 1, 5)), date(2026, 11, 11))
    t = parse_tickers({"results": [{"ticker": "ZZA", "type": "CS", "active": False, "cik": "123",
                                    "composite_figi": "BBG0", "delisted_utc": "2025-03-01T00:00:00Z"}]})
    assert t[0]["cik"] == "0000000123" and t[0]["ticker_key"] == "ZZA|BBG0" and t[0]["delisted_utc"].year == 2025
    sp = parse_splits({"results": [{"ticker": "ZZA", "execution_date": "2026-06-01", "split_from": 1, "split_to": 4},
                                   {"ticker": "ZZB", "execution_date": "bad"}]})
    assert sp == [{"ticker": "ZZA", "execution_date": date(2026, 6, 1), "split_from": 1.0, "split_to": 4.0,
                   "adjustment_type": None}]


def test_store_roundtrip_and_split_adjustment(tmp_path):
    st = MarketStore(tmp_path)
    st.write(date(2026, 5, 29), [{"ticker": "ZZA", "close": 400.0, "volume": 100.0}])
    st.write(date(2026, 6, 1), [{"ticker": "ZZA", "close": 101.0, "volume": 400.0}])
    st.write(date(2026, 6, 2), [])  # séance vide : enregistrée quand même
    assert st.sessions() == [date(2026, 5, 29), date(2026, 6, 1), date(2026, 6, 2)]
    df = st.load()
    splits = pd.DataFrame([{"ticker": "ZZA", "execution_date": date(2026, 6, 1), "split_from": 1.0, "split_to": 4.0}])
    f = split_factors(splits, df["date"], df["ticker"])
    adj = df["close"] * f
    assert list(adj) == [100.0, 101.0]   # 400 avant un split 1:4 = 100 : pas de faux krach de -75 %


def test_missing_sessions_newest_first(tmp_path):
    st = MarketStore(tmp_path)
    st.write(date(2026, 11, 10), [])
    m = missing_sessions(st, date(2026, 11, 12), 5)
    assert m == [date(2026, 11, 11), date(2026, 11, 9), date(2026, 11, 6), date(2026, 11, 5)]


@respx.mock
def test_sync_sessions_stops_cleanly_on_quota(engine, settings, mv, tmp_path):
    respx.get(url__regex=r"https://api\.massive\.com/v2/aggs/grouped/locale/us/market/stocks/2026-11-11.*").mock(
        return_value=httpx.Response(200, json=grouped_payload(date(2026, 11, 11))))
    respx.get(url__regex=r"https://api\.massive\.com/v2/aggs/grouped/.*2026-11-10.*").mock(
        return_value=httpx.Response(200, json=grouped_payload(date(2026, 11, 10))))
    st = MarketStore(tmp_path / "m")
    ctx = JobContext(run_id=0)
    sync_sessions(ctx, mv, st, date(2026, 11, 12), 2)
    assert ctx.ok == 2 and len(st.load()) == 6
    # la clé n'apparaît pas dans le journal des appels
    from stockhunter.db.models import ApiCall
    with session_scope(engine) as s:
        assert all("MASSIVE_SECRET" not in e for e in s.scalars(select(ApiCall.endpoint)))


@respx.mock
def test_reference_pagination_and_splits(engine, mv):
    page2 = "https://api.massive.com/v3/reference/tickers?cursor=abc"
    respx.get(url__regex=r"https://api\.massive\.com/v3/reference/tickers\?(?!cursor).*active=true.*").mock(
        return_value=httpx.Response(200, json={"results": [{"ticker": "ZZA", "type": "CS", "active": True}],
                                               "next_url": page2}))
    route2 = respx.get(url__regex=r".*/v3/reference/tickers\?cursor=abc.*").mock(
        return_value=httpx.Response(200, json={"results": [{"ticker": "ZZB", "type": "ETF", "active": True}]}))
    respx.get(url__regex=r".*/v3/reference/tickers\?(?!cursor).*active=false.*").mock(
        return_value=httpx.Response(200, json={"results": [{"ticker": "ZZD", "type": "CS", "active": False}]}))
    respx.get(url__regex=r".*/stocks/v1/splits.*").mock(return_value=httpx.Response(200, json={"results": [
        {"ticker": "ZZA", "execution_date": "2026-06-01", "split_from": 1, "split_to": 2}]}))
    ctx = JobContext(run_id=0)
    sync_reference(engine, ctx, mv)
    assert "apiKey=MASSIVE_SECRET" in str(route2.calls[0].request.url)  # la clé est ajoutée à next_url
    sync_reference(engine, ctx, mv)  # < 7 jours : pas de nouvel appel
    assert route2.call_count == 1
    sync_splits(engine, ctx, mv, date(2026, 11, 12), 504)
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(MarketTicker)) == 3
        assert s.scalar(select(func.count()).select_from(MarketTicker).where(MarketTicker.active.is_(False))) == 1
        assert s.scalar(select(func.count()).select_from(MarketSplit)) == 1


def test_missing_key(settings, engine):
    with pytest.raises(ProviderError, match="SH_MASSIVE_API_KEY"):
        MassiveProvider(settings, engine)


def test_sessions_outside_free_plan_window_are_not_requested(tmp_path):
    m = missing_sessions(MarketStore(tmp_path), date(2026, 11, 12), 504)
    assert date(2024, 11, 11) not in m and date(2024, 11, 12) in m   # cas réels du 12/11/2026 (403)
