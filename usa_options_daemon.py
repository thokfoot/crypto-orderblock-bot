"""
usa_options_daemon.py
=====================
Autonomous Production Daemon for SPY Hedged Options Spread (US Market).

[QUANT AUDIT COUNCIL CONSENSUS NOTICE]:
This module is running in PAPER-SIMULATION SANDBOX ONLY.
Live capital deployment is quarantined per Council Consensus until a full
Black-Scholes-Merton Greek engine (Delta, Gamma, Vega, intraday Theta decay)
and real multi-leg Cboe options chain integration are completed.

Features:
- Market Clock: Automatically tracks US Market (09:30 - 16:00 EST / America/New_York).
- Auto-Sleep: Sleeps outside market hours, wakes up Monday at 09:30 EST (19:00 IST).
- Strategy: 100% Hedged SPY Bull Call Spread (ATM Long Call + OTM Short Call).
- Risk Controls: Spot-guided Stop Loss (-0.30% underlying SPY) & +50% Take-Profit.
- Telegram Alerts: Direct alerts to @trade234_bot on Entry, Exit, and Daily P&L.
- Virtual Capital: $2,000.00 initial capital.
"""

import os
import sys
import time
import json
import logging
import urllib.request
import urllib.parse
from datetime import datetime, time as dtime
import pytz
import yfinance as yf

# Configuration (Dedicated USA Options Bot: @trade234_bot)
TELEGRAM_BOT_TOKEN = os.environ.get("USA_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "8453830677:AAE5ufDdwU-lTyrw1taYlIFt5aoQLeErXkI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "988970998")
INITIAL_CAPITAL = 2000.0
POLL_INTERVAL_SECS = 60
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "0"))  # 0 = infinite loop, >0 = bounded cloud run

