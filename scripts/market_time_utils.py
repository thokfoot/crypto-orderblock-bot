from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd


def normalize_1m_frame(frame: pd.DataFrame | None, timezone_name: str) -> pd.DataFrame:
    """Return a sorted, timezone-aware 1-minute frame in the exchange timezone."""
    if frame is None or frame.empty:
        return pd.DataFrame() if frame is None else frame.copy()

    result = frame.copy()
    tz = ZoneInfo(timezone_name)
    index = pd.DatetimeIndex(result.index)
    if index.tz is None:
        # yfinance normally supplies exchange-local timestamps; if it does not,
        # treat them as exchange-local rather than silently shifting from UTC.
        index = index.tz_localize(tz)
    else:
        index = index.tz_convert(tz)
    result.index = index
    result = result[~result.index.duplicated(keep="last")].sort_index()
    return result


def completed_1m_bars(
    frame: pd.DataFrame | None,
    now: dt.datetime,
    timezone_name: str,
) -> pd.DataFrame:
    """Exclude the currently-forming minute; retain bars whose minute has closed."""
    result = normalize_1m_frame(frame, timezone_name)
    if result.empty:
        return result

    tz = ZoneInfo(timezone_name)
    local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    last_closed_bar_start = local_now.replace(second=0, microsecond=0) - dt.timedelta(minutes=1)
    return result.loc[result.index <= pd.Timestamp(last_closed_bar_start)]


def exact_minute_window(
    frame: pd.DataFrame | None,
    session_date: dt.date,
    start_hhmm: str,
    end_hhmm: str,
    timezone_name: str,
) -> pd.DataFrame | None:
    """Return a complete, contiguous, inclusive minute window, else None."""
    result = normalize_1m_frame(frame, timezone_name)
    if result.empty:
        return None

    tz = ZoneInfo(timezone_name)
    start_time = dt.time.fromisoformat(start_hhmm)
    end_time = dt.time.fromisoformat(end_hhmm)
    start = pd.Timestamp(dt.datetime.combine(session_date, start_time, tzinfo=tz))
    end = pd.Timestamp(dt.datetime.combine(session_date, end_time, tzinfo=tz))
    expected = pd.date_range(start=start, end=end, freq="min")
    actual = result.loc[(result.index >= start) & (result.index <= end)]

    if len(actual) != len(expected) or not actual.index.equals(expected):
        return None
    return actual


def is_fresh_1m_data(
    frame: pd.DataFrame | None,
    now: dt.datetime,
    timezone_name: str,
    max_age_seconds: int = 180,
) -> bool:
    """Freshness is measured from the close of the latest completed one-minute bar."""
    completed = completed_1m_bars(frame, now, timezone_name)
    if completed.empty:
        return False

    tz = ZoneInfo(timezone_name)
    local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    latest_start = completed.index[-1].to_pydatetime()
    latest_close = latest_start + dt.timedelta(minutes=1)
    age = (local_now - latest_close).total_seconds()
    return 0 <= age <= max_age_seconds


def validate_blackout_continuity(
    blackout_bars: pd.DataFrame,
    last_processed_ts: pd.Timestamp | dt.datetime | None = None,
    expected_latest_ts: pd.Timestamp | dt.datetime | None = None,
    expected_range: pd.DatetimeIndex | None = None,
) -> list[pd.Timestamp]:
    """
    Validate that blackout_bars contains every expected 1-minute bar without gaps.
    Returns a list of missing Timestamp objects (empty if data is contiguous and complete).

    Checks:
    - Missing first candle(s): bars between (last_processed_ts + 1m) and first received bar.
    - Missing middle candle(s): internal gaps within received bars.
    - Missing last candle(s): bars between last received bar and expected_latest_ts.
    """
    if blackout_bars.empty:
        if expected_range is not None:
            return list(expected_range)
        if last_processed_ts is not None and expected_latest_ts is not None:
            start_ts = pd.Timestamp(last_processed_ts) + dt.timedelta(minutes=1)
            end_ts = pd.Timestamp(expected_latest_ts)
            if start_ts <= end_ts:
                return list(pd.date_range(start=start_ts, end=end_ts, freq="min"))
        return []

    bars_idx = pd.DatetimeIndex(blackout_bars.index)
    tz = bars_idx.tz

    if expected_range is not None:
        target_range = expected_range
        if tz is not None and target_range.tz is None:
            target_range = target_range.tz_localize(tz)
        elif tz is not None and target_range.tz != tz:
            target_range = target_range.tz_convert(tz)
        return list(target_range.difference(bars_idx))

    # Derive start timestamp (first expected bar after last_processed)
    if last_processed_ts is not None:
        start_ts = pd.Timestamp(last_processed_ts)
        if tz is not None:
            if start_ts.tz is None:
                start_ts = start_ts.tz_localize(tz)
            else:
                start_ts = start_ts.tz_convert(tz)
        start_ts = start_ts + dt.timedelta(minutes=1)
    else:
        start_ts = bars_idx[0]

    # Derive end timestamp (expected latest completed bar)
    if expected_latest_ts is not None:
        end_ts = pd.Timestamp(expected_latest_ts)
        if tz is not None:
            if end_ts.tz is None:
                end_ts = end_ts.tz_localize(tz)
            else:
                end_ts = end_ts.tz_convert(tz)
    else:
        end_ts = bars_idx[-1]

    if start_ts > end_ts:
        return []

    full_expected = pd.date_range(start=start_ts, end=end_ts, freq="min")
    missing = full_expected.difference(bars_idx)
    return list(missing)


