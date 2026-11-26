"""
scripts/market_data_fallback.py
Multi-Feed Market Data & Telemetry Fallback Engine
Strategy v3.2.5 & v3.3 Cloud Execution Runners

ARCHITECTURE & INTEGRITY RULES:
1. Trade Execution Integrity:
   Trading signals, breakouts, Stop-Loss (SL), and Take-Profit (TP) evaluations
   MUST ONLY operate on complete provider OHLCV 1-minute bars.
   Synthetic single-price candles (Open=High=Low=Close, Volume=0) are STRICTLY
   PROHIBITED from being injected into the execution DataFrame.
2. Direct REST Failover:
   Direct queries to Yahoo v8 Chart REST endpoints (query1 and query2) serve as
   the secondary OHLCV feed if the yfinance client drops requests or fails.
3. Independent Secondary OHLCV Feed (Twelve Data):
   Twelve Data REST API serves as an independent secondary provider for genuine
   1-minute OHLCV bars when primary Yahoo feeds are empty, stale, or have missing candles.
   Rate-limited and cached to protect API credits.
4. Telemetry Reference Feed:
   Google Finance and CNBC public endpoints provide emergency spot-price telemetry.
   If primary data feeds stall or drop candles, reference spot quotes are emitted
   to Telegram alerts for human oversight, while the execution engine pauses safely.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from zoneinfo import ZoneInfo
import pandas as pd

try:
    from scripts.market_time_utils import (
        NSE_2026_HOLIDAYS,
        US_2026_HOLIDAYS,
        is_fresh_1m_data,
        normalize_1m_frame,
    )
except ImportError:
    try:
        from market_time_utils import (
            NSE_2026_HOLIDAYS,
            US_2026_HOLIDAYS,
            is_fresh_1m_data,
            normalize_1m_frame,
        )
    except ImportError:
        NSE_2026_HOLIDAYS = set()
        US_2026_HOLIDAYS = set()

        def normalize_1m_frame(frame, timezone_name):
            if frame is None or frame.empty:
                return pd.DataFrame()
            res = frame.copy()
            tz = ZoneInfo(timezone_name)
            idx = pd.DatetimeIndex(res.index)
            res.index = idx.tz_localize(tz) if idx.tz is None else idx.tz_convert(tz)
            return res[~res.index.duplicated(keep="last")].sort_index()

        def is_fresh_1m_data(frame, now, timezone_name, max_age_seconds=180):
            if frame is None or frame.empty:
                return False
            tz = ZoneInfo(timezone_name)
            local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
            latest_bar = frame.index[-1].to_pydatetime()
            age = (local_now - latest_bar).total_seconds()
            return 0 <= age <= max_age_seconds

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

IST = ZoneInfo("Asia/Kolkata")
ET = ZoneInfo("America/New_York")

# In-memory rate limiting, global quota protection, and caching for Twelve Data API credits
_TWELVE_DATA_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_TWELVE_DATA_GLOBAL_CALL_TIMESTAMPS: list[float] = []
TWELVE_DATA_PER_SYMBOL_COOLDOWN_SECONDS = 60.0
TWELVE_DATA_GLOBAL_MAX_CALLS_PER_MINUTE = 6  # Conservative ceiling below the 8 req/min limit


def is_market_session_active(exchange: str, now_dt: dt.datetime | None = None) -> bool:
    """
    Check if the exchange's regular trading session is currently open.
    - NSE: 09:15 to 15:30 IST, Mon-Fri, excluding holidays.
    - US: 09:30 to 16:00 ET, Mon-Fri, excluding holidays.
    """
    is_nse = exchange.upper() in ("NSE", "INDIA", "NIFTY")
    tz = IST if is_nse else ET
    cur_now = now_dt.astimezone(tz) if (now_dt and now_dt.tzinfo) else (now_dt.replace(tzinfo=tz) if now_dt else dt.datetime.now(tz))

    # Check weekday (0 = Monday, 4 = Friday)
    if cur_now.weekday() >= 5:
        return False

    cur_date = cur_now.date()
    holidays = NSE_2026_HOLIDAYS if is_nse else US_2026_HOLIDAYS
    if cur_date in holidays:
        return False

    cur_time = cur_now.time()
    if is_nse:
        return dt.time(9, 15) <= cur_time <= dt.time(15, 30)
    else:
        return dt.time(9, 30) <= cur_time <= dt.time(16, 0)


# -----------------------------------------------------------------------------
# INDEPENDENT ZERO-KEY REALTIME TELEMETRY (GOOGLE FINANCE + CNBC)
# -----------------------------------------------------------------------------
def fetch_google_finance_quote(symbol: str, exchange: str = "NSE") -> dict[str, Any] | None:
    """
    Fetches real-time reference spot price from Google Finance.
    Used for monitoring telemetry and alerting; NOT injected as synthetic OHLCV.
    Accurately maps exchanges including NYSEARCA (SPY), NASDAQ (QQQ), and NSE.
    """
    clean_sym = symbol.replace(".NS", "").replace(".BO", "").upper()

    # Accurate exchange routing for Google Finance
    if clean_sym in {"SPY", "IVV", "VOO", "DIA"}:
        exch_tag = "NYSEARCA"
    elif clean_sym in {
        "QQQ", "AAPL", "MSFT", "NVDA", "AVGO", "CSCO", "AMZN",
        "GOOGL", "META", "NFLX", "AMD", "QCOM", "TXN", "INTC", "MU", "AMAT",
    }:
        exch_tag = "NASDAQ"
    elif clean_sym in {
        "JPM", "V", "MA", "BAC", "WFC", "GS", "LLY", "JNJ",
        "ABBV", "MRK", "PFE", "AMGN", "REGN", "VRTX", "TSLA",
        "HD", "MCD", "NKE", "SBUX", "GE", "CAT", "BA", "UNP",
        "HON", "ETN", "XOM", "CVX", "COP", "SLB", "EOG", "VLO",
    }:
        exch_tag = "NYSE"
    elif exchange.upper() in {"NASDAQ", "US"}:
        exch_tag = "NASDAQ"
    elif exchange.upper() in {"NYSE", "NYSEARCA"}:
        exch_tag = exchange.upper()
    else:
        exch_tag = "NSE"

    url = f"https://www.google.com/finance/quote/{clean_sym}:{exch_tag}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})

    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # Target the primary ticker price container (class N6SYTe)
        m = re.search(r'class="N6SYTe"[^>]*><span[^>]*><span[^>]*>[^0-9]*([0-9,.]+)<', html)
        if m:
            clean_str = m.group(1).replace(",", "")
            price = float(clean_str)
            return {
                "source": "GoogleFinance_Telemetry",
                "symbol": clean_sym,
                "exchange": exch_tag,
                "price": price,
                "timestamp": dt.datetime.now(IST if exch_tag == "NSE" else ET),
                "is_synthetic_spot": True,
                "ok": True,
            }

        match_attr = re.search(r'data-last-price="([0-9.]+)"', html)
        if match_attr:
            price = float(match_attr.group(1))
            return {
                "source": "GoogleFinance_Attr_Telemetry",
                "symbol": clean_sym,
                "exchange": exch_tag,
                "price": price,
                "timestamp": dt.datetime.now(IST if exch_tag == "NSE" else ET),
                "is_synthetic_spot": True,
                "ok": True,
            }
    except Exception:
        pass

    return None


def fetch_cnbc_us_quote(symbol: str) -> dict[str, Any] | None:
    """
    Fetches real-time US reference spot price from CNBC's public JSON endpoint.
    Used for telemetry and heartbeat verification only.
    """
    clean_sym = symbol.upper()
    url = f"https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols={clean_sym}&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="ignore"))
            quotes = raw.get("FormattedQuoteResult", {}).get("FormattedQuote", [])
            if quotes:
                q = quotes[0]
                last_price = float(q.get("last", "0").replace(",", ""))
                if last_price > 0:
                    return {
                        "source": "CNBC_Public_Telemetry",
                        "symbol": clean_sym,
                        "exchange": "US",
                        "price": last_price,
                        "timestamp": dt.datetime.now(ET),
                        "is_synthetic_spot": True,
                        "ok": True,
                    }
    except Exception:
        pass
    return None


def fetch_live_reference_spot(symbol: str, exchange: str = "NSE") -> dict[str, Any] | None:
    """
    Master telemetry fallback fetcher for monitoring alerts during primary feed delays.
    """
    gf = fetch_google_finance_quote(symbol, exchange)
    if gf and gf.get("ok"):
        return gf

    if exchange.upper() in {"US", "NASDAQ", "NYSE", "NYSEARCA"}:
        cnbc = fetch_cnbc_us_quote(symbol)
        if cnbc and cnbc.get("ok"):
            return cnbc

    return None


# -----------------------------------------------------------------------------
# DIRECT YAHOO V8 REST CHART FETCHER (RAW OHLCV MINUTE DATA)
# -----------------------------------------------------------------------------
def fetch_raw_yahoo_v8_bars(symbol: str, range_str: str = "1d", interval: str = "1m") -> pd.DataFrame:
    """
    Queries Yahoo's raw v8 Chart API directly across redundant server endpoints
    (query1 and query2). Retrieves genuine OHLCV bars, bypassing yfinance client bugs.
    """
    clean_sym = symbol if (".NS" in symbol or ".BO" in symbol or "^" in symbol) else symbol
    endpoints = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{clean_sym}?range={range_str}&interval={interval}",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{clean_sym}?range={range_str}&interval={interval}",
    ]

    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                res = data.get("chart", {}).get("result")
                if not res:
                    continue
                block = res[0]
                timestamps = block.get("timestamp")
                indicators = block.get("indicators", {}).get("quote", [{}])[0]
                if not timestamps or not indicators:
                    continue

                opens = indicators.get("open", [])
                highs = indicators.get("high", [])
                lows = indicators.get("low", [])
                closes = indicators.get("close", [])
                volumes = indicators.get("volume", [])

                rows = []
                for i, ts in enumerate(timestamps):
                    o = opens[i] if i < len(opens) else None
                    h = highs[i] if i < len(highs) else None
                    l = lows[i] if i < len(lows) else None
                    c = closes[i] if i < len(closes) else None
                    v = volumes[i] if i < len(volumes) else None

                    # Only accept bars with complete OHLC
                    if o is not None and h is not None and l is not None and c is not None:
                        rows.append({
                            "timestamp": ts,
                            "Open": float(o),
                            "High": float(h),
                            "Low": float(l),
                            "Close": float(c),
                            "Volume": float(v) if v is not None else 0.0,
                        })

                if rows:
                    df = pd.DataFrame(rows)
                    df["Datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
                    df.set_index("Datetime", inplace=True)
                    df.drop(columns=["timestamp"], inplace=True)
                    return df
        except Exception:
            continue

    return pd.DataFrame()


def _fetch_yfinance_history(clean_sym: str, period: str) -> pd.DataFrame:
    """
    Internal helper to isolate yfinance client invocation.
    Allows clean mocking without requiring yfinance to be installed.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(clean_sym)
        raw = t.history(period=period, interval="1m", prepost=False, auto_adjust=False)
        return raw if (raw is not None and not raw.empty) else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


