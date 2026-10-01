"""Checks that run before a new JSON file is allowed to replace the old one."""

from __future__ import annotations

REGIME_LABELS = {"SYSTEMIC", "BROADENING", "CONCENTRATED", "CALM", "UNKNOWN"}
CURVE_SHAPES = {"backwardation", "contango", "flat"}


def _walk_measures(obj: object, path: str = "") -> None:
    if isinstance(obj, dict):
        if "value" in obj and "unit" in obj:
            value = obj["value"]
            if value is not None and not isinstance(value, (int, float)):
                raise ValueError(f"{path}.value must be a number or null, got {type(value).__name__}")
            if not isinstance(obj["unit"], str) or not obj["unit"]:
                raise ValueError(f"{path}.unit must be a non-empty string")
        for key, child in obj.items():
            _walk_measures(child, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, list):
        for index, child in enumerate(obj):
            _walk_measures(child, f"{path}[{index}]")


def _reject_fake_five_year(obj: object) -> None:
    if isinstance(obj, dict):
        label = obj.get("window_label")
        if isinstance(label, str) and "5y" in label.lower():
            raise ValueError(f"window_label {label!r} pretends to be a 5-year window")
        for child in obj.values():
            _reject_fake_five_year(child)
    elif isinstance(obj, list):
        for child in obj:
            _reject_fake_five_year(child)


def _history_matches(feed: dict, column: str) -> None:
    series = feed["series"][column]
    rows = [row for row in feed["history"] if row.get(column) is not None]
    if series.get("value") is None:
        if rows:
            raise ValueError(f"{column} has history but a null latest value")
        return
    if not rows:
        raise ValueError(f"{column} has a value but no history row")
    last = rows[-1]
    if series["as_of"] != last["date"]:
        raise ValueError(f"{column} as_of {series['as_of']} != last history date {last['date']}")
    if series["value"] != last[column]:
        raise ValueError(f"{column} value {series['value']} != history {last[column]}")


def validate_credit(feed: dict) -> None:
    if feed.get("feed") != "credit":
        raise ValueError("credit feed name missing")
    if feed.get("methodology", {}).get("no_forward_fill") is not True:
        raise ValueError("credit feed must set no_forward_fill")
    label = feed.get("regime", {}).get("label")
    if label not in REGIME_LABELS:
        raise ValueError(f"unexpected regime {label}")
    _walk_measures(feed)
    _reject_fake_five_year(feed)
    for column in ("hy_oas", "bb_oas", "ccc_oas", "ig_oas", "hy_yield", "ccc_bb", "hy_ig"):
        _history_matches(feed, column)
    percentile = feed["series"]["hy_oas"].get("percentile_available") or {}
    if "window_start" not in percentile or "n_days" not in percentile:
        raise ValueError("percentile window is missing its start or count")
    if not feed.get("references", {}).get("bb_oas"):
        raise ValueError("BB reference checks are missing")
    speed = feed.get("credit_speed") or {}
    if speed.get("unit") != "bp":
        raise ValueError("credit speed must keep its unit separate")
    windows = (feed.get("yield_decomposition") or {}).get("windows") or {}
    for key in ("5d", "20d", "3m"):
        if key not in windows:
            raise ValueError(f"missing yield decomposition window {key}")


def validate_oil(feed: dict) -> None:
    if feed.get("feed") != "oil":
        raise ValueError("oil feed name missing")
    _walk_measures(feed)
    for name in ("brent", "wti"):
        block = feed.get(name) or {}
        if block.get("value") is None:
            raise ValueError(f"{name} headline is missing")
        if block.get("unit") != "usd_per_bbl":
            raise ValueError(f"{name} unit must be separate from the price")
    alert = feed.get("alert") or {}
    if not isinstance(alert.get("flag"), bool):
        raise ValueError("oil alert flag must be true or false")
    thresholds = (feed.get("methodology") or {}).get("alert") or {}
    if thresholds.get("brent_5d_percent_greater_than") != 5.0:
        raise ValueError("oil alert threshold drifted")
    curve = feed.get("curve") or {}
    if curve.get("available"):
        for leg_name in ("wti", "brent"):
            leg = curve.get(leg_name) or {}
            if leg.get("available") and leg.get("shape") not in CURVE_SHAPES:
                raise ValueError(f"bad curve shape for {leg_name}")
    inventories = feed.get("inventories") or {}
    if "available" not in inventories:
        raise ValueError("inventories block must say whether it is available")
