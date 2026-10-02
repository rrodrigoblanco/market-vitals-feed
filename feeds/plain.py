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


def _ordinal(value: float) -> str:
    number = int(round(value))
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _history_clause(block: dict, name: str) -> str:
    level = block.get("value")
    if not isinstance(level, (int, float)):
        return f"{name} does not have a published level"
    text = f"{name} {_points(level)} percentage points"
    window = block.get("percentile_available") or {}
    if isinstance(window.get("value"), (int, float)) and window.get("n_days") and window.get("window_start"):
        text += (
            f", the {_ordinal(window['value'])} percentile of the {window['n_days']} days "
            f"still on file since {long_date(window['window_start'])}"
        )
    return text


def credit_sentences(
    series: dict,
    label: str,
    *,
    speed: dict | None = None,
    decomposition: dict | None = None,
) -> tuple[str, str]:
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

    second = (
        f"{_history_clause(hy, 'High-yield spreads are')}, "
        f"{_history_clause(series.get('ig_oas') or {}, 'investment-grade spreads are')}, and "
        f"{_history_clause(series.get('bb_oas') or {}, 'BB spreads are')}."
    )
    accel = ((speed or {}).get("acceleration") or {})
    prior = (speed or {}).get("prior_5d") or {}
    if speed and speed.get("value") is not None and prior.get("value") is not None and accel.get("value") is not None:
        if accel.get("direction") == "steady":
            pace = (
                f"The five-day pace is a move of {_points(speed['value'])} percentage points, "
                f"versus {_points(prior['value'])} percentage points over the five days before that, so the pace is about the same."
            )
        else:
            word = "speeding up" if accel.get("direction") == "increasing" else "slowing down"
            pace = (
                f"The five-day pace is a move of {_points(speed['value'])} percentage points, "
                f"versus {_points(prior['value'])} percentage points over the five days before that, "
                f"so the pace is {word} by {_points(accel['value'])} percentage points."
            )
    elif hy_move is not None:
        pace = (
            f"The five-day pace is a move of {_points(hy_move)} percentage points, "
            "and there is not yet an earlier five-day pace to measure acceleration."
        )
    else:
        pace = "The five-day pace is not available yet."

    gap = series.get("ccc_bb") or {}
    window = (((decomposition or {}).get("windows") or {}).get("5d") or {})
    spread_part = window.get("spread_part") if isinstance(window.get("spread_part"), dict) else {}
    treasury_part = window.get("treasury_part") if isinstance(window.get("treasury_part"), dict) else {}
    yield_change = window.get("yield_change") if isinstance(window.get("yield_change"), dict) else {}
    gap_text = (
        f"The gap between CCC and BB bonds is {_points(gap['value'])} percentage points"
        if isinstance(gap.get("value"), (int, float))
        else "The CCC-minus-BB gap is not available"
    )
    if all(isinstance(part.get("value"), (int, float)) for part in (spread_part, treasury_part, yield_change)):
        split = (
            f"the all-in junk yield moved {_points(yield_change['value'])} percentage points over five days, "
            f"of which {_points(spread_part['value'])} was the credit spread and "
            f"{_points(treasury_part['value'])} was Treasury rates"
        )
    else:
        split = "the split of the all-in yield between credit and Treasury rates is not available for this window"
    gap_clause = gap_text[0].lower() + gap_text[1:] if gap_text else gap_text
    third = f"{pace.rstrip('.')}, and {gap_clause}, and {split}, {_closer(label)}"
    summary = " ".join((first, second, third))
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


def oil_sentences(
    brent: dict,
    wti: dict,
    curve: dict | None,
    alert_on: bool,
    diesel: dict | None = None,
    inventories: dict | None = None,
    spillover: dict | None = None,
) -> tuple[str, str]:
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
    curve_body = _curve_sentence(curve).rstrip(".")
    crack = (diesel or {}).get("futures_crack") or {}
    spot_crack = (diesel or {}).get("spot_crack") or {}
    if crack.get("available") and isinstance(crack.get("value"), (int, float)):
        second = f"{curve_body}, and the heating-oil crack versus Brent is {float(crack['value']):.2f} dollars a barrel."
    elif spot_crack.get("available") and isinstance(spot_crack.get("value"), (int, float)):
        second = (
            f"{curve_body}, and the EIA diesel crack versus Brent spot is "
            f"{float(spot_crack['value']):.2f} dollars a barrel as of {long_date(spot_crack.get('as_of'))}."
        )
    else:
        second = curve_body + "."
    stocks = inventories or {}
    dgs = (spillover or {}).get("dgs30") or {}
    bei = (spillover or {}).get("t5yie") or {}
    extras: list[str] = []
    if stocks.get("available") and isinstance(stocks.get("value"), (int, float)):
        extras.append(
            f"US commercial crude stocks were {float(stocks['value']):.2f} million barrels "
            f"in the week of {long_date(stocks.get('as_of'))}"
        )
    if isinstance(dgs.get("value"), (int, float)):
        extras.append(
            f"the 30-year yield is {float(dgs['value']):.2f} percent as of {long_date(dgs.get('as_of'))}"
        )
    if isinstance(bei.get("value"), (int, float)):
        extras.append(
            f"the 5-year breakeven is {float(bei['value']):.2f} percent as of {long_date(bei.get('as_of'))}"
        )
    if extras:
        third = ", ".join(extras) + ", and " + alert[0].lower() + alert[1:]
    else:
        third = alert
    return " ".join((first, second, third)), change
