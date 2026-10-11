"""
usa_options_daemon.py
=====================
Autonomous 24/7 Production Daemon for SPY Hedged Options Spread (US Market).

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

# Configuration
TELEGRAM_BOT_TOKEN = "8232616481:AAFnxgOYCN9WxjF-LCUM9lokJ5b5rXAbZzg"
TELEGRAM_CHAT_ID = "988970998"
INITIAL_CAPITAL = 2000.0
POLL_INTERVAL_SECS = 60

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
        log.warning(f"Telegram failed: {e}")

class USAOptionsTrader:
    def __init__(self):
        self.capital = INITIAL_CAPITAL
        self.active_trade = None
        self.load_state()

    def load_state(self):
        if os.path.exists("usa_dashboard.json"):
            try:
                with open("usa_dashboard.json", "r") as f:
                    data = json.load(f)
                    self.capital = data.get("capital", INITIAL_CAPITAL)
                    self.active_trade = data.get("active_trade", None)
            except Exception:
                pass

    def save_state(self):
        data = {
            "capital": round(self.capital, 2),
            "active_trade": self.active_trade,
            "updated_at": datetime.now(pytz.utc).isoformat()
        }
        with open("usa_dashboard.json", "w") as f:
            json.dump(data, f, indent=2)

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
                    f"• *Exit SPY Price:*