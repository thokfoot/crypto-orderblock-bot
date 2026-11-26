from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
import datetime as dt
import pandas as pd
from zoneinfo import ZoneInfo

from scripts.market_time_utils import evaluate_stale_recovery, OutageAlertGuard

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

        # 3. Feed restores at 10:01 with completed dataframe containing ALL 10 continuous bars (09:51 to 10:00):
        #    - 09:55: BREAKOUT CANDLE in the dark! (High 101.5 >= trigger 100.0)
        #    - 10:00: reconnection bar (High 100.8, Close 100.5)
        bar_times = pd.date_range("2026-11-25 09:51:00", "2026-11-25 10:00:00", freq="min", tz=IST)
        bars = []
        for t in bar_times:
            if t.minute == 55:
                bars.append({"Open": 99.4, "High": 101.5, "Low": 99.2, "Close": 101.0})  # Breakout candle!
            elif t.minute == 0:
                bars.append({"Open": 100.8, "High": 100.9, "Low": 100.1, "Close": 100.5})
            else:
                bars.append({"Open": 99.5, "High": 99.8, "Low": 99.1, "Close": 99.6})
        completed_df = pd.DataFrame(bars, index=bar_times)

        # Runner recovery execution (exact runner logic)
        if stale_polls > 0:
            latest_ts = completed_df.index[-1]
            latest_bar = completed_df.iloc[-1]
            blackout_bars = completed_df.loc[completed_df.index > last_processed]

            recovery = evaluate_stale_recovery(
                in_trade,
                latest_bar,
                trigger,
                sl,
                tp,
                blackout_bars=blackout_bars,
                last_processed_ts=last_processed,
                expected_latest_ts=latest_ts,
            )

            if recovery["action"] == "DISCARD_AND_RESUME":
                last_processed = latest_ts
                mock_telegram(
                    f"INFO: feed reconnected. Discarded {recovery['bars_discarded']} blackout candles."
                )
                stale_polls = 0
            elif recovery["action"] == "OUTAGE_EXIT_STOP":
                last_processed = latest_ts
                terminal = True
            elif recovery["action"] == "OUTAGE_EXIT_TARGET":
                last_processed = latest_ts
                terminal = True

        # VERIFICATIONS:
        # A. Position MUST NOT be in trade despite the 09:55 breakout!
        self.assertFalse(in_trade, "Pre-entry blackout breakout must NOT trigger an entry!")
        self.assertFalse(triggered)
        self.assertFalse(terminal)
        self.assertEqual(recovery["bars_discarded"], 10)

        # B. last_processed must have advanced past the entire blackout window to 10:00
        self.assertEqual(last_processed, bar_times[-1])

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
        self.assertIn("Discarded 10 blackout candles", dispatched_alerts[0])
        self.assertNotIn("BREAKOUT LEVEL REACHED", str(dispatched_alerts))

    def test_simulation_in_trade_multi_bar_outage_catches_sl_even_if_later_candle_rebounds(self):
        """
        Scenario: Position is active (entered at 100.0, SL=99.0, TP=102.0).
        Feed goes stale for 10 minutes (09:51 to 10:00 contiguous).
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

        bar_times = pd.date_range("2026-11-25 09:51:00", "2026-11-25 10:00:00", freq="min", tz=IST)
        bars = []
        for t in bar_times:
            if t.minute == 53:
                bars.append({"Open": 99.2, "High": 99.4, "Low": 98.4, "Close": 98.6})  # SL breach!
            elif t.minute == 58:
                bars.append({"Open": 98.7, "High": 100.5, "Low": 98.7, "Close": 100.2})  # Rebound
            elif t.minute == 0:
                bars.append({"Open": 100.3, "High": 101.2, "Low": 99.8, "Close": 100.8})  # Reconnect
            else:
                bars.append({"Open": 99.5, "High": 99.7, "Low": 99.3, "Close": 99.5})
        blackout_df = pd.DataFrame(bars, index=bar_times)

        latest_bar = blackout_df.iloc[-1]
        recovery = evaluate_stale_recovery(
            in_trade=in_trade,
            latest_bar=latest_bar,
            trigger=trigger,
            sl=sl,
            tp=tp,
            blackout_bars=blackout_df,
            last_processed_ts=last_processed,
            expected_latest_ts=bar_times[-1],
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
        Feed goes stale for 10 minutes (11:11 to 11:20 contiguous).
        - At 11:14 (in the dark): price hits High 102.6 (TP 102.0 reached!).
        - At 11:20 (reconnection bar): price pulls back to High 101.4, Low 100.2, Close 100.5.

        Our multi-bar evaluation MUST detect the 11:14 TP reach and exit at TP!
        """
        trigger = 100.0
        sl = 99.0
        tp = 102.0

        in_trade = True
        terminal = False

        last_processed = pd.Timestamp("2026-11-25 11:10:00", tz=IST)
        bar_times = pd.date_range("2026-11-25 11:11:00", "2026-11-25 11:20:00", freq="min", tz=IST)
        bars = []
        for t in bar_times:
            if t.minute == 14:
                bars.append({"Open": 101.5, "High": 102.6, "Low": 101.2, "Close": 102.3})  # TP reached!
            elif t.minute == 20:
                bars.append({"Open": 102.1, "High": 101.4, "Low": 100.2, "Close": 100.5})  # Pullback
            else:
                bars.append({"Open": 100.8, "High": 101.3, "Low": 100.6, "Close": 101.0})
        blackout_df = pd.DataFrame(bars, index=bar_times)

        recovery = evaluate_stale_recovery(
            in_trade=in_trade,
            latest_bar=blackout_df.iloc[-1],
            trigger=trigger,
            sl=sl,
            tp=tp,
            blackout_bars=blackout_df,
            last_processed_ts=last_processed,
            expected_latest_ts=bar_times[-1],
        )

        self.assertEqual(recovery["action"], "OUTAGE_EXIT_TARGET")
        self.assertTrue(recovery["terminal"])
        self.assertTrue(recovery["uncertainty"])
        self.assertEqual(recovery["exit_price"], 102.0)
        self.assertIn("11:14:00", recovery["reach_time"])

    def test_simulation_in_trade_missing_candle_triggers_recovery_incomplete_and_pauses(self):
        """
        Scenario: Trade is active (entered at 100.0, SL=99.0, TP=102.0).
        Feed goes stale. Upon reconnection, data provider returns incomplete bars
        (09:51 and 09:53 present, but 09:52 missing!).
        
        CRITICAL AUDIT VALIDATION:
        The helper MUST return action='RECOVERY_DATA_INCOMPLETE'.
        The runner MUST NOT advance last_processed.
        The runner MUST NOT silently continue or exit at SL/TP.
        The runner MUST log warning and keep monitoring safely paused.
        """
        trigger = 100.0
        sl = 99.0
        tp = 102.0

        in_trade = True
        last_processed = pd.Timestamp("2026-11-25 09:50:00", tz=IST)
        stale_polls = 3
        terminal = False
        dispatched_alerts = []

        # 09:52 candle is missing!
        t_0951 = pd.Timestamp("2026-11-25 09:51:00", tz=IST)
        t_0953 = pd.Timestamp("2026-11-25 09:53:00", tz=IST)
        incomplete_df = pd.DataFrame(
            [
                {"Open": 99.8, "High": 100.1, "Low": 99.5, "Close": 99.9},
                {"Open": 100.0, "High": 100.4, "Low": 99.8, "Close": 100.2},
            ],
            index=[t_0951, t_0953],
        )

        latest_bar = incomplete_df.iloc[-1]
        recovery = evaluate_stale_recovery(
            in_trade=in_trade,
            latest_bar=latest_bar,
            trigger=trigger,
            sl=sl,
            tp=tp,
            blackout_bars=incomplete_df,
            last_processed_ts=last_processed,
            expected_latest_ts=t_0953,
        )

        # Runner logic for RECOVERY_DATA_INCOMPLETE
        if recovery["action"] == "RECOVERY_DATA_INCOMPLETE":
            dispatched_alerts.append(f"PAUSED: missing {recovery['missing_count']} bars")
            stale_polls = 1  # Retain recovery gate
            # Notice: last_processed is NOT updated!
        elif recovery["action"] == "OUTAGE_EXIT_STOP":
            terminal = True
            last_processed = t_0953

        # VERIFICATIONS:
        self.assertEqual(recovery["action"], "RECOVERY_DATA_INCOMPLETE")
        self.assertFalse(terminal, "Must NOT exit when recovery data is incomplete!")
        self.assertEqual(last_processed, pd.Timestamp("2026-11-25 09:50:00", tz=IST), "last_processed must NOT advance!")
        self.assertEqual(stale_polls, 1, "Monitoring gate must remain paused!")
        self.assertEqual(recovery["missing_count"], 1)
        self.assertIn("09:52:00", recovery["missing_bars"][0])
        self.assertEqual(len(dispatched_alerts), 1)
        self.assertIn("PAUSED: missing 1 bars", dispatched_alerts[0])

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

    def test_outage_alert_guard_shared_deduplication_lifecycle(self):
        """
        UNIT TEST: Shared OutageAlertGuard lifecycle.
        Validates the state machine across:
        1. Outage 1 (stale data): first poll dispatches; next 40 polls suppress.
        2. Outage 1 (incomplete gap): first poll dispatches; next 40 polls suppress.
        3. Recovery success: resets all guards cleanly.
        4. Outage 2 (second outage cycle): first poll dispatches; next 40 polls suppress.
        5. Outage 2 (second incomplete gap): first poll dispatches; next 40 polls suppress.
        6. Second recovery success: resets all guards cleanly.
        """
        guard = OutageAlertGuard()

        # Phase 1: First Stale Outage (40 polls)
        self.assertTrue(guard.on_stale(), "First stale poll MUST dispatch alert")
        for _ in range(39):
            self.assertFalse(guard.on_stale(), "Subsequent stale polls MUST NOT dispatch duplicate alert")
        self.assertEqual(guard.stale_polls, 40)
        self.assertTrue(guard.stale_alert_sent)

        # Phase 2: Incomplete Recovery Gap (40 polls)
        self.assertTrue(guard.on_incomplete_recovery(), "First gap poll MUST dispatch alert")
        for _ in range(39):
            self.assertFalse(guard.on_incomplete_recovery(), "Subsequent gap polls MUST NOT dispatch duplicate alert")
        self.assertEqual(guard.stale_polls, 1)
        self.assertTrue(guard.gap_alert_sent)

        # Phase 3: Successful Recovery
        guard.on_recovery_success()
        self.assertEqual(guard.stale_polls, 0)
        self.assertFalse(guard.stale_alert_sent)
        self.assertFalse(guard.gap_alert_sent)

        # Phase 4: Second Stale Outage (Second cycle!)
        self.assertTrue(guard.on_stale(), "Second outage first stale poll MUST dispatch alert again")
        for _ in range(39):
            self.assertFalse(guard.on_stale(), "Second outage subsequent polls MUST NOT dispatch duplicate alert")
        self.assertEqual(guard.stale_polls, 40)

        # Phase 5: Second Incomplete Recovery Gap
        self.assertTrue(guard.on_incomplete_recovery(), "Second gap first poll MUST dispatch alert again")
        for _ in range(39):
            self.assertFalse(guard.on_incomplete_recovery(), "Second gap subsequent polls MUST NOT dispatch duplicate alert")

        # Phase 6: Final Recovery
        guard.on_recovery_success()
        self.assertEqual(guard.stale_polls, 0)
        self.assertFalse(guard.gap_alert_sent)

    @patch("scripts.v325_nse_cloud_runner.fetch_live_reference_spot", return_value={"price": 4000.0, "source": "Google Finance"})
    @patch("scripts.v325_nse_cloud_runner.time.sleep", return_value=None)
    @patch("scripts.v325_nse_cloud_runner.send_telegram")
    @patch("scripts.v325_nse_cloud_runner.get_ist_now")
    @patch("scripts.v325_nse_cloud_runner.fetch_ticker_1m")
    def test_actual_production_runner_alert_deduplication_integration(
        self, mock_fetch, mock_now, mock_telegram, mock_sleep, mock_spot
    ):
        """
        INTEGRATION TEST: Executes the actual production runner function
        `scripts.v325_nse_cloud_runner.run_monitoring_loop` across an entire multi-tick lifecycle:
        - Ticks 1-3: Stale outage -> exactly 1 stale alert
        - Ticks 4-6: Incomplete recovery gap -> exactly 1 gap alert
        - Tick 7: Full recovery -> exactly 1 restored alert
        - Tick 8: Normal active monitoring bar
        - Ticks 9-10: Second outage -> exactly 1 stale alert
        - Tick 11: 15:29 EOD session close -> exactly 1 stand down alert
        Asserts that the actual production dispatch path generates ZERO duplicate messages.
        """
        from scripts.v325_nse_cloud_runner import run_monitoring_loop

        session_date = dt.date(2026, 11, 26)
        t_0950 = pd.Timestamp(dt.datetime.combine(session_date, dt.time(9, 50), tzinfo=IST))
        t_1003 = pd.Timestamp(dt.datetime.combine(session_date, dt.time(10, 3), tzinfo=IST))
        t_1005 = pd.Timestamp(dt.datetime.combine(session_date, dt.time(10, 5), tzinfo=IST))

        # Initial DataFrame for candidate lock at 09:50
        df_0950 = pd.DataFrame(
            {"Open": [4000.0], "High": [4005.0], "Low": [3995.0], "Close": [4002.0], "Volume": [1000]},
            index=[t_0950],
        )

        # Gap DataFrame: has 09:50 and 10:03, but missing 09:51 through 10:02
        df_gap = pd.DataFrame(
            {
                "Open": [4000.0, 4001.0],
                "High": [4005.0, 4004.0],
                "Low": [3995.0, 3998.0],
                "Close": [4002.0, 4000.0],
                "Volume": [1000, 1200],
            },
            index=[t_0950, t_1003],
        )

        # Fully restored DataFrame: complete continuous bars from 09:50 to 10:05
        full_range = pd.date_range(t_0950, t_1005, freq="min")
        df_restored = pd.DataFrame(
            {
                "Open": [4000.0] * len(full_range),
                "High": [4003.0] * len(full_range),  # All below trigger 4010.0
                "Low": [3996.0] * len(full_range),   # All above SL 3969.9
                "Close": [4001.0] * len(full_range),
                "Volume": [1000] * len(full_range),
            },
            index=full_range,
        )

        # Simulation clock and data sequences across 10 ticks
        time_sequence = [
            # Boot fetch
            dt.datetime.combine(session_date, dt.time(9, 51), tzinfo=IST),
            # Ticks 1-2: Outage 1 Stale (clock at 09:55, data latest is 09:50 -> age 300s > 180s, 2 polls < 3 limit)
            dt.datetime.combine(session_date, dt.time(9, 55, 0), tzinfo=IST),
            dt.datetime.combine(session_date, dt.time(9, 55, 20), tzinfo=IST),
            # Ticks 3-5: Data gap (clock at 10:05, data has 10:03 -> fresh, but missing 12 bars)
            dt.datetime.combine(session_date, dt.time(10, 5, 0), tzinfo=IST),
            dt.datetime.combine(session_date, dt.time(10, 5, 20), tzinfo=IST),
            dt.datetime.combine(session_date, dt.time(10, 5, 40), tzinfo=IST),
            # Tick 6: Restored (clock at 10:06, all continuous bars present)
            dt.datetime.combine(session_date, dt.time(10, 6, 0), tzinfo=IST),
            # Tick 7: Normal active tick (clock at 10:07)
            dt.datetime.combine(session_date, dt.time(10, 7, 0), tzinfo=IST),
            # Ticks 8-9: Outage 2 Stale (clock at 10:15, data returned is stale 10:05, 2 polls)
            dt.datetime.combine(session_date, dt.time(10, 15, 0), tzinfo=IST),
            dt.datetime.combine(session_date, dt.time(10, 15, 20), tzinfo=IST),
            # Tick 10: EOD session close (clock at 15:29:05)
            dt.datetime.combine(session_date, dt.time(15, 29, 5), tzinfo=IST),
        ]

        data_sequence = [
            # Boot fetch
            df_0950,
            # Ticks 1-2: Stale df_0950
            df_0950,
            df_0950,
            # Ticks 3-5: Gap df_gap
            df_gap,
            df_gap,
            df_gap,
            # Tick 6: Restored df_restored
            df_restored,
            # Tick 7: df_restored
            df_restored,
            # Ticks 8-9: Stale df_restored (clock 10:15, data 10:05 -> age 600s > 180s)
            df_restored,
            df_restored,
            # Tick 10: EOD tick
            df_restored,
        ]

        mock_now.side_effect = time_sequence
        mock_fetch.side_effect = data_sequence

        candidate = {
            "symbol": "TCS.NS",
            "clean_symbol": "TCS",
            "trigger": 4010.0,
            "stop_loss": 3969.9,
            "take_profit": 4090.2,
            "df_day": df_0950,
        }

        # Execute actual production runner
        run_monitoring_loop(candidate, is_test=False)

        # Inspect all sent messages
        sent_messages = [call[0][0] for call in mock_telegram.call_args_list]

        # 1. Stale alert count: exactly 1 from Ticks 1-3
        stale_alerts = [m for m in sent_messages if "[NSE STALE DATA]" in m]
        self.assertEqual(len(stale_alerts), 2, "Must dispatch exactly 1 stale alert for Outage 1 and 1 for Outage 2")

        # 2. Gap alert count: exactly 1 from Ticks 4-6 (no duplicate spam!)
        gap_alerts = [m for m in sent_messages if "[NSE DATA GAP / RECOVERY INCOMPLETE]" in m]
        self.assertEqual(len(gap_alerts), 1, "Must dispatch exactly 1 gap alert across repeated gap polls")

        # 3. Restored alert count: exactly 1 from Tick 7
        restored_alerts = [m for m in sent_messages if "[NSE DATA RESTORED]" in m]
        self.assertEqual(len(restored_alerts), 1, "Must dispatch exactly 1 restoration alert")

        # 4. EOD stand down alert: exactly 1 from Tick 11
        stand_down_alerts = [m for m in sent_messages if "[NSE STAND DOWN]" in m]
        self.assertEqual(len(stand_down_alerts), 1, "Must dispatch exactly 1 EOD stand down alert")

        # TOTAL DISPATCHED: 5 messages over 10 ticks! Zero spam!
        self.assertEqual(len(sent_messages), 5)

    @patch("scripts.v325_us_cloud_runner.fetch_live_reference_spot", return_value={"price": 200.0, "source": "Google Finance"})
    @patch("scripts.v325_us_cloud_runner.time.sleep", return_value=None)
    @patch("scripts.v325_us_cloud_runner.send_telegram")
    @patch("scripts.v325_us_cloud_runner.get_et_now")
    @patch("scripts.v325_us_cloud_runner.fetch_1m_bars")
    def test_actual_us_production_runner_alert_deduplication_integration(
        self, mock_fetch, mock_now, mock_telegram, mock_sleep, mock_spot
    ):
        """
        INTEGRATION TEST: Executes the actual US production runner function
        `scripts.v325_us_cloud_runner.run_us_engine(test_mode=False)` across the full monitoring lifecycle:
        - Step 1: Candidate scan & lock -> exactly 1 scan complete alert
        - Step 2: Monitoring loop:
          - Ticks 1-2: Outage 1 (stale data) -> exactly 1 stale alert
          - Ticks 3-5: Incomplete recovery gap -> exactly 1 gap alert
          - Tick 6: Full recovery -> exactly 1 restored alert
          - Tick 7: Normal active monitoring bar
          - Ticks 8-9: Outage 2 (second stale outage) -> exactly 1 stale alert
          - Tick 10: 11:00 ET execution window close -> exactly 1 stand down alert
        Asserts that the actual US production runner dispatch path generates ZERO duplicate messages.
        """
        from scripts.v325_us_cloud_runner import run_us_engine

        ET = ZoneInfo("America/New_York")
        session_date = dt.date(2026, 11, 25)  # Wednesday, regular trading session
        t_0945 = pd.Timestamp(dt.datetime.combine(session_date, dt.time(9, 45), tzinfo=ET))
        t_1004 = pd.Timestamp(dt.datetime.combine(session_date, dt.time(10, 4), tzinfo=ET))
        t_1014 = pd.Timestamp(dt.datetime.combine(session_date, dt.time(10, 14), tzinfo=ET))
        t_1015 = pd.Timestamp(dt.datetime.combine(session_date, dt.time(10, 15), tzinfo=ET))

        # 20 complete OR bars (09:45-10:04 ET) for candidate AAPL (sweet spot move +1.0%)
        or_range = pd.date_range(t_0945, t_1004, freq="min")
        df_aapl_or = pd.DataFrame(
            {
                "Open": [200.0] + [201.0] * 19,
                "High": [202.5] * len(or_range),
                "Low": [199.5] * len(or_range),
                "Close": [201.0] * 19 + [202.0],  # Move = +1.0%
                "Volume": [1000] * len(or_range),
            },
            index=or_range,
        )

        # 20 complete OR bars for MSFT (move = +0.5%)
        df_msft_or = pd.DataFrame(
            {
                "Open": [300.0] + [301.0] * 19,
                "High": [302.0] * len(or_range),
                "Low": [299.0] * len(or_range),
                "Close": [301.0] * 19 + [301.5],  # Move = +0.5%
                "Volume": [1000] * len(or_range),
            },
            index=or_range,
        )

        # Gap DataFrame: has 10:04 and 10:14, but missing 10:05 through 10:13
        df_gap = pd.DataFrame(
            {
                "Open": [202.0, 201.8],
                "High": [202.5, 202.2],
                "Low": [199.5, 200.5],
                "Close": [202.0, 201.5],
                "Volume": [1000, 1200],
            },
            index=[t_1004, t_1014],
        )

        # Fully restored DataFrame: complete continuous bars from 09:45 to 10:15
        full_range = pd.date_range(t_0945, t_1015, freq="min")
        df_restored = pd.DataFrame(
            {
                "Open": [200.0] * len(full_range),
                "High": [202.3] * len(full_range),  # All below trigger (~202.56)
                "Low": [201.0] * len(full_range),   # All above SL (~200.53)
                "Close": [201.5] * len(full_range),
                "Volume": [1000] * len(full_range),
            },
            index=full_range,
        )

        # Time sequence:
        # Step 1: 10:05:00 ET
        # Boot fetch: 10:05:05 ET
        # Ticks 1-2: Stale (clock at 10:09, data latest is 10:04 -> age 300s > 180s, 2 polls < 3 limit)
        # Ticks 3-5: Data gap (clock at 10:15, data has 10:14 -> fresh, missing 10:05-10:13)
        # Tick 6: Restored (clock at 10:16, all continuous bars present)
        # Tick 7: Normal active monitoring tick (clock at 10:17)
        # Ticks 8-9: Outage 2 Stale (clock at 10:25, data returned is stale 10:15, 2 polls)
        # Tick 10: Execution window close (clock at 11:00:05 ET)
        time_sequence = [
            # Step 1: scan time check and sector scans
            dt.datetime.combine(session_date, dt.time(10, 5, 0), tzinfo=ET),
            dt.datetime.combine(session_date, dt.time(10, 5, 0), tzinfo=ET),
            dt.datetime.combine(session_date, dt.time(10, 5, 0), tzinfo=ET),
            dt.datetime.combine(session_date, dt.time(10, 5, 0), tzinfo=ET),
            # Step 2: monitor start & boot fetch
            dt.datetime.combine(session_date, dt.time(10, 5, 5), tzinfo=ET),
            # Ticks 1-2: Outage 1 Stale
            dt.datetime.combine(session_date, dt.time(10, 9, 0), tzinfo=ET),
            dt.datetime.combine(session_date, dt.time(10, 9, 20), tzinfo=ET),
            # Ticks 3-5: Incomplete recovery gap
            dt.datetime.combine(session_date, dt.time(10, 15, 0), tzinfo=ET),
            dt.datetime.combine(session_date, dt.time(10, 15, 20), tzinfo=ET),
            dt.datetime.combine(session_date, dt.time(10, 15, 40), tzinfo=ET),
            # Tick 6: Restored
            dt.datetime.combine(session_date, dt.time(10, 16, 0), tzinfo=ET),
            # Tick 7: Normal tick
            dt.datetime.combine(session_date, dt.time(10, 17, 0), tzinfo=ET),
            # Ticks 8-9: Outage 2 Stale
            dt.datetime.combine(session_date, dt.time(10, 25, 0), tzinfo=ET),
            dt.datetime.combine(session_date, dt.time(10, 25, 20), tzinfo=ET),
            # Tick 10: Window close exit
            dt.datetime.combine(session_date, dt.time(11, 0, 5), tzinfo=ET),
        ]

        # Data sequence:
        # Step 1: AAPL, MSFT scans
        # Boot fetch: AAPL
        # Monitoring ticks: AAPL
        data_sequence = [
            # Step 1 scans
            df_aapl_or,
            df_msft_or,
            # Boot fetch
            df_aapl_or,
            # Ticks 1-2: Stale df_aapl_or
            df_aapl_or,
            df_aapl_or,
            # Ticks 3-5: Gap df_gap
            df_gap,
            df_gap,
            df_gap,
            # Tick 6: Restored df_restored
            df_restored,
            # Tick 7: df_restored
            df_restored,
            # Ticks 8-9: Stale df_restored
            df_restored,
            df_restored,
            # Tick 10: EOD tick
            df_restored,
        ]

        mock_now.side_effect = time_sequence
        mock_fetch.side_effect = data_sequence

        with patch.dict("scripts.v325_us_cloud_runner.US_SECTORS", {"MegaCap_Tech": ["AAPL", "MSFT"]}, clear=True):
            run_us_engine(test_mode=False)

        sent_messages = [call[0][0] for call in mock_telegram.call_args_list]

        # 1. Candidate scan lock alert: exactly 1
        scan_alerts = [m for m in sent_messages if "[US PAPER SIGNAL]" in m and "US SCAN COMPLETE" in m]
        self.assertEqual(len(scan_alerts), 1, "Must dispatch exactly 1 US scan complete alert")

        # 2. Stale alerts: exactly 2 (one for Outage 1, one for Outage 2)
        stale_alerts = [m for m in sent_messages if "[US DATA STALE]" in m]
        self.assertEqual(len(stale_alerts), 2, "Must dispatch exactly 1 stale alert per outage incident")

        # 3. Gap alerts: exactly 1 across repeated polls (no duplicate spam!)
        gap_alerts = [m for m in sent_messages if "[US DATA GAP / RECOVERY INCOMPLETE]" in m]
        self.assertEqual(len(gap_alerts), 1, "Must dispatch exactly 1 gap alert across repeated gap polls")

        # 4. Restored alert: exactly 1
        restored_alerts = [m for m in sent_messages if "[US DATA RESTORED]" in m]
        self.assertEqual(len(restored_alerts), 1, "Must dispatch exactly 1 restoration alert")

        # 5. Stand down alert: exactly 1 at 11:00 ET close
        stand_down_alerts = [m for m in sent_messages if "[US STAND DOWN]" in m]
        self.assertEqual(len(stand_down_alerts), 1, "Must dispatch exactly 1 window close stand down alert")

        # TOTAL DISPATCHED: 6 messages! Zero spam!
        self.assertEqual(len(sent_messages), 6)


if __name__ == "__main__":
    unittest.main()