# -----------------------------------------------------------------------------
# INDEPENDENT SECONDARY FEED (TWELVE DATA OHLCV)
# -----------------------------------------------------------------------------
def fetch_twelve_data_1m_bars(symbol: str, exchange: str = "US", outputsize: int = 30) -> pd.DataFrame:
    """
    Independent Secondary Backup Feed: Twelve Data REST API.
    Fetches genuine, provider-observed 1-minute OHLCV bars.

    CREDIT PRESERVATION RULE:
    Used exclusively as a secondary backup when primary feeds (Yahoo/REST) fail,
    lag, or contain missing candles. Cached with a 60-second cooldown per symbol
    to strictly observe the 8 requests/minute rate limit.
    """
    global _TWELVE_DATA_GLOBAL_CALL_TIMESTAMPS
    import os
    api_key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
    if not api_key:
        return pd.DataFrame()

    clean_sym = symbol.replace(".NS", "").replace(".BO", "").upper()
    is_nse = exchange.upper() in ("NSE", "INDIA", "NIFTY")
    tz_name = "Asia/Kolkata" if is_nse else "America/New_York"
    cache_key = f"{clean_sym}:{exchange.upper()}"
    now_mono = time.monotonic()

    # Finding 2 & 3: Check per-symbol cache (covers both prior successes and cached failures)
    if cache_key in _TWELVE_DATA_CACHE:
        last_time, cached_df = _TWELVE_DATA_CACHE[cache_key]
        if (now_mono - last_time) < TWELVE_DATA_PER_SYMBOL_COOLDOWN_SECONDS:
            return cached_df.copy()

    # Finding 2: Global process-wide rate ceiling (max 6 calls per rolling 60 seconds)
    _TWELVE_DATA_GLOBAL_CALL_TIMESTAMPS = [
        t for t in _TWELVE_DATA_GLOBAL_CALL_TIMESTAMPS if (now_mono - t) < 60.0
    ]
    if len(_TWELVE_DATA_GLOBAL_CALL_TIMESTAMPS) >= TWELVE_DATA_GLOBAL_MAX_CALLS_PER_MINUTE:
        # Global ceiling reached: return existing cached DataFrame if available, else empty
        if cache_key in _TWELVE_DATA_CACHE:
            return _TWELVE_DATA_CACHE[cache_key][1].copy()
        return pd.DataFrame()

    # Record this outgoing call attempt in global rolling tracker
    _TWELVE_DATA_GLOBAL_CALL_TIMESTAMPS.append(now_mono)

    if is_nse:
        url = f"https://api.twelvedata.com/time_series?symbol={clean_sym}&exchange=NSE&interval=1min&outputsize={outputsize}&apikey={api_key}"
    else:
        url = f"https://api.twelvedata.com/time_series?symbol={clean_sym}&interval=1min&outputsize={outputsize}&apikey={api_key}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))

        if data.get("status") == "error":
            # Finding 3: Cache the failure so repeated errors don't trigger repeated requests
            _TWELVE_DATA_CACHE[cache_key] = (now_mono, pd.DataFrame())
            return pd.DataFrame()

        values = data.get("values", [])
        if not values:
            _TWELVE_DATA_CACHE[cache_key] = (now_mono, pd.DataFrame())
            return pd.DataFrame()

        rows = []
        for v in values:
            try:
                rows.append({
                    "Datetime": pd.to_datetime(v["datetime"]),
                    "Open": float(v["open"]),
                    "High": float(v["high"]),
                    "Low": float(v["low"]),
                    "Close": float(v["close"]),
                    "Volume": float(v.get("volume", 0.0)),
                })
            except (ValueError, KeyError):
                continue

        if rows:
            df = pd.DataFrame(rows).set_index("Datetime").sort_index()
            res = normalize_1m_frame(df, tz_name)
            # Store in cache
            _TWELVE_DATA_CACHE[cache_key] = (now_mono, res)
            return res
    except Exception:
        # Finding 3: Cache network/HTTP exception as failure to prevent retry spam
        _TWELVE_DATA_CACHE[cache_key] = (now_mono, pd.DataFrame())

    return pd.DataFrame()


