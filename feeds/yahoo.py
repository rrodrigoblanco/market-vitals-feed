"""Yahoo Finance daily bars for front-month energy futures.

The figure is the latest trade on Yahoo's daily bar, not an exchange settlement.
CME's settlement pages block scripted requests, so this feed does not call them.
The JSON says so.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote

from feeds.http_client import fetch_bytes
from feeds.serialize import round_half_up
from feeds.stats import dedupe

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range={range_}"

MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}
MONTH_CODES = "FGHJKMNQUVXZ"


def contract_symbol(root: str, year: int, month: int) -> str:
    code = MONTH_CODES[month - 1]
    return f"{root}{code}{year % 100:02d}.NYM"


def parse_contract_name(name: str | None) -> tuple[int, int] | None:
    """Pull 'Nov 26' out of a Yahoo short name. Brent's name often has no month."""
    if not name:
        return None
    parts = name.replace(",", " ").split()
    for index, part in enumerate(parts[:-1]):
        month = MONTHS.get(part)
        year_text = parts[index + 1]
        if month and len(year_text) == 2 and year_text.isdigit():
            return 2000 + int(year_text), month
    return None


def add_contract_months(year: int, month: int, steps: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + steps
    return index // 12, index % 12 + 1


def _bar_date(timestamp: int, offset_seconds: int) -> date:
    local = timezone(timedelta(seconds=offset_seconds))
    return datetime.fromtimestamp(timestamp, local).date()


def parse_chart(payload: dict, *, price_places: int) -> dict:
    result = (payload.get("chart") or {}).get("result")
    if not result:
        error = (payload.get("chart") or {}).get("error")
        raise ValueError(f"Yahoo chart error: {error}")
    block = result[0]
    meta = block.get("meta") or {}
    timestamps = block.get("timestamp") or []
    quotes = ((block.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quotes.get("close") or []
    offset = int(meta.get("gmtoffset") or 0)
    rows: list[tuple[date, float]] = []
    for stamp, close in zip(timestamps, closes):
        if close is None:
            continue
        price = float(round_half_up(Decimal(str(close)), price_places))
        rows.append((_bar_date(int(stamp), offset), price))
    observations = dedupe(rows)
    if not observations:
        raise ValueError(f"Yahoo returned no closes for {meta.get('symbol')}")
    quoted_at = None
    if meta.get("regularMarketTime"):
        quoted_at = (
            datetime.fromtimestamp(int(meta["regularMarketTime"]), timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    session_end = None
    regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    if regular.get("end"):
        session_end = datetime.fromtimestamp(int(regular["end"]), timezone.utc)
    return {
        "symbol": meta.get("symbol"),
        "short_name": meta.get("shortName") or meta.get("longName"),
        "observations": observations,
        "quoted_at": quoted_at,
        "session_end": session_end,
        "price_places": price_places,
    }


def load_chart(symbol: str, *, range_: str = "1y", price_places: int = 2) -> dict:
    url = CHART_URL.format(symbol=quote(symbol, safe=""), range_=range_)
    body = fetch_bytes(url)
    payload = json.loads(body.decode("utf-8"))
    parsed = parse_chart(payload, price_places=price_places)
    parsed["symbol"] = parsed["symbol"] or symbol
    return parsed


def session_complete(chart: dict, now: datetime, today: date) -> bool:
    """False while today's daily bar is still updating.

    A bar from an earlier date is a finished close. Today's bar is finished
    only after Yahoo's published session end.
    """
    if not chart.get("observations"):
        return True
    latest = chart["observations"][-1][0]
    if latest < today:
        return True
    end = chart.get("session_end")
    if end is None:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now >= end
