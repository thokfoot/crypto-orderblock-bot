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
from zoneinfo import ZoneInfo
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

ET = ZoneInfo("America/New_York")
EXPECTED_OR_BARS = 20
MAX_ENTRY_GAP_PCT = 0.0015  # Skip if the first eligible bar opens >0.15% above trigger.
MAX_DATA_STALENESS_SECONDS = 180

def get_et_now() -> datetime.datetime:
    """Return DST-aware US Eastern time."""
    return datetime.datetime.now(ET)

def fetch_1m_bars(ticker: str) -> pd.DataFrame:
    """Fetch 1-minute bars and normalize timestamps to America/New_York."""
    try:
        df = yf.Ticker(ticker).history(
            period="1d", interval="1m", prepost=False, auto_adjust=False
        )
        if df.empty:
            return pd.DataFrame()
        if df.index.tz is None:
            # yfinance normally supplies an exchange timezone; if it does not,
            # do not guess whether naive timestamps are UTC or Eastern.
            print(f"[DATA WARNING] {ticker}: timezone-naive 1m data rejected.")
            return pd.DataFrame()
        df.index = df.index.tz_convert(ET)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return pd.DataFrame()

def get_expected_or_index(session_date: datetime.date) -> pd.DatetimeIndex:
    start = datetime.datetime.combine(
        session_date, datetime.time(9, 45), tzinfo=ET
    )
    return pd.date_range(start=start, periods=EXPECTED_OR_BARS, freq="min")

