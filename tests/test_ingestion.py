from datetime import date

import httpx
import pytest
import respx
from sqlalchemy import func, select

from stockhunter.data import ingestion
from stockhunter.data.providers import FinnhubProvider, FredProvider, SecEdgarProvider, TiingoProvider
from stockhunter.db.models import Filing, FundamentalFact, Instrument, MacroObservation, MarketEvent, PriceBar
from stockhunter.db.session import session_scope
from stockhunter.jobs.runner import JobContext
from tests.fixtures import (
    FIXTURE_EARNINGS,
    FIXTURE_FRED_SERIES,
    FIXTURE_SEC_TICKERS,
    FIXTURE_companyfacts,
    FIXTURE_fred_obs,
    FIXTURE_submissions,
    FIXTURE_tiingo_bars,
)

TODAY = date(2026, 11, 12)
nosleep = {"sleep": lambda s: None}


def count(engine, model, *where):
    with session_scope(engine) as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def mock_tiingo(last_day=date(2026, 11, 11), split_on=None, calls=None):
    def prices(request, sym):
        start = date.fromisoformat(request.url.params["startDate"])
        if calls is not None:
            calls.append((sym, start))
        return httpx.Response(200, json=FIXTURE_tiingo_bars(start, last_day, split_on=split_on))

    respx.get(url__regex=r"https://api\.tiingo\.com/tiingo/daily/(?P<sym>\w+)/prices.*").mock(side_effect=prices)


def setup_universe(engine, settings):
    respx.get("https://www.sec.gov/files/company_tickers_exchange.json").mock(
        return_value=httpx.Response(200, json=FIXTURE_SEC_TICKERS))
    ctx = JobContext(run_id=0)
    ingestion.sync_universe(engine, ctx, settings.universe_file, SecEdgarProvider(settings, engine, **nosleep))
    return ctx


@respx.mock
def test_sync_universe_resolves_cik(engine, settings):
    ctx = setup_universe(engine, settings)
    assert ctx.ok == 3 and ctx.failed == 0
    with session_scope(engine) as s:
        zzta = s.scalar(select(Instrument).where(Instrument.symbol == "ZZTA"))
        assert zzta.cik == "0999999901" and zzta.exchange == "Nasdaq"


@respx.mock
def test_prices_initial_then_incremental_is_idempotent(engine, settings):
    setup_universe(engine, settings)
    calls = []
    mock_tiingo(calls=calls)
    tiingo = TiingoProvider(settings, engine, **nosleep)
    ctx = JobContext(run_id=0)
    ingestion.ingest_prices(engine, ctx, tiingo, 1, today=TODAY)
    assert ctx.failed == 0 and ctx.ok == 3
    n1 = count(engine, PriceBar)
    assert n1 > 3 * 240

    # même jour : toutes les séries contiennent déjà la séance du 11/11 -> aucun appel, aucun quota
    calls.clear()
    ctx = JobContext(run_id=0)
    ingestion.ingest_prices(engine, ctx, tiingo, 1, today=TODAY)
    assert calls == [] and ctx.ok == 3

    # lendemain : la séance du 12/11 manque -> incrémental avec recouvrement de 7 j, sans doublon
    ingestion.ingest_prices(engine, JobContext(run_id=0), tiingo, 1, today=date(2026, 11, 13))
    assert len(calls) == 3 and all(start == date(2026, 11, 4) for _, start in calls)
    assert count(engine, PriceBar) == n1
    with session_scope(engine) as s:
        assert s.scalar(select(Instrument.last_price_date).where(Instrument.symbol == "ZZTA")) == date(2026, 11, 11)


@respx.mock
def test_split_triggers_full_reload(engine, settings):
    setup_universe(engine, settings)
    calls = []
    mock_tiingo(last_day=date(2026, 11, 10), calls=calls)
    tiingo = TiingoProvider(settings, engine, **nosleep)
    ingestion.ingest_prices(engine, JobContext(run_id=0), tiingo, 1, symbols=["ZZTA"], today=TODAY)
    respx.clear()
    calls.clear()
    mock_tiingo(last_day=date(2026, 11, 11), split_on=date(2026, 11, 11), calls=calls)
    ingestion.ingest_prices(engine, JobContext(run_id=0), tiingo, 1, symbols=["ZZTA"], today=TODAY)
    assert [c[1] for c in calls] == [date(2026, 11, 3), date(2025, 11, 12)]


@respx.mock
def test_unknown_symbol_is_reported_not_invented(engine, settings):
    setup_universe(engine, settings)
    respx.get(url__regex=r"https://api\.tiingo\.com/tiingo/daily/zzbm/prices.*").mock(return_value=httpx.Response(404))
    mock_tiingo()
    ctx = JobContext(run_id=0)
    ingestion.ingest_prices(engine, ctx, TiingoProvider(settings, engine, **nosleep), 1, today=TODAY)
    assert ctx.failed == 1 and "ZZBM" in ctx.errors[0]
    with session_scope(engine) as s:
        iid = s.scalar(select(Instrument.id).where(Instrument.symbol == "ZZBM"))
    assert count(engine, PriceBar, PriceBar.instrument_id == iid) == 0


