"""Rebuild yields.json and macro.json without dropping the keys the site reads.

yields.json is fetched live by the dashboard. Every existing row field stays a
number or a string in the same place. New information is added beside those
fields, not instead of them. A row is emitted only when each required series
has a real print on that date. Nothing is copied forward from the day before.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from statistics import mean, pstdev

from feeds.calendar import is_business_day
from feeds.fred import load_series
from feeds.freshness import STORY_VERSION
from feeds.stats import change_n, dedupe, empirical_percentile
from feeds.yahoo import load_chart

# Thirty large-cap names. Breadth is the share of this list, and only this
# list, whose same-day close is above its own 200-day average. It is not the
# NYSE advance-decline line and it is not copied from an older file.
BREADTH_NAMES = (
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "AVGO",
    "TSLA",
    "BRK-B",
    "JPM",
    "LLY",
    "UNH",
    "XOM",
    "JNJ",
    "V",
    "MA",
    "PG",
    "HD",
    "COST",
    "ABBV",
    "BAC",
    "KO",
    "PEP",
    "WMT",
    "CRM",
    "AMD",
    "NFLX",
    "ORCL",
    "CSCO",
    "ACN",
)

SIGNAL_NAMES = (
    "Credit (HY OAS)",
    "Credit speed (5-day)",
    "Contagion (HY-IG)",
    "Volatility shape",
    "Nasdaq drawdown",
    "Breadth",
    "Semis vs trend",
)

Row = list[tuple[date, float]]


def _index(rows: list[tuple[date, float]]) -> dict[date, float]:
    return {day: value for day, value in dedupe(rows)}


def _on(index: dict[date, float], day: date) -> float | None:
    return index.get(day)


def _round(value: float, places: int) -> float:
    quant = Decimal("1") if places == 0 else Decimal("10") ** -places
    return float(Decimal(str(value)).quantize(quant))


def market_regime(
    *,
    hy: float,
    qqq_dd: float,
    vix: float,
    slope: float,
    breadth: float,
) -> str:
    """Labels the site already colors: RISK-ON, CAUTION, GROWTH SCARE, RISK-OFF.

    The 60 days already in the file stay RISK-ON except 2026-07-29, when the
    Nasdaq drawdown was worse than 10 percent and high yield was still under
    5 percentage points. These thresholds reproduce that day and leave the
    calmer days as RISK-ON.
    """
    if hy >= 600 or vix >= 35:
        return "RISK-OFF"
    if qqq_dd <= -0.10 and hy < 500:
        return "GROWTH SCARE"
    if hy >= 450 or slope > 0.5 or breadth < 0.40 or qqq_dd <= -0.15:
        return "CAUTION"
    return "RISK-ON"


def _drawdown(rows: Row, day: date, window: int) -> float | None:
    ordered = [value for obs, value in dedupe(rows) if obs <= day]
    if not ordered or dedupe(rows)[-1][0] < day and day not in _index(rows):
        return None
    if day not in _index(rows):
        return None
    if len(ordered) < window:
        return None
    windowed = ordered[-window:]
    peak = max(windowed)
    if peak == 0:
        return None
    return _round(windowed[-1] / peak - 1, 3)


def _above_average(rows: Row, day: date, window: int) -> float | None:
    ordered = [(obs, value) for obs, value in dedupe(rows) if obs <= day]
    if not ordered or ordered[-1][0] != day or len(ordered) < window:
        return None
    windowed = [value for _, value in ordered[-window:]]
    average = sum(windowed) / len(windowed)
    if average == 0:
        return None
    return _round(windowed[-1] / average - 1, 3)


def breadth_by_date(
    closes: dict[str, Row],
    *,
    window: int = 200,
    minimum_names: int = 24,
) -> dict[date, float]:
    """Share of the named list above its own average. Names without that day's close are left out."""
    prepared: dict[str, dict[date, float]] = {}
    for symbol, rows in closes.items():
        ordered = dedupe(rows)
        if len(ordered) < window:
            continue
        running: dict[date, float] = {}
        values = [value for _, value in ordered]
        for index, (obs, _) in enumerate(ordered):
            if index + 1 < window:
                continue
            windowed = values[index + 1 - window : index + 1]
            average = sum(windowed) / window
            if average == 0:
                continue
            running[obs] = windowed[-1] / average - 1
        prepared[symbol] = running
    days = sorted({obs for series in prepared.values() for obs in series})
    result: dict[date, float] = {}
    for day in days:
        observations = [series[day] for series in prepared.values() if day in series]
        if len(observations) < minimum_names:
            continue
        above = sum(1 for value in observations if value > 0)
        result[day] = _round(above / len(observations), 3)
    return result


