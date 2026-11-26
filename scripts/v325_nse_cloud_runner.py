"""
v325_nse_cloud_runner.py
NSE opening-range signal scanner and paper-signal monitor.
- Uses Asia/Kolkata time.
- Requires all 20 completed 09:15-09:34 IST opening-range bars.
- Rejects late starts, stale market data, incomplete ranges and missing ATR.
- Processes each new completed 1-minute bar once; never replays pre-lock candles.
- Sends signals only. This script does not place broker orders.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import sys
import pandas as pd
import time
import urllib.parse
import urllib.request
from typing import Any
from zoneinfo import ZoneInfo

try:
    from scripts.market_time_utils import completed_1m_bars, evaluate_stale_recovery, exact_minute_window, is_fresh_1m_data, normalize_1m_frame
    from scripts.market_data_fallback import fetch_live_reference_spot, fetch_resilient_1m_bars
except ModuleNotFoundError:
    from market_time_utils import completed_1m_bars, evaluate_stale_recovery, exact_minute_window, is_fresh_1m_data, normalize_1m_frame
    from market_data_fallback import fetch_live_reference_spot, fetch_resilient_1m_bars

IST_ZONE_NAME = "Asia/Kolkata"
IST = ZoneInfo(IST_ZONE_NAME)

# Ensure UTF-8 output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# -----------------------------------------------------------------------------
# SECTOR MAPPINGS & CONSTANTS (Strategy v3.2.5 Rulebook)
# -----------------------------------------------------------------------------
NSE_SECTORS = {
    "Banking": ["HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "KOTAKBANK.NS", "AXISBANK.NS", "INDUSINDBK.NS"],
    "FinServices": ["BAJFINANCE.NS", "BAJAJFINSV.NS", "SHRIRAMFIN.NS", "SBILIFE.NS", "HDFCLIFE.NS"],
    "IT_Tech": ["TCS.NS", "INFY.NS", "HCLTECH.NS", "WIPRO.NS", "TECHM.NS"],
    "Automobile": ["MARUTI.NS", "M&M.NS", "EICHERMOT.NS", "HEROMOTOCO.NS", "BAJAJ-AUTO.NS"],
    "FMCG": ["ITC.NS", "HINDUNILVR.NS", "NESTLEIND.NS", "BRITANNIA.NS", "TATACONSUM.NS"],
    "Pharma_Health": ["SUNPHARMA.NS", "DRREDDY.NS", "CIPLA.NS", "DIVISLAB.NS", "APOLLOHOSP.NS"],
    "Energy_Utilities": ["RELIANCE.NS", "BPCL.NS", "ONGC.NS", "COALINDIA.NS", "NTPC.NS", "POWERGRID.NS"],
    "Metals_Materials": ["TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS", "ULTRACEMCO.NS", "GRASIM.NS"],
    "Infra_CapGoods": ["LT.NS", "ADANIPORTS.NS", "ADANIENT.NS"],
    "Consumer_Retail": ["TITAN.NS", "ASIANPAINT.NS", "TRENT.NS", "BHARTIARTL.NS"],
}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# -----------------------------------------------------------------------------
# TELEGRAM DISPATCHER
# -----------------------------------------------------------------------------
def send_telegram(text: str) -> dict[str, Any]:
    token = os.getenv("AUTOMATION_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("AUTOMATION_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("[TELEGRAM FAILED] Missing bot token or chat ID; aborting so the workflow cannot report a false-green run.")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok") is not True:
            raise RuntimeError(data.get("description", "Telegram API rejected the message"))
        msg_id = data.get("result", {}).get("message_id")
        print(f"[Telegram Sent - msg_id {msg_id}] {text.splitlines()[0]}")
        return {"ok": True, "message_id": msg_id}
    except Exception as ex:
        print(f"[Telegram Error: {ex}] {text.splitlines()[0]}")
        raise RuntimeError("Telegram alert delivery failed; stopping the signal engine.") from ex

# -----------------------------------------------------------------------------
# MARKET DATA HELPERS
# -----------------------------------------------------------------------------
def get_ist_now() -> datetime.datetime:
    return datetime.datetime.now(IST)

def fetch_ticker_1m(symbol: str, period: str = "5d") -> Any:
    try:
        df = fetch_resilient_1m_bars(symbol, exchange="NSE", period=period)
        if df.empty:
            return None
        return normalize_1m_frame(df, IST_ZONE_NAME)
    except Exception as exc:
        print(f"[DATA FETCH ERROR] {symbol}: {exc}")
        return None

def compute_atr_pct(symbol: str, as_of: datetime.date | None = None) -> float | None:
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        df = t.history(period="60d", interval="1d", auto_adjust=False)
        if as_of is not None and not df.empty:
            df = df[df.index.date <= as_of]
        if len(df) >= 15:
            h = df["High"]
            l = df["Low"]
            c = df["Close"]
            tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
            atr14 = tr.rolling(14, min_periods=14).mean().iloc[-1]
            if pd.notna(atr14) and float(c.iloc[-1]) > 0:
                value = float((atr14 / c.iloc[-1]) * 100)
                if math.isfinite(value):
                    return value
    except Exception as exc:
        print(f"[ATR DATA ERROR] {symbol}: {exc}")
    return None

# -----------------------------------------------------------------------------
# SCANNER LOGIC (09:36 IST)
# -----------------------------------------------------------------------------
def run_0935_scan(target_date: datetime.date | None = None) -> dict[str, Any] | None:
    now_ist = get_ist_now()
    historical_mode = target_date is not None and target_date < now_ist.date()
    if target_date is None:
        target_date = now_ist.date()

    print("=" * 80)
    print(f"  STRATEGY v3.2.5 NSE 10-SECTOR SCANNER (Target Date: {target_date})")
    print(f"  Timestamp: {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}")
    print("=" * 80)

    # 1. Fetch Nifty Benchmark
    print("Fetching Nifty 50 benchmark (^NSEI)...")
    nifty_raw = fetch_ticker_1m("^NSEI")
    if nifty_raw is None or nifty_raw.empty:
        print("ERROR: Failed to fetch Nifty data")
        send_telegram("⚠️ [NSE DATA UNAVAILABLE] Nifty 1-minute data could not be fetched. No signal generated.")
        return None

    nifty_df = completed_1m_bars(nifty_raw, now_ist, IST_ZONE_NAME)
    if not historical_mode and not is_fresh_1m_data(nifty_raw, now_ist, IST_ZONE_NAME, max_age_seconds=180):
        print("ERROR: Nifty 1-minute data is stale (>180 seconds). Standing down.")
        send_telegram("⚠️ [NSE STALE DATA] Nifty 1-minute feed is older than 180 seconds. No signal generated.")
        return None

    nifty_or = exact_minute_window(nifty_df, target_date, "09:15", "09:34", IST_ZONE_NAME)
    if nifty_or is None:
        print(f"⚠️  INCOMPLETE NIFTY OR for {target_date}: expected all 20 bars from 09:15-09:34 IST.")
        send_telegram("⚠️ [NSE INCOMPLETE OR] Nifty opening range does not contain all 20 completed minutes. No signal generated.")
        return None

    nifty_open = nifty_or.iloc[0]["Open"]
    nifty_close = nifty_or.iloc[-1]["Close"]
    nifty_move = ((nifty_close - nifty_open) / nifty_open) * 100
    print(f"Nifty 50 Move: {nifty_move:+.2f}% (Open: {nifty_open:.2f} -> Close: {nifty_close:.2f})")

    # 2. Sector Performance Scan
    sector_results = []
    symbol_data = {}

    for sector, syms in NSE_SECTORS.items():
        moves = []
        positive_count = 0
        total = len(syms)

        for sym in syms:
            raw_df = fetch_ticker_1m(sym)
            if raw_df is None or raw_df.empty:
                continue
            if not historical_mode and not is_fresh_1m_data(raw_df, now_ist, IST_ZONE_NAME, max_age_seconds=180):
                print(f"[STALE DATA] Skipping {sym}; latest completed 1m bar is older than 180 seconds.")
                continue
            df = completed_1m_bars(raw_df, now_ist, IST_ZONE_NAME)
            df_day = df[df.index.date == target_date]
            or_bars = exact_minute_window(df_day, target_date, "09:15", "09:34", IST_ZONE_NAME)
            if or_bars is None:
                print(f"[INCOMPLETE OR] Skipping {sym}; expected all 20 bars from 09:15-09:34 IST.")
                continue

            o = or_bars.iloc[0]["Open"]
            c = or_bars.iloc[-1]["Close"]
            h = or_bars["High"].max()
            l = or_bars["Low"].min()
            move = ((c - o) / o) * 100
            moves.append(move)
            if move > 0:
                positive_count += 1

            symbol_data[sym] = {
                "sector": sector,
                "move": move,
                "orh": h,
                "orl": l,
                "open": o,
                "close": c,
                "df_day": df_day,
            }

        minimum_coverage = max(2, math.ceil(total * 0.60))
        if len(moves) >= minimum_coverage:
            avg_move = sum(moves) / len(moves)
            breadth = (positive_count / len(moves)) * 100
            alpha = avg_move - nifty_move
            sector_results.append({
                "sector": sector,
                "breadth": breadth,
                "avg_move": avg_move,
                "alpha": alpha,
                "positive": positive_count,
                "total": len(moves),
                "coverage": (len(moves) / total) * 100.0,
            })

    # Sort sectors: Breadth desc, Avg Move desc
    sector_results.sort(key=lambda s: (s["breadth"], s["avg_move"]), reverse=True)

    print("\n" + "=" * 80)
    print("RANK  SECTOR               BREADTH         AVG MOVE     ALPHA vs NIFTY")
    print("=" * 80)
    for idx, s in enumerate(sector_results, 1):
        print(f"#{idx:<4} {s['sector']:<20} {s['positive']}/{s['total']} ({s['breadth']:5.1f}%)   coverage={s['coverage']:4.0f}%   {s['avg_move']:+6.2f}%     {s['alpha']:+6.2f}pp")

    if not sector_results:
        msg = "🛡️ [NSE STAND DOWN] No sector met the minimum 60% valid-data coverage requirement. No signal generated."
        print(msg)
        send_telegram(msg)
        return None

    top_sector = sector_results[0]["sector"]
    print(f"\n🏆 #1 LEADER SECTOR: {top_sector}")

    # Rank constituents in #1 sector
    candidates = []
    for sym in NSE_SECTORS[top_sector]:
        if sym in symbol_data:
            d = symbol_data[sym]
            # Use only data available before the session being replayed/scanned.
            # This avoids same-day daily-candle look-ahead in historical tests.
            atr_as_of = target_date - datetime.timedelta(days=1)
            atr = compute_atr_pct(sym, as_of=atr_as_of)
            candidates.append({
                "symbol": sym,
                "move": d["move"],
                "orh": d["orh"],
                "orl": d["orl"],
                "atr": atr,
                "df_day": d["df_day"],
            })

    # Sort constituents by Move desc
    candidates.sort(key=lambda c: c["move"], reverse=True)

    # Filter ATR >= 2.50%. Missing ATR is not silently replaced with a qualifying value.
    qual_cands = [c for c in candidates if c["atr"] is not None and c["atr"] >= 2.50]
    if not qual_cands:
        msg = "🛡️ [NSE STAND DOWN] No candidate had a verified ATR >= 2.50%; missing ATR data is not treated as a pass."
        print(msg)
        send_telegram(msg)
        return None
    primary = qual_cands[0]

    sym = primary["symbol"]
    clean_sym = sym.replace(".NS", "")
    orh = round(primary["orh"], 2)
    trigger = round(orh * 1.0003, 2)
    stop_loss = round(trigger * 0.99, 2)  # 1.00% Hard SL
    take_profit = round(trigger * 1.02, 2)  # +2.00% Take Profit

    result = {
        "date": str(target_date),
        "sector": top_sector,
        "symbol": sym,
        "clean_symbol": clean_sym,
        "move": primary["move"],
        "atr": primary["atr"],
        "orh": orh,
        "trigger": trigger,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "df_day": primary["df_day"],
    }

    print("\n" + "#" * 80)
    print(f"🎯 PRIMARY CANDIDATE LOCKED: {clean_sym} ({top_sector})")
    print(f"   • 20-min Completed ORH (09:15-09:34): ₹{orh:.2f}")
    print(f"   • Pre-Declared Trigger (>= ORH x 1.0003): >= ₹{trigger:.2f}")
    print(f"   • 1.00% Hard Stop Loss: ₹{stop_loss:.2f}")
    print(f"   • +2.00% Take Profit Target: ₹{take_profit:.2f}")
    print(f"   • 14d ATR: {primary['atr']:.2f}% (Threshold: >= 2.50%)")
    print(f"   • Execution Gate: STRICTLY >= 09:45:00 IST (Law 2)")
    print("#" * 80)

    # Send 09:35 Telegram Scan Alert
    alert_msg = (
        f"🎯 *[NSE PAPER SIGNAL] SCAN COMPLETE — {target_date.isoformat()}*\n"
        f"• #1 Sector: *{top_sector}*\n"
        f"• Primary Candidate: *{clean_sym}* (ATR: {primary['atr']:.2f}%)\n"
        f"• Completed ORH: `₹{orh:.2f}`\n"
        f"• Pre-Declared Trigger: `₹{trigger:.2f}`\n"
        f"• 1.00% Hard SL: `₹{stop_loss:.2f}`\n"
        f"• +2.00% Take Profit: `₹{take_profit:.2f}`\n"
        f"• Execution Gate: *09:45:00 IST*\n"
        f"• Data: *Yahoo Finance 1-minute bars; freshness checked*\n"
        f"• Execution: *Signal monitoring only; this script places no broker orders.*"
    )
    send_telegram(alert_msg)

    return result

# -----------------------------------------------------------------------------
# INTRADAY EXECUTION & MONITORING LOOP (09:45 - 15:30 IST)
# -----------------------------------------------------------------------------
def run_monitoring_loop(candidate: dict[str, Any], is_test: bool = False) -> None:
    sym = candidate["symbol"]
    clean_sym = candidate["clean_symbol"]
    trigger = candidate["trigger"]
    sl = candidate["stop_loss"]
    tp = candidate["take_profit"]

    print("\n" + "=" * 80)
    print(f"  LAUNCHING INTRADAY MONITORING LOOP: {clean_sym}")
    print(f"  Gate: >= 09:45:00 IST | Trigger: >= ₹{trigger:.2f} | SL: ₹{sl:.2f} | TP: ₹{tp:.2f}")
    print("=" * 80)

    if is_test:
        print("[TEST MODE] Evaluating completed bars from historical data...")
        df_day = candidate["df_day"]
        post_gate = df_day.between_time("09:45", "15:29")
        triggered = False
        in_trade = False
        entry_price = 0.0
        entry_time = None

        for ts, bar in post_gate.iterrows():
            t_str = ts.strftime("%H:%M:%S")
            h = bar["High"]
            l = bar["Low"]
            c = bar["Close"]

            if not in_trade:
                if h >= trigger:
                    in_trade = True
                    triggered = True
                    entry_price = trigger
                    entry_time = t_str
                    print(f"🚨 [TEST] TRIGGER HIT @ {t_str} @ ₹{entry_price:.2f}")
            else:
                if l <= sl:
                    print(f"🛑 [TEST] HARD SL HIT @ {t_str} @ ₹{sl:.2f} (-1.00% LOSS)")
                    break
                elif h >= tp:
                    print(f"🎯 [TEST] TAKE PROFIT HIT @ {t_str} @ ₹{tp:.2f} (+2.00% NET PROFIT)")
                    break

        if not triggered:
            print(f"🛡️ [TEST] UNTRIGGERED / STAND DOWN (no historical trigger detected)")

        test_ping = (
            f"✅ *[HISTORICAL TEST COMPLETE] Strategy v3.2.5*\n"
            f"• Candidate: *{clean_sym}*\n"
            f"• Historical replay finished.\n"
            f"• This does not prove live market-data freshness or broker execution.\n"
            f"• This script sends signals only; it places no broker orders."
        )
        send_telegram(test_ping)
        return

    # LIVE SIGNAL-MONITORING LOOP. Process each completed minute exactly once.
    triggered = False
    in_trade = False
    entry_price = 0.0
    # Start from the latest completed bar at candidate-lock time. Earlier bars
    # are deliberately not replayed as if they were observed live.
    monitor_boot_now = get_ist_now()
    monitor_boot_df = fetch_ticker_1m(sym, period="1d")
    monitor_boot_completed = completed_1m_bars(monitor_boot_df, monitor_boot_now, IST_ZONE_NAME)
    last_processed = (
        monitor_boot_completed.index[-1]
        if not monitor_boot_completed.empty
        else pd.Timestamp(datetime.datetime.combine(monitor_boot_now.date(), datetime.time(9, 44), tzinfo=IST))
    )
    last_close = None
    stale_polls = 0
    stale_alert_sent = False
    gap_alert_sent = False
    terminal = False

    while not terminal:
        now = get_ist_now()
        df = fetch_ticker_1m(sym, period="1d")
        fresh = df is not None and is_fresh_1m_data(df, now, IST_ZONE_NAME, max_age_seconds=180)

        if not fresh:
            stale_polls += 1
            if not stale_alert_sent:
                ref_spot = fetch_live_reference_spot(clean_sym, exchange="NSE")
                spot_info = f" (Ref Spot: ₹{ref_spot['price']:.2f} via {ref_spot['source']})" if ref_spot else ""
                msg = (
                    f"⚠️ [NSE STALE DATA] {clean_sym}: latest completed 1-minute bar "
                    f"is older than 180 seconds{spot_info}. Signal monitoring is paused "
                    f"safely (no unverified fills)."
                )
                print(msg)
                send_telegram(msg)
                stale_alert_sent = True
        else:
            if stale_polls > 0:
                completed = completed_1m_bars(df, now, IST_ZONE_NAME)
                if completed.empty:
                    continue

                latest_ts = completed.index[-1]
                latest_bar = completed.iloc[-1]
                latest_c = float(latest_bar["Close"])
                latest_h = float(latest_bar["High"])
                latest_l = float(latest_bar["Low"])
                bar_time = latest_ts.strftime("%H:%M:%S IST")

                # Slice all completed bars formed during the outage window
                blackout_bars = (
                    completed.loc[completed.index > last_processed]
                    if last_processed is not None
                    else completed.iloc[[-1]]
                )
                if blackout_bars.empty:
                    blackout_bars = completed.iloc[[-1]]

                recovery = evaluate_stale_recovery(
                    in_trade,
                    latest_bar,
                    trigger,
                    sl,
                    tp,
                    blackout_bars=blackout_bars,
                    last_processed_ts=last_processed,
                    expected_latest_ts=latest_ts,
                    exchange="NSE",
                )

                if recovery["action"] == "RECOVERY_DATA_INCOMPLETE":
                    if not gap_alert_sent:
                        missing_preview = ", ".join(recovery["missing_bars"][:3])
                        if recovery["missing_count"] > 3:
                            missing_preview += f" (+{recovery['missing_count'] - 3} more)"
                        msg = (
                            f"⚠️ *[NSE DATA GAP / RECOVERY INCOMPLETE]*\n"
                            f"• Stock: *{clean_sym}*\n"
                            f"• Missing Candles: `{recovery['missing_count']}` ({missing_preview})\n"
                            f"• Action: Signal monitoring paused; cannot safely determine trade state from incomplete data."
                        )
                        print(msg)
                        send_telegram(msg)
                        gap_alert_sent = True
                    stale_polls = 1  # Retain recovery gate until complete data is supplied
                    continue

                # Recovery succeeded: clear outage & gap alert guards
                stale_alert_sent = False
                gap_alert_sent = False
                stale_polls = 0
                last_processed = latest_ts

                if recovery["action"] == "DISCARD_AND_RESUME":
                    msg = (
                        f"ℹ️ [NSE DATA RESTORED] {clean_sym}: feed reconnected at {bar_time}. "
                        f"Blackout candles discarded to prevent retrospective backfilling. "
                        f"Resuming live forward monitoring."
                    )
                    print(msg)
                    send_telegram(msg)
                    continue
                elif recovery["action"] == "OUTAGE_EXIT_STOP":
                    exit_price = recovery["exit_price"]
                    exit_msg = (
                        f"🛑 *[NSE PAPER SIGNAL — STOP OUTAGE EXIT]*\n"
                        f"• Stock: *{clean_sym}*\n"
                        f"• Observable Exit Price: `₹{exit_price:.2f}`\n"
                        f"• Reconnection Time: `{bar_time}`\n"
                        f"• Execution Uncertainty: *TRUE (Stop crossed during data blackout; recorded at observable market price)*"
                    )
                    print(exit_msg)
                    send_telegram(exit_msg)
                    terminal = True
                    break
                elif recovery["action"] == "OUTAGE_EXIT_TARGET":
                    exit_price = recovery["exit_price"]
                    tp_msg = (
                        f"🎯 *[NSE PAPER SIGNAL — TARGET LEVEL HIT (POST-OUTAGE)]*\n"
                        f"• Stock: *{clean_sym}*\n"
                        f"• Observable Exit Price: `₹{exit_price:.2f}`\n"
                        f"• Reconnection Time: `{bar_time}`\n"
                        f"• Execution Uncertainty: *TRUE (Target reached during/after feed outage)*"
                    )
                    print(tp_msg)
                    send_telegram(tp_msg)
                    terminal = True
                    break
                else:
                    msg = (
                        f"ℹ️ [NSE DATA RESTORED] {clean_sym}: feed resumed at {bar_time}. "
                        f"Trade active (Current: ₹{latest_c:.2f}, SL: ₹{sl:.2f}, TP: ₹{tp:.2f})."
                    )
                    print(msg)
                    send_telegram(msg)
                    continue

            stale_polls = 0
            stale_alert_sent = False
            completed = completed_1m_bars(df, now, IST_ZONE_NAME)
            post_gate = completed.loc[
                (completed.index > last_processed)
                & (completed.index.time >= datetime.time(9, 45))
                & (completed.index.time <= datetime.time(15, 28))
            ]

            for ts, bar in post_gate.iterrows():
                last_processed = ts
                bar_time = ts.strftime("%H:%M:%S IST")
                h = float(bar["High"])
                l = float(bar["Low"])
                c = float(bar["Close"])
                last_close = c

                if not in_trade:
                    if h >= trigger:
                        # A 1-minute OHLC candle cannot reveal intrabar order.
                        # If the same candle also crosses the stop, skip this
                        # ambiguous entry rather than inventing a fill.
                        if l <= sl:
                            msg = (
                                f"⚠️ [NSE AMBIGUOUS BAR — NO ENTRY] {clean_sym} at {bar_time}: "
                                f"trigger and stop were both inside one candle. Stand down."
                            )
                            print(msg)
                            send_telegram(msg)
                            terminal = True
                            break

                        in_trade = True
                        triggered = True
                        entry_price = trigger
                        alert = (
                            f"🚨 *[NSE PAPER SIGNAL] BREAKOUT LEVEL REACHED*\n"
                            f"• Stock: *{clean_sym}*\n"
                            f"• Reference Fill: `₹{entry_price:.2f}`\n"
                            f"• Time: `{bar_time}`\n"
                            f"• 1.00% Hard SL: `₹{sl:.2f}`\n"
                            f"• +2.00% TP Target: `₹{tp:.2f}`\n"
                            f"• Note: *Signal only; no broker order was placed.*"
                        )
                        print(alert)
                        send_telegram(alert)
                else:
                    # If SL and TP touch in the same candle, conservatively assume SL first.
                    if l <= sl:
                        exit_msg = (
                            f"🛑 *[NSE PAPER SIGNAL — STOP LEVEL HIT]*\n"
                            f"• Stock: *{clean_sym}*\n"
                            f"• Reference Exit: `₹{sl:.2f}`\n"
                            f"• Time: `{bar_time}`\n"
                            f"• Note: *Signal only; no broker order was placed.*"
                        )
                        print(exit_msg)
                        send_telegram(exit_msg)
                        terminal = True
                        break
                    elif h >= tp:
                        tp_msg = (
                            f"🎯 *[NSE PAPER SIGNAL — TARGET LEVEL HIT]*\n"
                            f"• Stock: *{clean_sym}*\n"
                            f"• Reference Exit: `₹{tp:.2f}`\n"
                            f"• Time: `{bar_time}`\n"
                            f"• Note: *Signal only; no broker order was placed.*"
                        )
                        print(tp_msg)
                        send_telegram(tp_msg)
                        terminal = True
                        break

        if terminal:
            break

        if now.time() >= datetime.time(15, 29):
            if in_trade and last_close is not None and fresh:
                msg = (
                    f"🏁 *[NSE PAPER SIGNAL — EOD WINDOW CLOSED]*\n"
                    f"• Stock: *{clean_sym}*\n"
                    f"• Reference Exit: `₹{last_close:.2f}`\n"
                    f"• Note: *Signal only; no broker order was placed.*"
                )
            elif in_trade:
                msg = f"⚠️ [NSE MONITOR ENDED] {clean_sym}: final fresh bar unavailable; exit price unverified. Manual review required."
            else:
                msg = f"🛡️ [NSE STAND DOWN] {clean_sym} did not reach trigger before 15:29 IST. No signal entry."
            print(msg)
            send_telegram(msg)
            break

        if stale_polls >= 3 and not in_trade:
            msg = f"🛡️ [NSE STAND DOWN] {clean_sym}: market data remained stale for 3 consecutive checks. No signal entry."
            print(msg)
            send_telegram(msg)
            break

        time.sleep(20)

# -----------------------------------------------------------------------------
# MAIN CLI ENTRYPOINT
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Strategy v3.2.5 NSE Autonomous Cloud Runner")
    parser.add_argument("--test", action="store_true", help="Run a historical replay for 2026-11-20; no live monitoring or broker orders")
    args = parser.parse_args()

    if args.test:
        # Historical validation date used by the existing strategy test.
        target_date = datetime.date(2026, 11, 20)
        cand = run_0935_scan(target_date)
        if cand:
            run_monitoring_loop(cand, is_test=True)
    else:
        # Reject delayed GitHub cron runs instead of producing stale intraday signals.
        now = get_ist_now()
        if not (datetime.time(9, 34) <= now.time() <= datetime.time(9, 42)):
            msg = (
                f"🛡️ [NSE ENGINE SKIPPED] Outside the 09:34-09:42 IST scan window. "
                f"Current time: {now:%Y-%m-%d %H:%M:%S IST}. No signal generated."
            )
            print(msg)
            send_telegram(msg)
            return

        cand = run_0935_scan()
        if cand:
            if get_ist_now().time() >= datetime.time(15, 29):
                msg = "🛡️ [NSE STAND DOWN] Candidate scan finished after the execution window. No late/backfilled signal will be generated."
                print(msg)
                send_telegram(msg)
                return
            run_monitoring_loop(cand, is_test=False)
        else:
            print("ℹ️  No candidate locked (market closed, holiday, incomplete OR or stale data). No signal generated.")
            sys.exit(0)

if __name__ == "__main__":
    main()
