"""Calendrier des séances NYSE (jours fériés réguliers). Ne couvre pas les fermetures exceptionnelles
(ex. funérailles nationales) : elles apparaîtront comme des « trous » à faible sévérité dans les contrôles."""

from __future__ import annotations

from datetime import date
from functools import lru_cache

import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    Holiday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    nearest_workday,
)
from pandas.tseries.offsets import CustomBusinessDay


class NYSEHolidayCalendar(AbstractHolidayCalendar):
    rules = [
        Holiday("NewYearsDay", month=1, day=1, observance=nearest_workday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01", observance=nearest_workday),
        Holiday("IndependenceDay", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas", month=12, day=25, observance=nearest_workday),
    ]


@lru_cache(maxsize=1)
def _bday() -> CustomBusinessDay:
    return CustomBusinessDay(calendar=NYSEHolidayCalendar())


def trading_days(start: date, end: date) -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq=_bday())


def last_completed_session(today: date) -> date:
    """Dernière séance dont la clôture EOD est censée être disponible.

    Prudence : la séance du jour n'est jamais considérée comme complète (les données EOD arrivent
    en soirée US, donc la nuit en heure de Paris)."""
    days = trading_days(pd.Timestamp(today) - pd.Timedelta(days=14), pd.Timestamp(today) - pd.Timedelta(days=1))
    return days[-1].date()


def sessions_between(a: date, b: date) -> int:
    """Nombre de séances strictement après a et jusqu'à b inclus."""
    if b <= a:
        return 0
    return len(trading_days(pd.Timestamp(a) + pd.Timedelta(days=1), b))
