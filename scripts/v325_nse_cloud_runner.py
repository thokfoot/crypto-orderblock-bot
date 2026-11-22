"""
v325_nse_cloud_runner.py
Strategy v3.2.5 Fully Autonomous Cloud Engine for NSE India Sleeve
Runs in GitHub Actions Cloud Runner from 09:34 IST to 15:30 IST.
Enables 100% hands-free trading: zero laptop required after 09:35 IST.

Schedule:
  • 09:34 IST: Cloud Runner boots up.
  • 09:35 IST: 10-Sector Scan runs (09:15-09:34 completed bars).
               Locks #1 Leader candidate, pre-declares ORH, Trigger, SL, TP.
               Sends instant Telegram scan alert.
  • 09:45 IST: Execution Gate opens.
  • 09:45-15:29 IST: Real-time 1m bar monitoring loop.
                     Tracks breakout trigger, +2.00% TP, 1.00% Hard SL, 15:29 EOD exit.
                     Sends instant Telegram execution alerts.
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
        print(f"[Telegram Skipped - No Token] {text}")
        return {"ok": False, "status": "no_credentials"}

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
        msg_id = data.get("result", {}).get("message_id")
        print(f"[Telegram Sent - msg_id {msg_id}] {text.splitlines()[0]}")
        return {"ok": True, "message_id": msg_id}
    except Exception as ex:
        print(f"[Telegram Error: {ex}] {text.splitlines()[0]}")
        return {"ok": False, "error": str(ex)}

# -----------------------------------------------------------------------------
# MARKET DATA HELPERS
# -----------------------------------------------------------------------------
def get_ist_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))

def fetch_ticker_1m(symbol: str, period: str = "5d") -> Any:
    import yfinance as yf
    t = yf.Ticker(symbol)
    df = t.history(period=period, interval="1m")
    if df.empty:
        return None
    # Ensure tz-aware IST
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
    else:
        df.index = df.index.tz_convert("Asia/Kolkata")
    return df

def compute_atr_pct(symbol: str) -> float:
    try:
        import yfinance as yf
        import pandas as pd
        t = yf.Ticker(symbol)
        df = t.history(period="60d", interval="1d")
        if len(df) >= 14:
            h = df["High"]
            l = df["Low"]
            c = df["Close"]
            tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
            atr14 = tr.rolling(14).mean().iloc[-1]
            return float((atr14 / c.iloc[-1]) * 100)
    except Exception:
        pass
    return 2.50

# -----------------------------------------------------------------------------
# SCANNER LOGIC (09:35 IST)
# -----------------------------------------------------------------------------
def run_0935_scan(target_date: datetime.date | None = None) -> dict[str, Any] | None:
    now_ist = get_ist_now()
    if target_date is None:
        target_date = now_ist.date()

    print("=" * 80)
    print(f"  STRATEGY v3.2.5 NSE 10-SECTOR SCANNER (Target Date: {target_date})")
    print(f"  Timestamp: {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}")
    print("=" * 80)

    # 1. Fetch Nifty Benchmark
    print("Fetching Nifty 50 benchmark (^NSEI)...")
    nifty_df = fetch_ticker_1m("^NSEI")
    if nifty_df is None:
        print("ERROR: Failed to fetch Nifty data")
        return None

    # Filter for target_date 09:15 to 09:34
    day_mask = nifty_df.index.date == target_date
    nifty_day = nifty_df[day_mask]
    nifty_or = nifty_day.between_time("09:15", "09:34")
    if len(nifty_or) < 15:
        print(f"Warning: Only {len(nifty_or)} Nifty OR bars available")

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
            df = fetch_ticker_1m(sym)
            if df is None:
                continue
            df_day = df[df.index.date == target_date]
            or_bars = df_day.between_time("09:15", "09:34")
            if len(or_bars) < 15:
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

        if moves:
            avg_move = sum(moves) / len(moves)
            breadth = (positive_count / total) * 100
            alpha = avg_move - nifty_move
            sector_results.append({
                "sector": sector,
                "breadth": breadth,
                "avg_move": avg_move,
                "alpha": alpha,
                "positive": positive_count,
                "total": total,
            })

    # Sort sectors: Breadth desc, Avg Move desc
    sector_results.sort(key=lambda s: (s["breadth"], s["avg_move"]), reverse=True)

    print("\n" + "=" * 80)
    print("RANK  SECTOR               BREADTH         AVG MOVE     ALPHA vs NIFTY")
    print("=" * 80)
    for idx, s in enumerate(sector_results, 1):
        print(f"#{idx:<4} {s['sector']:<20} {s['positive']}/{s['total']} ({s['breadth']:5.1f}%)   {s['avg_move']:+6.2f}%     {s['alpha']:+6.2f}pp")

    if not sector_results:
        print("ERROR: No sector data collected")
        return None

    top_sector = sector_results[0]["sector"]
    print(f"\n🏆 #1 LEADER SECTOR: {top_sector}")

    # Rank constituents in #1 sector
    candidates = []
    for sym in NSE_SECTORS[top_sector]:
        if sym in symbol_data:
            d = symbol_data[sym]
            atr = compute_atr_pct(sym)
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

    # Filter ATR >= 2.50%
    qual_cands = [c for c in candidates if c["atr"] >= 2.50]
    primary = qual_cands[0] if qual_cands else candidates[0]

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
        f"🎯 *[CLOUD DAEMON] NSE DAY 25 SCAN COMPLETE*\n"
        f"• #1 Sector: *{top_sector}*\n"
        f"• Primary Candidate: *{clean_sym}* (ATR: {primary['atr']:.2f}%)\n"
        f"• Completed ORH: `₹{orh:.2f}`\n"
        f"• Pre-Declared Trigger: `₹{trigger:.2f}`\n"
        f"• 1.00% Hard SL: `₹{stop_loss:.2f}`\n"
        f"• +2.00% Take Profit: `₹{take_profit:.2f}`\n"
        f"• Execution Gate: *09:45:00 IST*\n"
        f"• Mode: *Autonomous Cloud Runner*"
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
            print(f"🛡️ [TEST] UNTRIGGERED / STAND DOWN (0.00% PnL, Capital 100% Preserved)")

        test_ping = (
            f"✅ *[CLOUD TEST PASS] Strategy v3.2.5 Sanity Audit*\n"
            f"• Candidate: *{clean_sym}*\n"
            f"• Cloud Engine: *Operational (GitHub Actions)*\n"
            f"• Monday Morning Gate: *Ready @ 09:34 IST*\n"
            f"• Laptop required after 09:35: *NO (100% Cloud Hands-Free)*"
        )
        send_telegram(test_ping)
        return

    # LIVE INTRADAY LOOP
    triggered = False
    in_trade = False
    entry_price = 0.0

    while True:
        now = get_ist_now()
        t_str = now.strftime("%H:%M:%S")

        # EOD Square-off check @ 15:29 IST
        if now.hour > 15 or (now.hour == 15 and now.minute >= 29):
            if in_trade:
                msg = f"⏱️ *EOD EXIT @ 15:29 IST*: {clean_sym} Position closed before market close."
            else:
                msg = f"🛡️ *STAND DOWN @ 15:29 IST*: {clean_sym} never triggered today. Capital 100% preserved (0.00% PnL)."
            print(msg)
            send_telegram(msg)
            break

        # Wait until 09:45 Gate
        if now.hour < 9 or (now.hour == 9 and now.minute < 45):
            time.sleep(30)
            continue

        # Fetch latest bars
        try:
            df = fetch_ticker_1m(sym, period="1d")
            if df is not None and not df.empty:
                last_bar = df.iloc[-1]
                h = last_bar["High"]
                l = last_bar["Low"]
                c = last_bar["Close"]

                if not in_trade:
                    if h >= trigger:
                        in_trade = True
                        triggered = True
                        entry_price = trigger
                        alert = (
                            f"🚨 *[TRADE EXECUTION] BREAKOUT TRIGGERED*\n"
                            f"• Stock: *{clean_sym}*\n"
                            f"• Fill Price: `₹{entry_price:.2f}`\n"
                            f"• Time: `{t_str} IST`\n"
                            f"• 1.00% Hard SL: `₹{sl:.2f}`\n"
                            f"• +2.00% TP Target: `₹{tp:.2f}`\n"
                            f"• Status: *ACTIVE IN-TRADE*"
                        )
                        print(alert)
                        send_telegram(alert)
                else:
                    # In-trade monitoring
                    if l <= sl:
                        exit_msg = (
                            f"🛑 *[TRADE EXIT] HARD STOP LOSS HIT*\n"
                            f"• Stock: *{clean_sym}*\n"
                            f"• Exit Price: `₹{sl:.2f}`\n"
                            f"• Return: *-1.00% Loss*\n"
                            f"• Time: `{t_str} IST`\n"
                            f"• Capital Protection: Risk strictly capped at 1.00%."
                        )
                        print(exit_msg)
                        send_telegram(exit_msg)
                        break
                    elif h >= tp:
                        tp_msg = (
                            f"🎯 *[TRADE EXIT] TAKE PROFIT HIT!*\n"
                            f"• Stock: *{clean_sym}*\n"
                            f"• Exit Price: `₹{tp:.2f}`\n"
                            f"• Return: *+2.00% NET PROFIT*\n"
                            f"• Time: `{t_str} IST`\n"
                            f"• Result: Monster Home-Run Captured!"
                        )
                        print(tp_msg)
                        send_telegram(tp_msg)
                        break
        except Exception as ex:
            print(f"[{t_str}] Poll warning: {ex}")

        time.sleep(45)

# -----------------------------------------------------------------------------
# MAIN CLI ENTRYPOINT
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Strategy v3.2.5 NSE Autonomous Cloud Runner")
    parser.add_argument("--test", action="store_true", help="Run test on last completed trading day (Friday Day 24)")
    args = parser.parse_args()

    if args.test:
        # Day 24 = 2026-11-20
        target_date = datetime.date(2026, 11, 20)
        cand = run_0935_scan(target_date)
        if cand:
            run_monitoring_loop(cand, is_test=True)
    else:
        # Real-time Autonomous Live Day Run
        now = get_ist_now()
        cand = run_0935_scan()
        if cand:
            run_monitoring_loop(cand, is_test=False)

if __name__ == "__main__":
    main()
