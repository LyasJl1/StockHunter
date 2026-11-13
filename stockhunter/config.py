"""Configuration centralisée. Les secrets viennent UNIQUEMENT des variables d'environnement."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderLimits(BaseSettings):
    """Quotas documentés des plans gratuits (vérifiés le 2026-11-12, à revérifier régulièrement)."""

    # Tiingo Free : 50 requêtes/heure, 1000/jour, ~500 symboles uniques/mois
    tiingo_per_hour: int = 50
    tiingo_per_day: int = 1000
    tiingo_unique_symbols_per_month: int = 500
    # SEC EDGAR : 10 req/s max ; on reste prudemment en dessous
    sec_per_second: float = 8.0
    # FRED : 120 req/min documentés ; marge de sécurité
    fred_per_minute: int = 100
    # Finnhub Free : 60 req/min (sources tierces mentionnent parfois 30) -> prudence
    finnhub_per_minute: int = 30
    # Massive (ex-Polygon) Stocks Basic gratuit : 5 appels/min, 2 ans d'historique
    massive_per_minute: int = 5


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SH_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./var/stockhunter.db"

    sec_user_agent: str | None = None
    tiingo_api_key: SecretStr | None = None
    fred_api_key: SecretStr | None = None
    finnhub_api_key: SecretStr | None = None
    massive_api_key: SecretStr | None = None
    market_dir: Path = Path("./var/market")
    market_history_days: int = Field(default=504, ge=20, le=504)  # plan gratuit Massive : 2 ans

    universe_file: Path | None = None
    price_history_years: int = Field(default=10, ge=1, le=40)

    cache_dir: Path = Path("./var/cache")
    http_timeout_s: float = 30.0
    http_max_retries: int = 4

    # Fraîcheur : nombre de jours ouvrés tolérés avant qu'une série soit marquée périmée
    stale_price_business_days: int = 2

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: SecretStr | None = None
    telegram_max_per_run: int = 15          # anti-spam : au-delà, les messages attendent le run suivant
    report_morning_hour_paris: int = 8      # rapport du matin, heure de Paris (changements d'heure gérés)

    history_dir: str = "var/history"        # historique long (Parquet), hors base opérationnelle
    db_volume_mb: float = 500.0            # volume PostgreSQL (Railway gratuit : 500 Mo) pour les seuils 80/90/95 %
    # Espace du volume hors répertoire de données (métadonnées du système de fichiers, etc.). Calibré le 13/11/2026 :
    # Railway mesurait 496 Mo utilisés pour 407 Mo de répertoire de données PostgreSQL -> 89 Mo.
    db_fs_overhead_mb: float = 89.0
    dashboard_password_hash: SecretStr | None = None   # hachage scrypt (stockhunter dashboard hash-password)
    dashboard_secret: SecretStr | None = None          # clé de signature des sessions (32+ caractères aléatoires)

    limits: ProviderLimits = Field(default_factory=ProviderLimits)

    def provider_status(self) -> dict[str, str]:
        """État de configuration des sources, sans jamais révéler de secret."""
        return {
            "sec_edgar": "configuré" if self.sec_user_agent else "MANQUANT: SH_SEC_USER_AGENT",
            "tiingo": "configuré" if self.tiingo_api_key else "MANQUANT: SH_TIINGO_API_KEY",
            "fred": "configuré" if self.fred_api_key else "MANQUANT: SH_FRED_API_KEY",
            "finnhub": "configuré" if self.finnhub_api_key else "optionnel: SH_FINNHUB_API_KEY absent",
            "massive": "configuré" if self.massive_api_key else "MANQUANT: SH_MASSIVE_API_KEY (univers large)",
            "telegram": "configuré" if self.telegram_bot_token and self.telegram_chat_id
            else "MANQUANT: SH_TELEGRAM_BOT_TOKEN / SH_TELEGRAM_CHAT_ID",
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
