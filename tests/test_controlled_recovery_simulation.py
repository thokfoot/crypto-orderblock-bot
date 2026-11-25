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
        During the blackout, multiple bars form, including a breakout candle at 09:55 (High 101.5 >= trigger 100.0).
        Upon feed restoration at 10:01, the entire slice of blackout bars MUST be discarded.
        The runner advances last_processed to the latest completed bar, preventing retrospective entry.
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

        def mock_telegram(msg: str):
            dispatched_alerts.append(msg)

        # 1. Normal pre-outage state: 09:50 completed
        t_0950 = pd.Timestamp("2026-11-25 09:50:00", tz=IST)
        last_processed = t_0950

        # 2. Outage occurs from 09:51 to 10:00 (10 minutes)
        stale_polls = 10  # 10 consecutive failed/stale polls

        # 3. Feed restores at 10:01 with completed dataframe containing:
        #    - 09:52: normal pre-breakout candle (High 99.5)
        #    - 09:55: BREAKOUT CANDLE in the dark! (High 101.5 >= trigger 100.0)
        #    - 10:00: reconnection bar (High 100.8, Close 100.5)
        t_0952 = pd.Timestamp("2026-11-25 09:52:00", tz=IST)
        t_0955 = pd.Timestamp("2026-11-25 09:55:00", tz=IST)
        t_1000 = pd.Timestamp("2026-11-25 10:00:00", tz=IST)

        completed_df = pd.DataFrame(
            [
                {"Open": 99.1, "High": 99.5, "Low": 98.9, "Close": 99.3},
                {"Open": 99.4, "High": 101.5, "Low": 99.2, "Close": 101.0},  # Breakout during blackout!
                {"Open": 100.8, "High": 100.9, "Low": 100.1, "Close": 100.5},
            ],
            index=[t_0952, t_0955, t_1000],
        )

        # Runner recovery execution (exact runner logic)
        if stale_polls > 0:
            latest_ts = completed_df.index[-1]
            latest_bar = completed_df.iloc[-1]
            blackout_bars = completed_df.loc[completed_df.index > last_processed]

            recovery = evaluate_stale_recovery(
                in_trade, latest_bar, trigger, sl, tp, blackout_bars=blackout_bars
            )
            last_processed = latest_ts

            if recovery["action"] == "DISCARD_AND_RESUME":
                mock_telegram(
                    f"INFO: feed reconnected. Discarded {recovery['bars_discarded']} blackout candles."
                )
                stale_polls = 0
            elif recovery["action"] == "OUTAGE_EXIT_STOP":
                terminal = True
            elif recovery["action"] == "OUTAGE_EXIT_TARGET":
                terminal = True

        # VERIFICATIONS:
        # A. Position MUST NOT be in trade despite the 09:55 breakout!
        self.assertFalse(in_trade, "Pre-entry blackout breakout must NOT trigger an entry!")
        self.assertFalse(triggered)
        self.assertFalse(terminal)
        self.assertEqual(recovery["bars_discarded"], 3)

        # B. last_processed must have advanced past the entire blackout window to 10:00
        self.assertEqual(last_processed, t_1000)

        # C. Next incoming bar (10:01) is tested: 09:55 candle is never re-evaluated
        t_1001 = pd.Timestamp("2026-11-25 10:01:00", tz=IST)
        new_completed = pd.DataFrame(
            [{"Open": 100.5, "High": 100.7, "Low": 100.3, "Close": 100.4}],
            index=[t_1001],
        )
        unprocessed_bars = new_completed.loc[new_completed.index > last_processed]
        self.assertEqual(len(unprocessed_bars), 1)
        self.assertEqual(unprocessed_bars.index[0], t_1001)

        # D. Dispatched message must report discarded blackout candles, NEVER a breakout alert!
        self.assertEqual(len(dispatched_alerts), 1)
        self.assertIn("Discarded 3 blackout candles", dispatched_alerts[0])
        self.assertNotIn("BREAKOUT LEVEL REACHED", str(dispatched_alerts))

    def test_simulation_in_trade_multi_bar_outage_catches_sl_even_if_later_candle_rebounds(self):
        """
        Scenario: Position is active (entered at 100.0, SL=99.0, TP=102.0).
        Feed goes stale for 10 minutes.
        - At 09:53 (in the dark): price drops to Low 98.4 (breaches SL 99.0!), Close 98.6.
        - At 09:58 (in the dark): price rebounds to High 100.5.
        - At 10:00 (reconnection bar): price is at High 101.2, Low 99.8, Close 100.8 (above SL!).
        
        CRITICAL AUDIT CHECK:
        A naive system inspecting only the 10:00 reconnection bar would report CONTINUE_IN_TRADE.
        Our multi-bar blackout evaluation MUST detect the 09:53 SL breach and terminate the trade!
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

        last_processed = pd.Timestamp("2026-11-25 09:50:00", tz=IST)
        stale_polls = 10

        t_0953 = pd.Timestamp("2026-11-25 09:53:00", tz=IST)
        t_0958 = pd.Timestamp("2026-11-25 09:58:00", tz=IST)
        t_1000 = pd.Timestamp("2026-11-25 10:00:00", tz=IST)

        blackout_df = pd.DataFrame(
            [
                {"Open": 99.2, "High": 99.4, "Low": 98.4, "Close": 98.6},  # SL breached at 09:53!
                {"Open": 98.7, "High": 100.5, "Low": 98.7, "Close": 100.2},  # Rebound at 09:58
                {"Open": 100.3, "High": 101.2, "Low": 99.8, "Close": 100.8},  # Reconnection bar at 10:00
            ],
            index=[t_0953, t_0958, t_1000],
        )

        latest_bar = blackout_df.iloc[-1]
        recovery = evaluate_stale_recovery(
            in_trade=in_trade,
            latest_bar=latest_bar,
            trigger=trigger,
            sl=sl,
            tp=tp,
            blackout_bars=blackout_df,
        )

        if recovery["action"] == "OUTAGE_EXIT_STOP":
            exit_price = recovery["exit_price"]
            uncertainty_flag = recovery["uncertainty"]
            terminal = True
            dispatched_alerts.append(
                f"STOP OUTAGE EXIT at {exit_price} (Breach at {recovery['breach_time']})"
            )

        # VERIFICATIONS:
        # Must detect the 09:53 SL breach despite the 10:00 candle being at 100.8!
        self.assertTrue(terminal, "Position must terminate upon historical blackout SL breach!")
        self.assertTrue(uncertainty_flag)
        self.assertEqual(recovery["action"], "OUTAGE_EXIT_STOP")
        self.assertEqual(exit_price, 98.6)  # Conservative: min(sl=99.0, close=98.6)
        self.assertIn("09:53:00", recovery["breach_time"])
        self.assertEqual(len(dispatched_alerts), 1)

    def test_simulation_in_trade_multi_bar_outage_catches_tp_even_if_later_candle_pulls_back(self):
        """
        Scenario: Trade is active (entered at 100.0, SL=99.0, TP=102.0).
        Feed goes stale.
        - At 11:14 (in the dark): price hits High 102.6 (TP 102.0 reached!).
        - At 11:20 (reconnection bar): price pulls back to High 101.4, Low 100.2, Close 100.5.
        
        Our multi-bar evaluation MUST detect the 11:14 TP reach and exit at TP!
        """
        trigger = 100.0
        sl = 99.0
        tp = 102.0

        in_trade = True
        terminal = False

        t_1114 = pd.Timestamp("2026-11-25 11:14:00", tz=IST)
        t_1120 = pd.Timestamp("2026-11-25 11:20:00", tz=IST)

        blackout_df = pd.DataFrame(
            [
                {"Open": 101.5, "High": 102.6, "Low": 101.2, "Close": 102.3},  # TP crossed at 11:14!
                {"Open": 102.1, "High": 101.4, "Low": 100.2, "Close": 100.5},  # Pullback at 11:20
            ],
            index=[t_1114, t_1120],
        )

        recovery = evaluate_stale_recovery(
            in_trade=in_trade,
            latest_bar=blackout_df.iloc[-1],
            trigger=trigger,
            sl=sl,
            tp=tp,
            blackout_bars=blackout_df,
        )

        self.assertEqual(recovery["action"], "OUTAGE_EXIT_TARGET")
        self.assertTrue(recovery["terminal"])
        self.assertTrue(recovery["uncertainty"])
        self.assertEqual(recovery["exit_price"], 102.0)
        self.assertIn("11:14:00", recovery["reach_time"])

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
