from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from market_time_utils import completed_1m_bars, exact_minute_window, is_fresh_1m_data


class MarketTimeUtilsTests(unittest.TestCase):
    def test_exact_twenty_minute_window_is_accepted(self):
        tz = ZoneInfo("America/New_York")
        index = pd.date_range("2026-11-24 09:45", periods=20, freq="min", tz=tz)
        frame = pd.DataFrame({"Close": range(20)}, index=index)
        result = exact_minute_window(frame, dt.date(2026, 11, 24), "09:45", "10:04", "America/New_York")
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 20)

    def test_incomplete_or_window_is_rejected(self):
        tz = ZoneInfo("Asia/Kolkata")
        index = pd.date_range("2026-11-24 09:15", periods=19, freq="min", tz=tz)
        frame = pd.DataFrame({"Close": range(19)}, index=index)
        result = exact_minute_window(frame, dt.date(2026, 11, 24), "09:15", "09:34", "Asia/Kolkata")
        self.assertIsNone(result)

    def test_current_forming_minute_is_excluded(self):
        tz = ZoneInfo("America/New_York")
        index = pd.date_range("2026-11-24 10:03", periods=3, freq="min", tz=tz)
        frame = pd.DataFrame({"Close": [1, 2, 3]}, index=index)
        now = dt.datetime(2026, 11, 24, 10, 05, 30, tzinfo=tz)
        result = completed_1m_bars(frame, now, "America/New_York")
        self.assertEqual(list(result["Close"]), [1, 2])

    def test_us_timezone_observes_dst(self):
        et = ZoneInfo("America/New_York")
        winter = dt.datetime(2026, 1, 15, 10, 0, tzinfo=et)
        summer = dt.datetime(2026, 7, 15, 10, 0, tzinfo=et)
        self.assertEqual(winter.utcoffset(), dt.timedelta(hours=-5))
        self.assertEqual(summer.utcoffset(), dt.timedelta(hours=-4))

    def test_stale_data_is_rejected(self):
        tz = ZoneInfo("Asia/Kolkata")
        index = pd.date_range("2026-11-24 09:15", periods=20, freq="min", tz=tz)
        frame = pd.DataFrame({"Close": range(20)}, index=index)
        now = dt.datetime(2026, 11, 24, 10, 0, tzinfo=tz)
        self.assertFalse(is_fresh_1m_data(frame, now, "Asia/Kolkata", max_age_seconds=180))


if __name__ == "__main__":
    unittest.main()