def _bp(rows: list[tuple[date, Decimal]]) -> list[tuple[date, int]]:
    converted = []
    for obs, value in dedupe([(day, float(level)) for day, level in rows]):
        converted.append((obs, int((Decimal(str(value)) * 100).quantize(Decimal("1")))))
    return converted


def build_yield_rows(
    *,
    hy_bp: list[tuple[date, int]],
    ig_bp: list[tuple[date, int]],
    vix: Row,
    vix3m: Row,
    qqq: Row,
    smh: Row,
    breadth: dict[date, float],
    limit: int = 60,
    drawdown_window: int = 252,
    sma_window: int = 200,
) -> list[dict]:
    hy_index = _index([(day, float(value)) for day, value in hy_bp])
    ig_index = _index([(day, float(value)) for day, value in ig_bp])
    vix_index = _index(vix)
    vix3m_index = _index(vix3m)
    hy_ordered = dedupe([(day, float(value)) for day, value in hy_bp])
    rows: list[dict] = []
    for index, (day, hy_value) in enumerate(hy_ordered):
        if not is_business_day(day):
            continue
        ig_value = _on(ig_index, day)
        vix_value = _on(vix_index, day)
        vix3m_value = _on(vix3m_index, day)
        breadth_value = breadth.get(day)
        qqq_dd = _drawdown(qqq, day, drawdown_window)
        smh_gap = _above_average(smh, day, sma_window)
        if None in (ig_value, vix_value, vix3m_value, breadth_value, qqq_dd, smh_gap):
            continue
        prior = hy_ordered[index - 5][1] if index >= 5 else None
        dhy = None if prior is None else int(hy_value - prior)
        if dhy is None:
            continue
        slope = _round(vix_value - vix3m_value, 2)
        regime = market_regime(
            hy=hy_value,
            qqq_dd=qqq_dd,
            vix=vix_value,
            slope=slope,
            breadth=breadth_value,
        )
        rows.append(
            {
                "date": day.isoformat(),
                "HY_OAS": int(hy_value),
                "IG_OAS": int(ig_value),
                "HY_IG": int(hy_value - ig_value),
                "dHY_5d": dhy,
                "VIX": _round(vix_value, 2),
                "VIX3M": _round(vix3m_value, 2),
                "VIX_slope": slope,
                "QQQ_DD": qqq_dd,
                "SMH_200d": smh_gap,
                "Breadth": breadth_value,
                "REGIME": regime,
            }
        )
    return rows[-limit:]


def _grade_hy(level: float) -> str:
    if level < 350:
        return "green"
    if level < 500:
        return "watch"
    return "red"


def _grade_speed(change: float) -> str:
    if change <= 10:
        return "green"
    if change <= 25:
        return "watch"
    return "red"


def _grade_gap(level: float) -> str:
    if level < 350:
        return "green"
    if level < 500:
        return "watch"
    return "red"


def _grade_slope(slope: float) -> str:
    if slope < 0:
        return "green"
    if slope < 1:
        return "watch"
    return "red"


def _grade_drawdown(value: float) -> str:
    if value > -0.05:
        return "green"
    if value > -0.10:
        return "watch"
    return "red"


