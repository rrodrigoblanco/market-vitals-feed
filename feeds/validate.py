"""Checks that run before a new JSON file is allowed to replace the old one."""

from __future__ import annotations

REGIME_LABELS = {"SYSTEMIC", "BROADENING", "CONCENTRATED", "CALM", "UNKNOWN"}
CURVE_SHAPES = {"backwardation", "contango", "flat"}
# These words are fine inside the regime block. They are not fine as the
# only explanation a non-expert would see in the summary sentences.
PLAIN_SHORTHAND = ("SYSTEMIC", "BROADENING", "CONCENTRATED", "CALM", "UNKNOWN")


def _require_plain_english(feed: dict, name: str) -> None:
    summary = feed.get("summary")
    change = feed.get("what_would_change_this")
    if not isinstance(summary, str) or not summary.endswith(".") or len(summary) < 80:
        raise ValueError(f"{name} summary must be a full plain-English paragraph")
    if not isinstance(change, str) or not change.endswith(".") or len(change) < 40:
        raise ValueError(f"{name} what_would_change_this must be one full sentence")
    for word in PLAIN_SHORTHAND:
        if word in summary or word in change:
            raise ValueError(f"{name} plain-English text uses the shorthand {word}")
    official = feed.get("official_as_of")
    if not isinstance(official, dict) or not official:
        raise ValueError(f"{name} official_as_of is missing")
    for key, day in official.items():
        if not isinstance(key, str) or not isinstance(day, str) or len(day) < 10:
            raise ValueError(f"{name} official_as_of has a bad entry")


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
    _require_plain_english(feed, "credit")
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


YIELD_ROW_FIELDS = (
    "date",
    "HY_OAS",
    "IG_OAS",
    "HY_IG",
    "dHY_5d",
    "VIX",
    "VIX3M",
    "VIX_slope",
    "QQQ_DD",
    "SMH_200d",
    "Breadth",
    "REGIME",
)
YIELD_REGIMES = {"RISK-ON", "CAUTION", "GROWTH SCARE", "RISK-OFF"}
MACRO_SIGNALS = (
    "Credit (HY OAS)",
    "Credit speed (5-day)",
    "Contagion (HY-IG)",
    "Volatility shape",
    "Nasdaq drawdown",
    "Breadth",
    "Semis vs trend",
)


def validate_yields(feed: dict) -> None:
    if not isinstance(feed.get("updatedAt"), str) or not feed["updatedAt"]:
        raise ValueError("yields.updatedAt is missing")
    rows = feed.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("yields.rows is missing")
    if feed.get("days") != len(rows):
        raise ValueError("yields.days must equal the number of rows")
    previous = ""
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("yields row is not an object")
        for field in YIELD_ROW_FIELDS:
            if field not in row:
                raise ValueError(f"yields row is missing {field}")
        if not isinstance(row["date"], str) or row["date"] <= previous:
            raise ValueError("yields dates must be unique and increasing")
        previous = row["date"]
        for field in YIELD_ROW_FIELDS:
            if field in {"date", "REGIME"}:
                continue
            if isinstance(row[field], bool) or not isinstance(row[field], (int, float)):
                raise ValueError(f"yields {field} must be a number")
        if row["REGIME"] not in YIELD_REGIMES:
            raise ValueError(f"yields regime {row['REGIME']} is not one the site colors")
        if int(row["HY_IG"]) != int(row["HY_OAS"]) - int(row["IG_OAS"]):
            raise ValueError("yields HY_IG is not HY minus IG")
        slope = row["VIX"] - row["VIX3M"]
        if abs(slope - row["VIX_slope"]) > 0.02:
            raise ValueError("yields VIX_slope is not VIX minus VIX3M")
    _require_plain_english(feed, "yields")


def validate_macro(feed: dict) -> None:
    macro = feed.get("macro")
    if not isinstance(macro, dict):
        raise ValueError("macro wrapper is missing")
    for key in ("updatedAt", "regime", "risk_lr", "headline", "advice", "signals", "raw", "alerts", "dispersion"):
        if key not in macro:
            raise ValueError(f"macro.{key} is missing")
    if macro["regime"] not in YIELD_REGIMES:
        raise ValueError("macro regime is not one the site colors")
    if not isinstance(macro["risk_lr"], (int, float)):
        raise ValueError("macro.risk_lr must be a number")
    names = [item.get("signal") for item in macro["signals"]]
    if tuple(names) != MACRO_SIGNALS:
        raise ValueError("macro signal names drifted")
    for item in macro["signals"]:
        for key in ("signal", "value", "grade", "reason", "trend"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f"macro signal {item.get('signal')} is missing {key}")
        if item["grade"] not in {"green", "watch", "red", "gray"}:
            raise ValueError("macro grade is not a known word")
    raw = macro["raw"]
    for key in ("hy_oas", "hy_ig", "vix_slope", "qqq_dd", "breadth", "smh_vs_200dma"):
        if isinstance(raw.get(key), bool) or not isinstance(raw.get(key), (int, float)):
            raise ValueError(f"macro.raw.{key} must be a number")
    dispersion = macro["dispersion"]
    for key in ("available", "signal", "value", "grade", "reason", "trend", "calibration", "interpretation", "raw"):
        if key not in dispersion:
            raise ValueError(f"dispersion.{key} is missing")
    if not isinstance(dispersion["value"], str):
        raise ValueError("dispersion.value must stay a string for the existing panel")
    calibration = dispersion["calibration"]
    for key in ("current_bps", "pctile_1y", "pctile_5y", "distribution_5y", "n_days"):
        if key not in calibration:
            raise ValueError(f"dispersion.calibration.{key} is missing")
    for key in ("min", "p10", "p25", "median", "p75", "p90", "max"):
        if key not in calibration["distribution_5y"]:
            raise ValueError(f"distribution_5y.{key} is missing")
    for key in ("bb_oas", "ccc_oas", "ccc_bb", "ccc_bb_5d", "ccc_bb_20d", "bb_20d", "ccc_20d", "hy_20d"):
        if key not in dispersion["raw"]:
            raise ValueError(f"dispersion.raw.{key} is missing")
    if not isinstance(feed.get("what_the_tape_is_saying"), str):
        raise ValueError("macro file is missing what_the_tape_is_saying")


def validate_tape(feed: dict) -> None:
    text = feed.get("what_the_tape_is_saying")
    if not isinstance(text, str) or len(text) < 80 or not text.endswith("."):
        raise ValueError("tape paragraph is missing")
    if not isinstance(feed.get("official_as_of"), dict):
        raise ValueError("tape official_as_of is missing")


def validate_status(feed: dict) -> None:
    if feed.get("status") not in {"ok", "stale", "error"}:
        raise ValueError("status must be ok, stale, or error")
    if not isinstance(feed.get("fail_job"), bool):
        raise ValueError("status.fail_job must be true or false")
    if feed["fail_job"] != (feed["status"] != "ok"):
        raise ValueError("status.fail_job does not match status")
    if not isinstance(feed.get("daily"), dict) or not isinstance(feed.get("errors"), list):
        raise ValueError("status daily series or errors are missing")


def validate_oil(feed: dict) -> None:
    if feed.get("feed") != "oil":
        raise ValueError("oil feed name missing")
    _require_plain_english(feed, "oil")
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
