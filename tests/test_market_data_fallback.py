"""
tests/test_market_data_fallback.py
Unit tests for zero-intervention resilient market data fallback engine.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo
import pandas as pd

from scripts.market_data_fallback import (
    is_market_session_active,
    fetch_google_finance_quote,
    fetch_cnbc_us_quote,
    fetch_independent_quote,
    fetch_raw_yahoo_v8_bars,
    fetch_resilient_1m_bars,
)


class TestMarketDataFallback(unittest.TestCase):
    def setUp(self):
        self.ist = ZoneInfo("Asia/Kolkata")
        self.et = ZoneInfo("America/New_York")

    def test_is_market_session_active_nse(self):
        # Regular trading day at 10:30 IST (Wednesday)
        active_dt = dt.datetime(2026, 11, 25, 10, 30, tzinfo=self.ist)
        self.assertTrue(is_market_session_active("NSE", active_dt))

        # Before market open at 08:30 IST
        early_dt = dt.datetime(2026, 11, 25, 8, 30, tzinfo=self.ist)
        self.assertFalse(is_market_session_active("NSE", early_dt))

        # After market close at 16:00 IST
        late_dt = dt.datetime(2026, 11, 25, 16, 0, tzinfo=self.ist)
        self.assertFalse(is_market_session_active("NSE", late_dt))

        # Weekend (Saturday)
        sat_dt = dt.datetime(2026, 11, 28, 11, 0, tzinfo=self.ist)
        self.assertFalse(is_market_session_active("NSE", sat_dt))

        # NSE Holiday (e.g., Republic Day Jan 26, 2026)
        holiday_dt = dt.datetime(2026, 1, 26, 11, 0, tzinfo=self.ist)
        self.assertFalse(is_market_session_active("NSE", holiday_dt))

    def test_is_market_session_active_us(self):
        # Regular trading day at 11:00 ET (Wednesday)
        active_dt = dt.datetime(2026, 11, 25, 11, 0, tzinfo=self.et)
        self.assertTrue(is_market_session_active("US", active_dt))

        # US Holiday (Thanksgiving Nov 26, 2026)
        thanksgiving_dt = dt.datetime(2026, 11, 26, 11, 0, tzinfo=self.et)
        self.assertFalse(is_market_session_active("US", thanksgiving_dt))

    @patch("urllib.request.urlopen")
    def test_fetch_google_finance_quote_success(self, mock_urlopen):
        sample_html = (
            '<html><body><div class="N6SYTe"><span><span>₹2,640.30</span></span></div></body></html>'
        )
        mock_response = MagicMock()
        mock_response.read.return_value = sample_html.encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        res = fetch_google_finance_quote("TCS", "NSE")
        self.assertIsNotNone(res)
        self.assertEqual(res["symbol"], "TCS")
        self.assertEqual(res["exchange"], "NSE")
        self.assertEqual(res["price"], 2640.30)
        self.assertTrue(res["ok"])

    @patch("urllib.request.urlopen")
    def test_fetch_cnbc_us_quote_success(self, mock_urlopen):
        sample_json = {
            "FormattedQuoteResult": {
                "FormattedQuote": [
                    {"symbol": "AMAT", "last": "311.18"}
                ]
            }
        }
        mock_response = MagicMock()
        import json
        mock_response.read.return_value = json.dumps(sample_json).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        res = fetch_cnbc_us_quote("AMAT")
        self.assertIsNotNone(res)
        self.assertEqual(res["symbol"], "AMAT")
        self.assertEqual(res["price"], 311.18)
        self.assertTrue(res["ok"])

    @patch("scripts.market_data_fallback.fetch_raw_yahoo_v8_bars")
    @patch("yfinance.Ticker")
    def test_fetch_resilient_1m_bars_fallback_to_raw_v8(self, mock_ticker, mock_v8):
        # Simulate yfinance failure
        mock_ticker.side_effect = Exception("Yahoo Finance API temporary 429 rate limit")

        # Mock direct v8 REST success
        fake_idx = pd.date_range("2026-11-25 09:15", periods=5, freq="min", tz="Asia/Kolkata")
        fake_df = pd.DataFrame({
            "Open": [2600.0] * 5,
            "High": [2610.0] * 5,
            "Low": [2595.0] * 5,
            "Close": [2605.0] * 5,
            "Volume": [1000] * 5,
        }, index=fake_idx)
        mock_v8.return_value = fake_df

        df = fetch_resilient_1m_bars("TCS", exchange="NSE")
        self.assertFalse(df.empty)
        self.assertEqual(len(df), 5)
        self.assertEqual(df["Close"].iloc[-1], 2605.0)

    @patch("scripts.market_data_fallback.is_market_session_active")
    @patch("scripts.market_data_fallback.fetch_independent_quote")
    @patch("yfinance.Ticker")
    def test_fetch_resilient_1m_bars_synthetic_fallback_during_outage(
        self, mock_ticker, mock_indep, mock_active
    ):
        # Session is active
        mock_active.return_value = True

        # yfinance returns stale data (last bar at 15:15 IST, 15 minutes ago)
        now_dt = dt.datetime(2026, 11, 25, 15, 25, tzinfo=self.ist)
        stale_idx = pd.date_range("2026-11-25 15:10", "2026-11-25 15:15", freq="min", tz="Asia/Kolkata")
        stale_df = pd.DataFrame({
            "Open": [2620.0] * 6,
            "High": [2625.0] * 6,
            "Low": [2618.0] * 6,
            "Close": [2622.0] * 6,
            "Volume": [500] * 6,
        }, index=stale_idx)
        mock_inst = MagicMock()
        mock_inst.history.return_value = stale_df
        mock_ticker.return_value = mock_inst

        # Google Finance provides live quote at ₹2,630.00
        mock_indep.return_value = {
            "source": "GoogleFinance_ZeroKey",
            "price": 2630.00,
            "ok": True,
        }

        # Call resilient fetcher with mocked current time
        with patch("datetime.datetime") as mock_dt:
            mock_dt.now.return_value = now_dt
            mock_dt.combine = dt.datetime.combine
            mock_dt.time = dt.time
            mock_dt.date = dt.date
            df = fetch_resilient_1m_bars("TCS", exchange="NSE")

        self.assertFalse(df.empty)
        # Should have appended a synthetic bar at the current minute
        self.assertIn(df.index[-1].minute, [25, stale_idx[-1].minute])


if __name__ == "__main__":
    unittest.main()
