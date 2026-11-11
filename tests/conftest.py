from __future__ import annotations

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
    eng = get_engine(settings.database_url)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def no_sleep():
    calls = []
    return calls.append
