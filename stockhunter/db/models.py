"""Schéma de la base. Principe : toute donnée est datée (date de marché / date de publication)
ET horodatée (fetched_at), avec sa source. Aucune ligne ne prétend être un cours "live".

Les tables des phases suivantes (scores, signaux, versions de stratégie, transactions...)
seront ajoutées par migrations Alembic successives.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class DataSource(Base):
    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    base_url: Mapped[str] = mapped_column(String(255))
    plan: Mapped[str] = mapped_column(String(50), default="free")
    license_note: Mapped[str] = mapped_column(Text, default="")


class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str | None] = mapped_column(String(255))
    exchange: Mapped[str | None] = mapped_column(String(30))
    asset_type: Mapped[str] = mapped_column(String(20), default="stock")  # stock | etf
    role: Mapped[str] = mapped_column(String(20), default="candidate")  # candidate | benchmark | sector_etf
    cik: Mapped[str | None] = mapped_column(String(10), index=True)
    sector: Mapped[str | None] = mapped_column(String(100))
    industry: Mapped[str | None] = mapped_column(String(150))
    sector_etf: Mapped[str | None] = mapped_column(String(20))
    industry_etf: Mapped[str | None] = mapped_column(String(20))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    first_price_date: Mapped[date | None] = mapped_column(Date)
    last_price_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    prices: Mapped[list["PriceBar"]] = relationship(back_populates="instrument")


class PriceBar(Base):
    """Barre journalière (EOD). Prix bruts + ajustés + dividende + facteur de split."""

    __tablename__ = "price_bars"
    __table_args__ = (
        UniqueConstraint("instrument_id", "bar_date", "source_id", name="uq_price_bar"),
        Index("ix_price_instr_date", "instrument_id", "bar_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"))
    bar_date: Mapped[date] = mapped_column(Date)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    adj_open: Mapped[float | None] = mapped_column(Float)
    adj_high: Mapped[float | None] = mapped_column(Float)
    adj_low: Mapped[float | None] = mapped_column(Float)
    adj_close: Mapped[float | None] = mapped_column(Float)
    adj_volume: Mapped[float | None] = mapped_column(Float)
    div_cash: Mapped[float] = mapped_column(Float, default=0.0)
    split_factor: Mapped[float] = mapped_column(Float, default=1.0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    instrument: Mapped[Instrument] = relationship(back_populates="prices")


class FundamentalFact(Base):
    """Fait XBRL point-in-time issu de SEC EDGAR.

    `filed_date` est la date à laquelle l'information est devenue publique : toute analyse
    historique DOIT filtrer sur filed_date <= date d'analyse (protection look-ahead).
    """

    __tablename__ = "fundamental_facts"
    __table_args__ = (
        # period_start est NULL pour les postes de bilan : une contrainte UNIQUE classique laisserait
        # passer des doublons (NULL != NULL en SQL). On déduplique donc sur une clé calculée non nulle.
        UniqueConstraint("instrument_id", "fact_key", name="uq_fact"),
        Index("ix_fact_lookup", "instrument_id", "concept", "filed_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"))
    fact_key: Mapped[str] = mapped_column(String(255))
    taxonomy: Mapped[str] = mapped_column(String(20))
    concept: Mapped[str] = mapped_column(String(150))
    unit: Mapped[str] = mapped_column(String(30))
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_period: Mapped[str | None] = mapped_column(String(5))
    form: Mapped[str | None] = mapped_column(String(20))
    filed_date: Mapped[date] = mapped_column(Date)
    accession: Mapped[str] = mapped_column(String(25))
    value: Mapped[float] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Filing(Base):
    """Dépôt officiel SEC (8-K, 10-K, 10-Q, 4...)."""

    __tablename__ = "filings"
    __table_args__ = (Index("ix_filing_instr_date", "instrument_id", "filed_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"))
    accession: Mapped[str] = mapped_column(String(25), unique=True)
    form: Mapped[str] = mapped_column(String(20), index=True)
    filed_date: Mapped[date] = mapped_column(Date)
    report_date: Mapped[date | None] = mapped_column(Date)
    items: Mapped[str | None] = mapped_column(String(100))  # items 8-K (ex. "2.02,9.01")
    primary_doc: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(500))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MacroSeries(Base):
    __tablename__ = "macro_series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    frequency: Mapped[str | None] = mapped_column(String(20))
    units: Mapped[str | None] = mapped_column(String(100))
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"))


class MacroObservation(Base):
    """Observation macro. `value` NULL = valeur manquante côté source (jamais remplacée par 0)."""

    __tablename__ = "macro_observations"
    __table_args__ = (UniqueConstraint("series_id", "obs_date", name="uq_macro_obs"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("macro_series.id", ondelete="CASCADE"))
    obs_date: Mapped[date] = mapped_column(Date)
    value: Mapped[float | None] = mapped_column(Float)
    realtime_start: Mapped[date | None] = mapped_column(Date)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MarketEvent(Base):
    """Événement daté et sourcé (résultats à venir, catalyseur...). Enrichi en phase 6."""

    __tablename__ = "market_events"
    __table_args__ = (
        UniqueConstraint("instrument_id", "event_type", "event_date", "source_id", name="uq_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"))
    event_type: Mapped[str] = mapped_column(String(40))  # earnings | filing_8k | ...
    event_date: Mapped[date] = mapped_column(Date, index=True)
    title: Mapped[str] = mapped_column(String(500))
    payload: Mapped[str | None] = mapped_column(Text)  # JSON brut de la source
    url: Mapped[str | None] = mapped_column(String(500))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiCall(Base):
    """Journal des appels API : sert au contrôle des quotas et à l'audit."""

    __tablename__ = "api_calls"
    __table_args__ = (Index("ix_api_calls_provider_ts", "provider", "called_at"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(30))
    endpoint: Mapped[str] = mapped_column(String(255))  # URL sans token
    symbol: Mapped[str | None] = mapped_column(String(20))
    status_code: Mapped[int | None] = mapped_column(Integer)
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    called_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_name: Mapped[str] = mapped_column(String(60), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|success|partial|failed|skipped
    items_ok: Mapped[int] = mapped_column(Integer, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str | None] = mapped_column(Text)


class JobLock(Base):
    """Verrou contre les exécutions concurrentes (fonctionne aussi entre conteneurs via la DB)."""

    __tablename__ = "job_locks"

    job_name: Mapped[str] = mapped_column(String(60), primary_key=True)
    owner: Mapped[str] = mapped_column(String(100))
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DataAnomaly(Base):
    __tablename__ = "data_anomalies"
    __table_args__ = (Index("ix_anomaly_open", "check_name", "resolved_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_run_id: Mapped[int | None] = mapped_column(ForeignKey("job_runs.id", ondelete="SET NULL"))
    check_name: Mapped[str] = mapped_column(String(60))
    severity: Mapped[str] = mapped_column(String(10))  # info | warning | critical
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    detail: Mapped[str] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# =========================================================================== Phase 2 : scanner
class StrategyVersion(Base):
    """Version figée d'une méthode de scoring : poids, seuils, paramètres. Une modification = nouvelle version.

    stage : RESEARCH -> BACKTEST -> OUT_OF_SAMPLE -> PAPER -> PROMOTED (aucune promotion sans validation).
    """

    __tablename__ = "strategy_versions"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_strategy_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    version: Mapped[str] = mapped_column(String(30))
    stage: Mapped[str] = mapped_column(String(20), default="RESEARCH")
    params_json: Mapped[str] = mapped_column(Text)
    params_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"))
    as_of: Mapped[date] = mapped_column(Date, index=True)  # dernière séance prise en compte
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    universe_size: Mapped[int] = mapped_column(Integer)
    scored: Mapped[int] = mapped_column(Integer)
    regime_json: Mapped[str] = mapped_column(Text)
    sectors_json: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


class Score(Base):
    """Résultat d'une action pour un scan. Jamais modifié après insertion (historique vérifiable)."""

    __tablename__ = "scores"
    __table_args__ = (
        UniqueConstraint("scan_run_id", "instrument_id", name="uq_score"),
        Index("ix_score_instr", "instrument_id", "as_of"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    scan_run_id: Mapped[int] = mapped_column(ForeignKey("scan_runs.id", ondelete="CASCADE"))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    as_of: Mapped[date] = mapped_column(Date)
    price_date: Mapped[date | None] = mapped_column(Date)
    close: Mapped[float | None] = mapped_column(Float)
    total: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), index=True)
    status_reason: Mapped[str] = mapped_column(Text)
    subscores_json: Mapped[str] = mapped_column(Text)
    strategies_json: Mapped[str] = mapped_column(Text)
    reasons_pos_json: Mapped[str] = mapped_column(Text)
    reasons_neg_json: Mapped[str] = mapped_column(Text)
    metrics_json: Mapped[str] = mapped_column(Text)
    sources_json: Mapped[str] = mapped_column(Text)
    rank: Mapped[int | None] = mapped_column(Integer)


class StatusChange(Base):
    __tablename__ = "status_changes"
    __table_args__ = (Index("ix_status_instr", "instrument_id", "changed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    scan_run_id: Mapped[int] = mapped_column(ForeignKey("scan_runs.id", ondelete="CASCADE"))
    as_of: Mapped[date] = mapped_column(Date)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# =========================================================================== Phase 3 : validation
class ValidationRun(Base):
    """Résultat d'une validation historique. Jamais modifié. `holdout_evaluated` trace chaque consultation de
    la période réservée : elle ne doit être évaluée qu'une fois par version, sur une méthode figée."""

    __tablename__ = "validation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    holdout_evaluated: Mapped[bool] = mapped_column(Boolean, default=False)
    data_fingerprint: Mapped[str] = mapped_column(String(64))
    params_json: Mapped[str] = mapped_column(Text)
    report_json: Mapped[str] = mapped_column(Text)
    verdicts_json: Mapped[str] = mapped_column(Text)


# =========================================================================== Univers large (Massive)
class MarketTicker(Base):
    """Référentiel de tous les titres US (actifs ET retirés de la cote : pas de biais du survivant)."""

    __tablename__ = "market_tickers"
    __table_args__ = (UniqueConstraint("ticker_key", name="uq_market_ticker"),
                      Index("ix_market_ticker_symbol", "ticker"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_key: Mapped[str] = mapped_column(String(80))  # ticker|composite_figi (un ticker peut être réattribué)
    ticker: Mapped[str] = mapped_column(String(20))
    name: Mapped[str | None] = mapped_column(String(255))
    type: Mapped[str | None] = mapped_column(String(20))           # CS, ETF, ADRC, PFD, WARRANT...
    primary_exchange: Mapped[str | None] = mapped_column(String(10))
    active: Mapped[bool] = mapped_column(Boolean)
    cik: Mapped[str | None] = mapped_column(String(10))
    composite_figi: Mapped[str | None] = mapped_column(String(20))
    delisted_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MarketSplit(Base):
    __tablename__ = "market_splits"
    __table_args__ = (UniqueConstraint("ticker", "execution_date", name="uq_market_split"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), index=True)
    execution_date: Mapped[date] = mapped_column(Date)
    split_from: Mapped[float] = mapped_column(Float)
    split_to: Mapped[float] = mapped_column(Float)
    adjustment_type: Mapped[str | None] = mapped_column(String(30))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
