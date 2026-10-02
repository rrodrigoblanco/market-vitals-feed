"""Decide whether a rebuilt file is worth a commit.

GitHub may start a scheduled run late or skip one. FRED posts once, at the
end of the day, and the print is the previous business day. A later run that
sees the same observation date must leave the file untouched. A live quote
that ticks on an unchanged FRED date must not create a commit either.
"""

from __future__ import annotations


STORY_VERSION = 3


def should_publish(
    existing: dict | None,
    latest_dates: dict[str, str],
    *,
    story_version: int | None = None,
) -> bool:
    """True when the file is missing plain English, or any official date moved forward.

    ``latest_dates`` maps a source id such as ``BAMLH0A0HYM2`` to ``YYYY-MM-DD``.
    A date that is equal to or older than the stored date does not count.
    """
    if not isinstance(existing, dict):
        return True
    summary = existing.get("summary")
    change = existing.get("what_would_change_this")
    if not isinstance(summary, str) or not summary.endswith("."):
        return True
    if not isinstance(change, str) or not change.endswith("."):
        return True
    if story_version is not None:
        current = existing.get("story_version")
        if not isinstance(current, int) or current < story_version:
            return True
    stored = existing.get("official_as_of")
    if not isinstance(stored, dict) or not stored:
        return True
    for key, day in latest_dates.items():
        if not isinstance(day, str) or len(day) < 10:
            continue
        previous = stored.get(key)
        if not isinstance(previous, str) or day > previous:
            return True
    return False


def publish_reason(
    existing: dict | None,
    latest_dates: dict[str, str],
    *,
    story_version: int | None = None,
) -> str | None:
    """A short reason for the log, or None when the run should leave the file alone."""
    if not should_publish(existing, latest_dates, story_version=story_version):
        return None
    if isinstance(existing, dict) and story_version is not None:
        current = existing.get("story_version")
        if not isinstance(current, int) or current < story_version:
            return "the trader story is out of date"
    summary = existing.get("summary") if isinstance(existing, dict) else None
    change = existing.get("what_would_change_this") if isinstance(existing, dict) else None
    if not isinstance(summary, str) or not summary.endswith("."):
        return "the plain-English summary is missing"
    if not isinstance(change, str) or not change.endswith("."):
        return "the plain-English summary is missing"
    stored = existing.get("official_as_of") if isinstance(existing, dict) else None
    if not isinstance(stored, dict) or not stored:
        return "the stored observation dates are missing"
    moved = []
    for key, day in latest_dates.items():
        if not isinstance(day, str) or len(day) < 10:
            continue
        previous = stored.get(key)
        if not isinstance(previous, str) or day > previous:
            moved.append(f"{key} {day}")
    if moved:
        return "newer observation: " + ", ".join(moved)
    return "the stored file needs a refresh"
