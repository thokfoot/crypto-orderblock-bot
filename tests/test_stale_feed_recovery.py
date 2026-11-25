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


if __name__ == "__main__":
    unittest.main()
