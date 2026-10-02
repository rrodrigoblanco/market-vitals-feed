"""Cross-asset paragraph. Every number names the series and the observation date.

A missing series is described as missing. It is not replaced with an older print.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from feeds.plain import long_date
from feeds.stats import change_n, dedupe


def _points(bp: int | float | None) -> str:
    if bp is None:
        return "an unpublished"
    amount = abs(Decimal(str(bp)) / Decimal(100))
    return f"{amount:.2f}"


def _signed_points(bp: int | float | None) -> str:
    if bp is None:
        return "an unpublished amount"
    amount = Decimal(str(bp)) / Decimal(100)
    if amount > 0:
        return f"up {amount:.2f} percentage points"
    if amount < 0:
        return f"down {abs(amount):.2f} percentage points"
    return "unchanged"


def _level(rows: list[tuple[date, float]] | None) -> tuple[date, float] | None:
    if not rows:
        return None
    return dedupe(rows)[-1]


def _change(rows: list[tuple[date, float]] | None, steps: int) -> float | None:
    if not rows:
        return None
    result = change_n(dedupe(rows), steps)
    if result is None:
        return None
    return float(result["change"])


def _trillions_from_millions(value: float) -> str:
    return f"{value / 1_000_000:.2f} trillion dollars"


def _repo_amount(value: float) -> str:
    """FRED prints reverse repo in billions, including fractions of a billion."""
    amount = abs(value)
    if amount >= 10:
        return f"{amount:.0f}"
    if amount >= 1:
        return f"{amount:.1f}"
    text = f"{amount:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _signed_billions(change_millions: float) -> str:
    billions = change_millions / 1_000
    if billions > 0:
        return f"up {abs(billions):.0f} billion dollars"
    if billions < 0:
        return f"down {abs(billions):.0f} billion dollars"
    return "unchanged"


def what_the_tape_is_saying(
    *,
    hy_bp: int | None,
    hy_as_of: str | None,
    hy_5d_bp: int | None,
    ig_5d_bp: int | None,
    ccc_bb_bp: int | None,
    yield_5d_bp: int | None,
    spread_5d_bp: int | None,
    treasury_5d_bp: int | None,
    dgs10: list[tuple[date, float]] | None,
    dgs30: list[tuple[date, float]] | None,
    real_yield: list[tuple[date, float]] | None,
    breakeven: list[tuple[date, float]] | None,
    dollar: list[tuple[date, float]] | None,
    vix: list[tuple[date, float]] | None,
    balance_sheet: list[tuple[date, float]] | None,
    reserves: list[tuple[date, float]] | None,
    rrp: list[tuple[date, float]] | None,
    brent: float | None,
    brent_as_of: str | None,
    brent_5d_pct: float | None,
    curve: str | None,
) -> str:
    """Three or four sentences a rates or credit trader can read straight through."""
    hy_day = long_date(hy_as_of)
    if hy_bp is None:
        credit = f"As of {hy_day}, the high-yield spread was not published, so the credit half of this read is incomplete."
    else:
        split = ""
        if spread_5d_bp is not None and treasury_5d_bp is not None and yield_5d_bp is not None:
            split = (
                f" Of the {_points(yield_5d_bp)} percentage-point move in the all-in junk yield, "
                f"{_points(spread_5d_bp)} was the credit spread and {_points(treasury_5d_bp)} was Treasury rates."
            )
        gap = ""
        if ccc_bb_bp is not None:
            gap = f" The CCC-minus-BB gap is {_points(ccc_bb_bp)} percentage points."
        ig = ""
        if ig_5d_bp is not None:
            ig = f" Investment-grade spreads are {_signed_points(ig_5d_bp)} over those same five trading days."
        credit = (
            f"As of {hy_day}, junk-bond spreads are {_points(hy_bp)} percentage points and are "
            f"{_signed_points(hy_5d_bp)} over five trading days.{ig}{gap}{split}"
        )

    ten = _level(dgs10)
    thirty = _level(dgs30)
    if ten and thirty:
        curve_gap = thirty[1] - ten[1]
        rates = (
            f"The 10-year yield is {ten[1]:.2f} percent as of {long_date(ten[0].isoformat())} and the "
            f"30-year is {thirty[1]:.2f} percent as of {long_date(thirty[0].isoformat())}, a gap of "
            f"{curve_gap:.2f} percentage points."
        )
    else:
        rates = "The 10-year or 30-year yield was not published, so the curve gap is not in this read."

    bei = _level(breakeven)
    real = _level(real_yield)
    oil_bits = []
    if brent is not None:
        move = "an unpublished five-day change" if brent_5d_pct is None else f"{brent_5d_pct:+.2f} percent over five trading days"
        shape = curve or "an unpublished curve shape"
        oil_bits.append(
            f"Brent is {brent:.2f} dollars a barrel as of {long_date(brent_as_of)} ({move}), and the curve is {shape}."
        )
    else:
        oil_bits.append("Brent was not published in this run.")
    if bei:
        bei_change = _change(breakeven, 5)
        if bei_change is None:
            bei_move = "the five-observation change was not available"
        else:
            bei_move = _signed_points(bei_change * 100) + " over five observations"
        oil_bits.append(
            f"The 5-year inflation breakeven is {bei[1]:.2f} percent as of {long_date(bei[0].isoformat())}, {bei_move}."
        )
    else:
        oil_bits.append("The 5-year breakeven was not published.")
    if real:
        oil_bits.append(
            f"The 10-year real yield is {real[1]:.2f} percent as of {long_date(real[0].isoformat())}."
        )
    else:
        oil_bits.append("The 10-year real yield was not published.")

    dollar_level = _level(dollar)
    vix_level = _level(vix)
    market = []
    if dollar_level:
        dollar_change = _change(dollar, 20)
        move = "the 20-observation change was not available" if dollar_change is None else f"{dollar_change:+.2f} index points over 20 observations"
        market.append(
            f"The broad dollar index is {dollar_level[1]:.2f} as of {long_date(dollar_level[0].isoformat())}, {move}."
        )
    else:
        market.append("The broad dollar index was not published.")
    if vix_level:
        market.append(f"VIX is {vix_level[1]:.2f} as of {long_date(vix_level[0].isoformat())}.")
    else:
        market.append("VIX was not published.")

    def _weekly(name: str, rows: list[tuple[date, float]] | None, *, millions: bool) -> str:
        level = _level(rows)
        if level is None:
            return f"The {name} was not published."
        change = _change(rows, 4)
        when = long_date(level[0].isoformat())
        if millions:
            subject = "Reserve balances are" if name == "reserve balances" else f"The {name} is"
            text = f"{subject} {_trillions_from_millions(level[1])} as of {when}"
            if change is None:
                return text + "."
            return text + f", {_signed_billions(change)} over the latest four weekly observations."
        level_text = _repo_amount(level[1])
        text = f"Overnight reverse repo is {level_text} billion dollars as of {when}"
        if change is None:
            return text + "."
        if change > 0:
            move = f"up {_repo_amount(change)} billion"
        elif change < 0:
            move = f"down {_repo_amount(change)} billion"
        else:
            move = "unchanged"
        return text + f", {move} over the latest four observations."

    liquidity = " ".join(
        (
            _weekly("Fed balance sheet", balance_sheet, millions=True),
            _weekly("reserve balances", reserves, millions=True),
            _weekly("overnight reverse repo", rrp, millions=False),
        )
    )
    return " ".join((credit, rates, " ".join(oil_bits), " ".join(market), liquidity))