def merge_ohlcv_frames(primary_df: pd.DataFrame, secondary_df: pd.DataFrame, tz_name: str) -> pd.DataFrame:
    """
    Safely merges two genuine OHLCV DataFrames.
    CRITICAL QUALITY RULE (Finding 1):
    Primary provider (Yahoo) candles are NEVER overwritten by secondary feed.
    Secondary feed candles are inserted ONLY for timestamps that are missing
    from the primary frame.
    """
    if primary_df.empty and secondary_df.empty:
        return pd.DataFrame()
    if primary_df.empty:
        return normalize_1m_frame(secondary_df, tz_name)
    if secondary_df.empty:
        return normalize_1m_frame(primary_df, tz_name)

    p = normalize_1m_frame(primary_df, tz_name)
    s = normalize_1m_frame(secondary_df, tz_name)

    p_utc = p.copy()
    p_utc.index = p_utc.index.tz_convert("UTC")
    s_utc = s.copy()
    s_utc.index = s_utc.index.tz_convert("UTC")

    # Finding 1: Only insert secondary candles for timestamps NOT in primary
    missing_mask = ~s_utc.index.isin(p_utc.index)
    bars_to_insert = s_utc.loc[missing_mask]
    if bars_to_insert.empty:
        return normalize_1m_frame(p, tz_name)

    combined = pd.concat([p_utc, bars_to_insert]).sort_index()
    return normalize_1m_frame(combined, tz_name)


