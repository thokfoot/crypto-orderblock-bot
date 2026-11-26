"""
tests/test_market_data_fallback.py
Unit tests for multi-feed market data fallback and telemetry module.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo
import pandas as pd

from scripts.market_data_fallback import (
    fetch_cnbc_us_quote,
    fetch_google_finance_quote,
    fetch_live_reference_spot,
    fetch_raw_yahoo_v8_bars,
    fetch_resilient_1m_bars,
    fetch_twelve_data_1m_bars,
    is_market_session_active,
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

        # NSE Holiday (Republic Day Jan 26, 2026)
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
    def test_fetch_google_finance_quote_symbol_routing_spy(self, mock_urlopen):
        sample_html = (
            '<html><body><div class="N6SYTe"><span><span>$716.70</span></span></div></body></html>'
        )
        mock_response = MagicMock()
        mock_response.read.return_value = sample_html.encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        res = fetch_google_finance_quote("SPY", "US")
        self.assertIsNotNone(res)
        self.assertEqual(res["symbol"], "SPY")
        # SPY must be routed specifically to NYSEARCA, NOT NASDAQ
        self.assertEqual(res["exchange"], "NYSEARCA")
        self.assertEqual(res["price"], 716.70)
        self.assertTrue(res["is_synthetic_spot"])

    @patch("urllib.request.urlopen")
    def test_fetch_google_finance_quote_nse_tcs(self, mock_urlopen):
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
        mock_response.read.return_value = json.dumps(sample_json).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        res = fetch_cnbc_us_quote("AMAT")
        self.assertIsNotNone(res)
        self.assertEqual(res["symbol"], "AMAT")
        self.assertEqual(res["price"], 311.18)

    @patch("scripts.market_data_fallback.fetch_google_finance_quote")
    def test_fetch_live_reference_spot(self, mock_gf):
        mock_gf.return_value = {
            "source": "GoogleFinance_Telemetry",
            "symbol": "TCS",
            "price": 2640.30,
            "ok": True,
        }
        spot = fetch_live_reference_spot("TCS", "NSE")
        self.assertIsNotNone(spot)
        self.assertEqual(spot["price"], 2640.30)

    @patch("scripts.market_data_fallback._fetch_yfinance_history")
    def test_fetch_resilient_1m_bars_primary_success(self, mock_yf):
        # Mock yfinance primary returning valid OHLCV
        fake_idx = pd.date_range("2026-11-25 09:15", periods=5, freq="min", tz="Asia/Kolkata")
        fake_df = pd.DataFrame({
            "Open": [2600.0] * 5,
            "High": [2610.0] * 5,
            "Low": [2595.0] * 5,
            "Close": [2605.0] * 5,
            "Volume": [1000] * 5,
        }, index=fake_idx)
        mock_yf.return_value = fake_df

        df = fetch_resilient_1m_bars("TCS", exchange="NSE")
        self.assertFalse(df.empty)
        self.assertEqual(len(df), 5)
        self.assertEqual(df["Close"].iloc[-1], 2605.0)

    @patch("scripts.market_data_fallback.fetch_raw_yahoo_v8_bars")
    @patch("scripts.market_data_fallback._fetch_yfinance_history")
    def test_fetch_resilient_1m_bars_fallback_to_raw_v8(self, mock_yf, mock_v8):
        # Primary fails / empty
        mock_yf.return_value = pd.DataFrame()

        # Direct REST v8 succeeds
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

    @patch("scripts.market_data_fallback.fetch_raw_yahoo_v8_bars")
    @patch("scripts.market_data_fallback._fetch_yfinance_history")
    def test_fetch_resilient_1m_bars_never_injects_synthetic_single_price_candles(
        self, mock_yf, mock_v8
    ):
        """
        CRITICAL ARCHITECTURAL TEST:
        When exchange feeds drop candles, fetch_resilient_1m_bars MUST NOT
        fabricate or invent synthetic candles with Open=High=Low=Close.
        It must return only true bars so downstream stale-data guards pause safely.
        """
        # Both yfinance and direct v8 return empty (feed outage)
        mock_yf.return_value = pd.DataFrame()
        mock_v8.return_value = pd.DataFrame()

        df = fetch_resilient_1m_bars("TCS", exchange="NSE")
        self.assertTrue(df.empty)
        self.assertEqual(len(df), 0)

    @patch.dict("os.environ", {"TWELVE_DATA_API_KEY": "dummy_test_key"})
    @patch("urllib.request.urlopen")
    def test_fetch_twelve_data_1m_bars_success(self, mock_urlopen):
        sample_json = {
            "meta": {"symbol": "SPY", "interval": "1min"},
            "values": [
                {
                    "datetime": "2026-11-25 15:59:00",
                    "open": "717.25",
                    "high": "717.40",
                    "low": "716.15",
                    "close": "716.76",
                    "volume": "1619677",
                },
                {
                    "datetime": "2026-11-25 15:58:00",
                    "open": "717.82",
                    "high": "717.88",
                    "low": "717.25",
                    "close": "717.27",
                    "volume": "517158",
                },
            ],
            "status": "ok",
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(sample_json).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        df = fetch_twelve_data_1m_bars("SPY", exchange="US")
        self.assertFalse(df.empty)
        self.assertEqual(len(df), 2)
        self.assertEqual(df["Close"].iloc[-1], 716.76)
        self.assertEqual(df["Volume"].iloc[-1], 1619677.0)

    @patch("scripts.market_data_fallback.fetch_twelve_data_1m_bars")
    @patch("scripts.market_data_fallback.fetch_raw_yahoo_v8_bars")
    @patch("scripts.market_data_fallback._fetch_yfinance_history")
    def test_fetch_resilient_1m_bars_failover_to_twelve_data(
        self, mock_yf, mock_v8, mock_td
    ):
        # Yahoo primary and REST both fail
        mock_yf.return_value = pd.DataFrame()
        mock_v8.return_value = pd.DataFrame()

        # Twelve Data backup succeeds
        fake_idx = pd.date_range("2026-11-25 15:50", periods=3, freq="min", tz="America/New_York")
        fake_df = pd.DataFrame({
            "Open": [715.0] * 3,
            "High": [716.0] * 3,
            "Low": [714.5] * 3,
            "Close": [715.5] * 3,
            "Volume": [20000] * 3,
        }, index=fake_idx)
        mock_td.return_value = fake_df

        df = fetch_resilient_1m_bars("SPY", exchange="US")
        self.assertFalse(df.empty)
        self.assertEqual(len(df), 3)
        self.assertEqual(df["Close"].iloc[-1], 715.5)
        # Verify Twelve Data was called as secondary backup
        mock_td.assert_called_once_with("SPY", exchange="US")


if __name__ == "__main__":
    unittest.main()
