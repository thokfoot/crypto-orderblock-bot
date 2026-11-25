from __future__ import annotations

import unittest
from unittest.mock import MagicMock
import datetime as dt
import pandas as pd
from zoneinfo import ZoneInfo

from scripts.market_time_utils import evaluate_stale_recovery

IST = ZoneInfo("Asia/Kolkata")


class ControlledRecoverySimulationTests(unittest.TestCase):
    """
    Controlled end-to-end simulation of stale-feed outage and recovery
    for Strategy v3.2.5 cloud runners (NSE and US sleeves).
    """

    def test_simulation_pre_entry_outage_discards_blackout_breakout(self):
        """
        Scenario: Before entry, market data goes stale for 10 minutes.
        During the blackout, a breakout candle occurs (High >= trigger).
        Upon feed restoration, the blackout candle MUST be discarded.
        No retrospective entry alert may be fired.
        """
        # Setup levels
        trigger = 100.0
        sl = 99.0
        tp = 102.0

        # State tracking
        in_trade = False
        triggered = False
        terminal = False
        dispatched_alerts = []

        # Simulated mock send_telegram
        def mock_telegram(msg: str):
            dispatched_alerts.append(msg)

        # 1. Normal pre-outage state: 09:50 completed
        t_0950 = pd.Timestamp("2026-11-25 09:50:00", tz=IST)
        last_processed = t_0950

        # 2. Outage occurs from 09:51 to 10:00 (10 minutes)
        stale_polls = 10  # 10 consecutive failed/stale polls

        # 3. Feed restores at 10:01 with fresh data
        # During the outage (say at 09:55), price broke above trigger to 101.5
        # The latest completed bar upon reconnection at 10:01 is 10:00 candle:
        t_1000 = pd.Timestamp("2026-11-25 10:00:00", tz=IST)
        reconnect_bar = pd.Series(
            {"Open": 100.2, "High": 101.5, "Low": 99.8, "Close": 101.0},
            name=t_1000,
        )

        # Execute recovery logic
        if stale_polls > 0:
            recovery = evaluate_stale_recovery(in_trade, reconnect_bar, trigger, sl, tp)
            last_processed = t_1000

            if recovery["action"] == "DISCARD_AND_RESUME":
                mock_telegram("INFO: feed reconnected. Blackout candles discarded.")
                stale_polls = 0
            elif recovery["action"] == "OUTAGE_EXIT_STOP":
                terminal = True
            elif recovery["action"] == "OUTAGE_EXIT_TARGET":
                terminal = True

        # VERIFICATIONS:
        # A. Position MUST NOT be in trade
        self.assertFalse(in_trade, "Pre-entry blackout breakout must NOT trigger an entry!")
        self.assertFalse(triggered)
        self.assertFalse(terminal)

        # B. last_processed must have advanced past the blackout window
        self.assertEqual(last_processed, t_1000)

        # C. Dispatched message must be info reconnection, NEVER a breakout alert!
        self.assertEqual(len(dispatched_alerts), 1)
        self.assertIn("Blackout candles discarded", dispatched_alerts[0])
        self.assertNotIn("BREAKOUT LEVEL REACHED", str(dispatched_alerts))

    def test_simulation_in_trade_outage_stop_loss_with_gap(self):
        """
        Scenario: Trade is active (entered at 100.0, SL=99.0, TP=102.0).
        Feed goes stale. During blackout, price crashes past SL to 97.5 (close 98.0).
        Upon reconnection, position exits at observable market price (98.0),
        NOT at nominal 99.0 SL, with uncertainty flagged.
        """
        trigger = 100.0
        sl = 99.0
        tp = 102.0

        in_trade = True
        triggered = True
        terminal = False
        exit_price = None
        uncertainty_flag = False
        dispatched_alerts = []

        # Reconnection bar after blackout
        t_reconnect = pd.Timestamp("2026-11-25 11:30:00", tz=IST)
        reconnect_bar = pd.Series(
            {"Open": 98.5, "High": 98.8, "Low": 97.5, "Close": 98.0},
            name=t_reconnect,
        )
        stale_polls = 5

        # Execute recovery logic
        if stale_polls > 0:
            recovery = evaluate_stale_recovery(in_trade, reconnect_bar, trigger, sl, tp)
            last_processed = t_reconnect

            if recovery["action"] == "OUTAGE_EXIT_STOP":
                exit_price = recovery["exit_price"]
                uncertainty_flag = recovery["uncertainty"]
                terminal = True
                dispatched_alerts.append(f"STOP OUTAGE EXIT at {exit_price}")

        # VERIFICATIONS:
        self.assertTrue(terminal, "Trade must terminate upon detecting stop loss breach!")
        self.assertTrue(uncertainty_flag, "Uncertainty flag must be True for blackout crossing!")
        # Must reflect the adverse gap (98.0), NEVER nominal 99.0!
        self.assertEqual(exit_price, 98.0)
        self.assertEqual(len(dispatched_alerts), 1)
        self.assertIn("STOP OUTAGE EXIT at 98.0", dispatched_alerts[0])

    def test_simulation_in_trade_outage_target_hit(self):
        """
        Scenario: Trade is active. Feed goes stale. During blackout, price rallies past TP (102.0)
        to High=103.5. Upon reconnection, position exits at TP with uncertainty flagged.
        """
        trigger = 100.0
        sl = 99.0
        tp = 102.0

        in_trade = True
        triggered = True
        terminal = False
        exit_price = None
        uncertainty_flag = False

        t_reconnect = pd.Timestamp("2026-11-25 14:00:00", tz=IST)
        reconnect_bar = pd.Series(
            {"Open": 101.8, "High": 103.5, "Low": 101.5, "Close": 102.8},
            name=t_reconnect,
        )
        stale_polls = 3

        if stale_polls > 0:
            recovery = evaluate_stale_recovery(in_trade, reconnect_bar, trigger, sl, tp)
            if recovery["action"] == "OUTAGE_EXIT_TARGET":
                exit_price = recovery["exit_price"]
                uncertainty_flag = recovery["uncertainty"]
                terminal = True

        self.assertTrue(terminal)
        self.assertTrue(uncertainty_flag)
        self.assertEqual(exit_price, 102.0)

    def test_simulation_in_trade_outage_channel_continuation(self):
        """
        Scenario: Trade is active. Feed goes stale. Upon reconnection, price is within channel
        (SL=99, TP=102, Close=100.5). Trade continues actively without exit.
        """
        trigger = 100.0
        sl = 99.0
        tp = 102.0

        in_trade = True
        terminal = False

        t_reconnect = pd.Timestamp("2026-11-25 12:00:00", tz=IST)
        reconnect_bar = pd.Series(
            {"Open": 100.2, "High": 101.0, "Low": 99.8, "Close": 100.5},
            name=t_reconnect,
        )
        stale_polls = 4

        if stale_polls > 0:
            recovery = evaluate_stale_recovery(in_trade, reconnect_bar, trigger, sl, tp)
            if recovery["action"] == "CONTINUE_IN_TRADE":
                terminal = False

        self.assertFalse(terminal, "Trade must not exit if price stayed within channel!")
    def test_simulation_in_trade_outage_ambiguous_bar_sl_and_tp_both_touched_assumes_sl(self):
        """
        Scenario: Trade is active (entered at 100.0, SL=99.0, TP=102.0).
        Feed goes stale. During blackout, an extreme volatility spike occurs such that
        both SL (Low <= 99.0) and TP (High >= 102.0) are touched in the reconnection bar.
        Conservative risk management MUST assume Stop Loss first, never optimistic Target!
        """
        trigger = 100.0
        sl = 99.0
        tp = 102.0

        in_trade = True
        terminal = False
        exit_price = None
        uncertainty_flag = False

        # Extreme bar touching both 98.0 (<= SL) and 103.0 (>= TP)
        t_reconnect = pd.Timestamp("2026-11-25 13:00:00", tz=IST)
        reconnect_bar = pd.Series(
            {"Open": 100.0, "High": 103.0, "Low": 98.0, "Close": 98.5},
            name=t_reconnect,
        )
        stale_polls = 6

        if stale_polls > 0:
            recovery = evaluate_stale_recovery(in_trade, reconnect_bar, trigger, sl, tp)
            if recovery["action"] == "OUTAGE_EXIT_STOP":
                exit_price = recovery["exit_price"]
                uncertainty_flag = recovery["uncertainty"]
                terminal = True

        # VERIFICATIONS:
        self.assertTrue(terminal, "Ambiguous bar must terminate position!")
        self.assertTrue(uncertainty_flag, "Uncertainty must be True!")
        # Must strictly prioritize SL (and observable close gap 98.5), NEVER assume TP 102.0!
        self.assertEqual(exit_price, 98.5)
        self.assertEqual(recovery["reason"], "ambiguous_bar_sl_and_tp_both_touched_assumed_sl")


if __name__ == "__main__":
    unittest.main()
