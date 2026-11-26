"""
scripts/market_data_fallback.py
Zero-Intervention Resilient Dual-Feed Market Data Fallback Module
Strategy v3.2.5 & v3.3 Autonomous Execution Engines

Requires ZERO user accounts, ZERO API keys, and ZERO manual token refreshes.
Provides automatic failover when Yahoo Finance drops candles or experiences latency.

Feeds:
1. Primary: yfinance / Yahoo Finance Python client.
2. Direct REST Fallback: Raw Yahoo v8 Chart REST API (query1 & query2 endpoints).
3. Independent Public Fallback: Google Finance Realtime Web Endpoint (NSE & US).
4. Independent US Fallback: CNBC Realtime Public REST API (US equities).
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
# INDEPENDENT ZERO-KEY REALTIME QUOTE FETCHER (GOOGLE FINANCE + CNBC)
# -----------------------------------------------------------------------------
def fetch_google_finance_quote(symbol: str, exchange: str = "NSE") -> dict[str, Any] | None:
    """
    Fetches real-time price from Google Finance with zero API keys or accounts.
    Supported exchanges: 'NSE', 'BSE', 'NASDAQ', 'NYSE', 'NYSEARCA'.
    """
    clean_sym = symbol.replace(".NS", "").replace(".BO", "")
    
    # Map exchange for Google Finance ticker syntax
    if exchange.upper() in {"US", "NASDAQ"}:
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
                "source": "GoogleFinance_ZeroKey",
                "symbol": clean_sym,
                "exchange": exch_tag,
                "price": price,
                "timestamp": dt.datetime.now(IST if exch_tag == "NSE" else ET),
                "ok": True,
            }
            
        # Fallback attribute data-last-price
        match_attr = re.search(r'data-last-price="([0-9.]+)"', html)
        if match_attr:
            price = float(match_attr.group(1))
            return {
                "source": "GoogleFinance_Attr",
                "symbol": clean_sym,
                "exchange": exch_tag,
                "price": price,
                "timestamp": dt.datetime.now(IST if exch_tag == "NSE" else ET),
                "ok": True,
            }
    except Exception:
        pass
        
    return None


def fetch_cnbc_us_quote(symbol: str) -> dict[str, Any] | None:
    """
    Fetches official real-time US quote from CNBC's public JSON endpoint.
    Zero API key required.
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
                        "source": "CNBC_Public_REST",
                        "symbol": clean_sym,
                        "exchange": "US",
                        "price": last_price,
                        "timestamp": dt.datetime.now(ET),
                        "ok": True,
                    }
    except Exception:
        pass
    return None


def fetch_independent_quote(symbol: str, exchange: str = "NSE") -> dict[str, Any] | None:
    """
    Master fallback quote fetcher trying independent sources in sequence.
    Zero accounts, zero keys.
    """
    # 1. Try Google Finance (works for both NSE & US)
    gf = fetch_google_finance_quote(symbol, exchange)
    if gf and gf.get("ok"):
        return gf
        
    # 2. If US stock, try CNBC public REST endpoint
    if exchange.upper() in {"US", "NASDAQ", "NYSE", "NYSEARCA"}:
        cnbc = fetch_cnbc_us_quote(symbol)
        if cnbc and cnbc.get("ok"):
            return cnbc
            
    return None


# -----------------------------------------------------------------------------
# DIRECT YAHOO V8 REST CHART FETCHER (BYPASSING YFINANCE CRUMB CACHE)
# -----------------------------------------------------------------------------
def fetch_raw_yahoo_v8_bars(symbol: str, range_str: str = "1d", interval: str = "1m") -> pd.DataFrame:
    """
    Queries Yahoo's raw v8 Chart API directly across redundant server endpoints
    (query1 and query2). Bypasses any yfinance library-level caching or dropping bugs.
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
                    
                    rows.append({
                        "timestamp": ts,
                        "Open": o,
                        "High": h,
                        "Low": l,
                        "Close": c,
                        "Volume": v,
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


# -----------------------------------------------------------------------------
# RESILIENT DUAL-FEED MERGER WITH ZERO-KEY FALLBACK
# -----------------------------------------------------------------------------
def fetch_resilient_1m_bars(symbol: str, exchange: str = "NSE", period: str = "1d") -> pd.DataFrame:
    """
    Main resilient entrypoint:
    1. Tries primary yfinance history.
    2. If yfinance is empty or fails, falls back to direct Yahoo v8 REST endpoints.
    3. Normalizes timezone (IST for NSE, ET for US).
    4. If market is actively open and latest bar is stale (>120s), supplements with
       an independent zero-key Google Finance / CNBC live quote so the engine never gets stranded.
    """
    import yfinance as yf
    
    clean_sym = symbol
    if exchange.upper() == "NSE" and not clean_sym.endswith(".NS") and not clean_sym.startswith("^"):
        clean_sym += ".NS"
        
    tz_name = "Asia/Kolkata" if exchange.upper() in ("NSE", "INDIA", "NIFTY") else "America/New_York"
    tz_obj = IST if exchange.upper() in ("NSE", "INDIA", "NIFTY") else ET
    
    df = pd.DataFrame()
    
    # Tier 1: Try yfinance
    try:
        t = yf.Ticker(clean_sym)
        raw = t.history(period=period, interval="1m", prepost=False, auto_adjust=False)
        if raw is not None and not raw.empty:
            df = normalize_1m_frame(raw, tz_name)
    except Exception:
        df = pd.DataFrame()
        
    # Tier 2: If yfinance failed or returned empty, try direct v8 API
    if df.empty:
        v8_df = fetch_raw_yahoo_v8_bars(clean_sym, range_str=period if period in ("1d", "5d", "1mo") else "1d", interval="1m")
        if not v8_df.empty:
            v8_df.dropna(subset=["Close"], inplace=True)
            df = normalize_1m_frame(v8_df, tz_name)
            
    # Tier 3: Verify latest price during ACTIVE market hours
    now_dt = dt.datetime.now(tz_obj)
    if is_market_session_active(exchange, now_dt):
        need_fallback = False
        if df.empty:
            need_fallback = True
        else:
            last_ts = df.index[-1]
            age_seconds = (now_dt - last_ts).total_seconds()
            if age_seconds > 120:
                need_fallback = True
                
        if need_fallback:
            indep = fetch_independent_quote(symbol, exchange)
            if indep and indep.get("ok"):
                live_price = float(indep["price"])
                bar_dt = now_dt.replace(second=0, microsecond=0)
                
                synthetic_bar = pd.DataFrame([{
                    "Open": live_price,
                    "High": live_price,
                    "Low": live_price,
                    "Close": live_price,
                    "Volume": 0,
                }], index=pd.DatetimeIndex([bar_dt], tz=tz_obj))
                
                if df.empty:
                    df = synthetic_bar
                elif bar_dt > df.index[-1]:
                    df = pd.concat([df, synthetic_bar])
                elif bar_dt == df.index[-1]:
                    # Update close of currently forming bar
                    df.loc[bar_dt, "Close"] = live_price
                    df.loc[bar_dt, "High"] = max(df.loc[bar_dt, "High"], live_price)
                    df.loc[bar_dt, "Low"] = min(df.loc[bar_dt, "Low"], live_price)
                    
    return df
