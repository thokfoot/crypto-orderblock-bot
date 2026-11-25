from __future__ import annotations

import unittest
import pandas as pd
from scripts.market_time_utils import evaluate_stale_recovery


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


if __name__ == "__main__":
    unittest.main()


