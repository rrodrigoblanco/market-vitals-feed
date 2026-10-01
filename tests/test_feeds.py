"""Offline tests for the credit and oil feeds. No network."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from feeds.calendar import add_months, business_days_between, is_stale, nyse_holidays
from feeds.credit import (
    THRESHOLDS,
    acceleration_direction,
    build_credit_feed,
    classify_regime,
)
from feeds.eia import parse_eia_api, parse_wpsr_table1
from feeds.fred import parse_csv
from feeds.oil import _crack, build_alert, build_oil_feed, curve_shape
from feeds.serialize import measure, write_if_changed
from feeds.stats import empirical_percentile
from feeds.validate import validate_credit, validate_oil
from feeds.yahoo import contract_symbol, parse_contract_name, session_complete


def weekdays_ending(end: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def dec_series(pairs: list[tuple[date, str]]) -> list[tuple[date, Decimal]]:
    return [(day, Decimal(value)) for day, value in pairs]


class CalendarTests(unittest.TestCase):
    def test_known_2026_holidays(self):
        holidays = nyse_holidays(2026)
        self.assertIn(date(2026, 1, 1), holidays)
        self.assertIn(date(2026, 1, 19), holidays)
        self.assertIn(date(2026, 4, 3), holidays)  # Good Friday
        self.assertIn(date(2026, 7, 3), holidays)  # July 4 observed Friday
        self.assertNotIn(date(2026, 7, 4), holidays)

    def test_age_and_stale_boundary(self):
        self.assertEqual(business_days_between(date(2026, 9, 30), date(2026, 10, 1)), 1)
        self.assertEqual(business_days_between(date(2026, 9, 29), date(2026, 10, 1)), 2)
        self.assertEqual(business_days_between(date(2026, 9, 28), date(2026, 10, 1)), 3)
        self.assertFalse(is_stale(date(2026, 9, 29), date(2026, 10, 1)))
        self.assertTrue(is_stale(date(2026, 9, 28), date(2026, 10, 1)))
        # July 3 2026 is the observed Independence Day, so it is not counted.
        self.assertEqual(business_days_between(date(2026, 7, 2), date(2026, 7, 6)), 1)

    def test_add_months_clamps(self):
        self.assertEqual(add_months(date(2026, 9, 30), -3), date(2026, 6, 30))
        self.assertEqual(add_months(date(2024, 5, 31), -3), date(2024, 2, 29))
        self.assertEqual(add_months(date(2025, 5, 31), -3), date(2025, 2, 28))


class RegimeTests(unittest.TestCase):
    def test_thresholds_are_the_documented_ones(self):
        self.assertEqual(THRESHOLDS["bb_move_5d_bp"], 15)
        self.assertEqual(THRESHOLDS["ig_widen_5d_bp"], 10)
        self.assertEqual(THRESHOLDS["bb_elevated_level_bp"], 222)
        self.assertEqual(THRESHOLDS["bb_elevated_percentile"], 90.0)
        self.assertEqual(THRESHOLDS["acceleration_deadband_bp"], 5)

    def test_systemic_needs_elevated_bb_and_widening_ig(self):
        result = classify_regime(
            bb_5=20, bb_20=30, bb_level=230, bb_percentile=50,
            ccc_5=40, ccc_20=60, gap_5=20, gap_20=30,
            ig_5=12, ig_20=4, hy_ig_5=10, hy_ig_20=0,
        )
        self.assertEqual(result["label"], "SYSTEMIC")
        self.assertTrue(result["flags"]["ig_widening"])
        self.assertTrue(result["flags"]["bb_elevated"])

    def test_broadening_when_bb_moves_with_ccc_and_ig_is_calm(self):
        result = classify_regime(
            bb_5=35, bb_20=41, bb_level=194, bb_percentile=71.5,
            ccc_5=86, ccc_20=126, gap_5=51, gap_20=85,
            ig_5=7, ig_20=3, hy_ig_5=32, hy_ig_20=43,
        )
        self.assertEqual(result["label"], "BROADENING")
        self.assertFalse(result["flags"]["bb_elevated"])
        self.assertTrue(result["flags"]["ig_calm"])
        self.assertTrue(result["flags"]["hy_ig_widening"])
        self.assertIn("widening", result["plain_english"])

    def test_elevated_bb_without_ig_is_not_systemic(self):
        result = classify_regime(
            bb_5=20, bb_20=30, bb_level=230, bb_percentile=95,
            ccc_5=40, ccc_20=80, gap_5=20, gap_20=40,
            ig_5=4, ig_20=3, hy_ig_5=20, hy_ig_20=10,
        )
        self.assertEqual(result["label"], "BROADENING")
        self.assertIn("not called SYSTEMIC", result["plain_english"])

    def test_ig_widening_without_elevated_bb_is_not_systemic(self):
        result = classify_regime(
            bb_5=20, bb_20=10, bb_level=180, bb_percentile=50,
            ccc_5=40, ccc_20=10, gap_5=10, gap_20=10,
            ig_5=12, ig_20=0, hy_ig_5=5, hy_ig_20=0,
        )
        self.assertEqual(result["label"], "BROADENING")
        self.assertIn("not called SYSTEMIC", result["plain_english"])

    def test_concentrated_when_only_ccc_leads(self):
        result = classify_regime(
            bb_5=5, bb_20=10, bb_level=180, bb_percentile=50,
            ccc_5=10, ccc_20=20, gap_5=25, gap_20=10,
            ig_5=2, ig_20=1, hy_ig_5=4, hy_ig_20=2,
        )
        self.assertEqual(result["label"], "CONCENTRATED")
        self.assertTrue(result["flags"]["ccc_led"])
        self.assertFalse(result["flags"]["bb_moving"])

    def test_calm(self):
        result = classify_regime(
            bb_5=2, bb_20=4, bb_level=180, bb_percentile=50,
            ccc_5=5, ccc_20=8, gap_5=3, gap_20=4,
            ig_5=1, ig_20=1, hy_ig_5=2, hy_ig_20=2,
        )
        self.assertEqual(result["label"], "CALM")

    def test_unknown_when_nothing_is_measurable(self):
        result = classify_regime(
            bb_5=None, bb_20=None, bb_level=None, bb_percentile=None,
            ccc_5=None, ccc_20=None, gap_5=None, gap_20=None,
            ig_5=None, ig_20=None, hy_ig_5=None, hy_ig_20=None,
        )
        self.assertEqual(result["label"], "UNKNOWN")

    def test_acceleration_direction_uses_a_deadband(self):
        self.assertEqual(acceleration_direction(5), "increasing")
        self.assertEqual(acceleration_direction(4), "steady")
        self.assertEqual(acceleration_direction(-5), "decreasing")
        self.assertEqual(acceleration_direction(-4), "steady")
        self.assertEqual(acceleration_direction(0), "steady")


class CreditBuildTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 10, 1)
        self.recent = weekdays_ending(date(2026, 9, 30), 45)
        self.june = date(2026, 6, 30)

    def _feed(self):
        recent = self.recent
        bb_values = {day: "1.60" for day in recent}
        for day in recent[:6]:
            bb_values[day] = "2.50"
        bb_values[recent[-1]] = "1.80"
        del bb_values[recent[10]]
        bb_pairs = [
            (date(2025, 4, 1), "2.00"),
            (date(2025, 4, 7), "3.06"),
            (date(2026, 3, 2), "1.80"),
            (date(2026, 3, 30), "2.22"),
            (self.june, "1.60"),
        ] + [(day, bb_values[day]) for day in recent if day in bb_values]

        def flat(raw: str, last: str | None = None, skip_last: bool = False) -> list[tuple[date, str]]:
            pairs = [(self.june, raw)]
            for day in recent:
                if skip_last and day == recent[-1]:
                    continue
                pairs.append((day, last if last and day == recent[-1] else raw))
            return pairs

        hy = flat("3.00", "3.39")
        ccc = flat("8.00", "8.60")
        ig = flat("0.80", skip_last=True)
        hy_yield = [(self.june, "7.00")] + [
            (day, "8.16" if day == recent[-1] else "7.68") for day in recent
        ]
        treasury = flat("4.50")
        raw = {
            "hy_oas": dec_series(hy),
            "bb_oas": dec_series(bb_pairs),
            "ccc_oas": dec_series(ccc),
            "ig_oas": dec_series(ig),
            "hy_yield": dec_series(hy_yield),
            "dgs10": dec_series(treasury),
            "dgs30": dec_series(treasury),
            "dgs5": dec_series(treasury),
        }
        sources = {key: "fred_csv" for key in raw}
        return build_credit_feed(raw, today=self.today, source_by_id=sources)

    def test_build_does_not_forward_fill_and_labels_the_real_window(self):
        feed = self._feed()
        validate_credit(feed)
        self.assertEqual(feed["regime"]["label"], "BROADENING")
        self.assertFalse(feed["regime"]["flags"]["bb_elevated"])
        self.assertTrue(feed["regime"]["flags"]["ig_calm"])

        hy = feed["series"]["hy_oas"]
        self.assertEqual(hy["value"], 339)
        self.assertEqual(hy["unit"], "bp")
        self.assertEqual(hy["as_of"], "2026-09-30")
        self.assertFalse(hy["stale"])
        self.assertEqual(hy["change_5d"]["value"], 39)
        self.assertEqual(hy["change_5d"]["unit"], "bp")
        self.assertNotIsInstance(hy["value"], str)

        window = hy["percentile_available"]
        self.assertEqual(window["window_label"], "available_history")
        self.assertIn("window_start", window)
        self.assertGreater(window["n_days"], 0)
        self.assertNotIn("5y", json.dumps(window))

        ig = feed["series"]["ig_oas"]
        self.assertEqual(ig["as_of"], self.recent[-2].isoformat())
        self.assertNotEqual(ig["as_of"], hy["as_of"])
        last = feed["history"][-1]
        self.assertEqual(last["date"], "2026-09-30")
        self.assertIsNone(last["ig_oas"])
        self.assertEqual(last["hy_oas"], 339)
        previous = feed["history"][-2]
        self.assertEqual(previous["ig_oas"], 80)
        self.assertNotEqual(last.get("ig_oas"), previous["ig_oas"])

        hole = next(row for row in feed["history"] if row["date"] == self.recent[10].isoformat())
        self.assertIsNone(hole["bb_oas"])
        self.assertIsNotNone(hole["hy_oas"])

        window_5d = feed["yield_decomposition"]["windows"]["5d"]
        self.assertEqual(window_5d["spread_part"]["value"], 39)
        self.assertEqual(window_5d["yield_change"]["value"], 48)
        self.assertEqual(window_5d["treasury_part"]["value"], 9)
        self.assertAlmostEqual(window_5d["spread_share_of_yield_move"], 0.813, places=3)

        window_3m = feed["yield_decomposition"]["windows"]["3m"]
        self.assertEqual(window_3m["from_date"], "2026-06-30")
        self.assertEqual(window_3m["spread_part"]["value"], 39)
        self.assertEqual(window_3m["yield_change"]["value"], 116)
        self.assertEqual(window_3m["treasury_part"]["value"], 77)

        self.assertEqual(feed["credit_speed"]["value"], 39)
        self.assertEqual(feed["credit_speed"]["acceleration"]["direction"], "increasing")
        self.assertEqual(feed["credit_speed"]["acceleration"]["value"], 39)

        april = next(item for item in feed["references"]["bb_oas"] if item["month"] == "2025-04")
        march = next(item for item in feed["references"]["bb_oas"] if item["month"] == "2026-03")
        self.assertTrue(april["verified"])
        self.assertEqual(april["fred_value"], 306)
        self.assertEqual(april["fred_date"], "2025-04-07")
        self.assertTrue(march["verified"])
        self.assertEqual(march["fred_value"], 222)
        self.assertEqual(march["fred_date"], "2026-03-30")

        bb_window = feed["series"]["bb_oas"]["percentile_available"]
        self.assertEqual(bb_window["window_start"], "2025-04-01")
        self.assertLess(bb_window["value"], 90)

    def test_percentile_definition_matches_the_share_at_or_below(self):
        self.assertEqual(empirical_percentile([1, 2, 3, 4], 3), 75.0)


class FredParseTests(unittest.TestCase):
    def test_dots_are_dropped_and_not_filled(self):
        text = "\n".join(
            [
                "observation_date,BAMLH0A0HYM2",
                "2026-09-28,3.02",
                "2026-09-29,.",
                "2026-09-30,3.12",
            ]
        )
        rows = parse_csv(text, "BAMLH0A0HYM2")
        self.assertEqual([day.isoformat() for day, _ in rows], ["2026-09-28", "2026-09-30"])
        self.assertEqual(rows[1][1], Decimal("3.12"))


class OilTests(unittest.TestCase):
    def test_alert_requires_both_legs_and_a_strict_five_percent(self):
        rising = {"pct": 6.0, "from_date": "2026-09-24", "to_date": "2026-10-01"}
        rates_up = {"value": 0.1, "unit": "percentage_points"}
        self.assertTrue(build_alert(rising, rates_up)["flag"])
        self.assertFalse(build_alert({"pct": 5.0, "from_date": "a", "to_date": "b"}, rates_up)["flag"])
        self.assertFalse(build_alert(rising, {"value": 0.0, "unit": "percentage_points"})["flag"])
        self.assertFalse(build_alert(rising, {"value": -0.05, "unit": "percentage_points"})["flag"])
        self.assertFalse(build_alert(None, rates_up)["flag"])

    def test_curve_names(self):
        self.assertEqual(curve_shape(93, 75), "backwardation")
        self.assertEqual(curve_shape(70, 75), "contango")
        self.assertEqual(curve_shape(70, 70.04), "flat")

    def test_crack_formula(self):
        self.assertEqual(_crack(4.789, 113.96), 87.18)

    def test_contract_names(self):
        self.assertEqual(parse_contract_name("Crude Oil Nov 26"), (2026, 11))
        self.assertIsNone(parse_contract_name("Brent Crude Oil Last Day Financ"))
        self.assertEqual(contract_symbol("CL", 2026, 11), "CLX26.NYM")
        self.assertEqual(contract_symbol("BZ", 2027, 12), "BZZ27.NYM")

    def test_wpsr_parser_and_thousand_barrel_guard(self):
        text = "\n".join(
            [
                '"STUB_1","9/25/26","9/18/26","Difference","Percent Change"',
                '"Crude Oil","711.087","710.950","0.137","0.000"',
                '"Commercial (Excluding SPR)","427.320","426.398","0.922","0.200"',
                '"Strategic Petroleum Reserve (SPR)","283.767","284.552","-0.785","-0.300"',
            ]
        )
        parsed = parse_wpsr_table1(text)
        self.assertEqual(parsed["as_of"], "2026-09-25")
        self.assertAlmostEqual(parsed["value"], 427.32, places=3)
        self.assertAlmostEqual(parsed["change_1w"]["value"], 0.922, places=3)
        self.assertEqual(parsed["unit"], "million_barrels")
        self.assertAlmostEqual(parsed["spr"]["value"], 283.767, places=3)

        thousands = "\n".join(
            [
                "STUB_1,9/25/26,9/18/26",
                "Commercial (Excluding SPR),427320,426398",
            ]
        )
        scaled = parse_wpsr_table1(thousands)
        self.assertAlmostEqual(scaled["value"], 427.32, places=2)

    def test_eia_api_parser(self):
        payload = {
            "response": {
                "data": [
                    {"period": "2026-09-18", "value": "426398", "units": "Thousand Barrels"},
                    {"period": "2026-09-25", "value": "427320", "units": "Thousand Barrels"},
                ]
            }
        }
        parsed = parse_eia_api(payload)
        self.assertEqual(parsed["as_of"], "2026-09-25")
        self.assertAlmostEqual(parsed["value"], 427.32, places=2)
        self.assertAlmostEqual(parsed["change_1w"]["value"], 0.922, places=3)

    def test_feed_uses_futures_curve_and_alert(self):
        days = weekdays_ending(date(2026, 10, 1), 25)
        brent_obs = [(day, 100.0) for day in days]
        brent_obs[-1] = (days[-1], 106.0)
        wti_obs = [(day, 90.0) for day in days]
        heat_obs = [(day, 4.0) for day in days]
        now = datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)
        session_end = datetime(2026, 10, 2, 3, 59, tzinfo=timezone.utc)

        def chart(symbol, name, rows):
            return {
                "symbol": symbol,
                "short_name": name,
                "observations": rows,
                "quoted_at": "2026-10-01T22:00:00Z",
                "session_end": session_end,
            }

        brent = chart("BZ=F", "Brent Crude Oil Last Day Financ", brent_obs)
        wti = chart("CL=F", "Crude Oil Nov 26", wti_obs)
        heat = chart("HO=F", "Heating Oil Nov 26", heat_obs)
        probes = {
            "CLX26.NYM": chart("CLX26.NYM", "Crude Oil Nov 26", wti_obs),
            "CLX27.NYM": chart("CLX27.NYM", "Crude Oil Nov 27", [(day, 80.0) for day in days]),
            "BZZ26.NYM": chart("BZZ26.NYM", "Brent Dec 26", brent_obs),
            "BZX26.NYM": chart("BZX26.NYM", "Brent Nov 26", [(day, 110.0) for day in days]),
            "BZZ27.NYM": chart("BZZ27.NYM", "Brent Dec 27", [(day, 90.0) for day in days]),
        }
        fred = {
            "brent_spot": [(date(2026, 9, 29), 113.96)],
            "wti_spot": [(date(2026, 9, 29), 96.16)],
            "ulsd_spot": [(date(2026, 9, 29), 4.789)],
            "dgs30": [(day, 4.0) for day in days[:-1]] + [(days[-1], 4.10)],
            "t5yie": [(days[-1], 2.36)],
        }
        inventories = {
            "available": True,
            "name": "US commercial crude inventories",
            "value": 427.32,
            "unit": "million_barrels",
            "as_of": "2026-09-25",
            "source": "test",
        }
        feed = build_oil_feed(
            today=date(2026, 10, 1),
            now=now,
            brent_chart=brent,
            wti_chart=wti,
            heat_chart=heat,
            curve_probes=probes,
            fred=fred,
            inventories=inventories,
        )
        validate_oil(feed)
        self.assertEqual(feed["brent"]["headline"], "futures")
        self.assertEqual(feed["brent"]["value"], 106.0)
        self.assertEqual(feed["brent"]["unit"], "usd_per_bbl")
        self.assertFalse(feed["brent"]["futures"]["session_complete"])
        self.assertEqual(feed["brent"]["spot"]["as_of"], "2026-09-29")
        self.assertEqual(feed["brent"]["spot"]["value"], 113.96)
        self.assertTrue(feed["alert"]["flag"])
        self.assertEqual(feed["curve"]["wti"]["shape"], "backwardation")
        self.assertEqual(feed["curve"]["wti"]["deferred"]["symbol"], "CLX27.NYM")
        self.assertEqual(feed["curve"]["brent"]["shape"], "backwardation")
        self.assertEqual(feed["diesel"]["futures_crack"]["value"], 62.0)
        self.assertEqual(feed["diesel"]["spot_crack"]["as_of"], "2026-09-29")
        self.assertEqual(feed["brent_wti_spread"]["value"], 16.0)
        self.assertEqual(feed["brent_wti_spread"]["as_of"], "2026-10-01")

    def test_missing_yahoo_falls_back_and_omits_the_curve(self):
        days = weekdays_ending(date(2026, 9, 29), 25)
        fred = {
            "brent_spot": [(day, 100.0) for day in days],
            "wti_spot": [(day, 90.0) for day in days],
            "ulsd_spot": [(day, 3.0) for day in days],
            "dgs30": [(day, 4.0) for day in days],
            "t5yie": [(days[-1], 2.3)],
        }
        feed = build_oil_feed(
            today=date(2026, 10, 1),
            now=datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc),
            brent_chart=None,
            wti_chart=None,
            heat_chart=None,
            curve_probes={},
            fred=fred,
            inventories={"available": False, "value": None, "unit": "million_barrels", "note": "down"},
            extra_notes=["Yahoo Finance did not answer."],
        )
        validate_oil(feed)
        self.assertEqual(feed["brent"]["headline"], "spot")
        self.assertFalse(feed["curve"]["available"])
        self.assertFalse(feed["diesel"]["futures_crack"]["available"])
        self.assertTrue(feed["diesel"]["spot_crack"]["available"])
        self.assertFalse(feed["alert"]["flag"])

    def test_session_complete_only_after_the_bar_closes(self):
        chart = {
            "observations": [(date(2026, 10, 1), 90.0)],
            "session_end": datetime(2026, 10, 2, 3, 59, tzinfo=timezone.utc),
        }
        midday = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
        after = datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc)
        self.assertFalse(session_complete(chart, midday, date(2026, 10, 1)))
        self.assertTrue(session_complete(chart, after, date(2026, 10, 2)))


class SerializeTests(unittest.TestCase):
    def test_measure_rejects_strings(self):
        with self.assertRaises(TypeError):
            measure("968 bps", "bp")

    def test_unchanged_file_is_left_alone_and_protected_names_are_refused(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credit.json"
            first = {"feed": "credit", "generated_at": "2026-10-01T00:00:00Z", "value": 1}
            second = {"feed": "credit", "generated_at": "2026-10-01T06:00:00Z", "value": 1}
            self.assertTrue(write_if_changed(path, first))
            self.assertFalse(write_if_changed(path, second))
            self.assertIn("2026-10-01T00:00:00Z", path.read_text(encoding="utf-8"))
            with self.assertRaises(RuntimeError):
                write_if_changed(Path(tmp) / "yields.json", {"value": 1})


class RepoSafetyTests(unittest.TestCase):
    def test_legacy_files_are_not_dirty(self):
        subprocess.check_call(
            [
                "git",
                "diff",
                "--exit-code",
                "--",
                "macro.json",
                "yields.json",
                "durability.json",
                "gpu_waterfall.json",
            ],
            cwd=ROOT,
        )


if __name__ == "__main__":
    unittest.main()
