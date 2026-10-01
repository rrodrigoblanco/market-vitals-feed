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
from pathlib import Path
from zoneinfo import ZoneInfo

from feeds.credit import SERIES_SPEC, build_credit_feed
from feeds.eia import load_inventories
from feeds.fred import load_series
from feeds.freshness import publish_reason, should_publish
from feeds.oil import FRED_OIL, collect_oil
from feeds.serialize import FORBIDDEN_OUTPUTS, write_if_changed
from feeds.validate import validate_credit, validate_oil

PROTECTED = (
    "macro.json",
    "yields.json",
    "durability.json",
    "gpu_waterfall.json",
)

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
    for series_id in credit_ids + oil_ids:
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
            publish_credit = should_publish(existing_credit, credit_dates)
            if publish_credit:
                print(f"credit.json will update. {publish_reason(existing_credit, credit_dates)}.")
            else:
                print(f"credit.json unchanged. Newest observation is still {_newest(credit_dates)}.")

    if only in (None, "oil"):
        missing = [series_id for series_id in oil_ids if series_id not in oil_dates]
        if missing:
            detail = "; ".join(errors.get(series_id, series_id) for series_id in missing)
            failures.append(f"oil: could not read the latest FRED date ({detail})")
        else:
            publish_oil = should_publish(existing_oil, oil_dates)
            if publish_oil:
                print(f"oil.json will update. {publish_reason(existing_oil, oil_dates)}.")
            else:
                print(f"oil.json unchanged. Newest observation is still {_newest(oil_dates)}.")

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

    if not publish_credit and not publish_oil:
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
