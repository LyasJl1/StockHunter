import pytest

from stockhunter import cli
from stockhunter.config import get_settings


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SH_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setenv("SH_TIINGO_API_KEY", "CLI_SECRET_VALUE")
    monkeypatch.delenv("SH_FRED_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # pas de .env parasite
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_init_db_migrations_and_status(env, capsys):
    assert cli.main(["init-db"]) == 0
    assert cli.main(["init-db"]) == 0  # idempotent
    assert cli.main(["sync-universe"]) == 0  # sans SEC configurée : chargé sans CIK, avec avertissement
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Couverture RÉELLE" in out and "pas de cours live" in out
    assert "CLI_SECRET_VALUE" not in out
    assert "MANQUANT: SH_FRED_API_KEY" in out


def test_ingest_without_key_is_skipped_explicitly(env, capsys):
    cli.main(["init-db"])
    assert cli.main(["ingest", "macro"]) == 2
    assert "SH_FRED_API_KEY non configurée" in capsys.readouterr().out
