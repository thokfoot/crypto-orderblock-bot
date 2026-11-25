from __future__ import annotations

import unittest
from zoneinfo import ZoneInfo
import pandas as pd
from scripts.market_time_utils import evaluate_stale_recovery, validate_blackout_continuity


class StaleFeedRecoveryRegressionTests(unittest.TestCase):
    """
    Focused regression tests for stale-feed outage recovery.
    Verifies that:
    1. Pre-entry blackout candles cannot trigger a new entry.
    2. In-trade outages are recorded as unknown outage exits at observable market prices,
       not assumed exact nominal fills.
    """

    def test_pre_entry_outage_discards_and_never_triggers_entry(self):
        """Pre-entry outage: breakout candles formed in the dark are discarded."""
        # Simulated candle whose High crossed trigger during the blackout
        bar = pd.Series({"Open": 100.0, "High": 105.0, "Low": 99.5, "Close": 104.0})
        trigger = 101.0
        sl = 99.0
        tp = 103.0

        recovery = evaluate_stale_recovery(
            in_trade=False,
            latest_bar=bar,
            trigger=trigger,
            sl=sl,
            tp=tp,
        )

        self.assertEqual(recovery["action"], "DISCARD_AND_RESUME")
        self.assertFalse(recovery["triggered"])
        self.assertFalse(recovery["terminal"])
        self.assertIsNone(recovery["exit_price"])
        self.assertEqual(recovery["reason"], "anti_backfill_discard")

    def test_in_trade_outage_stop_uses_observable_market_price_and_flags_uncertainty(self):
        """In-trade outage: stop loss breach records observable market price and flags uncertainty."""
        # Stock gapped down during blackout: SL was 100.0, but reconnection Close is 98.5
        bar = pd.Series({"Open": 99.0, "High": 99.2, "Low": 98.0, "Close": 98.5})
        trigger = 102.0
        sl = 100.0
        tp = 106.0

        recovery = evaluate_stale_recovery(
            in_trade=True,
            latest_bar=bar,
            trigger=trigger,
            sl=sl,
            tp=tp,
        )

        self.assertEqual(recovery["action"], "OUTAGE_EXIT_STOP")
        self.assertTrue(recovery["triggered"])
        self.assertTrue(recovery["terminal"])
        self.assertTrue(recovery["uncertainty"])
        # Observable exit price must capture the adverse gap (min(sl, close) = 98.5), NOT assume nominal 100.0!
        self.assertEqual(recovery["exit_price"], 98.5)
        self.assertEqual(recovery["reason"], "stop_crossed_during_outage")

    def test_in_trade_outage_target_flags_uncertainty(self):
        """In-trade outage: target hit records target with uncertainty flag."""
        # Stock rallied to target during blackout
        bar = pd.Series({"Open": 104.0, "High": 107.0, "Low": 103.5, "Close": 106.5})
        trigger = 102.0
        sl = 100.0
        tp = 106.0

        recovery = evaluate_stale_recovery(
            in_trade=True,
            latest_bar=bar,
            trigger=trigger,
            sl=sl,
            tp=tp,
        )

        self.assertEqual(recovery["action"], "OUTAGE_EXIT_TARGET")
        self.assertTrue(recovery["triggered"])
        self.assertTrue(recovery["terminal"])
        self.assertTrue(recovery["uncertainty"])
        self.assertEqual(recovery["exit_price"], 106.0)
        self.assertEqual(recovery["reason"], "target_reached_during_outage")

    def test_in_trade_outage_within_channel_continues_without_exit(self):
        """In-trade outage: price remained within SL/TP channel; trade continues."""
        bar = pd.Series({"Open": 102.5, "High": 104.0, "Low": 101.5, "Close": 103.0})
        trigger = 102.0
        sl = 100.0
        tp = 106.0

        recovery = evaluate_stale_recovery(
            in_trade=True,
            latest_bar=bar,
            trigger=trigger,
            sl=sl,
            tp=tp,
        )

        self.assertEqual(recovery["action"], "CONTINUE_IN_TRADE")
        self.assertTrue(recovery["triggered"])
        self.assertFalse(recovery["terminal"])
        self.assertFalse(recovery["uncertainty"])
        self.assertIsNone(recovery["exit_price"])
        self.assertEqual(recovery["current_price"], 103.0)

    def test_in_trade_multi_bar_dataframe_detects_intermediate_stop_loss(self):
        """Multi-bar outage window: detects SL breach in candle 1 even if candle 2 rebounded."""
        idx = pd.date_range("2026-11-25 10:00:00", "2026-11-25 10:01:00", freq="min")
        df = pd.DataFrame(
            [
                {"Open": 101.0, "High": 101.2, "Low": 99.4, "Close": 99.5},  # Breaches SL 100.0 at 10:00
                {"Open": 99.6, "High": 103.0, "Low": 99.6, "Close": 102.5},  # Rebounds above SL at 10:01
            ],
            index=idx,
        )
        recovery = evaluate_stale_recovery(
            in_trade=True,
            latest_bar=df.iloc[-1],
            trigger=102.0,
            sl=100.0,
            tp=106.0,
            blackout_bars=df,
            last_processed_ts=pd.Timestamp("2026-11-25 09:59:00"),
            expected_latest_ts=idx[-1],
        )
        self.assertEqual(recovery["action"], "OUTAGE_EXIT_STOP")
        self.assertTrue(recovery["terminal"])
        self.assertTrue(recovery["uncertainty"])
        self.assertEqual(recovery["exit_price"], 99.5)  # min(sl=100.0, close=99.5)

    def test_in_trade_multi_bar_dataframe_channel_survival(self):
        """Multi-bar outage window: all bars stay within channel."""
        idx = pd.date_range("2026-11-25 10:00:00", "2026-11-25 10:01:00", freq="min")
        df = pd.DataFrame(
            [
                {"Open": 102.0, "High": 103.5, "Low": 101.5, "Close": 102.8},
                {"Open": 102.8, "High": 104.2, "Low": 102.0, "Close": 103.9},
            ],
            index=idx,
        )
        recovery = evaluate_stale_recovery(
            in_trade=True,
            latest_bar=df.iloc[-1],
            trigger=102.0,
            sl=100.0,
            tp=106.0,
            blackout_bars=df,
            last_processed_ts=pd.Timestamp("2026-11-25 09:59:00"),
            expected_latest_ts=idx[-1],
        )
        self.assertEqual(recovery["action"], "CONTINUE_IN_TRADE")
        self.assertFalse(recovery["terminal"])
        self.assertEqual(recovery["current_price"], 103.9)

    def test_missing_first_blackout_candle_returns_recovery_data_incomplete(self):
        """Data continuity: missing first blackout candle (09:51) triggers RECOVERY_DATA_INCOMPLETE."""
        last_processed = pd.Timestamp("2026-11-25 09:50:00")
        # Received bars: 09:52, 09:53, 09:54 (09:51 is missing!)
        idx = pd.date_range("2026-11-25 09:52:00", "2026-11-25 09:54:00", freq="min")
        df = pd.DataFrame(
            [
                {"Open": 101.0, "High": 101.5, "Low": 100.5, "Close": 101.2},
                {"Open": 101.2, "High": 101.8, "Low": 100.8, "Close": 101.4},
                {"Open": 101.4, "High": 102.0, "Low": 101.0, "Close": 101.6},
            ],
            index=idx,
        )
        recovery = evaluate_stale_recovery(
            in_trade=True,
            latest_bar=df.iloc[-1],
            trigger=102.0,
            sl=100.0,
            tp=106.0,
            blackout_bars=df,
            last_processed_ts=last_processed,
            expected_latest_ts=idx[-1],
        )
        self.assertEqual(recovery["action"], "RECOVERY_DATA_INCOMPLETE")
        self.assertFalse(recovery["terminal"])
        self.assertTrue(recovery["uncertainty"])
        self.assertIsNone(recovery["exit_price"])
        self.assertEqual(recovery["missing_count"], 1)
        self.assertIn("09:51:00", recovery["missing_bars"][0])

    def test_missing_middle_blackout_candle_returns_recovery_data_incomplete(self):
        """Data continuity: missing middle blackout candle (09:52) triggers RECOVERY_DATA_INCOMPLETE."""
        last_processed = pd.Timestamp("2026-11-25 09:50:00")
        # Received bars: 09:51, 09:53, 09:54 (09:52 is missing!)
        t_0951 = pd.Timestamp("2026-11-25 09:51:00")
        t_0953 = pd.Timestamp("2026-11-25 09:53:00")
        t_0954 = pd.Timestamp("2026-11-25 09:54:00")
        df = pd.DataFrame(
            [
                {"Open": 101.0, "High": 101.5, "Low": 100.5, "Close": 101.2},
                {"Open": 101.2, "High": 101.8, "Low": 100.8, "Close": 101.4},
                {"Open": 101.4, "High": 102.0, "Low": 101.0, "Close": 101.6},
            ],
            index=[t_0951, t_0953, t_0954],
        )
        recovery = evaluate_stale_recovery(
            in_trade=True,
            latest_bar=df.iloc[-1],
            trigger=102.0,
            sl=100.0,
            tp=106.0,
            blackout_bars=df,
            last_processed_ts=last_processed,
            expected_latest_ts=t_0954,
        )
        self.assertEqual(recovery["action"], "RECOVERY_DATA_INCOMPLETE")
        self.assertFalse(recovery["terminal"])
        self.assertEqual(recovery["missing_count"], 1)
        self.assertIn("09:52:00", recovery["missing_bars"][0])

    def test_missing_last_blackout_candle_returns_recovery_data_incomplete(self):
        """Data continuity: missing last blackout candle (09:54) triggers RECOVERY_DATA_INCOMPLETE."""
        last_processed = pd.Timestamp("2026-11-25 09:50:00")
        expected_latest = pd.Timestamp("2026-11-25 09:54:00")
        # Received bars: 09:51, 09:52, 09:53 (09:54 is missing!)
        idx = pd.date_range("2026-11-25 09:51:00", "2026-11-25 09:53:00", freq="min")
        df = pd.DataFrame(
            [
                {"Open": 101.0, "High": 101.5, "Low": 100.5, "Close": 101.2},
                {"Open": 101.2, "High": 101.8, "Low": 100.8, "Close": 101.4},
                {"Open": 101.4, "High": 102.0, "Low": 101.0, "Close": 101.6},
            ],
            index=idx,
        )
        recovery = evaluate_stale_recovery(
            in_trade=True,
            latest_bar=df.iloc[-1],
            trigger=102.0,
            sl=100.0,
            tp=106.0,
            blackout_bars=df,
            last_processed_ts=last_processed,
            expected_latest_ts=expected_latest,
        )
        self.assertEqual(recovery["action"], "RECOVERY_DATA_INCOMPLETE")
        self.assertFalse(recovery["terminal"])
        self.assertEqual(recovery["missing_count"], 1)
        self.assertIn("09:54:00", recovery["missing_bars"][0])

    def test_continuity_nse_session_boundary_overnight_weekend_no_false_gap(self):
        """Exchange Calendar: Outage spanning Friday close to Monday open does NOT trigger false data gap."""
        tz_kolkata = ZoneInfo("Asia/Kolkata")
        t_fri_close_prev = pd.Timestamp("2026-11-20 15:25:00", tz=tz_kolkata)
        t_mon_open_curr = pd.Timestamp("2026-11-23 09:20:00", tz=tz_kolkata)

        # Tradable bars: Friday 15:26-15:29 (4 bars) + Monday 09:15-09:20 (6 bars) = 10 bars total
        fri_bars = pd.date_range("2026-11-20 15:26:00", "2026-11-20 15:29:00", freq="min", tz=tz_kolkata)
        mon_bars = pd.date_range("2026-11-23 09:15:00", "2026-11-23 09:20:00", freq="min", tz=tz_kolkata)
        combined_idx = fri_bars.append(mon_bars)

        df = pd.DataFrame(
            [{"Open": 100.0, "High": 101.0, "Low": 99.5, "Close": 100.5}] * len(combined_idx),
            index=combined_idx,
        )

        missing = validate_blackout_continuity(
            blackout_bars=df,
            last_processed_ts=t_fri_close_prev,
            expected_latest_ts=t_mon_open_curr,
            exchange="NSE",
        )
        self.assertEqual(missing, [], "Weekend and overnight non-trading hours must NOT be flagged as missing candles!")

        recovery = evaluate_stale_recovery(
            in_trade=True,
            latest_bar=df.iloc[-1],
            trigger=102.0,
            sl=99.0,
            tp=105.0,
            blackout_bars=df,
            last_processed_ts=t_fri_close_prev,
            expected_latest_ts=t_mon_open_curr,
            exchange="NSE",
        )
        self.assertEqual(recovery["action"], "CONTINUE_IN_TRADE")

    def test_continuity_us_session_boundary_overnight_no_false_gap(self):
        """Exchange Calendar: US outage spanning Mon close to Tue open does NOT trigger false data gap."""
        tz_ny = ZoneInfo("America/New_York")
        t_mon_close_prev = pd.Timestamp("2026-11-23 15:55:00", tz=tz_ny)
        t_tue_open_curr = pd.Timestamp("2026-11-24 09:35:00", tz=tz_ny)

        # Tradable bars: Monday 15:56-15:59 (4 bars) + Tuesday 09:30-09:35 (6 bars) = 10 bars total
        mon_bars = pd.date_range("2026-11-23 15:56:00", "2026-11-23 15:59:00", freq="min", tz=tz_ny)
        tue_bars = pd.date_range("2026-11-24 09:30:00", "2026-11-24 09:35:00", freq="min", tz=tz_ny)
        combined_idx = mon_bars.append(tue_bars)

        df = pd.DataFrame(
            [{"Open": 100.0, "High": 101.0, "Low": 99.5, "Close": 100.5}] * len(combined_idx),
            index=combined_idx,
        )

        missing = validate_blackout_continuity(
            blackout_bars=df,
            last_processed_ts=t_mon_close_prev,
            expected_latest_ts=t_tue_open_curr,
            exchange="US",
        )
        self.assertEqual(missing, [], "US overnight hours (16:00 to 09:29) must NOT be flagged as missing candles!")

        recovery = evaluate_stale_recovery(
            in_trade=True,
            latest_bar=df.iloc[-1],
            trigger=102.0,
            sl=99.0,
            tp=105.0,
            blackout_bars=df,
            last_processed_ts=t_mon_close_prev,
            expected_latest_ts=t_tue_open_curr,
            exchange="US",
        )
        self.assertEqual(recovery["action"], "CONTINUE_IN_TRADE")

    def test_continuity_market_holiday_does_not_trigger_data_gap(self):
        """Exchange Calendar: NSE official holiday (Republic Day Jan 26) generates zero expected bars."""
        tz_kolkata = ZoneInfo("Asia/Kolkata")
        # Friday Jan 23 close to Tuesday Jan 27 open (Monday Jan 26 was Republic Day holiday!)
        t_fri_close_prev = pd.Timestamp("2026-01-23 15:25:00", tz=tz_kolkata)
        t_tue_open_curr = pd.Timestamp("2026-01-27 09:20:00", tz=tz_kolkata)

        fri_bars = pd.date_range("2026-01-23 15:26:00", "2026-01-23 15:29:00", freq="min", tz=tz_kolkata)
        tue_bars = pd.date_range("2026-01-27 09:15:00", "2026-01-27 09:20:00", freq="min", tz=tz_kolkata)
        combined_idx = fri_bars.append(tue_bars)  # ZERO candles on Jan 26!

        df = pd.DataFrame(
            [{"Open": 100.0, "High": 101.0, "Low": 99.5, "Close": 100.5}] * len(combined_idx),
            index=combined_idx,
        )

        missing = validate_blackout_continuity(
            blackout_bars=df,
            last_processed_ts=t_fri_close_prev,
            expected_latest_ts=t_tue_open_curr,
            exchange="NSE",
        )
        self.assertEqual(missing, [], "Market holiday must NOT trigger a false data-gap alert!")

    def test_continuity_genuine_missing_candle_during_session_detected(self):
        """Exchange Calendar: Genuine missing candle within regular trading session is strictly flagged."""
        tz_kolkata = ZoneInfo("Asia/Kolkata")
        last_processed = pd.Timestamp("2026-11-25 10:00:00", tz=tz_kolkata)
        expected_latest = pd.Timestamp("2026-11-25 10:05:00", tz=tz_kolkata)

        # Expected: 10:01, 10:02, 10:03, 10:04, 10:05. Received: 10:01, 10:02, 10:04, 10:05 (10:03 missing!)
        t_1001 = pd.Timestamp("2026-11-25 10:01:00", tz=tz_kolkata)
        t_1002 = pd.Timestamp("2026-11-25 10:02:00", tz=tz_kolkata)
        t_1004 = pd.Timestamp("2026-11-25 10:04:00", tz=tz_kolkata)
        t_1005 = pd.Timestamp("2026-11-25 10:05:00", tz=tz_kolkata)
        df = pd.DataFrame(
            [{"Open": 100.0, "High": 101.0, "Low": 99.5, "Close": 100.5}] * 4,
            index=[t_1001, t_1002, t_1004, t_1005],
        )

        missing = validate_blackout_continuity(
            blackout_bars=df,
            last_processed_ts=last_processed,
            expected_latest_ts=expected_latest,
            exchange="NSE",
        )
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0], pd.Timestamp("2026-11-25 10:03:00", tz=tz_kolkata))

        recovery = evaluate_stale_recovery(
            in_trade=True,
            latest_bar=df.iloc[-1],
            trigger=102.0,
            sl=99.0,
            tp=105.0,
            blackout_bars=df,
            last_processed_ts=last_processed,
            expected_latest_ts=expected_latest,
            exchange="NSE",
        )
        self.assertEqual(recovery["action"], "RECOVERY_DATA_INCOMPLETE")
        self.assertEqual(recovery["missing_count"], 1)

    def test_continuity_explicit_expected_range_used_directly(self):
        """Auditor Requirement #4: Explicitly supplied expected_range is used directly."""
        tz_kolkata = ZoneInfo("Asia/Kolkata")
        explicit_range = pd.date_range("2026-11-25 11:00:00", "2026-11-25 11:03:00", freq="min", tz=tz_kolkata)
        # Supplied bars missing 11:02
        received_idx = pd.DatetimeIndex(
            [
                pd.Timestamp("2026-11-25 11:00:00", tz=tz_kolkata),
                pd.Timestamp("2026-11-25 11:01:00", tz=tz_kolkata),
                pd.Timestamp("2026-11-25 11:03:00", tz=tz_kolkata),
            ]
        )
        df = pd.DataFrame([{"Open": 100.0, "High": 101.0, "Low": 99.5, "Close": 100.5}] * 3, index=received_idx)

        missing = validate_blackout_continuity(
            blackout_bars=df,
            expected_range=explicit_range,
        )
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0], pd.Timestamp("2026-11-25 11:02:00", tz=tz_kolkata))


if __name__ == "__main__":
    unittest.main()



