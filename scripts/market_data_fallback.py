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
3. Telemetry Reference Feed:
   Google Finance and CNBC public endpoints provide emergency spot-price telemetry.
   If primary data feeds stall or drop candles, reference spot quotes are emitted
   to Telegram alerts for human oversight, while the execution engine pauses safely.
"""

from __future__ import annotations

import datetime as dt
import json
import re
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
        normalize_1m_frame,
    )
except ImportError:
    try:
        from market_time_utils import (
            NSE_2026_HOLIDAYS,
            US_2026_HOLIDAYS,
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

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

IST = ZoneInfo("Asia/Kolkata")
ET = ZoneInfo("America/New_York")


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


def fetch_twelve_data_1m_bars(symbol: str, exchange: str = "US", outputsize: int = 30) -> pd.DataFrame:
    """
    Independent Secondary Backup Feed: Twelve Data REST API.
    Fetches genuine, provider-observed 1-minute OHLCV bars.

    CREDIT PRESERVATION RULE:
    Used exclusively as a secondary backup when primary feeds (Yahoo/REST) fail.
    Requires TWELVE_DATA_API_KEY environment variable.
    """
    import os
    api_key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
    if not api_key:
        return pd.DataFrame()

    clean_sym = symbol.replace(".NS", "").replace(".BO", "").upper()
    is_nse = exchange.upper() in ("NSE", "INDIA", "NIFTY")
    tz_name = "Asia/Kolkata" if is_nse else "America/New_York"

    if is_nse:
        url = f"https://api.twelvedata.com/time_series?symbol={clean_sym}&exchange=NSE&interval=1min&outputsize={outputsize}&apikey={api_key}"
    else:
        url = f"https://api.twelvedata.com/time_series?symbol={clean_sym}&interval=1min&outputsize={outputsize}&apikey={api_key}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))

        if data.get("status") == "error":
            return pd.DataFrame()

        values = data.get("values", [])
        if not values:
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
            return normalize_1m_frame(df, tz_name)
    except Exception:
        pass

    return pd.DataFrame()


# -----------------------------------------------------------------------------
# RESILIENT DUAL-FEED MERGER (STRICT TRUE OHLCV ONLY)
# -----------------------------------------------------------------------------
def fetch_resilient_1m_bars(symbol: str, exchange: str = "NSE", period: str = "1d") -> pd.DataFrame:
    """
    Main resilient OHLCV entrypoint:
    1. Tries primary yfinance history.
    2. If yfinance is empty or fails, falls back to direct Yahoo v8 REST endpoints
       (query1/query2) to retrieve genuine OHLCV bars.
    3. If both Yahoo feeds fail, fails over to Independent Secondary Backup:
       Twelve Data REST API (strictly preserving API credits).
    4. Normalizes timezone (IST for NSE, ET for US).

    CRITICAL POLICY:
    This function NEVER appends synthetic/interpolated candles. If exchange OHLCV
    data is missing or dropped across all feeds, the returned DataFrame reflects
    genuine market data only, allowing the runner's evaluate_stale_recovery guard to pause safely.
    """
    clean_sym = symbol
    if exchange.upper() == "NSE" and not clean_sym.endswith(".NS") and not clean_sym.startswith("^"):
        clean_sym += ".NS"

    tz_name = "Asia/Kolkata" if exchange.upper() in ("NSE", "INDIA", "NIFTY") else "America/New_York"

    # Tier 1: Try yfinance client
    raw = _fetch_yfinance_history(clean_sym, period)
    if not raw.empty:
        return normalize_1m_frame(raw, tz_name)

    # Tier 2: Direct REST query1/query2 failover
    v8_df = fetch_raw_yahoo_v8_bars(
        clean_sym,
        range_str=period if period in ("1d", "5d", "1mo") else "1d",
        interval="1m"
    )
    if not v8_df.empty:
        return normalize_1m_frame(v8_df, tz_name)

    # Tier 3: Independent Secondary Backup — Twelve Data OHLCV
    # (Called ONLY when Tier 1 & 2 fail, strictly preserving user credit limit)
    td_df = fetch_twelve_data_1m_bars(symbol, exchange=exchange)
    if not td_df.empty:
        return normalize_1m_frame(td_df, tz_name)

    return pd.DataFrame()
