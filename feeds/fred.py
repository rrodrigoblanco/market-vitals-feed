"""Load FRED series from the public CSV endpoint, or the API when a key is set.

Missing prints (the '.' token) are dropped. They are never carried forward
onto a later date.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal

from feeds.http_client import FetchError, fetch_bytes

CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
API_URL = (
    "https://api.stlouisfed.org/fred/series/observations"
    "?series_id={series_id}&api_key={api_key}&file_type=json"
)


def _parse_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%Y-%m-%d").date()


def _is_missing(text: str) -> bool:
    stripped = text.strip()
    return stripped in {"", ".", "nan", "NaN", "null"}


def parse_csv(text: str, series_id: str) -> list[tuple[date, Decimal]]:
    sample = text.lstrip("\ufeff").strip()
    if not sample or sample[:1] in {"<", "{"}:
        raise ValueError(f"FRED did not return a CSV for {series_id}")
    reader = csv.DictReader(io.StringIO(sample))
    if not reader.fieldnames:
        raise ValueError(f"FRED CSV for {series_id} has no header")
    fields = [name.strip() for name in reader.fieldnames]
    date_key = "observation_date" if "observation_date" in fields else "DATE"
    if date_key not in fields:
        raise ValueError(f"FRED CSV for {series_id} has no date column: {fields}")
    value_key = series_id if series_id in fields else next(
        key for key in fields if key != date_key
    )
    rows: dict[date, Decimal] = {}
    for raw in reader:
        # DictReader keys match the original header, which we stripped in `fields`
        # only as a copy. Read with the original names.
        day_text = raw.get(date_key) or raw.get(date_key.strip())
        value_text = raw.get(value_key) or raw.get(value_key.strip())
        if day_text is None or value_text is None:
            continue
        if _is_missing(value_text):
            continue
        rows[_parse_date(day_text)] = Decimal(value_text.strip())
    return sorted(rows.items())


def parse_api(payload: dict, series_id: str) -> list[tuple[date, Decimal]]:
    observations = payload.get("observations")
    if not isinstance(observations, list):
        message = payload.get("error_message") or "response had no observations"
        raise ValueError(f"FRED API error for {series_id}: {message}")
    rows: dict[date, Decimal] = {}
    for item in observations:
        value_text = str(item.get("value", ""))
        if _is_missing(value_text):
            continue
        rows[_parse_date(str(item["date"]))] = Decimal(value_text.strip())
    return sorted(rows.items())


def load_series(
    series_id: str,
    api_key: str | None = None,
    observation_start: date | None = None,
) -> tuple[list[tuple[date, Decimal]], str]:
    """Return (observations, source_label).

    A bad or rate-limited API key falls back to the public CSV so one secret
    cannot blank the feed. ``observation_start`` limits the download to a
    recent window, which is how a scheduled run checks the latest date
    without pulling the whole history.
    """
    if api_key:
        url = API_URL.format(series_id=series_id, api_key=api_key)
        if observation_start is not None:
            url += "&observation_start=" + observation_start.isoformat()
        try:
            body = fetch_bytes(url, redact=api_key)
            payload = json.loads(body.decode("utf-8"))
            return parse_api(payload, series_id), "fred_api"
        except (FetchError, ValueError, json.JSONDecodeError, KeyError, OSError):
            rows = _load_csv(series_id, observation_start)
            return rows, "fred_csv_fallback"
    return _load_csv(series_id, observation_start), "fred_csv"


def _load_csv(series_id: str, observation_start: date | None = None) -> list[tuple[date, Decimal]]:
    url = CSV_URL.format(series_id=series_id)
    if observation_start is not None:
        url += "&cosd=" + observation_start.isoformat()
    body = fetch_bytes(url)
    text = body.decode("utf-8", errors="replace")
    rows = parse_csv(text, series_id)
    if not rows:
        raise ValueError(f"FRED series {series_id} returned no observations")
    return rows
