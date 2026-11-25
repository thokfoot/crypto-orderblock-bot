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


# Canonical Trading Holidays for 2026
NSE_2026_HOLIDAYS: set[dt.date] = {
    dt.date(2026, 1, 26),  # Republic Day
    dt.date(2026, 3, 3),   # Holi
    dt.date(2026, 3, 20),  # Id-ul-Fitr
    dt.date(2026, 4, 3),   # Good Friday
    dt.date(2026, 4, 14),  # Dr. Ambedkar Jayanti
    dt.date(2026, 5, 1),   # Maharashtra Day
    dt.date(2026, 5, 27),  # Bakri Id
    dt.date(2026, 6, 26),  # Muharram
    dt.date(2026, 8, 15),  # Independence Day
    dt.date(2026, 10, 2),  # Mahatma Gandhi Jayanti
    dt.date(2026, 10, 20), # Dussehra
    dt.date(2026, 11, 8),  # Diwali / Laxmi Pujan
    dt.date(2026, 11, 10), # Diwali Balipratipada
    dt.date(2026, 11, 24), # Gurunanak Jayanti
    dt.date(2026, 12, 25), # Christmas
}

US_2026_HOLIDAYS: set[dt.date] = {
    dt.date(2026, 1, 1),   # New Year's Day
    dt.date(2026, 1, 19),  # Martin Luther King Jr. Day
    dt.date(2026, 2, 16),  # Presidents' Day
    dt.date(2026, 4, 3),   # Good Friday
    dt.date(2026, 5, 25),  # Memorial Day
    dt.date(2026, 6, 19),  # Juneteenth
    dt.date(2026, 7, 3),   # Independence Day (Observed)
    dt.date(2026, 9, 7),   # Labor Day
    dt.date(2026, 11, 26), # Thanksgiving
    dt.date(2026, 12, 25), # Christmas
}


def get_exchange_expected_1m_range(
    start_ts: pd.Timestamp | dt.datetime,
    end_ts: pd.Timestamp | dt.datetime,
    exchange: str = "NSE",
    custom_holidays: set[dt.date] | None = None,
) -> pd.DatetimeIndex:
    """
    Generate the exact set of expected 1-minute tradable bar start timestamps
    for an exchange, accounting for regular session trading hours, weekends,
    and official exchange holidays.

    Exchanges supported:
    - 'NSE' (India): 09:15 to 15:29 IST, Mon-Fri, excluding NSE holidays.
    - 'US' (NYSE/NASDAQ): 09:30 to 15:59 ET, Mon-Fri, excluding US holidays.
    """
    if exchange.upper() in ("NSE", "INDIA", "NIFTY"):
        open_time = dt.time(9, 15)
        close_time = dt.time(15, 29)
        tz_name = "Asia/Kolkata"
        holiday_set = set(NSE_2026_HOLIDAYS)
    else:
        open_time = dt.time(9, 30)
        close_time = dt.time(15, 59)
        tz_name = "America/New_York"
        holiday_set = set(US_2026_HOLIDAYS)

    if custom_holidays:
        holiday_set.update(custom_holidays)

    tz = ZoneInfo(tz_name)
    s = pd.Timestamp(start_ts)
    s = s.tz_convert(tz) if s.tz is not None else s.tz_localize(tz)
    e = pd.Timestamp(end_ts)
    e = e.tz_convert(tz) if e.tz is not None else e.tz_localize(tz)

    if s > e:
        return pd.DatetimeIndex([], tz=tz)

    expected_bars = []
    cur_date = s.date()
    while cur_date <= e.date():
        if cur_date.weekday() < 5 and cur_date not in holiday_set:
            day_open = pd.Timestamp(dt.datetime.combine(cur_date, open_time, tzinfo=tz))
            day_close = pd.Timestamp(dt.datetime.combine(cur_date, close_time, tzinfo=tz))

            day_start = max(s, day_open)
            day_end = min(e, day_close)

            if day_start <= day_end:
                day_bars = pd.date_range(start=day_start, end=day_end, freq="min")
                expected_bars.extend(day_bars)
        cur_date += dt.timedelta(days=1)

    if not expected_bars:
        return pd.DatetimeIndex([], tz=tz)
    return pd.DatetimeIndex(expected_bars, tz=tz)


