"""Date-true changes, percentiles, and distributions.

Every change records the two observation dates it actually used. Nothing in
here invents a value for a date that was not in the series.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import TypeVar

from feeds.calendar import add_months

Number = int | float
Observation = tuple[date, Number]
T = TypeVar("T")


def dedupe(observations: list[Observation]) -> list[Observation]:
    by_date: dict[date, Number] = {}
    for day, value in observations:
        by_date[day] = value
    return sorted(by_date.items())


def change_n(observations: list[Observation], steps: int) -> dict | None:
    """Latest value minus the value `steps` real observations earlier."""
    if steps <= 0 or len(observations) <= steps:
        return None
    to_date, to_value = observations[-1]
    from_date, from_value = observations[-1 - steps]
    return {
        "from_date": from_date,
        "to_date": to_date,
        "from_value": from_value,
        "to_value": to_value,
        "change": to_value - from_value,
        "observations_back": steps,
    }


def change_months(observations: list[Observation], months: int) -> dict | None:
    """Latest value minus the last real print on or before the calendar date."""
    if not observations:
        return None
    to_date, to_value = observations[-1]
    target = add_months(to_date, -months)
    prior = [row for row in observations if row[0] <= target]
    if not prior:
        return None
    from_date, from_value = prior[-1]
    return {
        "from_date": from_date,
        "to_date": to_date,
        "from_value": from_value,
        "to_value": to_value,
        "change": to_value - from_value,
        "observations_back": None,
        "calendar_months": months,
        "target_date": target,
    }


def value_on(observations: list[Observation], day: date) -> Number | None:
    """Exact-date lookup. A nearby print is not a substitute."""
    for obs_date, value in observations:
        if obs_date == day:
            return value
    return None


def speed_pair(observations: list[Observation], steps: int = 5) -> tuple[dict | None, dict | None]:
    """Today's N-step change, and that same change as of N observations ago."""
    current = change_n(observations, steps)
    if len(observations) <= steps:
        return current, None
    prior = change_n(observations[:-steps], steps)
    return current, prior


def previous_speed(observations: list[Observation], steps: int = 5) -> dict | None:
    """The N-step change ending on the previous observation."""
    if len(observations) <= steps + 1:
        return None
    return change_n(observations[:-1], steps)


def empirical_percentile(values: list[Number], current: Number) -> float:
    """Share of observations at or below `current`, in percent."""
    if not values:
        raise ValueError("percentile window is empty")
    count = sum(1 for value in values if value <= current)
    return 100.0 * count / len(values)


def trailing_year(observations: list[Observation]) -> list[Observation]:
    """Observations strictly after as_of minus 365 calendar days, through as_of."""
    if not observations:
        return []
    as_of = observations[-1][0]
    cutoff = as_of - timedelta(days=365)
    return [row for row in observations if row[0] > cutoff]


def quantile(sorted_values: list[Number], percent: float) -> float:
    """Linear interpolation between ranks. `percent` is 0 to 100."""
    if not sorted_values:
        raise ValueError("empty distribution")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (percent / 100.0) * (len(sorted_values) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    weight = rank - low
    return float(sorted_values[low]) * (1.0 - weight) + float(sorted_values[high]) * weight


def distribution(values: list[Number]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "min": float(ordered[0]),
        "p10": quantile(ordered, 10),
        "p25": quantile(ordered, 25),
        "median": quantile(ordered, 50),
        "p75": quantile(ordered, 75),
        "p90": quantile(ordered, 90),
        "max": float(ordered[-1]),
    }


def align_difference(
    left: list[Observation],
    right: list[Observation],
) -> list[Observation]:
    """left minus right, only on dates where both series have a real print."""
    right_by_date = dict(right)
    rows = []
    for day, left_value in left:
        if day in right_by_date:
            rows.append((day, left_value - right_by_date[day]))
    return rows


def shared_level(
    left: list[Observation],
    right: list[Observation],
    combine,
) -> list[Observation]:
    right_by_date = dict(right)
    rows = []
    for day, left_value in left:
        if day in right_by_date:
            rows.append((day, combine(left_value, right_by_date[day])))
    return rows
