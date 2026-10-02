"""Oil tracker.

Headline Brent and WTI prices are Yahoo front-month futures when that source
answers, because FRED's EIA spot prices lag by a few days. Yahoo's number is
the latest trade on the daily bar, not a settlement. The FRED spot is kept
beside it and labeled as a different, slower series.

Curve shape needs two futures months. FRED does not publish that curve, so if
Yahoo is down the curve is omitted rather than invented.

The alert is: Brent up more than 5 percent over 5 trading observations, and
the 30-year Treasury yield higher over its own 5 trading observations.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from feeds.calendar import business_days_between, is_stale
from feeds.eia import load_inventories
from feeds.fred import load_series
from feeds.freshness import STORY_VERSION
from feeds.plain import oil_sentences
from feeds.serialize import measure, round_half_up
from feeds.stats import change_n, dedupe, shared_level, value_on
from feeds.yahoo import (
    add_contract_months,
    contract_symbol,
    load_chart,
    parse_contract_name,
    session_complete,
)

ALERT_BRENT_5D_PCT = 5.0
ALERT_DGS30_5D_PP = 0.0
CURVE_MONTHS = 12
CURVE_DEADBAND = 0.05
STALE_AFTER = 2

FRED_OIL = {
    "brent_spot": {"fred_id": "DCOILBRENTEU", "name": "Brent spot", "places": 2},
    "wti_spot": {"fred_id": "DCOILWTICO", "name": "WTI spot", "places": 2},
    "ulsd_spot": {"fred_id": "DHOILNYH", "name": "NY Harbor ULSD spot", "places": 3},
    "dgs30": {"fred_id": "DGS30", "name": "30-year Treasury yield", "places": 2},
    "t5yie": {"fred_id": "T5YIE", "name": "5-year breakeven inflation", "places": 2},
}


def _money(value: Decimal | str | float, places: int) -> float:
    return float(round_half_up(value, places))


def _pct(new: float, old: float) -> float | None:
    if old == 0:
        return None
    return float(round_half_up((Decimal(str(new)) / Decimal(str(old)) - 1) * 100, 2))


def _price_change(observations: list[tuple[date, float]], steps: int) -> dict | None:
    result = change_n(observations, steps)
    if result is None:
        return None
    percent = _pct(result["to_value"], result["from_value"])
    return {
        "value": float(round_half_up(Decimal(str(result["change"])), 2)),
        "unit": "usd_per_bbl",
        "pct": percent,
        "pct_unit": "percent",
        "from_date": result["from_date"].isoformat(),
        "to_date": result["to_date"].isoformat(),
        "from_value": result["from_value"],
        "to_value": result["to_value"],
        "observations_back": steps,
    }


def _yield_change(observations: list[tuple[date, float]], steps: int) -> dict | None:
    result = change_n(observations, steps)
    if result is None:
        return None
    delta = float(round_half_up(Decimal(str(result["change"])), 2))
    bp = int(round_half_up(Decimal(str(result["to_value"])) * 100, 0)) - int(
        round_half_up(Decimal(str(result["from_value"])) * 100, 0)
    )
    return measure(
        delta,
        "percentage_points",
        change_bp=measure(bp, "bp"),
        from_date=result["from_date"].isoformat(),
        to_date=result["to_date"].isoformat(),
        from_value=result["from_value"],
        to_value=result["to_value"],
        observations_back=steps,
    )


def curve_shape(front: float, deferred: float, deadband: float = CURVE_DEADBAND) -> str:
    gap = front - deferred
    if gap > deadband:
        return "backwardation"
    if gap < -deadband:
        return "contango"
    return "flat"


def build_alert(
    brent_change: dict | None,
    dgs30_change: dict | None,
) -> dict:
    """Fire only when both legs clear their thresholds. Missing data does not alert."""
    thresholds = {
        "brent_5d_percent_greater_than": ALERT_BRENT_5D_PCT,
        "dgs30_5d_change_percentage_points_greater_than": ALERT_DGS30_5D_PP,
    }
    rule = (
        "Alert when Brent is up more than 5 percent over 5 trading observations "
        "and the 30-year Treasury yield is higher over its own 5 trading observations. "
        "The two windows can end on different days. Each date is printed below."
    )
    if brent_change is None or dgs30_change is None or brent_change.get("pct") is None:
        return {
            "flag": False,
            "plain_english": "No alert. One of the two legs is missing, so the rule was not scored.",
            "rule": rule,
            "thresholds": thresholds,
            "brent_5d_pct": None if brent_change is None else measure(brent_change.get("pct"), "percent"),
            "dgs30_5d_change": dgs30_change,
            "brent_leg": False,
            "rates_leg": False,
        }
    brent_pct = brent_change["pct"]
    dgs_change = dgs30_change["value"]
    brent_leg = brent_pct > ALERT_BRENT_5D_PCT
    rates_leg = dgs_change > ALERT_DGS30_5D_PP
    flag = brent_leg and rates_leg
    if flag:
        sentence = (
            f"Alert. Brent is up {brent_pct} percent over 5 trading days and the "
            f"30-year yield is higher by {dgs_change} percentage points."
        )
    else:
        sentence = (
            f"No alert. Brent's 5-day move is {brent_pct} percent and the 30-year "
            f"yield's 5-day move is {dgs_change} percentage points. Both legs have to clear the rule."
        )
    return {
        "flag": flag,
        "plain_english": sentence,
        "rule": rule,
        "thresholds": thresholds,
        "brent_5d_pct": measure(
            brent_pct,
            "percent",
            from_date=brent_change["from_date"],
            to_date=brent_change["to_date"],
        ),
        "dgs30_5d_change": dgs30_change,
        "brent_leg": brent_leg,
        "rates_leg": rates_leg,
    }


def _stale_block(as_of: date | None, today: date) -> dict:
    if as_of is None:
        return {"stale": None, "business_days_old": None}
    return {
        "stale": is_stale(as_of, today, STALE_AFTER),
        "business_days_old": business_days_between(as_of, today),
    }


def _spot_block(observations: list[tuple[date, float]], spec: dict, today: date) -> dict | None:
    if not observations:
        return None
    as_of, value = observations[-1]
    stale = _stale_block(as_of, today)
    return {
        "fred_id": spec["fred_id"],
        "name": spec["name"],
        "value": value,
        "unit": "usd_per_bbl" if spec["fred_id"] != "DHOILNYH" else "usd_per_gal",
        "as_of": as_of.isoformat(),
        **stale,
        "source": f"FRED {spec['fred_id']} (EIA spot, usually a few days behind the futures market)",
        "change_1d": _price_change(observations, 1),
        "change_5d": _price_change(observations, 5),
        "change_20d": _price_change(observations, 20),
    }


def _futures_block(chart: dict, today: date, now: datetime) -> dict:
    observations = chart["observations"]
    as_of, value = observations[-1]
    complete = session_complete(chart, now, today)
    stale = _stale_block(as_of, today)
    return {
        "value": value,
        "unit": "usd_per_bbl",
        "as_of": as_of.isoformat(),
        "quoted_at": chart.get("quoted_at"),
        "symbol": chart.get("symbol"),
        "contract_name": chart.get("short_name"),
        "price_type": "latest_daily_bar",
        "session_complete": complete,
        **stale,
        "source": "Yahoo Finance daily bar",
        "note": (
            "Latest trade on Yahoo's daily bar for the front-month future. "
            "This is newer than the FRED spot, and it is not an exchange settlement. "
            "Crude trades most of the day, so a bar dated today can still change until the session ends."
        ),
        "change_1d": _price_change(observations, 1),
        "change_5d": _price_change(observations, 5),
        "change_20d": _price_change(observations, 20),
    }


def _spread(left: list[tuple[date, float]], right: list[tuple[date, float]]) -> dict | None:
    rows = shared_level(left, right, lambda a, b: float(round_half_up(Decimal(str(a)) - Decimal(str(b)), 2)))
    if not rows:
        return None
    as_of, value = rows[-1]
    return {
        "value": value,
        "unit": "usd_per_bbl",
        "as_of": as_of.isoformat(),
        "definition": "Brent minus WTI on the latest date where both have a real print. The two prices are not mixed across days.",
        "change_1d": _price_change(rows, 1),
        "change_5d": _price_change(rows, 5),
        "change_20d": _price_change(rows, 20),
    }


def _identify_front(root: str, chart: dict, probes: dict[str, dict]) -> tuple[int, int] | None:
    named = parse_contract_name(chart.get("short_name"))
    if named:
        symbol = contract_symbol(root, *named)
        probed = probes.get(symbol)
        if probed and probed["observations"]:
            front_price = chart["observations"][-1][1]
            probe_price = probed["observations"][-1][1]
            if abs(front_price - probe_price) <= 0.15:
                return named
        elif named:
            return named
    latest_price = chart["observations"][-1][1]
    latest_date = chart["observations"][-1][0]
    matches: list[tuple[int, int]] = []
    for symbol, probed in probes.items():
        if not symbol.startswith(root):
            continue
        price = value_on(probed["observations"], latest_date)
        if price is None and probed["observations"]:
            price = probed["observations"][-1][1]
        if price is None or abs(price - latest_price) > 0.15:
            continue
        candidate = _month_from_symbol(symbol) or parse_contract_name(probed.get("short_name"))
        if candidate is not None:
            matches.append(candidate)
    if matches:
        return min(matches)
    return named


def _month_from_symbol(symbol: str) -> tuple[int, int] | None:
    # CLX26.NYM or BZZ26.NYM
    stem = symbol.split(".")[0]
    if len(stem) < 4:
        return None
    code = stem[-3]
    year_text = stem[-2:]
    if code not in "FGHJKMNQUVXZ" or not year_text.isdigit():
        return None
    return 2000 + int(year_text), "FGHJKMNQUVXZ".index(code) + 1


def build_curve_leg(
    *,
    name: str,
    root: str,
    front_chart: dict,
    probes: dict[str, dict],
) -> dict:
    front_month = _identify_front(root, front_chart, probes)
    if front_month is None:
        return {
            "available": False,
            "name": name,
            "reason": f"Could not tell which {name} contract is the front month.",
        }
    deferred_month = add_contract_months(front_month[0], front_month[1], CURVE_MONTHS)
    deferred_symbol = contract_symbol(root, *deferred_month)
    deferred = probes.get(deferred_symbol)
    if not deferred or not deferred["observations"]:
        return {
            "available": False,
            "name": name,
            "front_contract": contract_symbol(root, *front_month),
            "deferred_contract": deferred_symbol,
            "reason": f"No price for the later contract {deferred_symbol}.",
        }
    front_date, front_price = front_chart["observations"][-1]
    deferred_price = value_on(deferred["observations"], front_date)
    deferred_date = front_date
    if deferred_price is None:
        deferred_date, deferred_price = deferred["observations"][-1]
        if deferred_date != front_date:
            return {
                "available": False,
                "name": name,
                "reason": (
                    f"Front month is dated {front_date.isoformat()} and {deferred_symbol} "
                    f"is dated {deferred_date.isoformat()}. The curve is omitted rather than mixing days."
                ),
            }
    gap = float(round_half_up(Decimal(str(front_price)) - Decimal(str(deferred_price)), 2))
    shape = curve_shape(front_price, deferred_price)
    return {
        "available": True,
        "name": name,
        "front": {
            "symbol": front_chart["symbol"],
            "contract": contract_symbol(root, *front_month),
            "contract_month": f"{front_month[0]:04d}-{front_month[1]:02d}",
            "value": front_price,
            "unit": "usd_per_bbl",
            "as_of": front_date.isoformat(),
        },
        "deferred": {
            "symbol": deferred_symbol,
            "contract_month": f"{deferred_month[0]:04d}-{deferred_month[1]:02d}",
            "tenor_months": CURVE_MONTHS,
            "value": deferred_price,
            "unit": "usd_per_bbl",
            "as_of": deferred_date.isoformat(),
        },
        "front_minus_deferred": measure(gap, "usd_per_bbl"),
        "shape": shape,
        "shape_plain_english": {
            "backwardation": "Front month is above the later month. Near-term oil is priced richer than oil for later delivery.",
            "contango": "Front month is below the later month. Oil for later delivery is priced richer.",
            "flat": "Front month and the later month are within 5 cents. The curve is flat.",
        }[shape],
        "source": "Yahoo Finance",
        "price_type": "latest_daily_bar",
    }


def _crack(ulsd_per_gal: float, brent: float) -> float:
    amount = Decimal(str(ulsd_per_gal)) * 42 - Decimal(str(brent))
    return float(round_half_up(amount, 2))


def _crack_series(
    ulsd: list[tuple[date, float]],
    brent: list[tuple[date, float]],
) -> list[tuple[date, float]]:
    return dedupe(shared_level(ulsd, brent, lambda gal, bbl: _crack(gal, bbl)))


def _history(brent: list[tuple[date, float]], wti: list[tuple[date, float]], limit: int = 140) -> list[dict]:
    bmap = dict(brent)
    wmap = dict(wti)
    dates = sorted(set(bmap) | set(wmap))[-limit:]
    rows = []
    for day in dates:
        bval = bmap.get(day)
        wval = wmap.get(day)
        spread = None
        if bval is not None and wval is not None:
            spread = float(round_half_up(Decimal(str(bval)) - Decimal(str(wval)), 2))
        rows.append({"date": day.isoformat(), "brent": bval, "wti": wval, "brent_wti": spread})
    return rows


def build_oil_feed(
    *,
    today: date,
    now: datetime,
    brent_chart: dict | None,
    wti_chart: dict | None,
    heat_chart: dict | None,
    curve_probes: dict[str, dict],
    fred: dict[str, list[tuple[date, float]]],
    inventories: dict,
    generated_at: str | None = None,
    extra_notes: list[str] | None = None,
) -> dict:
    notes = list(extra_notes or [])
    brent_fut = _futures_block(brent_chart, today, now) if brent_chart else None
    wti_fut = _futures_block(wti_chart, today, now) if wti_chart else None
    brent_spot = _spot_block(fred.get("brent_spot") or [], FRED_OIL["brent_spot"], today)
    wti_spot = _spot_block(fred.get("wti_spot") or [], FRED_OIL["wti_spot"], today)

    if brent_fut is None and brent_spot is None:
        raise ValueError("No Brent price from Yahoo or FRED")
    if wti_fut is None and wti_spot is None:
        raise ValueError("No WTI price from Yahoo or FRED")

    if brent_fut is None or wti_fut is None:
        notes.append(
            "Yahoo Finance did not return both front-month prices. "
            "The missing headline falls back to the lagged EIA spot on FRED. "
            "That spot is a different instrument and an older date."
        )

    def headline(fut, spot, name):
        if fut is not None:
            return {
                "name": name,
                "headline": "futures",
                "futures": fut,
                "spot": spot,
                "value": fut["value"],
                "unit": fut["unit"],
                "as_of": fut["as_of"],
                "stale": fut["stale"],
                "source": fut["source"],
                "change_1d": fut["change_1d"],
                "change_5d": fut["change_5d"],
                "change_20d": fut["change_20d"],
            }
        return {
            "name": name,
            "headline": "spot",
            "futures": None,
            "spot": spot,
            "value": spot["value"],
            "unit": spot["unit"],
            "as_of": spot["as_of"],
            "stale": spot["stale"],
            "source": spot["source"],
            "change_1d": spot["change_1d"],
            "change_5d": spot["change_5d"],
            "change_20d": spot["change_20d"],
        }

    brent = headline(brent_fut, brent_spot, "Brent")
    wti = headline(wti_fut, wti_spot, "WTI")
    brent["note"] = (
        "The headline is the front-month future when Yahoo answers, because it is fresher. "
        "The spot beside it is the EIA daily price from FRED. They are different instruments, so the levels will not match."
    )
    wti["note"] = brent["note"]

    if brent["headline"] == "futures" and wti["headline"] == "futures":
        spread = _spread(brent_chart["observations"], wti_chart["observations"])
        spread_source = "Yahoo Finance front-month futures"
    else:
        spread = _spread(fred.get("brent_spot") or [], fred.get("wti_spot") or [])
        spread_source = "FRED EIA spots"
    if spread is not None:
        spread["source"] = spread_source
    spot_spread = _spread(fred.get("brent_spot") or [], fred.get("wti_spot") or [])
    if spot_spread is not None:
        spot_spread["source"] = "FRED EIA spots"
        spot_spread["label"] = "Brent spot minus WTI spot"

    curve = {"available": False, "wti": None, "brent": None, "primary": None}
    if brent_chart and wti_chart:
        wti_leg = build_curve_leg(name="WTI", root="CL", front_chart=wti_chart, probes=curve_probes)
        brent_leg = build_curve_leg(name="Brent", root="BZ", front_chart=brent_chart, probes=curve_probes)
        curve = {
            "available": bool(wti_leg.get("available") or brent_leg.get("available")),
            "primary": "wti" if wti_leg.get("available") else ("brent" if brent_leg.get("available") else None),
            "wti": wti_leg,
            "brent": brent_leg,
            "note": (
                "Front month versus the contract about 12 months later. "
                "Backwardation means the front month is richer. Contango means the later month is richer. "
                "A gap of 5 cents or less is called flat."
            ),
        }
    else:
        curve = {
            "available": False,
            "wti": None,
            "brent": None,
            "primary": None,
            "note": (
                "Curve shape needs futures months. Yahoo was unavailable and FRED does not "
                "publish a front-versus-later crude curve, so the curve is omitted."
            ),
        }

    diesel = {"futures_crack": {"available": False}, "spot_crack": {"available": False}}
    if heat_chart and brent_chart:
        cracks = _crack_series(heat_chart["observations"], brent_chart["observations"])
        if cracks:
            as_of, value = cracks[-1]
            heat_price = value_on(heat_chart["observations"], as_of)
            brent_price = value_on(brent_chart["observations"], as_of)
            diesel["futures_crack"] = {
                "available": True,
                "label": "NYMEX heating oil front month minus Brent front month",
                "plain_english": (
                    "A diesel proxy. NYMEX heating oil (HO) is the NY Harbor ultra-low-sulfur "
                    "diesel future, quoted in dollars per gallon. Multiply by 42 to get dollars "
                    "per barrel, then subtract Brent."
                ),
                "value": value,
                "unit": "usd_per_bbl",
                "as_of": as_of.isoformat(),
                "heating_oil_usd_per_gal": heat_price,
                "brent_usd_per_bbl": brent_price,
                "formula": "heating_oil_usd_per_gal * 42 - brent_usd_per_bbl",
                "source": "Yahoo Finance HO=F and BZ=F",
                "price_type": "latest_daily_bar",
                "symbol_heating_oil": heat_chart.get("symbol"),
                "change_5d": _price_change(cracks, 5),
            }
    else:
        diesel["futures_crack"] = {
            "available": False,
            "reason": "Heating-oil or Brent futures were unavailable, so the futures crack was omitted.",
        }
    ulsd = fred.get("ulsd_spot") or []
    brent_spot_obs = fred.get("brent_spot") or []
    spot_cracks = _crack_series(ulsd, brent_spot_obs)
    if spot_cracks:
        as_of, value = spot_cracks[-1]
        diesel["spot_crack"] = {
            "available": True,
            "label": "EIA NY Harbor ULSD spot minus EIA Brent spot",
            "plain_english": (
                "The same diesel-minus-Brent idea using EIA spot prices from FRED. "
                "It usually lags the futures crack by a few days."
            ),
            "value": value,
            "unit": "usd_per_bbl",
            "as_of": as_of.isoformat(),
            "ulsd_usd_per_gal": value_on(ulsd, as_of),
            "brent_usd_per_bbl": value_on(brent_spot_obs, as_of),
            "formula": "DHOILNYH * 42 - DCOILBRENTEU",
            "fred_ids": ["DHOILNYH", "DCOILBRENTEU"],
            "source": "FRED",
            "change_5d": _price_change(spot_cracks, 5),
        }
    else:
        diesel["spot_crack"] = {
            "available": False,
            "reason": "FRED did not have both ULSD and Brent spots on a shared date.",
        }

    def yield_series_block(key: str, unit_plain: str) -> dict:
        spec = FRED_OIL[key]
        rows = fred.get(key) or []
        if not rows:
            return {
                "fred_id": spec["fred_id"],
                "name": spec["name"],
                "value": None,
                "unit": "percent",
                "as_of": None,
                "stale": None,
                "available": False,
            }
        as_of, value = rows[-1]
        stale = _stale_block(as_of, today)
        return {
            "fred_id": spec["fred_id"],
            "name": spec["name"],
            "plain_english": unit_plain,
            "value": value,
            "unit": "percent",
            "as_of": as_of.isoformat(),
            **stale,
            "change_5d": _yield_change(rows, 5),
            "change_20d": _yield_change(rows, 20),
            "source": f"FRED {spec['fred_id']}",
        }

    spillover = {
        "dgs30": yield_series_block(
            "dgs30",
            "The 30-year Treasury yield. An oil spike that arrives with a higher long rate is the alert case.",
        ),
        "t5yie": yield_series_block(
            "t5yie",
            "The bond market's 5-year inflation expectation. Context for whether an oil move is showing up in inflation pricing.",
        ),
        "note": "These two series are the rate and inflation backdrop. They are not oil prices.",
    }

    brent_obs = brent_chart["observations"] if brent["headline"] == "futures" else (fred.get("brent_spot") or [])
    alert = build_alert(_price_change(brent_obs, 5), spillover["dgs30"].get("change_5d"))

    history_source = "yahoo_front_month" if brent_chart and wti_chart else "fred_spot"
    history = _history(
        brent_chart["observations"] if brent_chart else (fred.get("brent_spot") or []),
        wti_chart["observations"] if wti_chart else (fred.get("wti_spot") or []),
    )

    now_stamp = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    summary, what_would_change = oil_sentences(
        brent,
        wti,
        curve,
        bool(alert["flag"]),
        diesel=diesel,
        inventories=inventories,
        spillover=spillover,
    )
    official_as_of: dict[str, str] = {}
    for key, spec in FRED_OIL.items():
        rows = fred.get(key) or []
        if rows:
            official_as_of[spec["fred_id"]] = rows[-1][0].isoformat()
    if inventories.get("available") and inventories.get("as_of"):
        official_as_of["EIA_COMMERCIAL_CRUDE"] = inventories["as_of"]
    return {
        "feed": "oil",
        "schema_version": 1,
        "story_version": STORY_VERSION,
        "summary": summary,
        "what_would_change_this": what_would_change,
        "official_as_of": official_as_of,
        "generated_at": now_stamp,
        "run_date": today.isoformat(),
        "run_timezone": "America/New_York",
        "description": (
            "Oil tracker. Front-month futures come from Yahoo when available. "
            "EIA spot prices come from FRED and lag. Inventories come from EIA."
        ),
        "methodology": {
            "prices": (
                "Headline Brent and WTI are Yahoo Finance front-month daily bars (BZ=F and CL=F). "
                "price_type is latest_daily_bar, not a settlement. FRED DCOILBRENTEU and DCOILWTICO "
                "are the EIA spot prices and are labeled separately."
            ),
            "changes": "1-day, 5-day, and 20-day changes step back that many real observations of the same series.",
            "curve": (
                "Front month versus the contract 12 months later. Omitted when that later contract "
                "cannot be read. Not filled in from a model."
            ),
            "diesel": (
                "Futures crack uses NYMEX heating oil (HO=F, dollars per gallon) times 42, minus Brent. "
                "Spot crack uses FRED DHOILNYH times 42, minus DCOILBRENTEU, on a shared date."
            ),
            "inventories": (
                "Weekly US commercial crude stocks, excluding the SPR. The public EIA weekly file "
                "is used with no key. EIA_API_KEY, when set, is tried first and abandoned if it fails or is older."
            ),
            "alert": {
                "brent_5d_percent_greater_than": ALERT_BRENT_5D_PCT,
                "dgs30_5d_change_percentage_points_greater_than": ALERT_DGS30_5D_PP,
                "rule": (
                    "Flag is true only when Brent's own 5-observation percent change is greater than 5 "
                    "and the 30-year yield's own 5-observation change is greater than 0."
                ),
            },
            "stale_after_business_days": STALE_AFTER,
            "publish_rule": (
                "A run commits this file only when official_as_of contains a date newer than "
                "the copy already stored, or when the plain-English summary is missing. "
                "A Yahoo price that changes on an unchanged FRED or EIA date does not commit."
            ),
            "sources_not_used": [
                "CME Group settlement pages block scripted downloads, so official settlements are not in this file.",
                "FRED does not currently serve the weekly commercial-crude stock series on the public CSV endpoint, so inventories are read from EIA directly.",
            ],
        },
        "frontend_notes": [
            "Prices are numbers. The unit is a separate field. Do not append '$/bbl' if you already show the unit.",
            "Do not treat the futures headline and the FRED spot as two prints of the same price.",
            "null means that source had no print. Do not copy the previous value forward.",
        ],
        "brent": brent,
        "wti": wti,
        "brent_wti_spread": spread,
        "brent_wti_spot_spread": spot_spread,
        "curve": curve,
        "diesel": diesel,
        "inventories": inventories,
        "spillover": spillover,
        "alert": alert,
        "history": history,
        "history_source": history_source,
        "notes": notes,
    }


def _fred_float(rows: list[tuple[date, Decimal]], places: int) -> list[tuple[date, float]]:
    return dedupe([(day, _money(value, places)) for day, value in rows])


def _symbols_to_probe(root: str, chart: dict, today: date) -> list[str]:
    """Front month, the month a year later, and a short scan when the name has no month."""
    symbols: list[str] = []
    named = parse_contract_name(chart.get("short_name"))
    if named:
        symbols.append(contract_symbol(root, *named))
        symbols.append(contract_symbol(root, *add_contract_months(named[0], named[1], CURVE_MONTHS)))
    year, month = today.year, today.month
    for _ in range(4):
        symbols.append(contract_symbol(root, year, month))
        deferred = add_contract_months(year, month, CURVE_MONTHS)
        symbols.append(contract_symbol(root, *deferred))
        year, month = add_contract_months(year, month, 1)
    ordered: list[str] = []
    for symbol in symbols:
        if symbol not in ordered:
            ordered.append(symbol)
    return ordered


def collect_oil(
    *,
    fred_api_key: str | None,
    eia_api_key: str | None,
    today: date | None = None,
    now: datetime | None = None,
    preloaded_fred: dict[str, list[tuple[date, Decimal]]] | None = None,
    preloaded_inventories: dict | None = None,
    inventory_notes: list[str] | None = None,
) -> dict:
    ny = ZoneInfo("America/New_York")
    if now is None:
        now = datetime.now(ny)
    if today is None:
        today = now.astimezone(ny).date()
    notes: list[str] = []

    def safe_chart(symbol: str, range_: str, places: int, *, quiet: bool = False) -> dict | None:
        try:
            return load_chart(symbol, range_=range_, price_places=places)
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                notes.append(f"Yahoo {symbol} could not be read: {exc}")
            return None

    brent_chart = safe_chart("BZ=F", "1y", 2)
    wti_chart = safe_chart("CL=F", "1y", 2)
    heat_chart = safe_chart("HO=F", "1y", 4)

    probes: dict[str, dict] = {}
    symbols: list[str] = []
    if wti_chart:
        symbols.extend(_symbols_to_probe("CL", wti_chart, today))
    if brent_chart:
        symbols.extend(_symbols_to_probe("BZ", brent_chart, today))
    seen_symbols: set[str] = set()
    for symbol in symbols:
        if symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        probed = safe_chart(symbol, "5d", 2, quiet=True)
        if probed:
            probes[symbol] = probed

    fred_rows: dict[str, list[tuple[date, float]]] = {}
    for key, spec in FRED_OIL.items():
        try:
            if preloaded_fred is not None and spec["fred_id"] in preloaded_fred:
                rows = preloaded_fred[spec["fred_id"]]
            else:
                rows, _source = load_series(spec["fred_id"], fred_api_key)
            fred_rows[key] = _fred_float(rows, spec["places"])
        except Exception as exc:  # noqa: BLE001
            notes.append(f"FRED {spec['fred_id']} could not be read: {exc}")
            fred_rows[key] = []

    if preloaded_inventories is not None:
        inventories = preloaded_inventories
        notes.extend(inventory_notes or [])
    else:
        inventories, inventory_notes = load_inventories(eia_api_key)
        notes.extend(inventory_notes)

    return build_oil_feed(
        today=today,
        now=now.astimezone(timezone.utc),
        brent_chart=brent_chart,
        wti_chart=wti_chart,
        heat_chart=heat_chart,
        curve_probes=probes,
        fred=fred_rows,
        inventories=inventories,
        extra_notes=notes,
    )
