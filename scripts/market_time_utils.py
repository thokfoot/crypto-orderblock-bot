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


def evaluate_stale_recovery(
    in_trade: bool,
    latest_bar: pd.Series,
    trigger: float,
    sl: float,
    tp: float,
) -> dict:
    """
    Evaluate market state after recovering from a stale data feed outage.
    
    Rules:
    1. Pre-entry (in_trade=False):
       Blackout candles formed during the outage are strictly discarded to prevent
       retrospective backfilling of entries. Returns action='DISCARD_AND_RESUME'.
    2. In-trade (in_trade=True):
       Evaluates actual observable market price on the reconnection bar.
       If price breached SL or TP during the outage, records an outage exit at the
       observable market price (conservative min(sl, close) for stop loss) with
       uncertainty provenance, rather than assuming an exact synthetic fill.
    """
    latest_c = float(latest_bar["Close"])
    latest_h = float(latest_bar["High"])
    latest_l = float(latest_bar["Low"])

    if not in_trade:
        return {
            "action": "DISCARD_AND_RESUME",
            "triggered": False,
            "terminal": False,
            "exit_price": None,
            "uncertainty": False,
            "reason": "anti_backfill_discard",
        }

    # In-trade evaluation
    # If both SL and TP are touched in the same recovery bar, conservative risk management
    # strictly prioritizes SL first (worst-case execution) to eliminate optimistic bias.
    if latest_l <= sl:
        exit_price = min(sl, latest_c)  # Conservative: accounts for potential adverse gap-down
        reason = "ambiguous_bar_sl_and_tp_both_touched_assumed_sl" if latest_h >= tp else "stop_crossed_during_outage"
        return {
            "action": "OUTAGE_EXIT_STOP",
            "triggered": True,
            "terminal": True,
            "exit_price": exit_price,
            "uncertainty": True,
            "reason": reason,
        }
    elif latest_h >= tp:
        return {
            "action": "OUTAGE_EXIT_TARGET",
            "triggered": True,
            "terminal": True,
            "exit_price": tp,
            "uncertainty": True,
            "reason": "target_reached_during_outage",
        }
    else:
        return {
            "action": "CONTINUE_IN_TRADE",
            "triggered": True,
            "terminal": False,
            "exit_price": None,
            "uncertainty": False,
            "current_price": latest_c,
            "reason": "trade_active_within_channel",
        }

