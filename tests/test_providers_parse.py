from datetime import date

from stockhunter.data.calendar import last_completed_session, trading_days
from stockhunter.data.providers import (
    parse_fred_observations,
    parse_sec_companyfacts,
    parse_sec_submissions,
    parse_sec_ticker_map,
    parse_tiingo_prices,
    sic_to_sector,
)
from tests.fixtures import (
    FIXTURE_SEC_TICKERS,
    FIXTURE_companyfacts,
    FIXTURE_fred_obs,
    FIXTURE_submissions,
    FIXTURE_tiingo_bars,
)


def test_tiingo_parse_keeps_dates_and_corporate_actions():
    rows = parse_tiingo_prices(FIXTURE_tiingo_bars(date(2026, 3, 2), date(2026, 3, 6), split_on=date(2026, 3, 4)))
    assert [r["bar_date"] for r in rows][0] == date(2026, 3, 2)
    assert sum(r["split_factor"] != 1 for r in rows) == 1
    assert all(r["adj_close"] is not None for r in rows)


def test_sec_ticker_map_pads_cik():
    m = parse_sec_ticker_map(FIXTURE_SEC_TICKERS)
    assert m["ZZTA"]["cik"] == "0999999901" and m["ZZTB"]["exchange"] == "NYSE"


def test_companyfacts_point_in_time_and_whitelist():
    facts = parse_sec_companyfacts(FIXTURE_companyfacts(999999901))
    concepts = {f["concept"] for f in facts}
    assert "SomethingIgnored" not in concepts
    assets = next(f for f in facts if f["concept"] == "Assets")
    assert assets["period_start"] is None and assets["filed_date"] == date(2026, 8, 1)
    assert "|-|" in assets["fact_key"]  # clé non nulle même sans period_start
    rev_keys = {f["fact_key"] for f in facts if f["concept"] == "Revenues"}
    assert len(rev_keys) == 1  # doublon exact -> même clé


def test_submissions_filter_and_url():
    subs = parse_sec_submissions(FIXTURE_submissions(999999901))
    assert [f["form"] for f in subs.filings] == ["8-K", "10-Q"]
    assert subs.filings[0]["url"].endswith("/999999901/099999990126000011/ev8k.htm")
    assert subs.filings[0]["items"] == "2.02,9.01" and subs.filings[1]["items"] is None
    assert sic_to_sector(subs.sic) == "Technology"


def test_fred_missing_value_is_none_not_zero():
    obs = parse_fred_observations(FIXTURE_fred_obs(date(2026, 9, 1), date(2026, 9, 3)))
    assert obs[0]["value"] is None and obs[1]["value"] == 4.10


def test_nyse_calendar():
    days = {d.date() for d in trading_days(date(2026, 3, 30), date(2026, 4, 6))}
    assert date(2026, 4, 3) not in days  # Vendredi saint
    assert date(2026, 10, 12) in {d.date() for d in trading_days(date(2026, 10, 12), date(2026, 10, 12))}  # Columbus Day = ouvert
    # jeudi 12/11/2026 : la séance du jour n'est jamais considérée comme complète
    assert last_completed_session(date(2026, 11, 12)) == date(2026, 11, 11)
    assert last_completed_session(date(2026, 11, 30)) == date(2026, 11, 27)  # lundi après Thanksgiving


def test_exceptional_closure_2025_01_09():
    days = {d.date() for d in trading_days(date(2025, 1, 6), date(2025, 1, 10))}
    assert date(2025, 1, 9) not in days and date(2025, 1, 8) in days
