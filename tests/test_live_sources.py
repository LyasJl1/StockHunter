"""Tests RÉELS vers les API (réseau + clés requis). Désactivés par défaut.

    pytest -m live
"""

from datetime import date, timedelta

import pytest

from stockhunter.config import Settings
from stockhunter.data.providers import FinnhubProvider, FredProvider, SecEdgarProvider, TiingoProvider
from stockhunter.db.models import Base
from stockhunter.db.session import get_engine

pytestmark = pytest.mark.live


@pytest.fixture
def live(tmp_path):
    s = Settings(database_url=f"sqlite:///{tmp_path / 'live.db'}", cache_dir=tmp_path / "c")
    e = get_engine(s.database_url)
    Base.metadata.create_all(e)
    return s, e


def test_sec_live(live):
    s, e = live
    if not s.sec_user_agent:
        pytest.skip("SH_SEC_USER_AGENT absent")
    subs = SecEdgarProvider(s, e).fetch_submissions("320193", "AAPL")
    assert subs.filings and all(f["filed_date"] for f in subs.filings)


def test_tiingo_live(live):
    s, e = live
    if not s.tiingo_api_key:
        pytest.skip("SH_TIINGO_API_KEY absent")
    bars = TiingoProvider(s, e).fetch_prices("SPY", date.today() - timedelta(days=10))
    assert bars and bars[-1]["bar_date"] <= date.today()


def test_fred_live(live):
    s, e = live
    if not s.fred_api_key:
        pytest.skip("SH_FRED_API_KEY absent")
    assert FredProvider(s, e).fetch_observations("DGS10", date.today() - timedelta(days=30))


def test_finnhub_live(live):
    s, e = live
    if not s.finnhub_api_key:
        pytest.skip("SH_FINNHUB_API_KEY absent")
    events = FinnhubProvider(s, e).fetch_earnings_calendar(date.today(), date.today() + timedelta(days=7))
    assert isinstance(events, list)


def test_finnhub_news_live(live):
    """Actualités réelles : au moins un article récent sur AAPL, avec lien et date de publication."""
    s, e = live
    if not s.finnhub_api_key:
        pytest.skip("SH_FINNHUB_API_KEY absent")
    news = FinnhubProvider(s, e).fetch_company_news("AAPL", date.today() - timedelta(days=7), date.today())
    assert news, "aucun article reçu"
    a = news[0]
    assert a["url"].startswith("http") and a["headline"] and a["published_at"].tzinfo is not None
