"""US equity business days, used only to decide whether a print is stale.

Holidays follow the regular NYSE schedule (observed weekends, plus Good
Friday). One-off closures are not included. A stale flag that is wrong by
one unusual holiday is acceptable; copying a price onto a new date is not.
"""

from __future__ import annotations

import calendar as cal
from datetime import date, timedelta
from functools import lru_cache


def add_months(day: date, months: int) -> date:
    """Return the same day-of-month, shifted by `months`, clamped to the month."""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last = cal.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    shift = (weekday - first.weekday()) % 7
    return first + timedelta(days=shift + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    shift = (cursor.weekday() - weekday) % 7
    return cursor - timedelta(days=shift)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=32)
def nyse_holidays(year: int) -> frozenset[date]:
    """Standard NYSE full closures for one calendar year."""
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),  # MLK
        _nth_weekday(year, 2, 0, 3),  # Presidents
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial
        _observed(date(year, 6, 19)),  # Juneteenth
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    # New Year's observed on the previous Friday when Jan 1 is a Saturday
    # lands in the prior year. Pull that in so a year-boundary check works.
    new_year = date(year + 1, 1, 1)
    if new_year.weekday() == 5:
        holidays.add(date(year, 12, 31))
    return frozenset(holidays)


def is_business_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def business_days_between(start: date, end: date) -> int:
    """Count business days strictly after `start` and on or before `end`.

    This is how old the observation is. A print from the previous business
    day has age 1. Age 0 means the print is dated today or later.
    """
    if end <= start:
        return 0
    count = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        if is_business_day(cursor):
            count += 1
    return count


def is_stale(as_of: date, today: date, after_business_days: int = 2) -> bool:
    """True when the print is older than `after_business_days` business days."""
    return business_days_between(as_of, today) > after_business_days