def _grade_breadth(value: float) -> str:
    if value > 0.55:
        return "green"
    if value > 0.35:
        return "watch"
    return "red"


def _grade_semis(value: float) -> str:
    if value > 0:
        return "green"
    if value > -0.05:
        return "watch"
    return "red"


def _trend(current: float, previous: float | None, *, worse_when_higher: bool) -> str:
    if previous is None:
        return "flat →"
    delta = current - previous
    if abs(delta) < 1e-9 or (worse_when_higher and abs(delta) < 0.005 and abs(current) < 5):
        if abs(delta) < (0.5 if abs(current) > 2 else 0.002):
            return "flat →"
    worse = delta > 0 if worse_when_higher else delta < 0
    if abs(delta) < (1 if abs(current) >= 5 else 0.005):
        return "flat →"
    return "worsening ↑" if worse else "improving ↓"


def _percentile_list(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def pick(fraction: float) -> float:
        if not ordered:
            raise ValueError("empty distribution")
        index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
        return ordered[index]

    return {
        "min": ordered[0],
        "p10": pick(0.10),
        "p25": pick(0.25),
        "median": pick(0.50),
        "p75": pick(0.75),
        "p90": pick(0.90),
        "max": ordered[-1],
    }


def dispersion_block(
    *,
    bb: list[tuple[date, int]],
    ccc: list[tuple[date, int]],
    hy: list[tuple[date, int]],
    interpretation: str,
) -> dict:
    gap: list[tuple[date, int]] = []
    bb_index = {day: value for day, value in bb}
    for day, value in ccc:
        if day in bb_index:
            gap.append((day, int(value - bb_index[day])))
    if not gap:
        raise ValueError("CCC and BB have no shared dates")
    latest_day, latest = gap[-1]
    bb_now = bb_index[latest_day]
    ccc_now = dict(ccc)[latest_day]
    values = [float(value) for _, value in gap]
    year_cut = latest_day.replace(year=latest_day.year - 1)
    year_values = [float(value) for day, value in gap if day > year_cut]
    z_window = values[-252:]
    z_score = None
    if len(z_window) >= 20:
        spread = pstdev(z_window)
        z_score = 0.0 if spread == 0 else _round((values[-1] - mean(z_window)) / spread, 1)
    distribution = {key: int(round(value)) for key, value in _percentile_list(values).items()}
    gap_5 = change_n([(day, float(value)) for day, value in gap], 5)
    gap_20 = change_n([(day, float(value)) for day, value in gap], 20)
    bb_20 = change_n([(day, float(value)) for day, value in bb], 20)
    ccc_20 = change_n([(day, float(value)) for day, value in ccc], 20)
    hy_20 = change_n([(day, float(value)) for day, value in hy], 20)

    def _change(result: dict | None) -> int | None:
        if result is None:
            return None
        return int(round(result["change"]))

    pct_1y = empirical_percentile(year_values or values, float(latest))
    pct_available = empirical_percentile(values, float(latest))
    gap_20_value = _change(gap_20)
    reason = (
        f"CCC-BB {latest}bps ({pct_available:.0f}th percentile of {len(values)} days "
        f"since {gap[0][0].isoformat()}, z {z_score if z_score is not None else 'n/a'}). "
        f"20d {gap_20_value if gap_20_value is not None else 'n/a'}."
    )
    stressed = "CALM" not in interpretation and "not a market-wide" not in interpretation
    grade = "green" if "CALM" in interpretation else "red" if interpretation.startswith("SYSTEMIC") else "gray"
    return {
        "available": True,
        "signal": "Credit dispersion (CCC-BB)",
        "value": f"{latest} bps",
        "value_bp": latest,
        "grade": grade,
        "reason": reason,
        "trend": _trend(float(latest), None if gap_5 is None else float(gap_5["from_value"]), worse_when_higher=True),
        "calibration": {
            "current_bps": latest,
            "pctile_1y": _round(pct_1y, 1),
            "pctile_5y": _round(pct_available, 1),
            "z_252d": z_score,
            "distribution_5y": distribution,
            "n_days": len(values),
            "window_start": gap[0][0].isoformat(),
            "window_end": latest_day.isoformat(),
            "window_note": (
                "pctile_5y and distribution_5y keep the names the dashboard already reads. "
                "The numbers are the full FRED sample still on file, not a five-year window."
            ),
        },
        "interpretation": interpretation,
        "divergence_condition_met": stressed and "CONCENTRATED" in interpretation,
        "alerts_enabled": False,
        "alert": False,
        "raw": {
            "bb_oas": bb_now,
            "ccc_oas": ccc_now,
            "ccc_bb": latest,
            "ccc_bb_5d": _change(gap_5),
            "ccc_bb_20d": gap_20_value,
            "bb_20d": _change(bb_20),
            "ccc_20d": _change(ccc_20),
            "hy_20d": _change(hy_20),
        },
    }


def _signal(name: str, value: str, grade: str, reason: str, trend: str) -> dict:
    return {"signal": name, "value": value, "grade": grade, "reason": reason, "trend": trend}


def _official_dates(latest: str, series_dates: dict[str, str] | None) -> dict[str, str]:
    """Keep the old row labels and add the FRED ids the publish gate compares."""
    dates = {"HY_OAS": latest, "VIX": latest, "QQQ": latest}
    for key, value in (series_dates or {}).items():
        if isinstance(key, str) and isinstance(value, str) and len(value) >= 10:
            dates[key] = value[:10]
    return dates


def build_macro_document(
    rows: list[dict],
    dispersion: dict,
    *,
    updated_at: str,
    tape: str,
    summary: str,
    official_as_of: dict[str, str] | None = None,
) -> dict:
    latest = rows[-1]
    previous = rows[-6] if len(rows) >= 6 else None
    hy = latest["HY_OAS"]
    speed = latest["dHY_5d"]
    gap = latest["HY_IG"]
    slope = latest["VIX_slope"]
    drawdown = latest["QQQ_DD"]
    breadth = latest["Breadth"]
    semis = latest["SMH_200d"]
    signals = [
        _signal(
            "Credit (HY OAS)",
            f"{hy} bps",
            _grade_hy(hy),
            "Junk-bond stress is low — lenders relaxed." if hy < 350 else "Junk-bond spreads are no longer low.",
            _trend(hy, None if previous is None else previous["HY_OAS"], worse_when_higher=True),
        ),
        _signal(
            "Credit speed (5-day)",
            f"{speed:+.0f} bps",
            _grade_speed(speed),
            "Credit is widening — watch pace." if speed > 10 else "Credit speed is quiet.",
            _trend(speed, None if previous is None else previous["dHY_5d"], worse_when_higher=True),
        ),
        _signal(
            "Contagion (HY-IG)",
            f"{gap} bps",
            _grade_gap(gap),
            "Junk-vs-safe gap normal — not spreading." if gap < 350 else "The junk-versus-safe gap is wide.",
            _trend(gap, None if previous is None else previous["HY_IG"], worse_when_higher=True),
        ),
        _signal(
            "Volatility shape",
            f"slope {slope:.1f}",
            _grade_slope(slope),
            "Near-term fear below 3-month — calm." if slope < 0 else "Near-term fear is above the 3-month reading.",
            _trend(slope, None if previous is None else previous["VIX_slope"], worse_when_higher=True),
        ),
        _signal(
            "Nasdaq drawdown",
            f"{drawdown * 100:.1f}%",
            _grade_drawdown(drawdown),
            "Near highs." if drawdown > -0.05 else "Nasdaq is off its high.",
            _trend(drawdown, None if previous is None else previous["QQQ_DD"], worse_when_higher=False),
        ),
        _signal(
            "Breadth",
            f"{breadth * 100:.0f}%",
            _grade_breadth(breadth),
            f"{breadth * 100:.0f}% of the 30-name list is above its own 200-day average.",
            _trend(breadth, None if previous is None else previous["Breadth"], worse_when_higher=False),
        ),
        _signal(
            "Semis vs trend",
            f"{semis * 100:+.0f}%",
            _grade_semis(semis),
            "Semis above trend." if semis > 0 else "Semis are below their 200-day average.",
            _trend(semis, None if previous is None else previous["SMH_200d"], worse_when_higher=False),
        ),
    ]
    reds = [item["signal"] for item in signals if item["grade"] == "red"]
    watches = [item["signal"] for item in signals if item["grade"] == "watch"]
    regime = latest["REGIME"]
    if regime == "RISK-OFF":
        risk_lr = 0.0
        advice = "Step back. The credit or volatility reading is in the danger zone. Do not add risk here."
    elif regime == "GROWTH SCARE":
        risk_lr = 0.5
        advice = "This is an equity drawdown with credit still open. Trim into strength and do not assume it is only a dip."
    elif regime == "CAUTION" or reds:
        risk_lr = 1.0 if regime == "RISK-ON" else 0.5
        advice = "The regime label is not a blow-up, but a red or caution signal is on. Add slowly, in thirds, and keep dry powder."
    else:
        risk_lr = 1.0
        advice = "Stay invested. Add to conviction names on normal pullbacks, in thirds. Keep dry powder."
    headline = f"{regime} — {len(reds)} red, {len(watches)} watch"
    return {
        "story_version": STORY_VERSION,
        "summary": summary,
        "what_would_change_this": (
            "The headline would leave risk-on if junk spreads reached 4.50 percentage points, "
            "near-term volatility rose above the 3-month reading by more than half a point, "
            "or the Nasdaq-100 fell more than 10 percent below its trailing high."
        ),
        "what_the_tape_is_saying": tape,
        "official_as_of": _official_dates(latest["date"], official_as_of),
        "macro": {
            "updatedAt": updated_at,
            "regime": regime,
            "risk_lr": risk_lr,
            "headline": headline,
            "advice": advice,
            "signals": signals,
            "raw": {
                "hy_oas": hy,
                "hy_ig": gap,
                "vix_slope": slope,
                "qqq_dd": drawdown,
                "breadth": breadth,
                "smh_vs_200dma": semis,
            },
            "alerts": [f"{name} is RED" for name in reds],
            "dispersion": dispersion,
        },
    }


def yields_document(
    rows: list[dict],
    *,
    updated_at: str,
    summary: str,
    official_as_of: dict[str, str] | None = None,
) -> dict:
    latest = rows[-1]
    return {
        "updatedAt": updated_at,
        "days": len(rows),
        "story_version": STORY_VERSION,
        "summary": summary,
        "what_would_change_this": (
            "The latest row would turn to GROWTH SCARE if the Nasdaq-100 closed more than 10 percent "
            "below its trailing high while junk spreads stayed under 6 percentage points, and it would "
            "turn to RISK-OFF if junk spreads reached 6 percentage points or VIX reached 35."
        ),
        "official_as_of": _official_dates(latest["date"], official_as_of),
        "breadth_note": (
            "Breadth is the share of a fixed 30-name large-cap list whose close that day is above "
            "its own 200-day average. A name with no print that day is left out. The number is not "
            "copied forward onto a later date."
        ),
        "rows": rows,
    }


def fetch_equity_closes(symbols: list[str], *, range_: str = "2y") -> dict[str, Row]:
    from concurrent.futures import ThreadPoolExecutor

    def one(symbol: str) -> tuple[str, Row]:
        chart = load_chart(symbol, range_=range_, price_places=4)
        return symbol, chart["observations"]

    found: dict[str, Row] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for symbol, rows in pool.map(one, symbols):
            found[symbol] = rows
    return found


def load_fred_bp(series_id: str, api_key: str | None) -> list[tuple[date, int]]:
    rows, _source = load_series(series_id, api_key)
    return _bp(rows)


def stamp(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()
