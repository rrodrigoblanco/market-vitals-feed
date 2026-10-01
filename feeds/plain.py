"""Plain-English sentences for someone who does not follow credit or oil jargon.

The regime codes and the alert flag stay in their own fields. These sentences
do not use those codes. A percentage point is written out, because "bp" is
not meaningful on its own.
"""

from __future__ import annotations

from decimal import Decimal

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def long_date(iso: str | None) -> str:
    if not iso or len(iso) < 10:
        return "the latest published day"
    year, month, day = (int(part) for part in iso[:10].split("-"))
    return f"{MONTHS[month - 1]} {day}, {year}"


def _points(bp: int | float) -> str:
    amount = (Decimal(str(bp)) / Decimal(100)).copy_abs()
    return f"{amount:.2f}"


def _spread_move(bp: int | float | None, subject: str) -> str:
    if bp is None:
        return f"the five-day change in {subject} is not published yet"
    if bp > 0:
        return f"{subject} widened by {_points(bp)} percentage points"
    if bp < 0:
        return f"{subject} narrowed by {_points(bp)} percentage points"
    return f"{subject} did not change"


def _change_value(block: dict | None) -> int | float | None:
    if not isinstance(block, dict):
        return None
    change = block.get("change_5d")
    if not isinstance(change, dict):
        return None
    value = change.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _bb_comparison(level: int | float) -> str:
    if level >= 306:
        return (
            "which is at or above the April 2025 peak of 3.06 percentage points"
        )
    if level >= 222:
        return (
            "which is at or above the March 2026 high of 2.22 percentage points, "
            "though still under the April 2025 peak of 3.06 percentage points"
        )
    return (
        "which is still below the March 2026 high of 2.22 percentage points "
        "and the April 2025 peak of 3.06 percentage points"
    )


def _bb_sentence(series: dict) -> str:
    bb = series.get("bb_oas") or {}
    level = bb.get("value")
    move = _change_value(bb)
    if not isinstance(level, (int, float)):
        return "Better-quality junk bonds, called BB, do not have a published level in this file."
    if move is None:
        moved = "and the five-day change is not available"
    elif move > 0:
        moved = f"after widening by {_points(move)} percentage points"
    elif move < 0:
        moved = f"after narrowing by {_points(move)} percentage points"
    else:
        moved = "with no change over those five trading days"
    return (
        f"Better-quality junk bonds, called BB, are at {_points(level)} percentage points "
        f"{moved}, {_bb_comparison(level)}."
    )


def _closer(label: str) -> str:
    if label == "SYSTEMIC":
        return (
            "and BB bonds are already expensive versus their own history, "
            "so this is a market-wide credit problem."
        )
    if label == "BROADENING":
        return "so this is stress climbing through the junk market, not a market-wide credit problem."
    if label == "CONCENTRATED":
        return "so this looks like stress at the weak end, not a market-wide credit problem."
    if label == "UNKNOWN":
        return "so there is not enough of a five-day record yet to call this a credit stress event."
    return "and none of these moves are large enough to call this a credit stress event."


def _what_would_change(label: str) -> str:
    if label == "SYSTEMIC":
        return (
            "This would stop looking market-wide if investment-grade bonds were no longer "
            "widening by at least 0.10 percentage points over five trading days or 0.15 "
            "percentage points over twenty trading days, or if BB bonds fell below 2.22 "
            "percentage points and out of the top 10 percent of the days still on file."
        )
    if label == "BROADENING":
        return (
            "This would become a market-wide credit problem if BB bonds reached 2.22 "
            "percentage points, or the top 10 percent of the days still on file, and "
            "investment-grade bonds widened by at least 0.10 percentage points over five "
            "trading days or 0.15 percentage points over twenty trading days."
        )
    if label == "CONCENTRATED":
        return (
            "This would stop being limited to the weakest bonds if BB bonds widened by "
            "at least 0.15 percentage points over five trading days or 0.25 percentage "
            "points over twenty trading days, and it would become market-wide if those "
            "BB bonds were also at 2.22 percentage points or higher and investment-grade "
            "bonds widened by at least 0.10 percentage points over five trading days."
        )
    if label == "UNKNOWN":
        return (
            "This would change once FRED publishes enough recent days of junk, BB, CCC, "
            "and investment-grade bond spreads to measure a five-day move."
        )
    return (
        "This would stop looking quiet if the riskiest CCC bonds widened by at least "
        "0.30 percentage points over five trading days while BB bonds stayed put, or if "
        "BB bonds widened by at least 0.15 percentage points and CCC bonds widened with them."
    )