@respx.mock
def test_fundamentals_and_filings_dedup(engine, settings):
    setup_universe(engine, settings)
    respx.get(url__regex=r"https://data\.sec\.gov/api/xbrl/companyfacts/CIK(?P<c>\d+)\.json").mock(
        side_effect=lambda req, c: httpx.Response(200, json=FIXTURE_companyfacts(int(c))))
    respx.get(url__regex=r"https://data\.sec\.gov/submissions/CIK(?P<c>\d+)\.json").mock(
        side_effect=lambda req, c: httpx.Response(200, json=FIXTURE_submissions(int(c))))
    sec = SecEdgarProvider(settings, engine, **nosleep)
    ctx = JobContext(run_id=0)
    ingestion.ingest_fundamentals(engine, ctx, sec)
    assert ctx.ok == 2 and ctx.failed == 0  # l'ETF est ignoré
    n_facts, n_filings = count(engine, FundamentalFact), count(engine, Filing)
    assert n_facts == 2 * 3  # Revenues (dédupliqué), Assets, SharesOutstanding
    ingestion.ingest_fundamentals(engine, JobContext(run_id=0), sec)
    assert count(engine, FundamentalFact) == n_facts and count(engine, Filing) == n_filings
    with session_scope(engine) as s:
        inst = s.scalar(select(Instrument).where(Instrument.symbol == "ZZTA"))
        assert inst.sector == "Technology" and inst.industry.startswith("Services")


@respx.mock
def test_macro_keeps_missing_as_null(engine, settings):
    respx.get("https://api.stlouisfed.org/fred/series").mock(return_value=httpx.Response(200, json=FIXTURE_FRED_SERIES))
    respx.get("https://api.stlouisfed.org/fred/series/observations").mock(
        side_effect=lambda req: httpx.Response(200, json=FIXTURE_fred_obs(
            date.fromisoformat(req.url.params["observation_start"]), date(2026, 11, 11))))
    ctx = JobContext(run_id=0)
    ingestion.ingest_macro(engine, ctx, FredProvider(settings, engine, **nosleep), 1,
                           series={"DGS10": "x"}, today=TODAY)
    assert ctx.ok == 1
    assert count(engine, MacroObservation, MacroObservation.value.is_(None)) > 0
    n = count(engine, MacroObservation)
    ingestion.ingest_macro(engine, JobContext(run_id=0), FredProvider(settings, engine, **nosleep), 1,
                           series={"DGS10": "x"}, today=TODAY)
    assert count(engine, MacroObservation) == n


@respx.mock
def test_earnings_calendar_only_tracked_symbols(engine, settings):
    setup_universe(engine, settings)
    respx.get("https://finnhub.io/api/v1/calendar/earnings").mock(return_value=httpx.Response(200, json=FIXTURE_EARNINGS))
    ctx = JobContext(run_id=0)
    ingestion.ingest_earnings_calendar(engine, ctx, FinnhubProvider(settings, engine, **nosleep), today=TODAY)
    assert count(engine, MarketEvent) == 1
    with session_scope(engine) as s:
        ev = s.scalar(select(MarketEvent))
        assert ev.verified is False and "après clôture" in ev.title


def test_missing_keys_fail_explicitly(engine, settings):
    from stockhunter.data.http import ProviderError
    settings.tiingo_api_key = None
    with pytest.raises(ProviderError, match="SH_TIINGO_API_KEY"):
        TiingoProvider(settings, engine)
    settings.sec_user_agent = "noemail"
    with pytest.raises(ProviderError, match="e-mail"):
        SecEdgarProvider(settings, engine)


@respx.mock
def test_tiingo_429_stops_run_without_retry_and_resumes_oldest_first(engine, settings):
    setup_universe(engine, settings)
    calls = []
    mock_tiingo(calls=calls)
    tiingo = TiingoProvider(settings, engine, **nosleep)
    ingestion.ingest_prices(engine, JobContext(run_id=0), tiingo, 1, symbols=["ZZTA"], today=TODAY)
    assert len(calls) == 1  # un seul appel par symbole

    respx.clear()
    route = respx.get(url__regex=r"https://api\.tiingo\.com/.*").mock(return_value=httpx.Response(429))
    ctx = JobContext(run_id=0)
    ingestion.ingest_prices(engine, ctx, tiingo, 1, today=TODAY)
    assert route.call_count == 1          # pas de retry, arrêt immédiat du run
    assert ctx.failed == 1 and "quota" in ctx.errors[0]
    # l'ordre : symboles jamais chargés d'abord (ZZBM/ZZTB), ZZTA déjà à jour en dernier
    assert "ZZTA" not in ctx.errors[0]