def evaluate_stale_recovery(
    in_trade: bool,
    latest_bar: pd.Series | pd.DataFrame,
    trigger: float,
    sl: float,
    tp: float,
    blackout_bars: pd.DataFrame | None = None,
    last_processed_ts: pd.Timestamp | dt.datetime | None = None,
    expected_latest_ts: pd.Timestamp | dt.datetime | None = None,
    expected_range: pd.DatetimeIndex | None = None,
    require_continuity: bool = True,
) -> dict:
    """
    Evaluate market state after recovering from a stale data feed outage.
    
    Accepts either a single latest_bar (pd.Series or pd.DataFrame) or a full
    blackout_bars DataFrame covering every 1-minute candle formed during the outage.

    Rules:
    1. Data Continuity Validation:
       Before evaluating candle prices, checks whether any expected tradable 1-minute
       candles are missing from blackout_bars. If any candle is missing (first, middle,
       or last), returns action='RECOVERY_DATA_INCOMPLETE'. The runner must NOT silently
       continue or record a confirmed exit on partial data.
    2. Pre-entry (in_trade=False):
       Blackout candles formed during the outage are strictly discarded to prevent
       retrospective backfilling of entries, even if a breakout occurred during the outage.
       Returns action='DISCARD_AND_RESUME'.
    3. In-trade (in_trade=True):
       Evaluates every candle in the outage window in chronological order:
       - If a candle touches SL (or touches both SL and TP), stops out conservatively
         at observable market price min(sl, close), flagging execution uncertainty.
       - If a candle reaches TP without prior SL breach, exits at target with uncertainty.
       - If the price remained strictly within the [SL, TP] corridor across all blackout candles,
         returns action='CONTINUE_IN_TRADE'.
    """
    if blackout_bars is not None and not blackout_bars.empty:
        bars_df = blackout_bars.copy()
    elif isinstance(latest_bar, pd.DataFrame):
        bars_df = latest_bar.copy()
    elif isinstance(latest_bar, pd.Series):
        bars_df = pd.DataFrame([latest_bar])
    else:
        raise ValueError("Either latest_bar or blackout_bars must be provided as Series or DataFrame")

    # 1. Data Continuity Validation
    if require_continuity:
        missing_bars = validate_blackout_continuity(
            bars_df,
            last_processed_ts=last_processed_ts,
            expected_latest_ts=expected_latest_ts,
            expected_range=expected_range,
        )
        if missing_bars:
            return {
                "action": "RECOVERY_DATA_INCOMPLETE",
                "triggered": in_trade,
                "terminal": False,
                "exit_price": None,
                "uncertainty": True,
                "missing_bars": [
                    ts.strftime("%Y-%m-%d %H:%M:%S%z") if hasattr(ts, "strftime") else str(ts)
                    for ts in missing_bars
                ],
                "missing_count": len(missing_bars),
                "reason": "missing_blackout_candles_data_continuity_violation",
            }

    if not in_trade:
        return {
            "action": "DISCARD_AND_RESUME",
            "triggered": False,
            "terminal": False,
            "exit_price": None,
            "uncertainty": False,
            "reason": "anti_backfill_discard",
            "bars_discarded": len(bars_df),
        }

    latest_c = float(bars_df.iloc[-1]["Close"])

    # Chronologically evaluate every bar in the blackout window
    for ts, bar in bars_df.iterrows():
        b_high = float(bar["High"])
        b_low = float(bar["Low"])
        b_close = float(bar["Close"])

        # Stop loss check (conservative: if both SL and TP touched, assume SL first)
        if b_low <= sl:
            exit_price = min(sl, b_close)  # Conservative: accounts for potential adverse gap-down
            reason = (
                "ambiguous_bar_sl_and_tp_both_touched_assumed_sl"
                if b_high >= tp
                else "stop_crossed_during_outage"
            )
            return {
                "action": "OUTAGE_EXIT_STOP",
                "triggered": True,
                "terminal": True,
                "exit_price": exit_price,
                "uncertainty": True,
                "reason": reason,
                "breach_time": str(ts),
            }
        elif b_high >= tp:
            return {
                "action": "OUTAGE_EXIT_TARGET",
                "triggered": True,
                "terminal": True,
                "exit_price": tp,
                "uncertainty": True,
                "reason": "target_reached_during_outage",
                "reach_time": str(ts),
            }

    # Price remained within the channel across the ENTIRE blackout window
    return {
        "action": "CONTINUE_IN_TRADE",
        "triggered": True,
        "terminal": False,
        "exit_price": None,
        "uncertainty": False,
        "current_price": latest_c,
        "reason": "trade_active_within_channel",
    }