def _get_current_time(tz_obj: ZoneInfo) -> dt.datetime:
    return dt.datetime.now(tz_obj)


# -----------------------------------------------------------------------------
# RESILIENT DUAL-FEED MERGER (GAP-AWARE & CREDIT-PROTECTED)
# -----------------------------------------------------------------------------
def fetch_resilient_1m_bars(symbol: str, exchange: str = "NSE", period: str = "1d") -> pd.DataFrame:
    """
    Main resilient OHLCV entrypoint:
    1. Tries primary yfinance history.
    2. If yfinance is empty or fails, falls back to direct Yahoo v8 REST endpoints (query1/query2).
    3. Gap & Stale Detection:
       If during active market hours Yahoo returns empty data, OR data that is stale (>180s),
       OR data with internal gaps in today's completed bars, it automatically queries the
       independent secondary backup: Twelve Data REST API.
    4. Merges genuine OHLCV bars without fabricating synthetic candles.
    """
    clean_sym = symbol
    if exchange.upper() == "NSE" and not clean_sym.endswith(".NS") and not clean_sym.startswith("^"):
        clean_sym += ".NS"

    tz_name = "Asia/Kolkata" if exchange.upper() in ("NSE", "INDIA", "NIFTY") else "America/New_York"
    tz_obj = IST if exchange.upper() in ("NSE", "INDIA", "NIFTY") else ET

    # Tier 1: Try yfinance client
    raw = _fetch_yfinance_history(clean_sym, period)
    df = normalize_1m_frame(raw, tz_name) if not raw.empty else pd.DataFrame()

    # Tier 2: Direct REST query1/query2 failover if yfinance empty
    if df.empty:
        v8_df = fetch_raw_yahoo_v8_bars(
            clean_sym,
            range_str=period if period in ("1d", "5d", "1mo") else "1d",
            interval="1m"
        )
        if not v8_df.empty:
            df = normalize_1m_frame(v8_df, tz_name)

    # Tier 3: Gap & Stale Failover to Twelve Data (Independent Backup)
    now_dt = _get_current_time(tz_obj)
    needs_twelve_data = False

    if df.empty:
        needs_twelve_data = True
    elif is_market_session_active(exchange, now_dt):
        # 3a. Check if data is stale (>180s old)
        if not is_fresh_1m_data(df, now_dt, tz_name, max_age_seconds=180):
            needs_twelve_data = True
        else:
            # 3b. Check for internal missing candles during today's regular session
            today_bars = df[df.index.date == now_dt.date()]
            if len(today_bars) >= 2:
                diffs = today_bars.index.to_series().diff().iloc[1:]
                if (diffs > dt.timedelta(minutes=1)).any():
                    needs_twelve_data = True

    if needs_twelve_data:
        td_df = fetch_twelve_data_1m_bars(symbol, exchange=exchange, outputsize=30)
        if not td_df.empty:
            df = merge_ohlcv_frames(df, td_df, tz_name)

    return df
