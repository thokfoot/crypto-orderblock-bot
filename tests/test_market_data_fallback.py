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
    merge_ohlcv_frames,
)


class TestMarketDataFallback(unittest.TestCase):
    def setUp(self):
        self.ist = ZoneInfo("Asia/Kolkata")
        self.et = ZoneInfo("America/New_York")
        import scripts.market_data_fallback as fb
        fb._TWELVE_DATA_CACHE.clear()
        fb._TWELVE_DATA_GLOBAL_CALL_TIMESTAMPS.clear()

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
        mock_td.assert_called_once_with("SPY", exchange="US", outputsize=30)

    @patch("scripts.market_data_fallback._get_current_time")
    @patch("scripts.market_data_fallback.is_market_session_active")
    @patch("scripts.market_data_fallback.fetch_twelve_data_1m_bars")
    @patch("scripts.market_data_fallback._fetch_yfinance_history")
    def test_fetch_resilient_1m_bars_partial_gap_triggers_twelve_data_merge(
        self, mock_yf, mock_td, mock_active, mock_time
    ):
        """
        AUDIT VERIFICATION TEST:
        When Yahoo returns a non-empty DataFrame with an internal 5-minute data gap,
        fetch_resilient_1m_bars MUST NOT silently return the gapped frame.
        It must trigger Twelve Data and merge the missing candles.
        """
        mock_active.return_value = True
        mock_time.return_value = dt.datetime(2026, 11, 25, 10, 7, tzinfo=self.et)

        # Yahoo has bars at 10:00, 10:01, then a GAP, then 10:06 (5 minutes missing)
        gap_idx = [
            pd.Timestamp("2026-11-25 10:00", tz=self.et),
            pd.Timestamp("2026-11-25 10:01", tz=self.et),
            pd.Timestamp("2026-11-25 10:06", tz=self.et),
        ]
        yahoo_gapped_df = pd.DataFrame({
            "Open": [700.0, 701.0, 705.0],
            "High": [702.0, 703.0, 706.0],
            "Low": [699.0, 700.0, 704.0],
            "Close": [701.0, 702.0, 705.5],
            "Volume": [1000.0, 1200.0, 1500.0],
        }, index=pd.DatetimeIndex(gap_idx))
        mock_yf.return_value = yahoo_gapped_df

        # Twelve Data supplies the missing candles (10:02, 10:03, 10:04, 10:05)
        fill_idx = pd.date_range("2026-11-25 10:02", "2026-11-25 10:05", freq="min", tz="America/New_York")
        twelve_data_fill_df = pd.DataFrame({
            "Open": [702.0] * 4,
            "High": [704.0] * 4,
            "Low": [701.0] * 4,
            "Close": [703.0] * 4,
            "Volume": [1100.0] * 4,
        }, index=fill_idx)
        mock_td.return_value = twelve_data_fill_df

        result_df = fetch_resilient_1m_bars("SPY", exchange="US")

        # Twelve Data must have been called despite Yahoo being non-empty
        mock_td.assert_called_once_with("SPY", exchange="US", outputsize=30)
        # Merged result should now have all 7 contiguous minutes (10:00 to 10:06)
        self.assertEqual(len(result_df), 7)
        self.assertEqual(result_df.index[0].minute, 0)
        self.assertEqual(result_df.index[-1].minute, 6)

    @patch.dict("os.environ", {"TWELVE_DATA_API_KEY": "test_key"})
    @patch("urllib.request.urlopen")
    def test_twelve_data_rate_limiting_cache(self, mock_urlopen):
        """
        Verify that multiple calls to Twelve Data within 60s reuse cache and avoid duplicate requests.
        """
        sample_json = {
            "meta": {"symbol": "QQQ", "interval": "1min"},
            "values": [{
                "datetime": "2026-11-25 15:59:00",
                "open": "633.0",
                "high": "634.0",
                "low": "632.0",
                "close": "633.8",
                "volume": "50000",
            }],
            "status": "ok",
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(sample_json).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        # Call 1
        df1 = fetch_twelve_data_1m_bars("QQQ", exchange="US")
        self.assertEqual(len(df1), 1)
        self.assertEqual(mock_urlopen.call_count, 1)

        # Call 2 immediately (within 60s cooldown)
        df2 = fetch_twelve_data_1m_bars("QQQ", exchange="US")
        self.assertEqual(len(df2), 1)
        # Network request MUST NOT have incremented
        self.assertEqual(mock_urlopen.call_count, 1)

    def test_merge_ohlcv_frames_never_overwrites_primary_candles(self):
        """
        FINDING 1 AUDIT TEST:
        Primary Yahoo candles must NEVER be overwritten on overlapping timestamps.
        Secondary Twelve Data candles are inserted ONLY for genuinely missing timestamps.
        """
        ts1 = pd.Timestamp("2026-11-25 10:00", tz=self.et)
        ts2 = pd.Timestamp("2026-11-25 10:01", tz=self.et)
        ts_missing = pd.Timestamp("2026-11-25 10:02", tz=self.et)

        # Primary has ts1 and ts2 (with Close=700.0 and Close=701.0)
        primary = pd.DataFrame({
            "Open": [699.0, 700.0],
            "High": [701.0, 702.0],
            "Low": [698.0, 699.0],
            "Close": [700.0, 701.0],
            "Volume": [1000.0, 1200.0],
        }, index=pd.DatetimeIndex([ts1, ts2]))

        # Secondary has ts1, ts2 (with conflicting Close=999.0) and missing ts_missing (Close=705.0)
        secondary = pd.DataFrame({
            "Open": [999.0, 999.0, 704.0],
            "High": [999.0, 999.0, 706.0],
            "Low": [999.0, 999.0, 703.0],
            "Close": [999.0, 999.0, 705.0],
            "Volume": [5000.0, 5000.0, 1500.0],
        }, index=pd.DatetimeIndex([ts1, ts2, ts_missing]))

        merged = merge_ohlcv_frames(primary, secondary, "America/New_York")

        self.assertEqual(len(merged), 3)
        # Verify primary ts1 and ts2 were NOT overwritten by secondary 999.0
        self.assertEqual(merged.loc[ts1, "Close"], 700.0)
        self.assertEqual(merged.loc[ts2, "Close"], 701.0)
        # Verify missing ts_missing was inserted
        self.assertEqual(merged.loc[ts_missing, "Close"], 705.0)

    @patch.dict("os.environ", {"TWELVE_DATA_API_KEY": "test_key"})
    @patch("urllib.request.urlopen")
    def test_twelve_data_global_rate_ceiling(self, mock_urlopen):
        """
        FINDING 2 AUDIT TEST:
        Process-wide global call ceiling strictly prevents exceeding 6 calls/min across all symbols.
        """
        import scripts.market_data_fallback as fb
        fb._TWELVE_DATA_CACHE.clear()
        fb._TWELVE_DATA_GLOBAL_CALL_TIMESTAMPS.clear()

        sample_json = {
            "meta": {"symbol": "TEST", "interval": "1min"},
            "values": [{"datetime": "2026-11-25 15:59:00", "open": "100", "high": "101", "low": "99", "close": "100", "volume": "100"}],
            "status": "ok",
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(sample_json).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        # Call with 6 distinct symbols
        symbols = ["SYM1", "SYM2", "SYM3", "SYM4", "SYM5", "SYM6"]
        for s in symbols:
            df = fetch_twelve_data_1m_bars(s, exchange="US")
            self.assertEqual(len(df), 1)

        self.assertEqual(mock_urlopen.call_count, 6)

        # 7th call with a new symbol must be blocked by global ceiling!
        df7 = fetch_twelve_data_1m_bars("SYM7", exchange="US")
        self.assertTrue(df7.empty)
        # Network request must still be 6 (no 7th call made)
        self.assertEqual(mock_urlopen.call_count, 6)

    @patch.dict("os.environ", {"TWELVE_DATA_API_KEY": "test_key"})
    @patch("urllib.request.urlopen")
    def test_twelve_data_failure_is_cached(self, mock_urlopen):
        """
        FINDING 3 AUDIT TEST:
        API failure or HTTP error is cached for 60s so it does not retry on every poll.
        """
        import scripts.market_data_fallback as fb
        fb._TWELVE_DATA_CACHE.clear()

        # Simulate API error response
        error_json = {"code": 404, "message": "Symbol not available", "status": "error"}
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(error_json).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        # Call 1: fails
        df1 = fetch_twelve_data_1m_bars("FAILSYM", exchange="US")
        self.assertTrue(df1.empty)
        self.assertEqual(mock_urlopen.call_count, 1)

        # Call 2 immediately: must return cached failure without issuing another request
        df2 = fetch_twelve_data_1m_bars("FAILSYM", exchange="US")
        self.assertTrue(df2.empty)
        self.assertEqual(mock_urlopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
