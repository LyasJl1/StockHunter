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