def validate_blackout_continuity(
    blackout_bars: pd.DataFrame,
    last_processed_ts: pd.Timestamp | dt.datetime | None = None,
    expected_latest_ts: pd.Timestamp | dt.datetime | None = None,
    expected_range: pd.DatetimeIndex | None = None,
    exchange: str = "NSE",
    custom_holidays: set[dt.date] | None = None,
) -> list[pd.Timestamp]:
    """
    Validate that blackout_bars contains every expected 1-minute tradable bar without gaps.
    Returns a list of missing Timestamp objects (empty if data is contiguous and complete).

    Calendar-Aware Continuity:
    - If expected_range is explicitly provided, uses it directly (normalized to canonical exchange timezone).
    - If expected_range is None, derives the expected tradable bars from get_exchange_expected_1m_range,
      strictly respecting exchange session open/close boundaries, weekends, and holidays.
    - Missing first candle(s): tradable bars between (last_processed_ts + 1m) and first received bar.
    - Missing middle candle(s): internal gaps within tradable session hours.
    - Missing last candle(s): tradable bars between last received bar and expected_latest_ts.
    """
    tz_name = "Asia/Kolkata" if exchange.upper() in ("NSE", "INDIA", "NIFTY") else "America/New_York"
    exchange_tz = ZoneInfo(tz_name)

    # 1. Normalize blackout_bars index to canonical exchange timezone
    if blackout_bars.empty:
        bars_idx = pd.DatetimeIndex([], tz=exchange_tz)
    else:
        bars_idx = pd.DatetimeIndex(blackout_bars.index)
        if bars_idx.tz is None:
            bars_idx = bars_idx.tz_localize(exchange_tz)
        else:
            bars_idx = bars_idx.tz_convert(exchange_tz)

    # 2. Use explicit expected_range if provided (Auditor Requirement #4)
    if expected_range is not None:
        target_range = pd.DatetimeIndex(expected_range)
        if target_range.tz is None:
            target_range = target_range.tz_localize(exchange_tz)
        else:
            target_range = target_range.tz_convert(exchange_tz)
        missing = target_range.difference(bars_idx)
        return list(missing)

    # 3. If blackout_bars is empty and bounds are given, compute expected range
    if blackout_bars.empty:
        if last_processed_ts is not None and expected_latest_ts is not None:
            s = pd.Timestamp(last_processed_ts)
            s = (s.tz_convert(exchange_tz) if s.tz is not None else s.tz_localize(exchange_tz)) + dt.timedelta(minutes=1)
            e = pd.Timestamp(expected_latest_ts)
            e = e.tz_convert(exchange_tz) if e.tz is not None else e.tz_localize(exchange_tz)
            if s <= e:
                target_range = get_exchange_expected_1m_range(s, e, exchange=exchange, custom_holidays=custom_holidays)
                return list(target_range)
        return []

    # 4. Derive start_ts and end_ts with canonical exchange timezone
    if last_processed_ts is not None:
        s = pd.Timestamp(last_processed_ts)
        s = s.tz_convert(exchange_tz) if s.tz is not None else s.tz_localize(exchange_tz)
        start_ts = s + dt.timedelta(minutes=1)
    else:
        start_ts = bars_idx[0]

    if expected_latest_ts is not None:
        e = pd.Timestamp(expected_latest_ts)
        end_ts = e.tz_convert(exchange_tz) if e.tz is not None else e.tz_localize(exchange_tz)
    else:
        end_ts = bars_idx[-1]

    if start_ts > end_ts:
        return []

    # 5. Compute exchange-session-aware expected 1-minute range
    target_range = get_exchange_expected_1m_range(
        start_ts, end_ts, exchange=exchange, custom_holidays=custom_holidays
    )
    missing = target_range.difference(bars_idx)
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
    exchange: str = "NSE",
    custom_holidays: set[dt.date] | None = None,
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
            exchange=exchange,
            custom_holidays=custom_holidays,
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

