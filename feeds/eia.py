"""US crude inventories.

The public Weekly Petroleum Status Report CSV needs no key. EIA's JSON API is
used only when EIA_API_KEY is set, and a failure there falls back to the CSV.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

from feeds.http_client import FetchError, fetch_bytes
from feeds.serialize import round_half_up

WPSR_URL = "https://ir.eia.gov/wpsr/table1.csv"
API_URL = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"


def _number(text: str) -> Decimal:
    return Decimal(text.strip().replace(",", "").replace("%", ""))


def _parse_us_date(text: str):
    text = text.strip().strip('"')
    for pattern in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"Could not read an EIA week date from {text!r}")


def parse_wpsr_table1(text: str) -> dict:
    """Commercial crude stocks, excluding the Strategic Petroleum Reserve.

    The WPSR summary reports stocks in million barrels. The first section of
    table1.csv is the stock table.
    """
    sample = text.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(sample))
    header: list[str] | None = None
    commercial: list[str] | None = None
    spr: list[str] | None = None
    for row in reader:
        if not row or not any(cell.strip() for cell in row):
            if header and commercial:
                break
            continue
        label = row[0].strip()
        if label == "STUB_1":
            if header and commercial:
                break
            header = row
            continue
        if header is None:
            continue
        if label == "Commercial (Excluding SPR)" and commercial is None:
            commercial = row
        elif label == "Strategic Petroleum Reserve (SPR)" and spr is None:
            spr = row
    if not header or not commercial or len(header) < 3 or len(commercial) < 3:
        raise ValueError("EIA weekly file did not contain commercial crude stocks")
    as_of = _parse_us_date(header[1])
    previous_date = _parse_us_date(header[2])
    latest = _number(commercial[1])
    previous = _number(commercial[2])
    if latest > 10000:
        # Defensive: the API sometimes reports thousand barrels.
        latest = latest / Decimal(1000)
        previous = previous / Decimal(1000)
    change = latest - previous
    payload = {
        "available": True,
        "name": "US commercial crude inventories",
        "plain_english": (
            "Crude oil held by companies in the United States, not counting the "
            "Strategic Petroleum Reserve. This is the weekly number traders watch."
        ),
        "value": float(round_half_up(latest, 3)),
        "unit": "million_barrels",
        "as_of": as_of.isoformat(),
        "previous_value": float(round_half_up(previous, 3)),
        "previous_as_of": previous_date.isoformat(),
        "change_1w": {
            "value": float(round_half_up(change, 3)),
            "unit": "million_barrels",
            "from_date": previous_date.isoformat(),
            "to_date": as_of.isoformat(),
        },
        "source": "EIA Weekly Petroleum Status Report table 1 (public file, no key)",
        "source_url": WPSR_URL,
        "includes_spr": False,
    }
    if spr and len(spr) >= 2:
        spr_value = _number(spr[1])
        if spr_value > 10000:
            spr_value = spr_value / Decimal(1000)
        payload["spr"] = {
            "value": float(round_half_up(spr_value, 3)),
            "unit": "million_barrels",
            "as_of": as_of.isoformat(),
            "plain_english": "Strategic Petroleum Reserve. Kept separate from the commercial number.",
        }
    return payload


def load_wpsr() -> dict:
    body = fetch_bytes(WPSR_URL)
    return parse_wpsr_table1(body.decode("utf-8", errors="replace"))


def parse_eia_api(payload: dict) -> dict | None:
    """Pull the newest WCRSTUS1 row out of an EIA v2 response, if one is present."""
    response = payload.get("response") or payload
    rows = response.get("data")
    if not isinstance(rows, list) or not rows:
        return None
    parsed = []
    for row in rows:
        period = row.get("period")
        value = row.get("value")
        if period is None or value is None:
            continue
        units = str(row.get("units") or row.get("unit") or "")
        amount = Decimal(str(value))
        if "thousand" in units.lower() or amount > 10000:
            amount = amount / Decimal(1000)
            unit_note = "Converted from thousand barrels to million barrels."
        else:
            unit_note = "Treated as million barrels."
        parsed.append((str(period), amount, unit_note))
    if not parsed:
        return None
    parsed.sort()
    latest_period, latest_value, unit_note = parsed[-1]
    previous = parsed[-2] if len(parsed) > 1 else None
    result = {
        "available": True,
        "value": float(round_half_up(latest_value, 3)),
        "unit": "million_barrels",
        "as_of": latest_period,
        "source": "EIA API v2 series WCRSTUS1",
        "unit_note": unit_note,
        "change_1w": None,
    }
    if previous:
        prev_period, prev_value, _ = previous
        result["previous_value"] = float(round_half_up(prev_value, 3))
        result["previous_as_of"] = prev_period
        result["change_1w"] = {
            "value": float(round_half_up(latest_value - prev_value, 3)),
            "unit": "million_barrels",
            "from_date": prev_period,
            "to_date": latest_period,
        }
    return result


def load_eia_api(api_key: str) -> dict:
    query = urlencode(
        {
            "api_key": api_key,
            "frequency": "weekly",
            "data[0]": "value",
            "facets[series][]": "WCRSTUS1",
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "length": "8",
        }
    )
    body = fetch_bytes(f"{API_URL}?{query}", redact=api_key)
    payload = json.loads(body.decode("utf-8"))
    parsed = parse_eia_api(payload)
    if parsed is None:
        raise ValueError("EIA API returned no WCRSTUS1 rows")
    return parsed


def load_inventories(api_key: str | None) -> tuple[dict, list[str]]:
    """Prefer a working API response when it is at least as new as the public file."""
    notes: list[str] = []
    public = None
    public_error = None
    try:
        public = load_wpsr()
    except (FetchError, ValueError, OSError) as exc:
        public_error = str(exc)
        notes.append(f"EIA public weekly file could not be read: {exc}")

    api_row = None
    if not api_key:
        notes.append(
            "EIA_API_KEY is not set. Inventories come from EIA's public weekly file, which does not need a key."
        )
    else:
        try:
            api_row = load_eia_api(api_key)
        except (FetchError, ValueError, json.JSONDecodeError, OSError) as exc:
            notes.append(f"EIA API failed ({exc}). The public weekly file is used instead.")

    if api_row and public:
        if api_row["as_of"] >= public["as_of"]:
            api_row["plain_english"] = public["plain_english"]
            api_row["name"] = public["name"]
            api_row["includes_spr"] = False
            if public.get("spr"):
                api_row["spr"] = public["spr"]
            api_row["public_file_as_of"] = public["as_of"]
            api_row["public_file_value"] = public["value"]
            return api_row, notes
        notes.append(
            "EIA API was older than the public weekly file, so the public file is the headline."
        )
        return public, notes
    if api_row:
        return api_row, notes
    if public:
        return public, notes
    return {
        "available": False,
        "value": None,
        "unit": "million_barrels",
        "as_of": None,
        "source": None,
        "note": public_error or "No inventory source returned data.",
    }, notes
