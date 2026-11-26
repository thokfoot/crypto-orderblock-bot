"""
v325_us_cloud_runner.py
Strategy v3.2.5 Fully Autonomous Cloud Engine for US Wall Street Sleeve
Runs against the US market using America/New_York (DST-aware) time.
- Scan is accepted only near 10:05 ET and requires all 20 completed OR bars.
- Monitoring processes each newly completed 1-minute bar once, through 11:00 ET.
- Stale data, incomplete bars and delayed starts cause a stand-down.
- This script sends paper signals only; it does not place broker orders.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Any
import pandas as pd
import yfinance as yf
from zoneinfo import ZoneInfo

try:
    from scripts.market_time_utils import (
        OutageAlertGuard,
        US_2026_HOLIDAYS,
        completed_1m_bars,
        evaluate_stale_recovery,
        exact_minute_window,
        is_fresh_1m_data,
    )
    from scripts.market_data_fallback import fetch_live_reference_spot, fetch_resilient_1m_bars
except ModuleNotFoundError:
    from market_time_utils import (
        OutageAlertGuard,
        US_2026_HOLIDAYS,
        completed_1m_bars,
        evaluate_stale_recovery,
        exact_minute_window,
        is_fresh_1m_data,
    )
    from market_data_fallback import fetch_live_reference_spot, fetch_resilient_1m_bars

# Ensure UTF-8 output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# -----------------------------------------------------------------------------
# US MEGA SECTOR MAPPINGS (Strategy v3.2.5 Rulebook)
# -----------------------------------------------------------------------------
US_SECTORS = {
    "Mega_Tech": ["AAPL", "MSFT", "NVDA", "AVGO", "CSCO", "ORCL"],
    "Mega_Internet": ["AMZN", "GOOGL", "META", "NFLX"],
    "Semiconductors": ["AMD", "QCOM", "TXN", "INTC", "MU", "AMAT"],
    "Fin_Payments": ["JPM", "V", "MA", "BAC", "WFC", "GS"],
    "Healthcare_Bio": ["LLY", "JNJ", "ABBV", "MRK", "PFE", "AMGN", "REGN", "VRTX"],
    "Consumer_Discretionary": ["TSLA", "HD", "MCD", "NKE", "SBUX"],
    "Industrial_Aerospace": ["GE", "CAT", "BA", "UNP", "HON", "ETN"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG", "VLO"]
}

ET = ZoneInfo("America/New_York")
ET_ZONE_NAME = "America/New_York"

# -----------------------------------------------------------------------------
# TELEGRAM DISPATCHER
# -----------------------------------------------------------------------------
def send_telegram(text: str) -> dict[str, Any]:
    token = os.getenv("AUTOMATION_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("AUTOMATION_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("[TELEGRAM FAILED] Missing bot token or chat ID; aborting so the workflow cannot report a false-green run.")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
        if result.get("ok") is not True:
            raise RuntimeError(result.get("description", "Telegram API rejected the message"))
        return result
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
        raise RuntimeError("Telegram alert delivery failed; stopping the signal engine.") from e

def fetch_1m_bars(ticker: str) -> pd.DataFrame:
    try:
        df = fetch_resilient_1m_bars(ticker, exchange="US", period="1d")
        if df.empty:
            return pd.DataFrame()
        return completed_1m_bars(df, datetime.datetime.now(ET), ET_ZONE_NAME)
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return pd.DataFrame()

def run_us_engine(test_mode: bool = False):
    print("=" * 80)
    print("  STRATEGY v3.2.5 AUTONOMOUS CLOUD ENGINE: US WALL STREET SLEEVE")
    print(f"  Timestamp: {datetime.datetime.now().isoformat()} | Test Mode: {test_mode}")
    print("=" * 80)

    # GitHub cron is UTC-based. The workflow runs at both possible UTC offsets;
    # only the run that lands near 10:05 New York time is allowed to scan.
    if not test_mode:
        now_et = datetime.datetime.now(ET)

        # Check official US holidays (e.g., Thanksgiving)
        if now_et.date() in US_2026_HOLIDAYS:
            msg = (
                f"🛡️ [US ENGINE STAND DOWN] Today ({now_et.date()}) is an official "
                f"US market holiday (Thanksgiving / Closed). Engine standing down."
            )
            print(msg)
            send_telegram(msg)
            return

        current_time = now_et.time()
        earliest = datetime.time(10, 3)
        latest = datetime.time(10, 15)
        if current_time < earliest or current_time > latest:
            msg = (
                f"🛡️ [US ENGINE SKIPPED] Outside scan window. "
                f"Current New York time: {now_et:%Y-%m-%d %H:%M:%S %Z}. "
                f"Allowed: 10:03-10:15 ET. No signal generated."
            )
            print(msg)
            send_telegram(msg)
            return
        while datetime.datetime.now(ET).time() < datetime.time(10, 5):
            time.sleep(5)
        now_et = datetime.datetime.now(ET)
        if now_et.time() > latest:
            msg = f"🛡️ [US ENGINE SKIPPED] Scan startup missed the 10:15 ET cutoff ({now_et:%H:%M:%S %Z})."
            print(msg)
            send_telegram(msg)
            return

    scan_now_et = datetime.datetime.now(ET)
    scan_date = scan_now_et.date()

    # Step 1: Scan 8 Mega Sectors
    print("\n[Step 1] Scanning 8 US Mega Sectors (09:45-10:04 completed OR bars)...")
    sector_results = []
    
    for sec_name, tickers in US_SECTORS.items():
        moves = []
        candidates = []
        for sym in tickers:
            raw_df = fetch_1m_bars(sym)
            if raw_df.empty:
                continue
            df = completed_1m_bars(raw_df, scan_now_et, ET_ZONE_NAME)
            if df.empty:
                continue
            if not test_mode and not is_fresh_1m_data(raw_df, scan_now_et, ET_ZONE_NAME, max_age_seconds=180):
                print(f"[STALE DATA] Skipping {sym}; latest completed 1m bar is older than 180 seconds.")
                continue
            or_bars = exact_minute_window(df, scan_date, "09:45", "10:04", ET_ZONE_NAME)
            if or_bars is None:
                print(f"[INCOMPLETE OR] Skipping {sym}; expected all 20 bars from 09:45-10:04 ET.")
                continue
            
            o = float(or_bars["Open"].iloc[0])
            c = float(or_bars["Close"].iloc[-1])
            h = float(or_bars["High"].max())
            l = float(or_bars["Low"].min())
            m = ((c - o) / o) * 100.0
            moves.append(m)
            candidates.append({"sym": sym, "move": m, "orh": h, "orl": l, "df": df})

        if len(candidates) >= 2:
            breadth = (sum(1 for x in moves if x > 0) / len(moves)) * 100.0
            avg_m = sum(moves) / len(moves)
            sector_results.append({
                "sec": sec_name, "breadth": breadth, "avg_m": avg_m, "candidates": candidates
            })

    if not sector_results:
        print("❌ No qualifying US sectors found. Standing down.")
        stand_down_msg = (
            "🧪 [US TEST ONLY] 🛡️ [US ENGINE STAND DOWN] No sector met minimum constituent breadth."
            if test_mode
            else "🛡️ [US ENGINE STAND DOWN] No sector met minimum constituent breadth."
        )
        send_telegram(stand_down_msg)
        return

    # Rank Leader Sector
    sector_results.sort(key=lambda x: (x["breadth"], x["avg_m"]), reverse=True)
    leader = sector_results[0]
    print(f"🏆 Leader Sector: {leader['sec']} (Breadth: {leader['breadth']:.0f}% Green, Avg Move: {leader['avg_m']:+.2f}%)")

    # Select Primary Candidate (sweet spot momentum 0.50% to 1.80%)
    valid_cands = [c for c in leader["candidates"] if c["move"] > 0]
    if not valid_cands:
        tag = "🧪 [US TEST ONLY] " if test_mode else ""
        msg = f"{tag}🛡️ [US STAND DOWN] {scan_date.isoformat()}: leader sector has no positive candidate."
        print(msg)
        send_telegram(msg)
        return
    valid_cands.sort(key=lambda x: x["move"], reverse=True)
    sweet_spot = [c for c in valid_cands if 0.50 <= c["move"] <= 1.80]
    primary = sweet_spot[0] if sweet_spot else valid_cands[0]

    sym = primary["sym"]
    orh = primary["orh"]
    orl = primary["orl"]
    trig = round(orh * 1.0003, 2)
    sl = round(trig * 0.9900, 2)
    tp = round(trig * 1.0100, 2)

    tag = "🧪 <b>[US TEST ONLY — DIAGNOSTIC SCAN]</b>" if test_mode else "🎯 <b>[US PAPER SIGNAL]</b>"
    data_desc = (
        "Current-day Yahoo Finance 1-minute data; test-mode freshness gate bypassed"
        if test_mode
        else "Yahoo Finance 1-minute bars (freshness checked)"
    )
    exec_desc = (
        "Manual diagnostic test run; Step 2 live monitoring loop skipped."
        if test_mode
        else "Signal monitoring only; this script sends no broker orders."
    )

    scan_msg = (
        f"{tag} <b>US SCAN COMPLETE — {scan_date.isoformat()}</b>\n\n"
        f"• <b>#1 Sector:</b> {leader['sec']} ({leader['breadth']:.0f}% Green)\n"
        f"• <b>Primary Candidate:</b> {sym}\n"
        f"• <b>Completed ORH:</b> ${orh:.2f}\n"
        f"• <b>Pre-Declared Trigger:</b> >= ${trig:.2f}\n"
        f"• <b>1.00% Hard SL:</b> ${sl:.2f}\n"
        f"• <b>+1.00% Take Profit:</b> ${tp:.2f}\n"
        f"• <b>Execution Window:</b> 10:05-11:00 ET (55 Mins)\n"
        f"• <b>Data:</b> {data_desc}\n"
        f"• <b>Execution:</b> {exec_desc}"
    )
    send_telegram(scan_msg)
    print(f"\nLocked {sym}: Trigger >= ${trig:.2f}, SL=${sl:.2f}, TP=${tp:.2f}")

    if test_mode:
        print("Test mode complete. Exiting.")
        return

    # Step 2: process each newly completed 1-minute bar once, through 11:00 ET.
    window_start = datetime.datetime.combine(scan_date, datetime.time(10, 5), tzinfo=ET)
    window_end = datetime.datetime.combine(scan_date, datetime.time(11, 0), tzinfo=ET)
    monitor_start_et = datetime.datetime.now(ET)
    if monitor_start_et >= window_end:
        msg = (
            f"🛡️ [US STAND DOWN] {sym}: candidate scan completed after the 11:00 ET execution window. "
            f"No late/backfilled signal will be generated."
        )
        print(msg)
        send_telegram(msg)
        return

    # Do not replay bars that closed before the candidate was actually locked.
    boot_df = fetch_1m_bars(sym)
    boot_completed = completed_1m_bars(boot_df, monitor_start_et, ET_ZONE_NAME)
    last_processed = (
        boot_completed.index[-1]
        if not boot_completed.empty
        else pd.Timestamp(window_start - datetime.timedelta(minutes=1))
    )
    in_trade = False
    entry_fill = 0.0
    last_close = None
    guard = OutageAlertGuard()
    terminal = False

    print("\n[Step 2] Monitoring completed 1-minute bars until 11:00 ET (signal-only; no orders).")
    while not terminal:
        now_et = datetime.datetime.now(ET)
        raw_df = fetch_1m_bars(sym)
        closed = completed_1m_bars(raw_df, now_et, ET_ZONE_NAME)

        fresh = is_fresh_1m_data(raw_df, now_et, ET_ZONE_NAME, max_age_seconds=180)
        if not fresh:
            if guard.on_stale():
                ref_spot = fetch_live_reference_spot(sym, exchange="US")
                spot_info = f" (Ref Spot: ${ref_spot['price']:.2f} via {ref_spot['source']})" if ref_spot else ""
                warn = (
                    f"⚠️ [US DATA STALE] {sym}: latest completed 1-minute market-data bar "
                    f"is older than 180 seconds{spot_info}. Signal monitoring is paused safely "
                    f"(no unverified fills; no live orders sent)."
                )
                print(warn)
                send_telegram(warn)
        else:
            if guard.stale_polls > 0:
                if not closed.empty:
                    latest_ts = closed.index[-1]
                    latest_bar = closed.iloc[-1]
                    latest_c = float(latest_bar["Close"])
                    latest_h = float(latest_bar["High"])
                    latest_l = float(latest_bar["Low"])
                    bar_time = latest_ts.strftime("%H:%M:%S %Z")

                    # Slice all completed bars formed during the outage window
                    blackout_bars = (
                        closed.loc[closed.index > last_processed]
                        if last_processed is not None
                        else closed.iloc[[-1]]
                    )
                    if blackout_bars.empty:
                        blackout_bars = closed.iloc[[-1]]

                    recovery = evaluate_stale_recovery(
                        in_trade,
                        latest_bar,
                        trig,
                        sl,
                        tp,
                        blackout_bars=blackout_bars,
                        last_processed_ts=last_processed,
                        expected_latest_ts=latest_ts,
                        exchange="US",
                    )

                    if recovery["action"] == "RECOVERY_DATA_INCOMPLETE":
                        if guard.on_incomplete_recovery():
                            missing_preview = ", ".join(recovery["missing_bars"][:3])
                            if recovery["missing_count"] > 3:
                                missing_preview += f" (+{recovery['missing_count'] - 3} more)"
                            msg = (
                                f"⚠️ <b>[US DATA GAP / RECOVERY INCOMPLETE]</b>\n"
                                f"• <b>Stock:</b> {sym}\n"
                                f"• <b>Missing Candles:</b> {recovery['missing_count']} ({missing_preview})\n"
                                f"• <b>Action:</b> Signal monitoring paused; cannot safely determine trade state from incomplete data."
                            )
                            print(msg)
                            send_telegram(msg)
                        continue

                    # Recovery succeeded: clear outage & gap alert guards
                    guard.on_recovery_success()
                    last_processed = latest_ts

                    if recovery["action"] == "DISCARD_AND_RESUME":
                        msg = (
                            f"ℹ️ [US DATA RESTORED] {sym}: feed reconnected at {bar_time}. "
                            f"Blackout candles discarded to prevent retrospective backfilling. "
                            f"Resuming live forward monitoring."
                        )
                        print(msg)
                        send_telegram(msg)
                        continue
                    elif recovery["action"] == "OUTAGE_EXIT_STOP":
                        exit_price = recovery["exit_price"]
                        pnl = ((exit_price - entry_fill) / entry_fill) * 100.0 if in_trade else -1.0
                        msg = (
                            f"🛑 <b>[US PAPER SIGNAL — STOP OUTAGE EXIT]</b>\n"
                            f"• <b>Stock:</b> {sym}\n"
                            f"• <b>Observable Exit Price:</b> ${exit_price:.2f}\n"
                            f"• <b>Indicative P&L:</b> {pnl:.2f}%\n"
                            f"• <b>Reconnection Time:</b> {bar_time}\n"
                            f"• <b>Execution Uncertainty:</b> TRUE (Stop crossed during blackout; recorded at observable price)"
                        )
                        print(msg)
                        send_telegram(msg)
                        terminal = True
                        break
                    elif recovery["action"] == "OUTAGE_EXIT_TARGET":
                        exit_price = recovery["exit_price"]
                        pnl = ((exit_price - entry_fill) / entry_fill) * 100.0 if in_trade else 2.0
                        msg = (
                            f"🎯 <b>[US PAPER SIGNAL — TARGET LEVEL HIT (POST-OUTAGE)]</b>\n"
                            f"• <b>Stock:</b> {sym}\n"
                            f"• <b>Observable Exit Price:</b> ${exit_price:.2f}\n"
                            f"• <b>Indicative P&L:</b> {pnl:.2f}%\n"
                            f"• <b>Reconnection Time:</b> {bar_time}\n"
                            f"• <b>Execution Uncertainty:</b> TRUE (Target reached during/after feed recovery)"
                        )
                        print(msg)
                        send_telegram(msg)
                        terminal = True
                        break
                    else:
                        msg = (
                            f"ℹ️ [US DATA RESTORED] {sym}: feed resumed at {bar_time}. "
                            f"Trade active (Current: ${latest_c:.2f}, SL: ${sl:.2f}, TP: ${tp:.2f})."
                        )
                        print(msg)
                        send_telegram(msg)
                        continue

            stale_polls = 0
            stale_alert_sent = False
            if not closed.empty:
                post = closed.loc[
                    (closed.index >= pd.Timestamp(window_start))
                    & (closed.index < pd.Timestamp(window_end))
                ]
                new_bars = post.loc[post.index > last_processed]

                for ts, bar in new_bars.iterrows():
                    last_processed = ts
                    bar_h = float(bar["High"])
                    bar_l = float(bar["Low"])
                    bar_c = float(bar["Close"])
                    bar_o = float(bar["Open"])
                    last_close = bar_c
                    bar_time = ts.strftime("%H:%M:%S %Z")

                    if not in_trade:
                        if bar_h >= trig:
                            # A 1-minute OHLC candle cannot reveal intrabar order.
                            # If the same candle also crosses the stop, skip the
                            # ambiguous signal rather than inventing a fill.
                            if bar_l <= sl:
                                msg = (
                                    f"⚠️ [US AMBIGUOUS BAR — NO ENTRY] {sym} at {bar_time}: "
                                    f"both trigger and stop levels were inside one candle. Stand down."
                                )
                                print(msg)
                                send_telegram(msg)
                                terminal = True
                                break

                            in_trade = True
                            base_fill = bar_o if bar_o > trig else trig
                            entry_fill = round(base_fill * 1.0005, 2)
                            trig_msg = (
                                f"🚨 <b>[US PAPER SIGNAL] BREAKOUT LEVEL REACHED</b>\n\n"
                                f"• <b>Stock:</b> {sym}\n"
                                f"• <b>Reference Fill:</b> ${entry_fill:.2f}\n"
                                f"• <b>Trigger:</b> ${trig:.2f}\n"
                                f"• <b>Hard SL:</b> ${sl:.2f}\n"
                                f"• <b>TP:</b> ${tp:.2f}\n"
                                f"• <b>Bar time:</b> {bar_time}\n"
                                f"• <b>Note:</b> Signal only; no broker order was placed."
                            )
                            print(trig_msg)
                            send_telegram(trig_msg)
                    else:
                        # If SL and TP are both touched in one candle, assume SL first.
                        if bar_l <= sl:
                            exit_price = round(sl * 0.9995, 2)
                            pnl = ((exit_price - entry_fill) / entry_fill) * 100.0
                            msg = (
                                f"🛑 <b>[US PAPER SIGNAL — STOP LEVEL HIT]</b>\n"
                                f"• <b>Stock:</b> {sym}\n"
                                f"• <b>Reference Exit:</b> ${exit_price:.2f}\n"
                                f"• <b>Indicative P&L:</b> {pnl:.2f}%\n"
                                f"• <b>Bar time:</b> {bar_time}\n"
                                f"• <b>Note:</b> Signal only; no broker order was placed."
                            )
                            print(msg)
                            send_telegram(msg)
                            terminal = True
                            break
                        elif bar_h >= tp:
                            exit_price = round(tp * 0.9995, 2)
                            pnl = ((exit_price - entry_fill) / entry_fill) * 100.0
                            msg = (
                                f"🎯 <b>[US PAPER SIGNAL — TARGET LEVEL HIT]</b>\n"
                                f"• <b>Stock:</b> {sym}\n"
                                f"• <b>Reference Exit:</b> ${exit_price:.2f}\n"
                                f"• <b>Indicative P&L:</b> {pnl:+.2f}%\n"
                                f"• <b>Bar time:</b> {bar_time}\n"
                                f"• <b>Note:</b> Signal only; no broker order was placed."
                            )
                            print(msg)
                            send_telegram(msg)
                            terminal = True
                            break

        if terminal:
            break

        if now_et >= window_end:
            if in_trade and last_close is not None and fresh:
                exit_price = round(last_close * 0.9995, 2)
                pnl = ((exit_price - entry_fill) / entry_fill) * 100.0
                msg = (
                    f"🏁 <b>[US PAPER SIGNAL — WINDOW CLOSED]</b>\n"
                    f"• <b>Stock:</b> {sym}\n"
                    f"• <b>Reference Exit:</b> ${exit_price:.2f}\n"
                    f"• <b>Indicative P&L:</b> {pnl:+.2f}%\n"
                    f"• <b>Note:</b> Signal only; no broker order was placed."
                )
            elif in_trade:
                msg = (
                    f"⚠️ [US MONITOR ENDED] {sym}: no fresh final bar available. "
                    f"Exit price is unverified; manual review required."
                )
            else:
                msg = f"🛡️ [US STAND DOWN] {sym} did not reach trigger before 11:00 ET. No signal entry."
            print(msg)
            send_telegram(msg)
            break

        if guard.stale_polls >= 3 and not in_trade:
            msg = f"🛡️ [US STAND DOWN] {sym}: market data remained stale for 3 consecutive checks. No signal entry."
            print(msg)
            send_telegram(msg)
            break

        time.sleep(20)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Run in test mode")
    args = parser.parse_args()
    run_us_engine(test_mode=args.test)
