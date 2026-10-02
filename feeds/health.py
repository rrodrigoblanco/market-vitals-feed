"""Health file. A green Actions run means the core daily series are not stale.

Stale means the latest real observation is more than 3 US equity business days
behind the New York run date. Weekly Fed series get 12 calendar days, because
a Wednesday print is still the current print on the next Tuesday.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from feeds.calendar import business_days_between

DAILY_STALE_AFTER = 3
WEEKLY_STALE_AFTER_CALENDAR_DAYS = 12

# These are the daily series a missed run must not hide.
CORE_DAILY = (
    "BAMLH0A0HYM2",
    "BAMLC0A0CM",
    "BAMLH0A1HYBB",
    "BAMLH0A3HYC",
    "VIXCLS",
    "DGS10",
    "T5YIE",
)
WEEKLY = ("WALCL", "WRESBAL")


def _age(as_of: str | None, today: date) -> dict:
    if not as_of:
        return {"as_of": None, "business_days_old": None, "stale": True, "reason": "no observation"}
    day = date.fromisoformat(as_of[:10])
    age = business_days_between(day, today)
    return {
        "as_of": day.isoformat(),
        "business_days_old": age,
        "stale": age > DAILY_STALE_AFTER,
        "reason": None if age <= DAILY_STALE_AFTER else f"{age} business days old",
    }


def _weekly(as_of: str | None, today: date) -> dict:
    if not as_of:
        return {"as_of": None, "calendar_days_old": None, "stale": True, "reason": "no observation"}
    day = date.fromisoformat(as_of[:10])
    age = (today - day).days
    stale = age > WEEKLY_STALE_AFTER_CALENDAR_DAYS
    return {
        "as_of": day.isoformat(),
        "calendar_days_old": age,
        "stale": stale,
        "reason": None if not stale else f"{age} calendar days old",
    }


def build_status(
    *,
    today: date,
    daily_as_of: dict[str, str | None],
    weekly_as_of: dict[str, str | None],
    feed_errors: list[str],
    now: datetime | None = None,
) -> dict:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    stamp = moment.isoformat().replace("+00:00", "Z")
    daily = {series_id: _age(daily_as_of.get(series_id), today) for series_id in CORE_DAILY}
    weekly = {series_id: _weekly(weekly_as_of.get(series_id), today) for series_id in WEEKLY}
    stale = [series_id for series_id, row in {**daily, **weekly}.items() if row["stale"]]
    if feed_errors:
        state = "error"
    elif stale:
        state = "stale"
    else:
        state = "ok"
    return {
        "status": state,
        "fail_job": state != "ok",
        "last_attempt": stamp,
        "last_success": None if feed_errors else stamp,
        "run_date": today.isoformat(),
        "run_timezone": "America/New_York",
        "stale_after_business_days": DAILY_STALE_AFTER,
        "weekly_stale_after_calendar_days": WEEKLY_STALE_AFTER_CALENDAR_DAYS,
        "daily": daily,
        "weekly": weekly,
        "stale_series": stale,
        "errors": list(feed_errors),
        "note": (
            "This file is the heartbeat. status ok means every core daily series is at most "
            "3 business days old and no feed raised an error. A stale or error state fails "
            "the GitHub Actions job after the latest good files have been written."
        ),
    }