def credit_sentences(series: dict, label: str) -> tuple[str, str]:
    """Two or three sentences, then one sentence on what would change the reading."""
    hy = series.get("hy_oas") or {}
    ccc_move = _change_value(series.get("ccc_oas"))
    hy_move = _change_value(hy)
    when = long_date(hy.get("as_of"))

    led = ccc_move is not None and hy_move is not None and ccc_move > hy_move and ccc_move > 0
    if led:
        ccc_part = (
            f"led by the riskiest bonds, called CCC, which widened by {_points(ccc_move)} percentage points"
        )
    else:
        ccc_part = _spread_move(ccc_move, "the riskiest bonds, called CCC,")

    if hy_move is None:
        first = (
            f"As of {when}, the five-day change in junk-bond spreads is not published yet, "
            f"and {ccc_part}."
        )
    elif led:
        first = (
            f"As of {when}, {_spread_move(hy_move, 'junk-bond spreads')} over the previous "
            f"five trading days, {ccc_part}."
        )
    else:
        first = (
            f"As of {when}, {_spread_move(hy_move, 'junk-bond spreads')} over the previous "
            f"five trading days, and {ccc_part}."
        )

    ig_move = _change_value(series.get("ig_oas"))
    ig_text = _spread_move(ig_move, "Safer corporate bonds, called investment grade,")
    if ig_move:
        ig_text += " over five trading days"
    third = f"{ig_text}, {_closer(label)}"
    summary = " ".join((first, _bb_sentence(series), third))
    return summary, _what_would_change(label)


def _percent_clause(change: dict | None) -> str:
    if not isinstance(change, dict):
        return ""
    pct = change.get("pct")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return ""
    if pct > 0:
        return f", up {pct:.2f} percent over five trading days"
    if pct < 0:
        return f", down {abs(pct):.2f} percent over five trading days"
    return ", unchanged over five trading days"


def _curve_sentence(curve: dict | None) -> str:
    curve = curve or {}
    for key, name in (("brent", "Brent"), ("wti", "WTI")):
        leg = curve.get(key) or {}
        if not leg.get("available"):
            continue
        shape = leg.get("shape")
        if shape == "backwardation":
            return (
                f"Near-term {name} costs more than {name} for delivery about a year later, "
                "so buyers are paying up for barrels today."
            )
        if shape == "contango":
            return (
                f"{name} for delivery about a year later costs more than near-term {name}, "
                "so the market is not paying extra for barrels today."
            )
        if shape == "flat":
            return (
                f"Near-term {name} and {name} for delivery about a year later cost about the same."
            )
    return (
        "The price gap between oil for delivery soon and oil for delivery next year is not "
        "in this file, because that later contract could not be read from a free source."
    )


def oil_sentences(brent: dict, wti: dict, curve: dict | None, alert_on: bool) -> tuple[str, str]:
    """Three sentences, then one sentence on what would turn the alert on or off."""
    headline = brent.get("headline")
    if headline == "futures":
        kind = "on the latest daily futures price"
    else:
        kind = "on the EIA spot price, which is published a few days late"
    first = (
        f"As of {long_date(brent.get('as_of'))}, Brent crude, the main global oil benchmark, "
        f"is {float(brent['value']):.2f} dollars a barrel {kind}"
        f"{_percent_clause(brent.get('change_5d'))}, and US WTI crude is "
        f"{float(wti['value']):.2f} dollars a barrel{_percent_clause(wti.get('change_5d'))}."
    )
    if alert_on:
        alert = (
            "The alert is on, because Brent is up more than 5 percent over five trading days "
            "and the 30-year Treasury yield is higher than it was five trading days earlier."
        )
        change = (
            "The alert would turn off if Brent's five-day gain fell to 5 percent or less, "
            "or if the 30-year Treasury yield was no longer higher than it was five trading days earlier."
        )
    else:
        alert = (
            "The alert is off, because it turns on only when Brent is up more than 5 percent "
            "over five trading days and the 30-year Treasury yield is also higher than it was "
            "five trading days earlier."
        )
        change = (
            "The alert would turn on if Brent rose by more than 5 percent over five trading days "
            "and the 30-year Treasury yield was higher than it was five trading days earlier."
        )
    return " ".join((first, _curve_sentence(curve), alert)), change
