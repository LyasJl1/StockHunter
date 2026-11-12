from __future__ import annotations

import os

import pytest
from pydantic import SecretStr

from stockhunter.config import Settings
from stockhunter.db.models import Base
from stockhunter.db.session import get_engine


@pytest.fixture
def settings(tmp_path):
    universe = tmp_path / "universe.csv"
    universe.write_text(
        "symbol,asset_type,role,sector\n"
        "ZZBM,etf,benchmark,Benchmark\n"
        "ZZTA,stock,candidate,\n"
        "ZZTB,stock,candidate,\n"
    )
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        sec_user_agent="StockHunterTest/0.1 test@example.com",
        tiingo_api_key=SecretStr("TIINGO_SECRET_TOKEN"),
        fred_api_key=SecretStr("FRED_SECRET_KEY"),
        finnhub_api_key=SecretStr("FINNHUB_SECRET"),
        universe_file=universe,
        cache_dir=tmp_path / "cache",
        price_history_years=1,
    )


@pytest.fixture
def engine(settings):
    """SQLite par défaut ; SH_TEST_DATABASE_URL=postgresql://... pour exécuter la suite sur PostgreSQL."""
    url = os.environ.get("SH_TEST_DATABASE_URL")
    if url:
        settings.database_url = url
    eng = get_engine(settings.database_url)
    if url:
        Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    if url:
        eng.dispose()


@pytest.fixture
def no_sleep():
    calls = []
    return calls.append
