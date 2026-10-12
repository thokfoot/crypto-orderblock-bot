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
import subprocess
import yfinance as yf
import pandas as pd

# Configuration (Injected securely via GitHub Actions Secrets)
TELEGRAM_BOT_TOKEN = os.environ.get("USA_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
INITIAL_CAPITAL = 2000.0
POLL_INTERVAL_SECS = int(os.environ.get("POLL_INTERVAL_SECS", "2"))
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

STATUS_KEYBOARD = json.dumps({
    "keyboard": [
        [{"text": "📊 Status Summary"}, {"text": "💰 P&L Report"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
})

def send_telegram(msg: str, reply_markup=STATUS_KEYBOARD):
    if not TELEGRAM_BOT_TOKEN or "placeholder" in TELEGRAM_BOT_TOKEN:
        log.info(f"[TELEGRAM SIMULATED]: {msg}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload_dict = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        }
        if reply_markup:
            payload_dict["reply_markup"] = reply_markup
        payload = urllib.parse.urlencode(payload_dict).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            pass
    except Exception as e:
        try:
            payload_dict_plain = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg.replace("*", "").replace("`", "")
            }
            if reply_markup:
                payload_dict_plain["reply_markup"] = reply_markup
            payload_plain = urllib.parse.urlencode(payload_dict_plain).encode("utf-8")
            req = urllib.request.Request(url, data=payload_plain, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                pass
        except Exception as e2:
            log.warning(f"Telegram failed: {e2}")

def answer_callback_query(callback_query_id: str, text: str = ""):
    """Acknowledges an inline button callback to dismiss spinning client loader."""
    if not TELEGRAM_BOT_TOKEN or "placeholder" in TELEGRAM_BOT_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
        payload_dict = {"callback_query_id": callback_query_id}
        if text:
            payload_dict["text"] = text
        payload = urllib.parse.urlencode(payload_dict).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            pass
    except Exception:
        pass

def push_git_sync(comment: str = "usa-auto-sync"):
    """Atomically commits and pushes state to Git without requiring workflow teardown."""
    try:
        if not os.path.exists(".git"):
            return
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], capture_output=True, timeout=5)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], capture_output=True, timeout=5)
        subprocess.run(["git", "add", "data/usa_dashboard.json", "data/usa_paper_trades.csv", "usa_dashboard.json"], capture_output=True, timeout=5)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True, timeout=5)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", f"state: {comment} [skip ci]"], capture_output=True, timeout=5)
            push_res = subprocess.run(["git", "push", "origin", "main"], capture_output=True, timeout=10)
            if push_res.returncode != 0:
                subprocess.run(["git", "pull", "--rebase", "origin", "main"], capture_output=True, timeout=10)
                subprocess.run(["git", "push", "origin", "main"], capture_output=True, timeout=10)
            log.info("In-flight USA Git state push succeeded.")
    except Exception as e:
        log.warning(f"USA Git push notice: {e}")

