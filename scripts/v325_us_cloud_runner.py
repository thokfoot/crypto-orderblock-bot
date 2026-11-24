"""
v325_us_cloud_runner.py
Strategy v3.2.5 Fully Autonomous Cloud Engine for US Wall Street Sleeve
Runs in GitHub Actions Cloud Runner from 10:04 ET to 11:05 ET (20:34 to 21:35 IST).
Enables 100% hands-free trading for the evening US sleeve:
- 10:05 ET: 8-Mega-Sector Scan runs (09:45-10:04 completed 20 OR bars).
           Locks #1 Leader candidate, pre-declares ORH, Trigger, SL, TP.
           Sends instant Telegram scan alert.
- 10:05-11:00 ET: Real-time 1m bar live monitoring loop (55-minute holding window).
                  Tracks breakout trigger, +1.00% TP, 1.00% Hard SL, 11:00 ET window exit.
                  Sends instant Telegram execution and settlement alerts.
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

# -----------------------------------------------------------------------------
# TELEGRAM DISPATCHER
# -----------------------------------------------------------------------------
def send_telegram(text: str) -> dict[str, Any]:
    token = os.getenv("AUTOMATION_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("AUTOMATION_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[TELEGRAM SKIPPED] Missing credentials.")
        return {"status": "skipped"}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
        return {"status": "error", "message": str(e)}

def fetch_1m_bars(ticker: str) -> pd.DataFrame:
    try:
        t = yf.Ticker(ticker)
        df = t.history(period="1d", interval="1m")
        if df.empty:
            return pd.DataFrame()
        return df
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return pd.DataFrame()

def run_us_engine(test_mode: bool = False):
    print("=" * 80)
    print("  STRATEGY v3.2.5 AUTONOMOUS CLOUD ENGINE: US WALL STREET SLEEVE")
    print(f"  Timestamp: {datetime.datetime.now().isoformat()} | Test Mode: {test_mode}")
    print("=" * 80)

    # Step 1: Scan 8 Mega Sectors
    print("\n[Step 1] Scanning 8 US Mega Sectors (09:45-10:04 completed OR bars)...")
    sector_results = []
    
    for sec_name, tickers in US_SECTORS.items():
        moves = []
        candidates = []
        for sym in tickers:
            df = fetch_1m_bars(sym)
            if df.empty:
                continue
            or_bars = df.between_time("09:45", "10:04")
            if len(or_bars) < 15 and not test_mode:
                continue
            if len(or_bars) == 0:
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
        send_telegram("🛡️ [US ENGINE STAND DOWN] No sector met minimum constituent breadth.")
        return

    # Rank Leader Sector
    sector_results.sort(key=lambda x: (x["breadth"], x["avg_m"]), reverse=True)
    leader = sector_results[0]
    print(f"🏆 Leader Sector: {leader['sec']} (Breadth: {leader['breadth']:.0f}% Green, Avg Move: {leader['avg_m']:+.2f}%)")

    # Select Primary Candidate (sweet spot momentum 0.50% to 1.80%)
    valid_cands = [c for c in leader["candidates"] if c["move"] > 0]
    if not valid_cands:
        print("❌ No positive moving candidate in leader sector.")
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

    scan_msg = (
        f"🎯 <b>[US CLOUD DAEMON] US DAY 25 SCAN COMPLETE</b>\n\n"
        f"• <b>#1 Sector:</b> {leader['sec']} ({leader['breadth']:.0f}% Green)\n"
        f"• <b>Primary Candidate:</b> {sym}\n"
        f"• <b>Completed ORH:</b> ${orh:.2f}\n"
        f"• <b>Pre-Declared Trigger:</b> >= ${trig:.2f}\n"
        f"• <b>1.00% Hard SL:</b> ${sl:.2f}\n"
        f"• <b>+1.00% Take Profit:</b> ${tp:.2f}\n"
        f"• <b>Execution Window:</b> 10:05-11:00 ET (55 Mins)\n"
        f"• <b>Mode:</b> Autonomous Cloud Runner"
    )
    send_telegram(scan_msg)
    print(f"\nLocked {sym}: Trigger >= ${trig:.2f}, SL=${sl:.2f}, TP=${tp:.2f}")

    if test_mode:
        print("Test mode complete. Exiting.")
        return

    # Step 2: 55-Minute Real-Time Polling Loop (10:05 to 11:00 ET)
    print("\n[Step 2] Entering 55-Minute Real-Time Monitoring Loop (until 11:00 ET)...")
    in_trade = False
    entry_fill = 0.0

    while True:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_et = now_utc.astimezone(datetime.timezone(datetime.timedelta(hours=-5)))
        current_time_str = now_et.strftime("%H:%M:%S")

        df = fetch_1m_bars(sym)
        if not df.empty:
            post = df.between_time("10:05", "11:05")
            if not post.empty:
                last_bar = post.iloc[-1]
                bar_h = float(last_bar["High"])
                bar_l = float(last_bar["Low"])
                bar_c = float(last_bar["Close"])
                bar_o = float(last_bar["Open"])

                # Check Trigger
                if not in_trade:
                    if bar_h >= trig:
                        in_trade = True
                        base_fill = bar_o if bar_o > trig else trig
                        entry_fill = round(base_fill * 1.0005, 2)  # 5 bps entry adverse slippage
                        trig_msg = (
                            f"🚨 <b>[US TRADE EXECUTION] BREAKOUT TRIGGERED</b>\n\n"
                            f"• <b>Stock:</b> {sym}\n"
                            f"• <b>Fill Price:</b> ${entry_fill:.2f}\n"
                            f"• <b>Trigger Level:</b> ${trig:.2f}\n"
                            f"• <b>Hard SL:</b> ${sl:.2f}\n"
                            f"• <b>TP Target:</b> ${tp:.2f}\n"
                            f"• <b>Time:</b> {current_time_str} ET\n"
                            f"• <b>Status:</b> ACTIVE IN-TRADE"
                        )
                        send_telegram(trig_msg)
                        print(f"🚨 TRIGGERED @ ${entry_fill:.2f} @ {current_time_str} ET!")

                # In-trade tracking
                else:
                    if bar_h >= tp:
                        exit_price = round(tp * 0.9995, 2)
                        pnl = ((exit_price - entry_fill) / entry_fill) * 100.0
                        tp_msg = (
                            f"🎯 <b>[US TAKE PROFIT HIT] TARGET REACHED</b>\n\n"
                            f"• <b>Stock:</b> {sym}\n"
                            f"• <b>Exit Price:</b> ${exit_price:.2f}\n"
                            f"• <b>Net P&L:</b> +{pnl:.2f}% (+1.00R WIN) 🚀\n"
                            f"• <b>Time:</b> {current_time_str} ET"
                        )
                        send_telegram(tp_msg)
                        print(f"🎯 TP HIT @ ${exit_price:.2f} (+{pnl:.2f}%)!")
                        break

                    elif bar_l <= sl:
                        exit_price = round(sl * 0.9995, 2)
                        pnl = ((exit_price - entry_fill) / entry_fill) * 100.0
                        sl_msg = (
                            f"🛑 <b>[US STOP LOSS HIT] HARD STOP EXECUTED</b>\n\n"
                            f"• <b>Stock:</b> {sym}\n"
                            f"• <b>Exit Price:</b> ${exit_price:.2f}\n"
                            f"• <b>Net P&L:</b> {pnl:.2f}% (-1.00R Loss)\n"
                            f"• <b>Time:</b> {current_time_str} ET\n"
                            f"• <b>Capital Protection:</b> Risk strictly capped."
                        )
                        send_telegram(sl_msg)
                        print(f"🛑 SL HIT @ ${exit_price:.2f} ({pnl:.2f}%)!")
                        break

        # Check 11:00 ET window close
        if now_et.hour >= 11:
            print(f"11:00 ET Window closed at {current_time_str} ET.")
            if in_trade:
                exit_price = round(bar_c * 0.9995, 2)
                pnl = ((exit_price - entry_fill) / entry_fill) * 100.0
                win_msg = (
                    f"🏆 <b>[US 55-MIN WINDOW SETTLEMENT] SESSION COMPLETE</b>\n\n"
                    f"• <b>Stock:</b> {sym}\n"
                    f"• <b>Entry Fill:</b> ${entry_fill:.2f}\n"
                    f"• <b>Window Exit:</b> ${exit_price:.2f}\n"
                    f"• <b>Net P&L:</b> {pnl:+.2f}% ({pnl/1.05:+.2f}R)\n"
                    f"• <b>Holding Duration:</b> 55 Mins (10:05-11:00 ET)\n"
                    f"• <b>Status:</b> SETTLED & LOCKED"
                )
                send_telegram(win_msg)
                print(f"🏆 WINDOW EXIT @ ${exit_price:.2f} ({pnl:+.2f}%)!")
            else:
                stand_msg = f"🛡️ <b>[US STAND DOWN @ 11:00 ET]</b> {sym} stayed below trigger ${trig:.2f}. Capital 100% preserved (0.00% PnL)."
                send_telegram(stand_msg)
                print(f"🛡️ Untriggered. Stood down at 11:00 ET.")
            break

        time.sleep(60)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Run in test mode")
    args = parser.parse_args()
    run_us_engine(test_mode=args.test)
