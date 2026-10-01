"""Credit monitor.

Spreads stay on the FRED observation date that actually printed them. A later
calendar day with no print does not inherit the previous number.

Regime labels, in order:

1. SYSTEMIC — BB's level is elevated AND investment-grade OAS is widening.
2. BROADENING — BB is widening AND CCC is leading, and rule 1 did not fire.
3. CONCENTRATED — CCC is leading AND BB is not widening, and rule 1 did not fire.
4. CALM — none of those.
5. UNKNOWN — the 5-day inputs are all missing, so there is nothing to judge.

"CCC is leading" means CCC itself is widening, or the CCC-minus-BB gap is
widening. "Elevated" uses the full available FRED sample, not a window labeled
5 years. The March 2026 reference (222 bp) also counts as elevated.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from feeds.calendar import business_days_between, is_stale
from feeds.fred import load_series
from feeds.serialize import json_number, measure, round_half_up
from feeds.stats import (
    align_difference,
    change_months,
    change_n,
    dedupe,
    distribution,
    empirical_percentile,
    previous_speed,
    speed_pair,
    trailing_year,
    value_on,
)

# Changes are in basis points. A series is "widening" only when the change
# is at least this large. Smaller moves do not flip the label.
THRESHOLDS = {
    "bb_move_5d_bp": 15,
    "bb_move_20d_bp": 25,
    "ccc_move_5d_bp": 30,
    "ccc_move_20d_bp": 50,
    "gap_widen_5d_bp": 20,
    "gap_widen_20d_bp": 40,
    "ig_widen_5d_bp": 10,
    "ig_widen_20d_bp": 15,
    "hy_ig_widen_5d_bp": 15,
    "hy_ig_widen_20d_bp": 25,
    "bb_elevated_percentile": 90.0,
    "bb_elevated_level_bp": 222,
    "acceleration_deadband_bp": 5,
    "stale_after_business_days": 2,
}

REFERENCES = (
    {
        "name": "April 2025 peak",
        "month": "2025-04",
        "provided_bp": 306,
        "tolerance_bp": 1,
        "note": "Supplied reference: BB OAS peaked at 306 bp in April 2025.",
    },
    {
        "name": "March 2026",
        "month": "2026-03",
        "provided_bp": 222,
        "tolerance_bp": 2,
        "note": "Supplied reference: BB OAS was about 222 bp in March 2026.",
    },
)

SERIES_SPEC = (
    {
        "id": "hy_oas",
        "fred_id": "BAMLH0A0HYM2",
        "name": "US high-yield spread",
        "plain_english": (
            "Extra yield that high-yield (junk) bonds pay over Treasuries. "
            "This is the option-adjusted spread, not the all-in interest rate."
        ),
        "kind": "oas",
    },
    {
        "id": "bb_oas",
        "fred_id": "BAMLH0A1HYBB",
        "name": "BB spread",
        "plain_english": "Spread on the better-quality slice of the junk-bond market.",
        "kind": "oas",
    },
    {
        "id": "ccc_oas",
        "fred_id": "BAMLH0A3HYC",
        "name": "CCC spread",
        "plain_english": "Spread on the weakest junk bonds (CCC and lower).",
        "kind": "oas",
    },
    {
        "id": "ig_oas",
        "fred_id": "BAMLC0A0CM",
        "name": "Investment-grade spread",
        "plain_english": "Spread on investment-grade corporate bonds. Stress that reaches here is broad.",
        "kind": "oas",
    },
    {
        "id": "hy_yield",
        "fred_id": "BAMLH0A0HYM2EY",
        "name": "High-yield all-in yield",
        "plain_english": (
            "The actual yield high-yield bonds pay, spread plus the Treasury rate. "
            "This is what a borrower refinancing today would care about."
        ),
        "kind": "percent",
    },
    {
        "id": "dgs10",
        "fred_id": "DGS10",
        "name": "10-year Treasury yield",
        "plain_english": "US 10-year government yield. Shown beside the credit spread, not inside it.",
        "kind": "percent",
    },
    {
        "id": "dgs30",
        "fred_id": "DGS30",
        "name": "30-year Treasury yield",
        "plain_english": "US 30-year government yield.",
        "kind": "percent",
    },
    {
        "id": "dgs5",
        "fred_id": "DGS5",
        "name": "5-year Treasury yield",
        "plain_english": (
            "US 5-year government yield. High-yield bonds often move with medium-term "
            "Treasuries. The yield-versus-spread split below does not copy this number; "
            "it is context."
        ),
        "kind": "percent",
        "optional": True,
    },
)

REQUIRED_IDS = tuple(spec["id"] for spec in SERIES_SPEC if not spec.get("optional"))

REGIME_RULES = [
    "SYSTEMIC if BB's level is elevated AND investment-grade OAS is widening.",
    "BROADENING if BB is widening AND CCC is leading, and the SYSTEMIC rule did not fire.",
    "CONCENTRATED if CCC is leading AND BB is not widening, and the SYSTEMIC rule did not fire.",
    "CALM if none of those rules fire.",
    "UNKNOWN if every 5-day input is missing.",
    "Rules are checked in that order. The first match wins.",
    "Widening means the spread rose by at least the threshold. A decline does not count as moving wider.",
    "CCC is leading if CCC's own spread is widening, or the CCC-minus-BB gap is widening.",
    "BB is elevated if its percentile over the full available FRED sample is at least 90, or its level is at least 222 bp (the March 2026 reference).",
    "The percentile is never labeled 5-year. FRED's ICE BofA history is only as long as the file says (window_start and n_days).",
]


def _at_least(change: int | None, threshold: int) -> bool:
    return change is not None and change >= threshold


def _bp_change(result: dict | None) -> int | None:
    if result is None:
        return None
    return int(result["change"])


def classify_regime(
    *,
    bb_5: int | None,
    bb_20: int | None,
    bb_level: int | None,
    bb_percentile: float | None,
    ccc_5: int | None,
    ccc_20: int | None,
    gap_5: int | None,
    gap_20: int | None,
    ig_5: int | None,
    ig_20: int | None,
    hy_ig_5: int | None,
    hy_ig_20: int | None,
) -> dict:
    """Return the regime label and the flags that produced it."""
    if all(value is None for value in (bb_5, ccc_5, ig_5, gap_5, hy_ig_5)):
        return {
            "label": "UNKNOWN",
            "plain_english": "The latest 5-day changes are missing, so there is no regime label.",
            "flags": {},
        }

    bb_moving = _at_least(bb_5, THRESHOLDS["bb_move_5d_bp"]) or _at_least(
        bb_20, THRESHOLDS["bb_move_20d_bp"]
    )
    ccc_moving = _at_least(ccc_5, THRESHOLDS["ccc_move_5d_bp"]) or _at_least(
        ccc_20, THRESHOLDS["ccc_move_20d_bp"]
    )
    gap_widening = _at_least(gap_5, THRESHOLDS["gap_widen_5d_bp"]) or _at_least(
        gap_20, THRESHOLDS["gap_widen_20d_bp"]
    )
    ccc_led = ccc_moving or gap_widening
    ig_widening = _at_least(ig_5, THRESHOLDS["ig_widen_5d_bp"]) or _at_least(
        ig_20, THRESHOLDS["ig_widen_20d_bp"]
    )
    hy_ig_widening = _at_least(hy_ig_5, THRESHOLDS["hy_ig_widen_5d_bp"]) or _at_least(
        hy_ig_20, THRESHOLDS["hy_ig_widen_20d_bp"]
    )
    elevated_by_percentile = (
        bb_percentile is not None and bb_percentile >= THRESHOLDS["bb_elevated_percentile"]
    )
    elevated_by_level = (
        bb_level is not None and bb_level >= THRESHOLDS["bb_elevated_level_bp"]
    )
    bb_elevated = elevated_by_percentile or elevated_by_level

    flags = {
        "bb_moving": bb_moving,
        "bb_elevated": bb_elevated,
        "bb_elevated_by_percentile": elevated_by_percentile,
        "bb_elevated_by_level": elevated_by_level,
        "ccc_moving": ccc_moving,
        "gap_widening": gap_widening,
        "ccc_led": ccc_led,
        "ig_widening": ig_widening,
        "ig_calm": not ig_widening,
        "hy_ig_widening": hy_ig_widening,
    }

    if bb_elevated and ig_widening:
        label = "SYSTEMIC"
        text = (
            "SYSTEMIC. BB spreads are high versus their own history, and "
            "investment-grade spreads are widening too. Stress is not stuck in the weakest junk bonds."
        )
    elif bb_moving and ccc_led:
        label = "BROADENING"
        if bb_elevated and not ig_widening:
            level_sentence = (
                "BB's level is elevated, but investment-grade spreads are calm, "
                "so this stays BROADENING and is not called SYSTEMIC."
            )
        elif ig_widening:
            level_sentence = (
                "Investment-grade spreads are widening as well, but BB's level is not "
                "elevated, so this stays BROADENING and is not called SYSTEMIC."
            )
        else:
            level_sentence = (
                "BB's level is not extreme, and investment-grade spreads are calm."
            )
        text = (
            "BROADENING. CCC and BB are both widening, so stress is climbing from "
            "the weakest junk into better junk. " + level_sentence
        )
    elif ccc_led and not bb_moving:
        label = "CONCENTRATED"
        text = (
            "CONCENTRATED. The widening is in CCC, or in the gap between CCC and BB. "
            "BB itself is not widening enough to call the move broad."
        )
    else:
        label = "CALM"
        text = (
            "CALM. BB, CCC, and investment grade are not widening enough to trip "
            "the concentrated, broadening, or systemic rules."
        )

    gap_clause = (
        "The high-yield minus investment-grade gap is widening."
        if hy_ig_widening
        else "The high-yield minus investment-grade gap is not widening on these thresholds."
    )
    text = f"{text} {gap_clause}"
    return {"label": label, "plain_english": text, "flags": flags}


def acceleration_direction(acceleration_bp: int | None) -> str | None:
    if acceleration_bp is None:
        return None
    deadband = THRESHOLDS["acceleration_deadband_bp"]
    if acceleration_bp >= deadband:
        return "increasing"
    if acceleration_bp <= -deadband:
        return "decreasing"
    return "steady"


def _convert(observations: list[tuple[date, Decimal]], kind: str) -> list[tuple[date, int | float]]:
    converted = []
    for day, raw in observations:
        if kind == "oas":
            value: int | float = int(round_half_up(raw * 100, 0))
        else:
            value = json_number(round_half_up(raw, 2), 2)
        converted.append((day, value))
    return dedupe(converted)


def _percent_points_to_bp(level: int | float) -> int:
    return int(round_half_up(Decimal(str(level)) * 100, 0))


def _format_change(result: dict | None, *, native: str) -> dict | None:
    """Express a change in basis points, with the dates that were used."""
    if result is None:
        return None
    if native == "oas":
        change_bp = int(result["change"])
        from_value = int(result["from_value"])
        to_value = int(result["to_value"])
    else:
        change_bp = _percent_points_to_bp(result["to_value"]) - _percent_points_to_bp(
            result["from_value"]
        )
        from_value = result["from_value"]
        to_value = result["to_value"]
    payload = measure(
        change_bp,
        "bp",
        from_date=result["from_date"].isoformat(),
        to_date=result["to_date"].isoformat(),
        from_value=from_value,
        to_value=to_value,
        observations_back=result.get("observations_back"),
    )
    if result.get("calendar_months") is not None:
        payload["calendar_months"] = result["calendar_months"]
        payload["target_date"] = result["target_date"].isoformat()
        payload["note"] = (
            "Compared with the last real print on or before the same calendar "
            "day this many months earlier. No fill-in if that exact day was missing."
        )
    else:
        payload["note"] = (
            "Compared with that many earlier real observations of this series. "
            "Not a calendar-day count, and not a copied-forward value."
        )
    return payload


def _percentile_block(observations: list[tuple[date, int | float]], label: str) -> dict | None:
    if not observations:
        return None
    current_date, current = observations[-1]
    percent = empirical_percentile([value for _, value in observations], current)
    return {
        "value": json_number(round_half_up(Decimal(str(percent)), 1), 1),
        "unit": "percentile",
        "window_label": label,
        "window_start": observations[0][0].isoformat(),
        "window_end": current_date.isoformat(),
        "n_days": len(observations),
        "note": (
            "Share of observations in this window that were at or below the latest value. "
            "The window is the dates and the count shown here. It is not a 5-year window."
            if label == "available_history"
            else "Trailing year: observations after the day one year before as_of. This is a 1-year window, not a 5-year window."
        ),
    }


def _distribution_block(observations: list[tuple[date, int | float]]) -> dict | None:
    if len(observations) < 2:
        return None
    values = [value for _, value in observations]
    raw = distribution(values)
    block = {
        "unit": "bp" if all(isinstance(value, int) for value in values) else "percent",
        "window_label": "available_history",
        "window_start": observations[0][0].isoformat(),
        "window_end": observations[-1][0].isoformat(),
        "n_days": len(observations),
    }
    for key, value in raw.items():
        places = 0 if block["unit"] == "bp" else 2
        block[key] = json_number(round_half_up(Decimal(str(value)), places), places)
    return block


def _series_payload(
    spec: dict,
    observations: list[tuple[date, int | float]],
    today: date,
) -> dict:
    as_of, latest = observations[-1]
    age = business_days_between(as_of, today)
    stale = is_stale(as_of, today, THRESHOLDS["stale_after_business_days"])
    native = "oas" if spec["kind"] == "oas" else "percent"
    level_unit = "bp" if native == "oas" else "percent"
    payload = {
        "id": spec["id"],
        "fred_id": spec.get("fred_id"),
        "name": spec["name"],
        "plain_english": spec["plain_english"],
        "value": latest,
        "unit": level_unit,
        "as_of": as_of.isoformat(),
        "stale": stale,
        "business_days_old": age,
        "n_observations": len(observations),
        "history_start": observations[0][0].isoformat(),
        "change_1d": _format_change(change_n(observations, 1), native=native),
        "change_5d": _format_change(change_n(observations, 5), native=native),
        "change_20d": _format_change(change_n(observations, 20), native=native),
        "change_3m": _format_change(change_months(observations, 3), native=native),
        "percentile_available": _percentile_block(observations, "available_history"),
        "percentile_1y": _percentile_block(trailing_year(observations), "trailing_1y"),
        "distribution_available": _distribution_block(observations) if native == "oas" else None,
    }
    return payload


def _verify_references(bb: list[tuple[date, int | float]], current_bp: int | None) -> list[dict]:
    verified = []
    for ref in REFERENCES:
        year_text, month_text = ref["month"].split("-")
        year, month = int(year_text), int(month_text)
        in_month = [row for row in bb if row[0].year == year and row[0].month == month]
        if in_month:
            fred_date, fred_value = max(in_month, key=lambda row: row[1])
            difference = abs(int(fred_value) - ref["provided_bp"])
            ok = difference <= ref["tolerance_bp"]
            entry = {
                "name": ref["name"],
                "month": ref["month"],
                "provided_value": ref["provided_bp"],
                "provided_unit": "bp",
                "fred_value": int(fred_value),
                "fred_unit": "bp",
                "fred_date": fred_date.isoformat(),
                "verified": ok,
                "tolerance_bp": ref["tolerance_bp"],
                "current_minus_provided_bp": (
                    None if current_bp is None else int(current_bp) - ref["provided_bp"]
                ),
                "note": ref["note"],
            }
            if ok:
                entry["check"] = (
                    f"FRED's highest BB print in {ref['month']} was {int(fred_value)} bp "
                    f"on {fred_date.isoformat()}, which matches the supplied reference."
                )
            else:
                entry["check"] = (
                    f"FRED's highest BB print in {ref['month']} was {int(fred_value)} bp "
                    f"on {fred_date.isoformat()}. The supplied reference was {ref['provided_bp']} bp."
                )
        else:
            entry = {
                "name": ref["name"],
                "month": ref["month"],
                "provided_value": ref["provided_bp"],
                "provided_unit": "bp",
                "fred_value": None,
                "fred_unit": "bp",
                "fred_date": None,
                "verified": False,
                "tolerance_bp": ref["tolerance_bp"],
                "current_minus_provided_bp": (
                    None if current_bp is None else int(current_bp) - ref["provided_bp"]
                ),
                "note": ref["note"],
                "check": f"No BB observations in {ref['month']} were in the FRED sample, so the reference could not be checked.",
            }
        verified.append(entry)
    return verified


def _decomposition_window(
    label: str,
    spread_obs: list[tuple[date, int | float]],
    yield_obs: list[tuple[date, int | float]],
    treasuries: dict[str, list[tuple[date, int | float]]],
    change: dict | None,
) -> dict:
    window = {
        "label": label,
        "from_date": None,
        "to_date": None,
        "yield_change": None,
        "spread_part": None,
        "treasury_part": None,
        "spread_share_of_yield_move": None,
        "dgs5_change": None,
        "dgs10_change": None,
        "dgs30_change": None,
    }
    if change is None:
        window["note"] = "Not enough history."
        return window
    from_date = change["from_date"]
    to_date = change["to_date"]
    window["from_date"] = from_date.isoformat()
    window["to_date"] = to_date.isoformat()
    spread_from = value_on(spread_obs, from_date)
    spread_to = value_on(spread_obs, to_date)
    yield_from = value_on(yield_obs, from_date)
    yield_to = value_on(yield_obs, to_date)
    if None in (spread_from, spread_to, yield_from, yield_to):
        window["note"] = (
            "Spread and yield did not both have a print on these two dates, "
            "so the split was left blank instead of borrowing a nearby day."
        )
        return window
    spread_bp = int(spread_to) - int(spread_from)
    yield_bp = _percent_points_to_bp(yield_to) - _percent_points_to_bp(yield_from)
    treasury_bp = yield_bp - spread_bp
    window["yield_change"] = measure(yield_bp, "bp")
    window["spread_part"] = measure(spread_bp, "bp")
    window["treasury_part"] = measure(treasury_bp, "bp")
    if yield_bp != 0:
        share = Decimal(spread_bp) / Decimal(yield_bp)
        window["spread_share_of_yield_move"] = json_number(round_half_up(share, 3), 3)
    for key in ("dgs5", "dgs10", "dgs30"):
        series = treasuries.get(key) or []
        start = value_on(series, from_date)
        end = value_on(series, to_date)
        if start is None or end is None:
            window[f"{key}_change"] = None
        else:
            delta = _percent_points_to_bp(end) - _percent_points_to_bp(start)
            window[f"{key}_change"] = measure(delta, "bp", from_date=from_date.isoformat(), to_date=to_date.isoformat())
    window["note"] = (
        "Treasury part = all-in yield change minus spread change, both in basis points. "
        "It will not exactly equal the 5-year, 10-year, or 30-year move. Those are shown beside it."
    )
    return window


def _history_rows(series_map: dict[str, list[tuple[date, int | float]]]) -> list[dict]:
    """One row per date that has at least one real print. Missing series stay null."""
    hy = series_map["hy_oas"]
    if not hy:
        return []
    start, end = hy[0][0], hy[-1][0]
    indexes = {
        key: {day: value for day, value in rows}
        for key, rows in series_map.items()
    }
    dates = set()
    for key in ("hy_oas", "bb_oas", "ccc_oas", "ig_oas", "hy_yield", "ccc_bb", "hy_ig"):
        for day in indexes.get(key, {}):
            if start <= day <= end:
                dates.add(day)
    hy_speed = {}
    for index in range(len(hy)):
        if index >= 5:
            hy_speed[hy[index][0]] = hy[index][1] - hy[index - 5][1]
    hy_accel = {}
    for index in range(len(hy)):
        if index >= 10:
            today_speed = hy[index][1] - hy[index - 5][1]
            prior_speed = hy[index - 5][1] - hy[index - 10][1]
            hy_accel[hy[index][0]] = today_speed - prior_speed
    rows = []
    for day in sorted(dates):
        row = {"date": day.isoformat()}
        for key in (
            "hy_oas",
            "bb_oas",
            "ccc_oas",
            "ig_oas",
            "ccc_bb",
            "hy_ig",
            "hy_yield",
            "dgs5",
            "dgs10",
            "dgs30",
        ):
            row[key] = indexes.get(key, {}).get(day)
        row["hy_oas_chg_5d"] = hy_speed.get(day)
        row["hy_oas_accel_5d"] = hy_accel.get(day)
        rows.append(row)
    return rows


def build_credit_feed(
    raw_series: dict[str, list[tuple[date, Decimal]]],
    *,
    today: date,
    source_by_id: dict[str, str],
    generated_at: str | None = None,
) -> dict:
    """Build the credit.json document from already-parsed FRED observations."""
    notes: list[str] = []
    converted: dict[str, list[tuple[date, int | float]]] = {}
    for spec in SERIES_SPEC:
        rows = raw_series.get(spec["id"]) or []
        if not rows:
            if spec.get("optional"):
                notes.append(f"{spec['fred_id']} was unavailable. It is context only, so the feed continues without it.")
                converted[spec["id"]] = []
                continue
            raise ValueError(f"Missing required series {spec['id']}")
        converted[spec["id"]] = _convert(rows, spec["kind"])

    for required in REQUIRED_IDS:
        if len(converted[required]) < 30:
            raise ValueError(f"{required} has too few observations to publish a feed")

    converted["ccc_bb"] = align_difference(converted["ccc_oas"], converted["bb_oas"])
    converted["hy_ig"] = align_difference(converted["hy_oas"], converted["ig_oas"])

    series_out = {}
    for spec in SERIES_SPEC:
        rows = converted[spec["id"]]
        if not rows:
            series_out[spec["id"]] = {
                "id": spec["id"],
                "fred_id": spec.get("fred_id"),
                "name": spec["name"],
                "plain_english": spec["plain_english"],
                "value": None,
                "unit": "bp" if spec["kind"] == "oas" else "percent",
                "as_of": None,
                "stale": None,
                "available": False,
            }
            continue
        series_out[spec["id"]] = _series_payload(spec, rows, today)

    derived_specs = (
        {
            "id": "ccc_bb",
            "fred_id": None,
            "name": "CCC minus BB",
            "plain_english": "How much wider the weakest junk is than BB. A rising gap means stress is concentrated in CCC.",
            "kind": "oas",
        },
        {
            "id": "hy_ig",
            "fred_id": None,
            "name": "High yield minus investment grade",
            "plain_english": "How much wider junk is than investment grade. This is the contagion gap.",
            "kind": "oas",
        },
    )
    for spec in derived_specs:
        series_out[spec["id"]] = _series_payload(spec, converted[spec["id"]], today)
        series_out[spec["id"]]["derived_from"] = (
            ["ccc_oas", "bb_oas"] if spec["id"] == "ccc_bb" else ["hy_oas", "ig_oas"]
        )
        series_out[spec["id"]]["construction"] = (
            "Subtracted only on dates where both legs have a real print. "
            "A missing leg stays blank. It is not filled from the previous day."
        )

    def chg(series_id: str, steps: int) -> int | None:
        return _bp_change(change_n(converted[series_id], steps))

    bb_level = series_out["bb_oas"]["value"]
    bb_pct_block = series_out["bb_oas"]["percentile_available"]
    bb_pct = None if bb_pct_block is None else bb_pct_block["value"]
    regime = classify_regime(
        bb_5=chg("bb_oas", 5),
        bb_20=chg("bb_oas", 20),
        bb_level=None if bb_level is None else int(bb_level),
        bb_percentile=None if bb_pct is None else float(bb_pct),
        ccc_5=chg("ccc_oas", 5),
        ccc_20=chg("ccc_oas", 20),
        gap_5=chg("ccc_bb", 5),
        gap_20=chg("ccc_bb", 20),
        ig_5=chg("ig_oas", 5),
        ig_20=chg("ig_oas", 20),
        hy_ig_5=chg("hy_ig", 5),
        hy_ig_20=chg("hy_ig", 20),
    )

    regime_public = {
        "label": regime["label"],
        "plain_english": regime["plain_english"],
        "as_of": series_out["hy_oas"]["as_of"],
        "flags": regime["flags"],
        "inputs": {
            "bb_change_5d": series_out["bb_oas"]["change_5d"],
            "bb_change_20d": series_out["bb_oas"]["change_20d"],
            "bb_level": measure(series_out["bb_oas"]["value"], "bp", as_of=series_out["bb_oas"]["as_of"]),
            "bb_percentile_available": series_out["bb_oas"]["percentile_available"],
            "ccc_change_5d": series_out["ccc_oas"]["change_5d"],
            "ccc_change_20d": series_out["ccc_oas"]["change_20d"],
            "ccc_bb_change_5d": series_out["ccc_bb"]["change_5d"],
            "ccc_bb_change_20d": series_out["ccc_bb"]["change_20d"],
            "ig_change_5d": series_out["ig_oas"]["change_5d"],
            "ig_change_20d": series_out["ig_oas"]["change_20d"],
            "hy_ig_change_5d": series_out["hy_ig"]["change_5d"],
            "hy_ig_change_20d": series_out["hy_ig"]["change_20d"],
            "hy_ig_level": measure(series_out["hy_ig"]["value"], "bp", as_of=series_out["hy_ig"]["as_of"]),
        },
        "thresholds": THRESHOLDS,
        "rules": REGIME_RULES,
    }

    current_speed, prior_speed = speed_pair(converted["hy_oas"], 5)
    prior_session = previous_speed(converted["hy_oas"], 5)
    accel_value = None
    if current_speed is not None and prior_speed is not None:
        accel_value = int(current_speed["change"] - prior_speed["change"])
    one_day_speed_change = None
    if current_speed is not None and prior_session is not None:
        one_day_speed_change = int(current_speed["change"] - prior_session["change"])
    direction = acceleration_direction(accel_value)
    if direction == "increasing":
        speed_sentence = "The 5-day pace is faster than it was 5 trading observations ago."
    elif direction == "decreasing":
        speed_sentence = "The 5-day pace is slower than it was 5 trading observations ago."
    elif direction == "steady":
        speed_sentence = "The 5-day pace is about the same as it was 5 trading observations ago."
    else:
        speed_sentence = "There is not enough history to measure acceleration."
    credit_speed = {
        "definition": (
            "Today's 5-observation change in the high-yield spread, using only real "
            "FRED dates. Acceleration is that number minus the same 5-observation "
            "change from 5 observations earlier. Increasing means the pace itself picked up."
        ),
        "value": None if current_speed is None else int(current_speed["change"]),
        "unit": "bp",
        "from_date": None if current_speed is None else current_speed["from_date"].isoformat(),
        "to_date": None if current_speed is None else current_speed["to_date"].isoformat(),
        "prior_5d": None
        if prior_speed is None
        else measure(
            int(prior_speed["change"]),
            "bp",
            from_date=prior_speed["from_date"].isoformat(),
            to_date=prior_speed["to_date"].isoformat(),
        ),
        "acceleration": measure(
            accel_value,
            "bp",
            direction=direction,
            deadband_bp=THRESHOLDS["acceleration_deadband_bp"],
            rule=(
                "increasing when acceleration is +5 bp or more, decreasing when it is "
                "-5 bp or less, otherwise steady."
            ),
        ),
        "change_in_speed_1d": None
        if one_day_speed_change is None
        else measure(
            one_day_speed_change,
            "bp",
            note=(
                "Today's 5-day change minus yesterday's 5-day change. "
                "This is a one-step tick. The direction word uses acceleration versus 5 observations ago, not this tick."
            ),
        ),
        "plain_english": speed_sentence,
    }

    spread_changes = {
        "5d": change_n(converted["hy_oas"], 5),
        "20d": change_n(converted["hy_oas"], 20),
        "3m": change_months(converted["hy_oas"], 3),
    }
    labels = {
        "5d": "5 trading observations",
        "20d": "20 trading observations",
        "3m": "3 calendar months",
    }
    decomposition = {
        "note": (
            "Each window splits the change in the high-yield all-in yield into the "
            "spread part and the Treasury-rate part. Treasury part = yield change minus "
            "spread change. Dates are the spread series' own dates. If the yield has no "
            "print on one of those dates, that window is left blank."
        ),
        "windows": {
            key: _decomposition_window(
                labels[key],
                converted["hy_oas"],
                converted["hy_yield"],
                converted,
                spread_changes[key],
            )
            for key in ("5d", "20d", "3m")
        },
    }

    references = _verify_references(converted["bb_oas"], None if bb_level is None else int(bb_level))
    for ref in references:
        if not ref["verified"]:
            notes.append(ref["check"])

    sources = sorted({source_by_id.get(spec["id"], "unknown") for spec in SERIES_SPEC if spec["id"] in source_by_id})
    stale_series = [
        series_id
        for series_id, block in series_out.items()
        if block.get("stale") is True
    ]
    if stale_series:
        notes.append(
            "Stale series (latest print older than 2 business days): " + ", ".join(stale_series) + "."
        )

    now = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "feed": "credit",
        "schema_version": 1,
        "generated_at": now,
        "run_date": today.isoformat(),
        "run_timezone": "America/New_York",
        "description": (
            "Credit monitor rebuilt from FRED. Each value keeps the observation date "
            "FRED published. Nothing is copied forward onto a newer date."
        ),
        "fred_source": sources[0] if len(sources) == 1 else sources,
        "methodology": {
            "no_forward_fill": True,
            "stale_after_business_days": THRESHOLDS["stale_after_business_days"],
            "stale_rule": (
                "stale is true when the latest real observation is more than 2 US equity "
                "business days older than the New York run date. Age of 2 is not stale."
            ),
            "business_day_calendar": "Weekdays minus the regular NYSE holiday schedule. One-off closures are not listed.",
            "percentile": (
                "Empirical percentile: 100 times the share of observations in the window "
                "that are less than or equal to the latest value. The window label, start "
                "date, end date, and n_days are the whole description. Do not call it 5-year."
            ),
            "changes": (
                "1-day, 5-day, and 20-day changes step back that many real observations. "
                "The 3-month change uses the last real print on or before the calendar day "
                "three months earlier."
            ),
            "yield_split": (
                "Treasury part of the high-yield yield change equals the yield change in "
                "basis points minus the OAS change in basis points."
            ),
            "regime_thresholds": THRESHOLDS,
            "regime_rules": REGIME_RULES,
            "references": [dict(ref) for ref in REFERENCES],
            "units": {
                "bp": "Basis points. 100 bp = 1 percentage point. Spread levels and all spread and yield changes use bp.",
                "percent": "Percent, already in percentage points. 8.16 means 8.16 percent, not 816 bp.",
                "percentile": "A number from 0 to 100. 100 means the latest value is the high of the window.",
            },
        },
        "frontend_notes": [
            "Every measurement has a numeric value and a separate unit field. Display the number once and the unit once. Do not append 'bp' or 'bps' if you already included the unit.",
            "Draw distribution markers from the numeric value. Passing a string such as '968 bps' makes the marker math fail and the dot sits on the minimum.",
            "null means FRED had no print that day. Do not copy the previous day's number into that cell.",
        ],
        "regime": regime_public,
        "credit_speed": credit_speed,
        "yield_decomposition": decomposition,
        "references": {"bb_oas": references},
        "series": series_out,
        "history": _history_rows(converted),
        "notes": notes,
    }


def collect_credit(api_key: str | None, today: date | None = None) -> dict:
    """Download FRED and build the feed."""
    if today is None:
        today = datetime.now(ZoneInfo("America/New_York")).date()
    raw: dict[str, list[tuple[date, Decimal]]] = {}
    sources: dict[str, str] = {}
    errors: list[str] = []
    for spec in SERIES_SPEC:
        try:
            rows, source = load_series(spec["fred_id"], api_key)
            raw[spec["id"]] = rows
            sources[spec["id"]] = source
        except Exception as exc:  # noqa: BLE001 - one series must not abort the others before we decide
            if spec.get("optional"):
                raw[spec["id"]] = []
                sources[spec["id"]] = "unavailable"
                continue
            errors.append(f"{spec['fred_id']}: {exc}")
    if errors:
        raise RuntimeError("FRED download failed: " + "; ".join(errors))
    return build_credit_feed(raw, today=today, source_by_id=sources)
