"""Contrôles qualité des données. Chaque exécution clôt les anomalies ouvertes puis enregistre
l'état courant, de sorte que `data_anomalies` reflète à tout moment les problèmes actifs + l'historique."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import pandas as pd
from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine

from stockhunter.data.calendar import last_completed_session, sessions_between, trading_days
from stockhunter.db.models import (
    DataAnomaly,
    Filing,
    FundamentalFact,
    Instrument,
    MacroObservation,
    MacroSeries,
    PriceBar,
)
from stockhunter.db.session import session_scope

# Fraîcheur tolérée par fréquence macro (en jours calendaires)
MACRO_MAX_AGE = {"Daily": 7, "Weekly": 14, "Monthly": 50, "Quarterly": 120}
EXTREME_MOVE = 0.5  # variation journalière ajustée > 50 % sans split = suspect
GAP_TOLERANCE = 3
PERIODIC_FORMS = ("10-Q", "10-K", "10-Q/A", "10-K/A", "20-F")   # séances manquantes tolérées sur l'historique (fermetures exceptionnelles)


@dataclass
class Finding:
    check: str
    severity: str  # info | warning | critical
    detail: str
    instrument_id: int | None = None


def check_prices(engine: Engine, today: date, stale_sessions: int) -> list[Finding]:
    findings: list[Finding] = []
    expected = last_completed_session(today)
    with session_scope(engine) as s:
        instruments = list(s.scalars(select(Instrument).where(Instrument.is_active.is_(True))))
        for inst in instruments:
            df = pd.read_sql(
                select(PriceBar.bar_date, PriceBar.open, PriceBar.high, PriceBar.low, PriceBar.close,
                       PriceBar.volume, PriceBar.adj_close, PriceBar.split_factor)
                .where(PriceBar.instrument_id == inst.id).order_by(PriceBar.bar_date),
                s.connection(),
            )
            if df.empty:
                findings.append(Finding("price_missing", "critical", f"{inst.symbol} : aucune donnée de prix", inst.id))
                continue
            df["bar_date"] = pd.to_datetime(df["bar_date"])
            last = df["bar_date"].iloc[-1].date()

            lag = sessions_between(last, expected)
            if lag > stale_sessions:
                findings.append(Finding("price_stale", "critical" if lag > 5 else "warning",
                                        f"{inst.symbol} : dernière barre {last}, {lag} séance(s) de retard "
                                        f"(attendu {expected})", inst.id))

            nulls = df[["open", "high", "low", "close", "adj_close"]].isna().any(axis=1).sum()
            if nulls:
                findings.append(Finding("price_nulls", "warning", f"{inst.symbol} : {nulls} barre(s) incomplète(s)", inst.id))

            bad = df[(df["high"] < df[["open", "close"]].max(axis=1) - 1e-9)
                     | (df["low"] > df[["open", "close"]].min(axis=1) + 1e-9)
                     | (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
                     | (df["volume"] < 0)]
            if len(bad):
                findings.append(Finding("price_ohlc_incoherent", "warning",
                                        f"{inst.symbol} : {len(bad)} barre(s) OHLC incohérente(s), "
                                        f"ex. {bad['bar_date'].iloc[0].date()}", inst.id))

            rets = df["adj_close"].pct_change()
            extreme = df[(rets.abs() > EXTREME_MOVE) & (df["split_factor"] == 1.0)]
            if len(extreme):
                findings.append(Finding("price_extreme_move", "warning",
                                        f"{inst.symbol} : {len(extreme)} variation(s) >50 % sans split, "
                                        f"ex. {extreme['bar_date'].iloc[0].date()} — à vérifier", inst.id))

            expected_days = trading_days(df["bar_date"].iloc[0], df["bar_date"].iloc[-1])
            missing = expected_days.difference(pd.DatetimeIndex(df["bar_date"]))
            if len(missing) > GAP_TOLERANCE:
                findings.append(Finding("price_gaps", "warning",
                                        f"{inst.symbol} : {len(missing)} séance(s) manquante(s), "
                                        f"ex. {missing[0].date()}", inst.id))

            if inst.asset_type == "stock":
                zero = int((df["volume"].tail(20) == 0).sum())
                if zero >= 3:
                    findings.append(Finding("price_zero_volume", "warning",
                                            f"{inst.symbol} : {zero} séance(s) à volume nul sur 20 (liquidité / suspension ?)",
                                            inst.id))
    return findings


def check_fundamentals(engine: Engine) -> list[Finding]:
    findings: list[Finding] = []
    with session_scope(engine) as s:
        for inst in s.scalars(select(Instrument).where(Instrument.is_active.is_(True), Instrument.asset_type == "stock")):
            if not inst.cik:
                findings.append(Finding("fund_no_cik", "warning", f"{inst.symbol} : CIK SEC inconnu", inst.id))
                continue
            n, last_filed = s.execute(
                select(func.count(), func.max(FundamentalFact.filed_date)).where(FundamentalFact.instrument_id == inst.id)
            ).one()
            if not n:
                findings.append(Finding("fund_missing", "warning", f"{inst.symbol} : aucun fait XBRL", inst.id))
                continue
            # Contrôle précis : chaque 10-Q/10-K déposé après le dernier fait XBRL connu est un rapport
            # publié mais absent de companyfacts -> les fondamentaux de ce titre sont en retard.
            missing = list(s.execute(
                select(Filing.form, Filing.filed_date, Filing.url).where(
                    Filing.instrument_id == inst.id,
                    Filing.form.in_(PERIODIC_FORMS),
                    Filing.filed_date > last_filed,
                ).order_by(Filing.filed_date)
            ))
            if missing:
                lst = ", ".join(f"{f} du {d}" for f, d, _ in missing)
                findings.append(Finding(
                    "fund_xbrl_lag", "warning",
                    f"{inst.symbol} : {len(missing)} rapport(s) publié(s) sans faits XBRL dans companyfacts "
                    f"({lst}) ; derniers fondamentaux utilisables : dépôt du {last_filed} ; "
                    f"source : {missing[-1][2]}", inst.id))
            elif (date.today() - last_filed).days > 120:
                findings.append(Finding("fund_stale", "info",
                                        f"{inst.symbol} : aucun 10-Q/10-K depuis le {last_filed} (>120 j)", inst.id))
    return findings


def check_macro(engine: Engine, today: date) -> list[Finding]:
    findings: list[Finding] = []
    with session_scope(engine) as s:
        for ser in s.scalars(select(MacroSeries)):
            last = s.scalar(select(func.max(MacroObservation.obs_date)).where(
                MacroObservation.series_id == ser.id, MacroObservation.value.is_not(None)))
            if last is None:
                findings.append(Finding("macro_missing", "warning", f"{ser.series_code} : aucune observation"))
                continue
            max_age = next((v for k, v in MACRO_MAX_AGE.items() if (ser.frequency or "").startswith(k)), 60)
            if (today - last).days > max_age:
                findings.append(Finding("macro_stale", "warning",
                                        f"{ser.series_code} : dernière valeur {last} (> {max_age} j)"))
    return findings


def run_quality_checks(engine: Engine, today: date | None = None, stale_sessions: int = 2,
                       job_run_id: int | None = None) -> list[Finding]:
    today = today or date.today()
    findings = check_prices(engine, today, stale_sessions) + check_fundamentals(engine) + check_macro(engine, today)
    now = datetime.now(timezone.utc)
    with session_scope(engine) as s:
        s.execute(update(DataAnomaly).where(DataAnomaly.resolved_at.is_(None)).values(resolved_at=now))
        for f in findings:
            s.add(DataAnomaly(job_run_id=job_run_id, check_name=f.check, severity=f.severity,
                              instrument_id=f.instrument_id, detail=f.detail))
    return findings
