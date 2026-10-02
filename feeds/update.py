"""Build credit.json and oil.json. Protected feeds are never opened for writing.

A run writes a file only when an official observation date is newer than the
date already stored, or when the plain-English summary is missing. Checking
that date uses a short FRED window. The full history, and Yahoo's oil curve,
are downloaded only when a file is actually going to be replaced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from feeds.credit import SERIES_SPEC, build_credit_feed
from feeds.eia import load_inventories
from feeds.fred import load_series
from feeds.freshness import STORY_VERSION, publish_reason, should_publish
from feeds.health import CORE_DAILY, WEEKLY, build_status
from feeds.legacy import (
    BREADTH_NAMES,
    breadth_by_date,
    build_macro_document,
    build_yield_rows,
    dispersion_block,
    fetch_equity_closes,
    stamp,
    yields_document,
)
from feeds.oil import FRED_OIL, collect_oil
from feeds.plain import long_date
from feeds.serialize import FORBIDDEN_OUTPUTS, round_half_up, write_if_changed
from feeds.stats import dedupe
from feeds.tape import what_the_tape_is_saying
from feeds.validate import validate_credit, validate_macro, validate_oil, validate_status, validate_tape, validate_yields

PROTECTED = (
    "durability.json",
    "gpu_waterfall.json",
)

TAPE_SERIES = (
    "DGS10",
    "DGS30",
    "T5YIE",
    "DFII10",
    "DTWEXBGS",
    "VIXCLS",
    "WALCL",
    "WRESBAL",
    "RRPONTSYD",
)
LEGACY_SERIES = ("BAMLH0A0HYM2", "BAMLC0A0CM", "BAMLH0A1HYBB", "BAMLH0A3HYC", "VIXCLS", "VXVCLS")

# Long enough to see a holiday week plus FRED's usual lag, short enough that
# a no-op run does not download decades of history.
PROBE_DAYS = 45


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _guard(root: Path) -> dict[str, str | None]:
    for name in FORBIDDEN_OUTPUTS:
        if name not in PROTECTED:
            raise RuntimeError(f"protected list drifted for {name}")
    return {name: _sha256(root / name) for name in PROTECTED}


def _read_feed(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _stored_dates(feed: dict | None) -> dict[str, str]:
    if not isinstance(feed, dict):
        return {}
    stored = feed.get("official_as_of")
    if not isinstance(stored, dict):
        return {}
    return {key: value for key, value in stored.items() if isinstance(key, str) and isinstance(value, str)}


def _probe_date(series_id: str, api_key: str | None, start: date, stored: str | None) -> tuple[str | None, str | None]:
    """Return (latest YYYY-MM-DD, error). An empty recent window is not an error."""
    try:
        rows, _source = load_series(series_id, api_key, observation_start=start)
    except ValueError as exc:
        if "no observations" in str(exc) and stored and stored >= start.isoformat():
            return stored, None
        return None, f"{series_id}: {exc}"
    except Exception as exc:  # noqa: BLE001 - one series must not hide the others
        return None, f"{series_id}: {exc}"
    if not rows:
        return None, f"{series_id}: empty"
    return rows[-1][0].isoformat(), None


def _basis_points(rows: list[tuple[date, Decimal]]) -> list[tuple[date, int]]:
    converted = []
    for day, value in dedupe([(day, value) for day, value in rows]):
        converted.append((day, int((Decimal(str(value)) * 100).quantize(Decimal("1")))))
    return converted


def _float_rows(rows: list[tuple[date, Decimal]], places: int) -> list[tuple[date, float]]:
    return dedupe([(day, float(round_half_up(value, places))) for day, value in rows])


def _measure_value(window: dict, key: str) -> int | float | None:
    block = window.get(key)
    if not isinstance(block, dict):
        return None
    value = block.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _tape_text(credit: dict, oil: dict, full_series) -> str:
    series = credit.get("series") or {}
    hy = series.get("hy_oas") or {}
    window = (((credit.get("yield_decomposition") or {}).get("windows") or {}).get("5d") or {})
    brent = oil.get("brent") or {}

    def rows(series_id: str, places: int) -> list[tuple[date, float]]:
        try:
            return _float_rows(full_series(series_id)[0], places)
        except Exception:  # noqa: BLE001
            return []

    return what_the_tape_is_saying(
        hy_bp=hy.get("value"),
        hy_as_of=hy.get("as_of"),
        hy_5d_bp=_measure_value(hy, "change_5d"),
        ig_5d_bp=_measure_value(series.get("ig_oas") or {}, "change_5d"),
        ccc_bb_bp=(series.get("ccc_bb") or {}).get("value"),
        yield_5d_bp=_measure_value(window, "yield_change"),
        spread_5d_bp=_measure_value(window, "spread_part"),
        treasury_5d_bp=_measure_value(window, "treasury_part"),
        dgs10=rows("DGS10", 2),
        dgs30=rows("DGS30", 2),
        real_yield=rows("DFII10", 2),
        breakeven=rows("T5YIE", 2),
        dollar=rows("DTWEXBGS", 2),
        vix=rows("VIXCLS", 2),
        balance_sheet=rows("WALCL", 0),
        reserves=rows("WRESBAL", 0),
        rrp=rows("RRPONTSYD", 3),
        brent=brent.get("value"),
        brent_as_of=brent.get("as_of"),
        brent_5d_pct=(brent.get("change_5d") or {}).get("pct")
        if isinstance((brent.get("change_5d") or {}).get("pct"), (int, float))
        else None,
        curve=((oil.get("curve") or {}).get("brent") or {}).get("shape"),
    )


def _newest(dates: dict[str, str]) -> str:
    if not dates:
        return "none"
    return max(dates.values())


def build(root: Path, *, only: str | None = None) -> int:
    """Write the feeds that have a newer official date. Returns 0 on a clean run."""
    root = root.resolve()
    before = _guard(root)
    fred_key = os.environ.get("FRED_API_KEY") or None
    eia_key = os.environ.get("EIA_API_KEY") or None
    if fred_key == "":
        fred_key = None
    if eia_key == "":
        eia_key = None

    today = datetime.now(ZoneInfo("America/New_York")).date()
    window_start = today - timedelta(days=PROBE_DAYS)
    existing_credit = _read_feed(root / "credit.json")
    existing_oil = _read_feed(root / "oil.json")
    stored_credit = _stored_dates(existing_credit)
    stored_oil = _stored_dates(existing_oil)

    credit_ids = [spec["fred_id"] for spec in SERIES_SPEC]
    optional_ids = {spec["fred_id"] for spec in SERIES_SPEC if spec.get("optional")}
    oil_ids = [spec["fred_id"] for spec in FRED_OIL.values()]
    wanted: list[str] = []
    for series_id in list(credit_ids) + list(oil_ids) + list(LEGACY_SERIES) + list(TAPE_SERIES):
        if series_id not in wanted:
            wanted.append(series_id)

    probed: dict[str, str] = {}
    errors: dict[str, str] = {}
    for series_id in wanted:
        stored = stored_credit.get(series_id) or stored_oil.get(series_id)
        latest, error = _probe_date(series_id, fred_key, window_start, stored)
        if latest:
            probed[series_id] = latest
        if error:
            errors[series_id] = error

    failures: list[str] = []
    credit_dates = {series_id: probed[series_id] for series_id in credit_ids if series_id in probed}
    oil_dates = {series_id: probed[series_id] for series_id in oil_ids if series_id in probed}

    inventories = None
    inventory_notes: list[str] = []
    if only in (None, "oil"):
        try:
            inventories, inventory_notes = load_inventories(eia_key)
            if inventories.get("available") and inventories.get("as_of"):
                oil_dates["EIA_COMMERCIAL_CRUDE"] = inventories["as_of"]
            else:
                inventories = None
                print("Oil inventories were not available. The stored weekly number is kept.")
        except Exception as exc:  # noqa: BLE001
            inventories = None
            print(f"Oil inventories could not be checked ({exc}). The stored weekly number is kept.")

    publish_credit = False
    publish_oil = False

    if only in (None, "credit"):
        missing = [
            series_id
            for series_id in credit_ids
            if series_id not in optional_ids and series_id not in credit_dates
        ]
        if missing:
            detail = "; ".join(errors.get(series_id, series_id) for series_id in missing)
            failures.append(f"credit: could not read the latest FRED date ({detail})")
        else:
            publish_credit = should_publish(existing_credit, credit_dates, story_version=STORY_VERSION)
            if publish_credit:
                print(f"credit.json will update. {publish_reason(existing_credit, credit_dates, story_version=STORY_VERSION)}.")
            else:
                print(f"credit.json unchanged. Newest observation is still {_newest(credit_dates)}.")

    if only in (None, "oil"):
        missing = [series_id for series_id in oil_ids if series_id not in oil_dates]
        if missing:
            detail = "; ".join(errors.get(series_id, series_id) for series_id in missing)
            failures.append(f"oil: could not read the latest FRED date ({detail})")
        else:
            publish_oil = should_publish(existing_oil, oil_dates, story_version=STORY_VERSION)
            if publish_oil:
                print(f"oil.json will update. {publish_reason(existing_oil, oil_dates, story_version=STORY_VERSION)}.")
            else:
                print(f"oil.json unchanged. Newest observation is still {_newest(oil_dates)}.")

    credit_payload = None
    oil_payload = None
    cache: dict[str, list] = {}

    def full_series(series_id: str):
        if series_id not in cache:
            rows, source = load_series(series_id, fred_key)
            cache[series_id] = (rows, source)
        return cache[series_id]

    if publish_credit:
        try:
            raw = {}
            sources = {}
            download_errors: list[str] = []
            for spec in SERIES_SPEC:
                try:
                    rows, source = full_series(spec["fred_id"])
                    raw[spec["id"]] = rows
                    sources[spec["id"]] = source
                except Exception as exc:  # noqa: BLE001
                    if spec.get("optional"):
                        raw[spec["id"]] = []
                        sources[spec["id"]] = "unavailable"
                        continue
                    download_errors.append(f"{spec['fred_id']}: {exc}")
            if download_errors:
                raise RuntimeError("FRED download failed: " + "; ".join(download_errors))
            payload = build_credit_feed(raw, today=today, source_by_id=sources)
            credit_payload = payload
            validate_credit(payload)
            changed = write_if_changed(root / "credit.json", payload)
            hy = payload["series"]["hy_oas"]
            print(
                f"credit.json {'wrote' if changed else 'unchanged'} "
                f"regime={payload['regime']['label']} hy={hy['value']} {hy['unit']} "
                f"as_of={hy['as_of']} stale={hy['stale']}"
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"credit: {exc}")
            traceback.print_exc()

    if publish_oil:
        try:
            preloaded = {}
            for spec in FRED_OIL.values():
                rows, _source = full_series(spec["fred_id"])
                preloaded[spec["fred_id"]] = rows
            payload = collect_oil(
                fred_api_key=fred_key,
                eia_api_key=eia_key,
                today=today,
                preloaded_fred=preloaded,
                preloaded_inventories=inventories,
                inventory_notes=inventory_notes,
            )
            previous_stocks = (existing_oil or {}).get("inventories") or {}
            if not payload["inventories"].get("available") and previous_stocks.get("available"):
                payload["inventories"] = previous_stocks
                if previous_stocks.get("as_of"):
                    payload["official_as_of"]["EIA_COMMERCIAL_CRUDE"] = previous_stocks["as_of"]
            validate_oil(payload)
            oil_payload = payload
            changed = write_if_changed(root / "oil.json", payload)
            brent = payload["brent"]
            print(
                f"oil.json {'wrote' if changed else 'unchanged'} "
                f"brent={brent['value']} {brent['unit']} as_of={brent['as_of']} "
                f"alert={payload['alert']['flag']}"
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"oil: {exc}")
            traceback.print_exc()

    publish_bundle = False
    if only is None:
        existing_yields = _read_feed(root / "yields.json")
        existing_macro = _read_feed(root / "macro.json")
        existing_tape = _read_feed(root / "tape.json")
        legacy_dates = {series_id: probed[series_id] for series_id in LEGACY_SERIES if series_id in probed}
        tape_dates = {series_id: probed[series_id] for series_id in TAPE_SERIES if series_id in probed}
        missing_legacy = [series_id for series_id in LEGACY_SERIES if series_id not in legacy_dates]
        if missing_legacy:
            detail = "; ".join(errors.get(series_id, series_id) for series_id in missing_legacy)
            failures.append(f"yields: could not read the latest FRED date ({detail})")
            publish_bundle = False
        else:
            publish_bundle = (
                should_publish(existing_yields, legacy_dates, story_version=STORY_VERSION)
                or should_publish(existing_macro, legacy_dates, story_version=STORY_VERSION)
                or should_publish(existing_tape, tape_dates, story_version=STORY_VERSION)
            )
            if publish_bundle:
                print("yields.json, macro.json, and tape.json will update.")
            else:
                print("yields.json, macro.json, and tape.json unchanged.")
        if publish_bundle and not any(item.startswith("yields:") for item in failures):
            try:
                closes = fetch_equity_closes(["QQQ", "SMH", *BREADTH_NAMES])
                breadth = breadth_by_date({name: closes[name] for name in BREADTH_NAMES if name in closes})
                hy_bp = _basis_points(full_series("BAMLH0A0HYM2")[0])
                ig_bp = _basis_points(full_series("BAMLC0A0CM")[0])
                bb_bp = _basis_points(full_series("BAMLH0A1HYBB")[0])
                ccc_bp = _basis_points(full_series("BAMLH0A3HYC")[0])
                vix_rows = _float_rows(full_series("VIXCLS")[0], 2)
                vix3m_rows = _float_rows(full_series("VXVCLS")[0], 2)
                rows = build_yield_rows(
                    hy_bp=hy_bp,
                    ig_bp=ig_bp,
                    vix=vix_rows,
                    vix3m=vix3m_rows,
                    qqq=closes["QQQ"],
                    smh=closes["SMH"],
                    breadth=breadth,
                )
                if len(rows) < 20:
                    raise RuntimeError(f"only {len(rows)} complete yield rows, so the old file was kept")
                updated = stamp()
                latest = rows[-1]
                yields_summary = (
                    f"As of {long_date(latest['date'])}, junk-bond spreads are {latest['HY_OAS'] / 100:.2f} percentage points, "
                    f"{latest['dHY_5d'] / 100:+.2f} percentage points over five trading days, and the junk-versus-safe "
                    f"gap is {latest['HY_IG'] / 100:.2f} percentage points. VIX is {latest['VIX']:.2f} and the "
                    f"3-month VIX is {latest['VIX3M']:.2f}, so near-term fear is "
                    f"{'below' if latest['VIX_slope'] < 0 else 'above'} the 3-month reading. "
                    f"The Nasdaq-100 is {latest['QQQ_DD'] * 100:.1f} percent from its trailing high, "
                    f"semiconductors are {latest['SMH_200d'] * 100:+.1f} percent versus their 200-day average, "
                    f"and {latest['Breadth'] * 100:.0f} percent of the 30-name list is above its own 200-day average."
                )
                credit_for_tape = credit_payload or _read_feed(root / "credit.json") or {}
                oil_for_tape = oil_payload or _read_feed(root / "oil.json") or {}
                tape_text = _tape_text(credit_for_tape, oil_for_tape, full_series)
                regime_text = ((credit_for_tape.get("regime") or {}).get("plain_english")) or latest["REGIME"]
                dispersion = dispersion_block(
                    bb=bb_bp,
                    ccc=ccc_bp,
                    hy=hy_bp,
                    interpretation=regime_text,
                )
                yields_payload = yields_document(
                    rows,
                    updated_at=updated,
                    summary=yields_summary,
                    official_as_of=legacy_dates,
                )
                macro_payload = build_macro_document(
                    rows,
                    dispersion,
                    updated_at=updated,
                    tape=tape_text,
                    summary=yields_summary,
                    official_as_of=legacy_dates,
                )
                tape_payload = {
                    "feed": "tape",
                    "story_version": STORY_VERSION,
                    "generated_at": updated,
                    "summary": tape_text,
                    "what_the_tape_is_saying": tape_text,
                    "what_would_change_this": (
                        "This read would turn into a market-wide stress story if junk spreads kept widening "
                        "and investment-grade spreads widened with them, or if oil rose more than 5 percent "
                        "in five trading days while the 30-year yield and the 5-year breakeven rose too."
                    ),
                    "official_as_of": tape_dates,
                }
                validate_yields(yields_payload)
                validate_macro(macro_payload)
                validate_tape(tape_payload)
                wrote_yields = write_if_changed(root / "yields.json", yields_payload)
                wrote_macro = write_if_changed(root / "macro.json", macro_payload)
                wrote_tape = write_if_changed(root / "tape.json", tape_payload)
                action = "wrote" if wrote_yields or wrote_macro or wrote_tape else "unchanged"
                print(
                    f"yields.json {action}: {len(rows)} rows through {latest['date']} "
                    f"HY {latest['HY_OAS']} regime {latest['REGIME']}"
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"yields: {exc}")
                traceback.print_exc()

    status = build_status(
        today=today,
        daily_as_of={series_id: probed.get(series_id) for series_id in CORE_DAILY},
        weekly_as_of={series_id: probed.get(series_id) for series_id in WEEKLY},
        feed_errors=failures,
    )
    try:
        validate_status(status)
        write_if_changed(root / "status.json", status)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not write status.json: {exc}", file=sys.stderr)
        return 1

    after = _guard(root)
    if after != before:
        drifted = [name for name in PROTECTED if after[name] != before[name]]
        print("ERROR: protected files changed: " + ", ".join(drifted), file=sys.stderr)
        return 1

    if failures:
        print("FEED FAILURES:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        print("Previous copies of the failed files were left in place.", file=sys.stderr)
        return 1

    if status["fail_job"]:
        print(
            f"STATUS {status['status']}. Stale: {', '.join(status['stale_series']) or 'none'}.",
            file=sys.stderr,
        )
        return 1

    if not publish_credit and not publish_oil and not publish_bundle:
        print("No newer observation date. Nothing to commit.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update credit.json and oil.json")
    parser.add_argument(
        "--only",
        choices=("credit", "oil"),
        help="Build one feed. The default builds both.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for the JSON files. Defaults to the repository root.",
    )
    args = parser.parse_args(argv)
    root = args.output_dir
    if root is None:
        root = Path(__file__).resolve().parents[1]
    return build(root, only=args.only)


if __name__ == "__main__":
    raise SystemExit(main())