def run_us_engine(test_mode: bool = False):
    now_et = get_et_now()
    print("=" * 80)
    print("  STRATEGY v3.2.5 US WALL STREET SIGNAL MONITOR")
    print(f"  Timestamp: {now_et.isoformat()} | Test Mode: {test_mode}")
    print("  MODE: SIGNAL / PAPER MONITOR ONLY — NO BROKER ORDERS ARE PLACED")
    print("=" * 80)

    # The opening range is 09:45-10:04 ET. Run only after the range is complete,
    # and refuse a stale scheduled run rather than scanning old prices.
    if not test_mode and not (
        datetime.time(10, 5) <= now_et.time().replace(tzinfo=None) <= datetime.time(10, 12)
    ):
        msg = (
            f"🛡️ [US ENGINE STAND DOWN] Scan started outside the permitted "
            f"10:05-10:12 ET window ({now_et.strftime('%H:%M:%S %Z')}). "
            "No candidate locked; no broker order was placed."
        )
        print(msg)
        send_telegram(msg)
        return

    session_date = now_et.date()
    print("\n[Step 1] Scanning 8 US sectors using 20 completed OR bars (09:45-10:04 ET)...")
    sector_results = []

    for sec_name, tickers in US_SECTORS.items():
        moves = []
        candidates = []
        for sym in tickers:
            df = fetch_1m_bars(sym)
            if df.empty:
                print(f"[DATA SKIP] {sym}: no 1-minute bars.")
                continue

            day_df = df[df.index.date == session_date]
            or_bars = day_df.between_time("09:45", "10:04", inclusive="both")
            expected = get_expected_or_index(session_date)
            if len(or_bars) < EXPECTED_OR_BARS or not expected.isin(or_bars.index).all():
                print(f"[DATA SKIP] {sym}: incomplete opening range "
                      f"({len(or_bars)}/{EXPECTED_OR_BARS} bars).")
                continue

            or_bars = or_bars.loc[expected]
            o = float(or_bars["Open"].iloc[0])
            c = float(or_bars["Close"].iloc[-1])
            h = float(or_bars["High"].max())
            l = float(or_bars["Low"].min())
            if o <= 0:
                print(f"[DATA SKIP] {sym}: invalid opening price.")
                continue
            move = ((c - o) / o) * 100.0
            moves.append(move)
            candidates.append({"sym": sym, "move": move, "orh": h, "orl": l})

        if len(candidates) >= 2:
            breadth = (sum(1 for item in moves if item > 0) / len(moves)) * 100.0
            avg_move = sum(moves) / len(moves)
            sector_results.append({
                "sec": sec_name,
                "breadth": breadth,
                "avg_m": avg_move,
                "candidates": candidates,
            })

    if not sector_results:
        msg = (
            f"🛡️ [US ENGINE STAND DOWN] No sector had sufficient complete "
            f"09:45-10:04 ET data for {session_date}. No trade signal generated."
        )
        print(msg)
        send_telegram(msg)
        return

    sector_results.sort(key=lambda item: (item["breadth"], item["avg_m"]), reverse=True)
    leader = sector_results[0]
    print(
        f"Leader Sector: {leader['sec']} "
        f"(Breadth: {leader['breadth']:.0f}% Green, Avg Move: {leader['avg_m']:+.2f}%)"
    )

    valid_cands = [item for item in leader["candidates"] if item["move"] > 0]
    valid_cands.sort(key=lambda item: item["move"], reverse=True)
    sweet_spot = [item for item in valid_cands if 0.50 <= item["move"] <= 1.80]

    # Do not silently fall back to an overextended or out-of-rule candidate.
    if not sweet_spot:
        msg = (
            f"🛡️ [US ENGINE STAND DOWN] Leader sector {leader['sec']} has no "
            "positive candidate in the 0.50%-1.80% momentum sweet spot. "
            "No trade signal generated."
        )
        print(msg)
        send_telegram(msg)
        return

    primary = sweet_spot[0]
    sym = primary["sym"]
    orh = primary["orh"]
    orl = primary["orl"]
    trigger = round(orh * 1.0003, 2)
    stop_loss = round(trigger * 0.9900, 2)
    take_profit = round(trigger * 1.0100, 2)

    scan_msg = (
        f"🎯 <b>[US PAPER SIGNAL SCAN] {session_date.isoformat()}</b>\n\n"
        f"• <b>#1 Sector:</b> {leader['sec']} ({leader['breadth']:.0f}% Green)\n"
        f"• <b>Primary Candidate:</b> {sym}\n"
        f"• <b>Completed ORH:</b> ${orh:.2f}\n"
        f"• <b>Pre-declared Trigger:</b> >= ${trigger:.2f}\n"
        f"• <b>Modelled Stop:</b> ${stop_loss:.2f}\n"
        f"• <b>Modelled Target:</b> ${take_profit:.2f}\n"
        f"• <b>Monitoring Window:</b> 10:05-11:00 ET\n"
        f"• <b>Mode:</b> Paper/signal only; no broker orders are placed."
    )
    send_telegram(scan_msg)
    print(
        f"Locked {sym}: Trigger >= ${trigger:.2f}, "
        f"modelled SL=${stop_loss:.2f}, TP=${take_profit:.2f}"
    )

    if test_mode:
        print("[TEST MODE] Scan completed; live monitoring was not started.")
        return

    print("\n[Step 2] Monitoring completed 1-minute bars from 10:05 through 10:59 ET.")
    in_trade = False
    finished = False
    entry_fill = 0.0
    last_processed = None
    last_close = None
    stale_alert_sent = False

    while not finished:
        now_et = get_et_now()
        now_floor = now_et.replace(second=0, microsecond=0)

        # At 11:00 ET, settle only from the latest completed in-window bar.
        if now_et.time().replace(tzinfo=None) >= datetime.time(11, 0):
            if in_trade and last_close is not None:
                exit_price = round(last_close * 0.9995, 2)  # modelled adverse exit slippage
                pnl_pct = ((exit_price - entry_fill) / entry_fill) * 100.0
                risk_per_share = max(entry_fill - stop_loss, 0.01)
                r_multiple = ((exit_price - entry_fill) / risk_per_share)
                msg = (
                    f"🏁 <b>[US PAPER WINDOW SETTLEMENT]</b>\n"
                    f"• <b>Stock:</b> {sym}\n"
                    f"• <b>Modelled Entry:</b> ${entry_fill:.2f}\n"
                    f"• <b>Modelled Exit:</b> ${exit_price:.2f}\n"
                    f"• <b>Illustrative P&amp;L:</b> {pnl_pct:+.2f}% ({r_multiple:+.2f}R)\n"
                    f"• <b>Last completed bar:</b> {last_processed.strftime('%H:%M %Z') if last_processed else 'N/A'}\n"
                    f"• <b>Mode:</b> Simulation only; no actual position was held."
                )
            elif in_trade:
                msg = (
                    f"⚠️ [US PAPER MONITOR INCOMPLETE] {sym}: session ended but no "
                    "valid completed-bar exit price was available. Review data manually."
                )
            else:
                msg = (
                    f"🛡️ [US PAPER STAND DOWN @ 11:00 ET] {sym} did not produce "
                    "a valid simulated entry. No broker order was placed."
                )
            print(msg)
            send_telegram(msg)
            break

        if now_et.time().replace(tzinfo=None) < datetime.time(10, 5):
            time.sleep(10)
            continue

        df = fetch_1m_bars(sym)
        if df.empty:
            time.sleep(20)
            continue

        window_bars = df.between_time("10:05", "10:59", inclusive="both")
        # Bar timestamps mark the start of a minute. Only process bars whose
        # minute has fully closed; never trade on an in-progress candle.
        completed = window_bars[window_bars.index < now_floor]
        if completed.empty:
            time.sleep(20)
            continue

        latest_bar_time = completed.index[-1]
        age_seconds = (now_et - latest_bar_time).total_seconds()
        if age_seconds > MAX_DATA_STALENESS_SECONDS:
            if not stale_alert_sent:
                msg = (
                    f"⚠️ [US DATA STALE — MONITOR STOPPED] {sym}: latest completed "
                    f"bar is {age_seconds:.0f}s old. No further simulated entries/exits "
                    "will be inferred from stale data."
                )
                print(msg)
                send_telegram(msg)
                stale_alert_sent = True
            break

        new_bars = completed if last_processed is None else completed[completed.index > last_processed]
        for bar_time, bar in new_bars.iterrows():
            last_processed = bar_time
            bar_open = float(bar["Open"])
            bar_high = float(bar["High"])
            bar_low = float(bar["Low"])
            last_close = float(bar["Close"])

            if not in_trade:
                if bar_high >= trigger:
                    if bar_open > trigger * (1 + MAX_ENTRY_GAP_PCT):
                        msg = (
                            f"⏭️ [US PAPER ENTRY SKIPPED] {sym}: opening gap exceeded "
                            f"the 0.15% limit. No simulated entry."
                        )
                        print(msg)
                        send_telegram(msg)
                        finished = True
                        break

                    entry_fill = round(max(bar_open, trigger) * 1.0005, 2)
                    in_trade = True
                    msg = (
                        f"🚨 <b>[US PAPER SIGNAL TRIGGERED]</b>\n"
                        f"• <b>Stock:</b> {sym}\n"
                        f"• <b>Modelled Entry:</b> ${entry_fill:.2f}\n"
                        f"• <b>Trigger:</b> ${trigger:.2f}\n"
                        f"• <b>Modelled Stop:</b> ${stop_loss:.2f}\n"
                        f"• <b>Modelled Target:</b> ${take_profit:.2f}\n"
                        f"• <b>Time:</b> {bar_time.strftime('%H:%M %Z')}\n"
                        f"• <b>Mode:</b> Simulation only; no broker order was sent."
                    )
                    print(msg)
                    send_telegram(msg)

                    # If one OHLC candle spans entry and stop, its intrabar
                    # ordering is unknowable. Use the conservative stop outcome.
                    if bar_low <= stop_loss:
                        exit_price = round(stop_loss * 0.9995, 2)
                        pnl_pct = ((exit_price - entry_fill) / entry_fill) * 100.0
                        stop_msg = (
                            f"🛑 [US PAPER AMBIGUOUS BAR — CONSERVATIVE STOP] {sym} "
                            f"same 1-minute bar crossed trigger and stop. "
                            f"Assumed modelled stop exit ${exit_price:.2f}; "
                            f"illustrative P&amp;L {pnl_pct:+.2f}%. No actual order."
                        )
                        print(stop_msg)
                        send_telegram(stop_msg)
                        finished = True
                        break
            else:
                # Conservative ordering: if TP and SL occur in the same OHLC
                # candle, assume the stop was reached first.
                if bar_low <= stop_loss:
                    exit_price = round(stop_loss * 0.9995, 2)
                    pnl_pct = ((exit_price - entry_fill) / entry_fill) * 100.0
                    risk_per_share = max(entry_fill - stop_loss, 0.01)
                    r_multiple = (exit_price - entry_fill) / risk_per_share
                    stop_msg = (
                        f"🛑 <b>[US PAPER STOP LEVEL REACHED]</b>\n"
                        f"• <b>Stock:</b> {sym}\n"
                        f"• <b>Modelled Exit:</b> ${exit_price:.2f}\n"
                        f"• <b>Illustrative P&amp;L:</b> {pnl_pct:+.2f}% ({r_multiple:+.2f}R)\n"
                        f"• <b>Mode:</b> Simulation only; no actual order was placed."
                    )
                    print(stop_msg)
                    send_telegram(stop_msg)
                    finished = True
                    break
                if bar_high >= take_profit:
                    exit_price = round(take_profit * 0.9995, 2)
                    pnl_pct = ((exit_price - entry_fill) / entry_fill) * 100.0
                    risk_per_share = max(entry_fill - stop_loss, 0.01)
                    r_multiple = (exit_price - entry_fill) / risk_per_share
                    tp_msg = (
                        f"🎯 <b>[US PAPER TARGET REACHED]</b>\n"
                        f"• <b>Stock:</b> {sym}\n"
                        f"• <b>Modelled Exit:</b> ${exit_price:.2f}\n"
                        f"• <b>Illustrative P&amp;L:</b> {pnl_pct:+.2f}% ({r_multiple:+.2f}R)\n"
                        f"• <b>Mode:</b> Simulation only; no actual order was placed."
                    )
                    print(tp_msg)
                    send_telegram(tp_msg)
                    finished = True
                    break

        if not finished:
            time.sleep(20)

    print("[US ENGINE COMPLETE] Signal monitor finished. No broker orders were placed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Run in test mode")
    args = parser.parse_args()
    run_us_engine(test_mode=args.test)