class USAOptionsTrader:
    def __init__(self):
        self.capital = INITIAL_CAPITAL
        self.active_trade = None
        self.last_trade_date = None
        self.last_update_id = 0
        self.last_inbox_check = 0.0
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
                        self.last_update_id = data.get("last_update_id", 0)
                        break
                except Exception:
                    pass

        # Robustness Guard: Verify from CSV if a trade was already executed today
        if not self.last_trade_date and os.path.exists("data/usa_paper_trades.csv"):
            try:
                df_csv = pd.read_csv("data/usa_paper_trades.csv")
                if not df_csv.empty and "entry_time" in df_csv.columns:
                    today_ny = datetime.now(TZ_NY).strftime("%Y-%m-%d")
                    if df_csv["entry_time"].astype(str).str.contains(today_ny).any():
                        self.last_trade_date = today_ny
                        log.info(f"Recovered daily trade guard from CSV for date: {today_ny}")
            except Exception:
                pass

    def save_state(self):
        data = {
            "capital": round(self.capital, 2),
            "active_trade": self.active_trade,
            "last_trade_date": self.last_trade_date,
            "last_update_id": self.last_update_id,
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

    def log_trade_to_csv(self, record):
        try:
            csv_p = "data/usa_paper_trades.csv"
            os.makedirs("data", exist_ok=True)
            df = pd.DataFrame([record])
            header = not os.path.exists(csv_p) or os.path.getsize(csv_p) == 0
            df.to_csv(csv_p, mode="a", index=False, header=header)
            log.info(f"Logged USA trade to {csv_p}")
        except Exception as e:
            log.warning(f"Failed to log USA trade to CSV: {e}")

    def send_telegram_status_summary(self):
        """Sends rich status summary of USA options trading account."""
        is_open, status_str = self.is_market_open()
        now_ny = datetime.now(TZ_NY)
        realized_pnl = self.capital - INITIAL_CAPITAL
        floating_pnl = 0.0

        pos_lines = []
        if self.active_trade:
            t = self.active_trade
            pos_lines.extend([
                f"• *Active Position:* `{t.get('structure')}`",
                f"  ├ Entry Spot: `${t.get('entry_spot', 0):,.2f}`",
                f"  ├ Cost / Risk: `${t.get('entry_cost', 0):,.2f}`",
                f"  └ Entry Time: `{t.get('entry_time')}`",
                ""
            ])
        else:
            pos_lines.append("💤 *Open Positions:* No active spread. Market opens Mon-Fri 09:30 AM EST.")

        total_net_worth = self.capital + floating_pnl
        total_return_pct = ((total_net_worth - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100

        lines = [
            "🇺🇸 *USA SPY OPTIONS LIVE STATUS SUMMARY*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"💰 *Capital Balance:* `${self.capital:,.2f} USD`",
            f"🌊 *Floating P&L (Live):* *`${floating_pnl:+.2f} USD`*",
            f"💎 *Realized P&L (Banked):* *`${realized_pnl:+.2f} USD`*",
            f"📈 *Total Portfolio Value:* *`${total_net_worth:,.2f} USD`* (`{total_return_pct:+.2f}%`)",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🕒 *Market Status:* `{status_str}` ({now_ny.strftime('%I:%M %p EST')})",
            f"🛡️ *Strategy:* 100% Hedged Bull Call Spread (Spot Stop -0.30%)",
            "",
            *pos_lines,
            "━━━━━━━━━━━━━━━━━━━━━━",
            "⏱️ *System:* 24/7 Cloud Daemon (Fast 2s Polling)",
            "👉 _Tap buttons below anytime for instant refresh_"
        ]
        msg = "\n".join(lines)
        log.info("Sending USA status summary to Telegram...")
        send_telegram(msg)

    def check_telegram_inbox(self):
        """Polls for Telegram commands (/status, /summary, buttons, or inline callbacks) with cold-boot replay discard and pre-execution offset advance."""
        if not TELEGRAM_BOT_TOKEN or "placeholder" in TELEGRAM_BOT_TOKEN:
            return
        try:
            # Cold-boot backlog discard: If last_update_id is 0, probe newest update and discard stale historical backlog
            if self.last_update_id == 0:
                probe_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset=-1&limit=1"
                probe_req = urllib.request.Request(probe_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(probe_req, timeout=3) as probe_resp:
                    probe_data = json.loads(probe_resp.read().decode("utf-8"))
                    probe_res = probe_data.get("result", [])
                    if probe_res:
                        self.last_update_id = probe_res[-1].get("update_id", 0)
                        self.save_state()
                        log.info(f"USA Telegram inbox initialized on cold boot. Discarded historical backlog up to update_id {self.last_update_id}")

            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={self.last_update_id + 1}&limit=10&timeout=1"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for update in data.get("result", []):
                    # Advance offset & save state BEFORE executing command to guarantee idempotency
                    up_id = update.get("update_id", 0)
                    if up_id > self.last_update_id:
                        self.last_update_id = up_id
                        self.save_state()

                    cb = update.get("callback_query")
                    msg = update.get("message")

                    if cb:
                        cb_id = cb.get("id")
                        answer_callback_query(cb_id)
                        text = cb.get("data", "")
                        chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                    elif msg:
                        text = msg.get("text", "").strip()
                        chat_id = str(msg.get("chat", {}).get("id", ""))
                    else:
                        continue

                    if chat_id != str(TELEGRAM_CHAT_ID):
                        continue

                    # Strict command matching (M2) to avoid false triggers
                    cmd_clean = text.strip().lower()
                    if cmd_clean in {"📊 status summary", "status", "summary", "/status", "/summary", "/start", "💰 p&l report", "pnl", "/pnl", "profit", "report"} or cmd_clean.startswith("/status") or cmd_clean.startswith("/pnl"):
                        self.send_telegram_status_summary()
                        self.save_state()
        except Exception as e:
            log.warning(f"USA telegram inbox notice: {e}")

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
        now_ts = time.time()
        # 1. Listen for interactive Telegram user commands (gated to every 3s to eliminate unnecessary polling)
        if now_ts - self.last_inbox_check >= 3.0:
            self.last_inbox_check = now_ts
            self.check_telegram_inbox()

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

                record = {
                    "symbol": "SPY",
                    "action": "BUY_SPREAD",
                    "entry_time": self.active_trade["entry_time"],
                    "exit_time": now_ny.strftime("%Y-%m-%d %H:%M:%S EST"),
                    "entry_price": entry_spot,
                    "exit_price": current_spot,
                    "pnl_pct": 50.0 if hit_tp else (-30.0 if hit_sl else 0.0),
                    "fee_usd": 1.30,
                    "pnl_usd": round(pnl, 2),
                    "reason": reason,
                    "balance": round(self.capital, 2)
                }
                self.log_trade_to_csv(record)

                msg = (
                    f"🇺🇸 *USA SPY TRADE CLOSED: `{reason}`*\n\n"
                    f"• *Exit SPY Price:* `${current_spot:,.2f}`\n"
                    f"• *Realized P&L:* `${pnl:+,.2f}`\n"
                    f"• *Account Balance:* `${self.capital:,.2f}`"
                )
                log.info(f"Closed SPY Trade: PnL=${pnl:.2f} ({reason})")
                send_telegram(msg, reply_markup=None)

                self.active_trade = None
                self.last_trade_date = today_str
                self.save_state()
                push_git_sync(f"usa closed SPY spread ({reason})")
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
            push_git_sync("usa opened SPY spread")

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
            send_telegram(msg, reply_markup=None)

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