# Market Hours (America/New_York)
TZ_NY = pytz.timezone("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
ENTRY_START = dtime(10, 0)
ENTRY_END = dtime(14, 30)
SQUARE_OFF = dtime(15, 45)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("usa_options.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger("USABot")

def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or "placeholder" in TELEGRAM_BOT_TOKEN:
        log.info(f"[TELEGRAM SIMULATED]: {msg}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            pass
    except Exception as e:
        try:
            payload_plain = urllib.parse.urlencode({
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg.replace("*", "").replace("`", "")
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload_plain, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                pass
        except Exception as e2:
            log.warning(f"Telegram failed: {e2}")

class USAOptionsTrader:
    def __init__(self):
        self.capital = INITIAL_CAPITAL
        self.active_trade = None
        self.last_trade_date = None
        self.load_state()

    def load_state(self):
        paths = ["data/usa_dashboard.json", "usa_dashboard.json"]
        for p in paths:
            if os.path.exists(p) and os.path.getsize(p) > 5:
                try:
                    with open(p, "r") as f:
                        data = json.load(f)
                        self.capital = data.get("capital", INITIAL_CAPITAL)
                        self.active_trade = data.get("active_trade", None)
                        self.last_trade_date = data.get("last_trade_date", None)
                        break
                except Exception:
                    pass

    def save_state(self):
        data = {
            "capital": round(self.capital, 2),
            "active_trade": self.active_trade,
            "last_trade_date": self.last_trade_date,
            "updated_at": datetime.now(pytz.utc).isoformat()
        }
        os.makedirs("data", exist_ok=True)
        for p in ["data/usa_dashboard.json", "usa_dashboard.json"]:
            try:
                temp_p = f"{p}.tmp.{os.getpid()}"
                with open(temp_p, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(temp_p, p)
            except Exception as e:
                log.warning(f"Could not atomically save to {p}: {e}")

    def is_market_open(self):
        now_ny = datetime.now(TZ_NY)
        # Weekday check (0 = Mon, 4 = Fri, 5/6 = Sat/Sun)
        if now_ny.weekday() >= 5:
            return False, "Weekend (Market Closed)"
        t = now_ny.time()
        if t < MARKET_OPEN or t >= MARKET_CLOSE:
            return False, f"Outside Market Hours ({t.strftime('%H:%M')} NY)"
        return True, "Market Open"

    def run_cycle(self):
        is_open, status_str = self.is_market_open()
        now_ny = datetime.now(TZ_NY)
        t = now_ny.time()
        today_str = now_ny.strftime("%Y-%m-%d")

        if not is_open:
            log.info(f"[USA CLOCK] {status_str}. Sleeping until market open...")
            return

        # Fetch live SPY spot
        try:
            spy_ticker = yf.Ticker("SPY")
            df = spy_ticker.history(period="1d", interval="1m")
            if df.empty:
                return
            current_spot = float(df.iloc[-1]["Close"])
        except Exception as e:
            log.warning(f"Failed to fetch SPY price: {e}")
            return

        # 1. Manage Active Trade
        if self.active_trade:
            entry_spot = self.active_trade["entry_spot"]
            entry_cost = self.active_trade["entry_cost"]

            # Spot Stop Loss: Exit if SPY moves against trade by 0.30%
            hit_sl = current_spot <= entry_spot * 0.9970
            # Spot Take Profit: +0.60% underlying move (~ +50% on option spread)
            hit_tp = current_spot >= entry_spot * 1.0060
            # End of day square-off (3:45 PM EST)
            hit_eod = t >= SQUARE_OFF

            if hit_sl or hit_tp or hit_eod:
                reason = "TAKE_PROFIT" if hit_tp else ("STOP_LOSS" if hit_sl else "EOD_SQUARE_OFF")
                pnl = (entry_cost * 0.50) if hit_tp else (-(entry_cost * 0.30) if hit_sl else 0.0)
                self.capital += pnl

                msg = (
                    f"🇺🇸 *USA SPY TRADE CLOSED: {reason}*\n\n"
                    f"• *Exit SPY Price:* `${current_spot:,.2f}`\n"
                    f"• *Realized P&L:* `${pnl:+,.2f}`\n"
                    f"• *Account Balance:* `${self.capital:,.2f}`"
                )
                log.info(f"Closed SPY Trade: PnL=${pnl:.2f} ({reason})")
                send_telegram(msg)

                self.active_trade = None
                self.last_trade_date = today_str
                self.save_state()
            return

        # 2. Check for New Entry (Between 10:00 AM and 2:30 PM EST)
        # Council Compliance: Max 1 trade per day to prevent re-entry death spirals
        if ENTRY_START <= t <= ENTRY_END and not self.active_trade:
            if self.last_trade_date == today_str:
                log.info("Daily trade limit reached for SPY (1 trade/day). Skipping entry.")
                return

            strike_long = round(current_spot)
            strike_short = strike_long + 2
            est_cost = 120.0  # $120 estimated spread debit

            self.active_trade = {
                "symbol": "SPY",
                "structure": f"Bull Call Spread ({strike_long}/{strike_short})",
                "entry_spot": current_spot,
                "entry_cost": est_cost,
                "entry_time": now_ny.strftime("%Y-%m-%d %H:%M:%S EST")
            }
            self.last_trade_date = today_str
            self.save_state()

            msg = (
                f"🇺🇸 *NEW USA SPY SPREAD OPENED*\n\n"
                f"• *Underlying:* `SPY @ ${current_spot:,.2f}`\n"
                f"• *Structure:* `Bull Call Spread {strike_long}/{strike_short}`\n"
                f"• *Cost / Max Risk:* `${est_cost:,.2f}`\n"
                f"• *Take Profit:* `+50%` ($60.00)\n"
                f"• *Stop Loss:* `Spot -0.30%` (Hedging protected)\n"
                f"• *Zero Overnight Risk:* Auto-exit at 3:45 PM EST"
            )
            log.info(f"Opened SPY spread @ ${current_spot:.2f}")
            send_telegram(msg)

def main():
    log.info("Starting Autonomous 24/7 USA Options Paper Trading Daemon...")
    trader = USAOptionsTrader()
    send_telegram("🚀 *USA SPY Options 24/7 Paper Trading Daemon Started*\nClock configured for New York Market (Mon-Fri 09:30-16:00 EST).")

    if RUN_MINUTES > 0:
        log.info(f"Cloud Runner Mode: Active for {RUN_MINUTES} minutes.")
        start_t = time.time()
        end_t = start_t + (RUN_MINUTES * 60)
        while time.time() < end_t:
            try:
                trader.run_cycle()
                time.sleep(POLL_INTERVAL_SECS)
            except Exception as e:
                log.error(f"Error in USA cycle: {e}")
                time.sleep(10)
        trader.save_state()
        log.info("USA Cloud Runner finished cleanly. State saved.")
    else:
        while True:
            try:
                trader.run_cycle()
                time.sleep(POLL_INTERVAL_SECS)
            except KeyboardInterrupt:
                log.info("USA Bot stopped by user.")
                break
            except Exception as e:
                log.error(f"Error in USA loop: {e}")
                time.sleep(10)

if __name__ == "__main__":
    main()