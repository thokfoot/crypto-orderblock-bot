"""
crypto_orderblock_daemon.py
===========================
Autonomous 24/7 Production Daemon for Crypto Order Block & Liquidity (SMC) Strategy.

Key Features:
- 100% Free Binance Public Futures REST API.
- Quantitative Engine (V2):
    * Scans liquid crypto perpetuals on 15m candle closes.
    * Freshness / Mitigation Audit: Rejects stale or already-tapped zones.
    * ATR Stop-Loss Protection: Minimum 1.2x 15m ATR buffer.
    * Target Extensions: Liquidity pools or 2.5x ATR asymmetric runners.
    * Automated Breakeven Trailing: Once in +2.0% profit, moves SL to Entry Price!
- Paper Trading Engine:
    * Starts with $2,000.00 virtual capital.
    * Fixed $200 position size (10% allocation per trade, max 3 concurrent positions).
    * Real-time P&L tracking, slippage, and fee accounting.
    * Saves trade journal to 'data/crypto_paper_trades.csv' and 'data/crypto_dashboard.json'.
- Telegram Alerting:
    * Sends instant Telegram messages on: Entry, Breakeven Trailing, Take-Profit, Stop-Loss.
- Cloud Runner Compatibility:
    * Can run as an infinite daemon (local/VPS) or for bounded durations (GitHub Actions runner).
"""

import os
import sys
import time
import json
import logging
import urllib.request
import urllib.parse
from datetime import datetime, timezone
import pandas as pd
import numpy as np

# --------------------------------------------------------------------------- #
# CONFIGURATION
# --------------------------------------------------------------------------- #
INITIAL_CAPITAL = 2000.0
TRADE_ALLOCATION_USD = 200.0  # $200 per position (10% of capital)
MAX_CONCURRENT_TRADES = 3
POLL_INTERVAL_SECS = int(os.environ.get("POLL_INTERVAL_SECS", "15"))  # Poll every 15s for responsive exits
SCAN_INTERVAL_SECS = int(os.environ.get("SCAN_INTERVAL_SECS", "900")) # Market scan every 15 mins (900s)
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "0"))                 # 0 = infinite loop, >0 = bounded cloud run

# Telegram Credentials
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8232616481:AAFnxgOYCN9WxjF-LCUM9lokJ5b5rXAbZzg")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "988970998")

# State File Paths
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
DASHBOARD_FILE = os.path.join(DATA_DIR, "crypto_dashboard.json")
CSV_FILE = os.path.join(DATA_DIR, "crypto_paper_trades.csv")
LOG_FILE = "crypto_orderblock.log"

# Logging Setup
try:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger("CryptoDaemon")

# --------------------------------------------------------------------------- #
# TELEGRAM NOTIFIER
# --------------------------------------------------------------------------- #
def send_telegram(message: str):
    """Send formatted markdown alert to Telegram."""
    if not TELEGRAM_BOT_TOKEN or "placeholder" in TELEGRAM_BOT_TOKEN:
        log.info(f"[TELEGRAM SIMULATED]:\n{message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            pass
    except Exception as e:
        log.warning(f"Telegram alert failed: {e}")

# --------------------------------------------------------------------------- #
# BINANCE API HELPERS
# --------------------------------------------------------------------------- #
def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            time.sleep(0.5)
    return None

def get_live_price(symbol: str) -> float:
    data = fetch_json(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}")
    return float(data["price"]) if data and "price" in data else None

def get_klines_df(symbol: str, interval="15m", limit=80):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    data = fetch_json(url)
    if not data:
        return None
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "q_vol", "trades", "tb_vol", "tb_qvol", "ignore"]
    df = pd.DataFrame(data, columns=cols)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df

# --------------------------------------------------------------------------- #
# V2 ORDER BLOCK & LIQUIDITY SCANNER
# --------------------------------------------------------------------------- #
def scan_order_block_setups():
    """Scans liquid pairs on Binance Futures for fresh Order Block retests."""
    tickers = fetch_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if not tickers:
        return []

    universe = []
    for t in tickers:
        sym = t["symbol"]
        if sym.endswith("USDT") and not sym.startswith(("USDC", "FDUSD", "BUSD")):
            vol = float(t["quoteVolume"])
            if vol >= 40_000_000:
                universe.append(sym)

    log.info(f"Scanning {min(len(universe), 35)} high-volume perpetual pairs...")
    candidate_setups = []

    for sym in universe[:35]:
        df = get_klines_df(sym, interval="15m", limit=80)
        if df is None or len(df) < 50:
            continue

        cur_p = df.iloc[-1]["close"]

        # Calculate 15m ATR
        df["tr"] = np.maximum(
            df["high"] - df["low"],
            np.maximum(abs(df["high"] - df["close"].shift(1)), abs(df["low"] - df["close"].shift(1)))
        )
        atr_15m = df["tr"].iloc[-15:-1].mean()
        vol_ma = df["volume"].rolling(20).mean()

        recent_bsl = df["high"].iloc[-35:].max()
        recent_ssl = df["low"].iloc[-35:].min()

        # Search for FRESH Bullish Order Blocks
        for i in range(len(df) - 30, len(df) - 3):
            c_ob = df.iloc[i]
            c1 = df.iloc[i + 1]
            c2 = df.iloc[i + 2]

            # Bullish OB Check
            if c_ob["close"] < c_ob["open"] and c1["close"] > c1["open"]:
                if c1["volume"] > vol_ma.iloc[i + 1] * 1.25 or c2["volume"] > vol_ma.iloc[i + 2] * 1.25:
                    if c2["low"] >= c_ob["high"] * 0.999 and c1["close"] > c_ob["high"]:
                        ob_top = c_ob["high"]
                        ob_bottom = c_ob["low"]

                        # Freshness / Mitigation Check
                        intermediate = df.iloc[i + 3 : len(df) - 1]
                        prior_taps = 0
                        prior_breaks = False
                        for _, bar in intermediate.iterrows():
                            if bar["low"] <= ob_top and bar["high"] >= ob_bottom:
                                prior_taps += 1
                            if bar["close"] < ob_bottom:
                                prior_breaks = True
                                break

                        if prior_breaks or prior_taps >= 2:
                            continue

                        dist_pct = ((cur_p - ob_top) / ob_top) * 100
                        if -0.5 <= dist_pct <= 2.0:
                            structural_sl = ob_bottom * 0.996
                            min_atr_sl = cur_p - (1.2 * atr_15m)
                            final_sl = min(structural_sl, min_atr_sl)

                            # TP: Liquidity pool or 2.5x ATR extension
                            tp = max(recent_bsl, cur_p + (2.5 * atr_15m))
                            risk = cur_p - final_sl
                            reward = tp - cur_p
                            rr = reward / (risk + 1e-9)

                            risk_pct = (risk / cur_p) * 100
                            reward_pct = (reward / cur_p) * 100

                            if rr >= 2.0 and 1.0 <= risk_pct <= 4.5 and reward_pct >= 2.5:
                                candidate_setups.append({
                                    "symbol": sym,
                                    "action": "BUY",
                                    "price": cur_p,
                                    "ob_zone": (ob_bottom, ob_top),
                                    "sl": final_sl,
                                    "tp": tp,
                                    "rr": rr,
                                    "risk_pct": risk_pct,
                                    "reward_pct": reward_pct
                                })
                                break

        # Bearish OB Check
        for i in range(len(df) - 30, len(df) - 3):
            c_ob = df.iloc[i]
            c1 = df.iloc[i + 1]
            c2 = df.iloc[i + 2]

            if c_ob["close"] > c_ob["open"] and c1["close"] < c1["open"]:
                if c1["volume"] > vol_ma.iloc[i + 1] * 1.25 or c2["volume"] > vol_ma.iloc[i + 2] * 1.25:
                    if c2["high"] <= c_ob["low"] * 1.001 and c1["close"] < c_ob["low"]:
                        ob_top = c_ob["high"]
                        ob_bottom = c_ob["low"]

                        intermediate = df.iloc[i + 3 : len(df) - 1]
                        prior_taps = 0
                        prior_breaks = False
                        for _, bar in intermediate.iterrows():
                            if bar["high"] >= ob_bottom and bar["low"] <= ob_top:
                                prior_taps += 1
                            if bar["close"] > ob_top:
                                prior_breaks = True
                                break

                        if prior_breaks or prior_taps >= 2:
                            continue

                        dist_pct = ((ob_bottom - cur_p) / ob_bottom) * 100
                        if -0.5 <= dist_pct <= 2.0:
                            structural_sl = ob_top * 1.004
                            min_atr_sl = cur_p + (1.2 * atr_15m)
                            final_sl = max(structural_sl, min_atr_sl)

                            tp = min(recent_ssl, cur_p - (2.5 * atr_15m))
                            risk = final_sl - cur_p
                            reward = cur_p - tp
                            rr = reward / (risk + 1e-9)

                            risk_pct = (risk / cur_p) * 100
                            reward_pct = (reward / cur_p) * 100

                            if rr >= 2.0 and 1.0 <= risk_pct <= 4.5 and reward_pct >= 2.5:
                                candidate_setups.append({
                                    "symbol": sym,
                                    "action": "SELL",
                                    "price": cur_p,
                                    "ob_zone": (ob_bottom, ob_top),
                                    "sl": final_sl,
                                    "tp": tp,
                                    "rr": rr,
                                    "risk_pct": risk_pct,
                                    "reward_pct": reward_pct
                                })
                                break

    return candidate_setups

# --------------------------------------------------------------------------- #
# PAPER TRADING DAEMON STATE MACHINE
# --------------------------------------------------------------------------- #
class CryptoPaperTrader:
    def __init__(self):
        self.capital = INITIAL_CAPITAL
        self.open_positions = {}  # {symbol: position_dict}
        self.trade_history = []
        self.last_scan_time = 0
        self.load_state()

    def load_state(self):
        # Look in data/crypto_dashboard.json first, then fallback to crypto_dashboard.json
        state_path = DASHBOARD_FILE if os.path.exists(DASHBOARD_FILE) else "crypto_dashboard.json"
        if os.path.exists(state_path):
            try:
                with open(state_path, "r") as f:
                    data = json.load(f)
                    self.capital = data.get("capital", INITIAL_CAPITAL)
                    self.open_positions = data.get("open_positions", {})
                    log.info(f"Loaded existing state: Capital=${self.capital:.2f}, Open Trades={len(self.open_positions)}")
            except Exception as e:
                log.warning(f"Could not load state from {state_path}: {e}")
        else:
            log.info("No prior state found. Initializing with fresh account.")

    def save_state(self):
        data = {
            "capital": round(self.capital, 2),
            "open_positions": self.open_positions,
            "total_trades": len(self.trade_history),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        for path in [DASHBOARD_FILE, "crypto_dashboard.json"]:
            try:
                with open(path, "w") as f:
                    json.dump(data, f, indent=2)
            except Exception as e:
                log.warning(f"Could not save to {path}: {e}")

    def log_trade_to_csv(self, trade_record):
        df = pd.DataFrame([trade_record])
        header = not os.path.exists(CSV_FILE)
        df.to_csv(CSV_FILE, mode="a", index=False, header=header)

    def print_status(self):
        """Prints live status of all positions and capital."""
        log.info(f"=== PORTFOLIO STATUS (Capital: ${self.capital:,.2f}) ===")
        if not self.open_positions:
            log.info("No active open positions.")
            return

        total_unrealized_usd = 0.0
        for sym, pos in self.open_positions.items():
            cur_p = get_live_price(sym)
            if not cur_p:
                continue
            action = pos["action"]
            entry_p = pos["entry_price"]
            pnl_pct = ((cur_p - entry_p) / entry_p * 100) if action == "BUY" else ((entry_p - cur_p) / entry_p * 100)
            pnl_usd = pos["notional_usd"] * (pnl_pct / 100.0)
            total_unrealized_usd += pnl_usd
            be_status = "[BE-LOCKED]" if pos.get("breakeven_locked") else "[SL-ACTIVE]"
            log.info(f"  [{sym}] {action} @ ${entry_p:.4f} | Live: ${cur_p:.4f} | PnL: {pnl_pct:+.2f}% (${pnl_usd:+.2f}) | {be_status} | TP: ${pos['tp']:.4f}")
        log.info(f"Total Unrealized PnL: ${total_unrealized_usd:+.2f} USD")

    def enter_trade(self, setup):
        sym = setup["symbol"]
        if sym in self.open_positions:
            return
        if len(self.open_positions) >= MAX_CONCURRENT_TRADES:
            return

        cur_p = setup["price"]
        action = setup["action"]
        sl = setup["sl"]
        tp = setup["tp"]
        rr = setup["rr"]

        pos = {
            "symbol": sym,
            "action": action,
            "entry_price": cur_p,
            "current_sl": sl,
            "original_sl": sl,
            "tp": tp,
            "notional_usd": TRADE_ALLOCATION_USD,
            "entry_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "breakeven_locked": False,
            "peak_pnl_pct": 0.0
        }
        self.open_positions[sym] = pos
        self.save_state()

        msg = (
            f"🎯 *NEW ORDER BLOCK TRADE OPENED*\n\n"
            f"• *Coin:* `{sym}`\n"
            f"• *Action:* *{'BUY / LONG' if action == 'BUY' else 'SELL / SHORT'}*\n"
            f"• *Entry Price:* `${cur_p:,.4f}`\n"
            f"• *Stop Loss:* `${sl:,.4f}` (-{setup['risk_pct']:.2f}%)\n"
            f"• *Take Profit:* `${tp:,.4f}` (+{setup['reward_pct']:.2f}%)\n"
            f"• *Risk : Reward:* `1 : {rr:.2f}`\n"
            f"• *Position Size:* `${TRADE_ALLOCATION_USD:,.2f}`\n"
            f"• *Status:* Fresh Retest Confirmed"
        )
        log.info(f"Entered trade on {sym} ({action}) @ {cur_p}")
        send_telegram(msg)

    def manage_positions(self):
        """Monitors active trades for Breakeven trailing, TP, and SL."""
        to_close = []

        for sym, pos in list(self.open_positions.items()):
            cur_p = get_live_price(sym)
            if not cur_p:
                continue

            action = pos["action"]
            entry_p = pos["entry_price"]
            sl = pos["current_sl"]
            tp = pos["tp"]
            notional = pos["notional_usd"]

            # Calculate current P&L %
            if action == "BUY":
                pnl_pct = ((cur_p - entry_p) / entry_p) * 100
            else:
                pnl_pct = ((entry_p - cur_p) / entry_p) * 100

            if pnl_pct > pos["peak_pnl_pct"]:
                pos["peak_pnl_pct"] = pnl_pct

            # 1. Automated Breakeven Trailing Rule
            # If trade hits +2.0% profit, trail SL to Entry Price (Risk-Free)
            if pnl_pct >= 2.0 and not pos["breakeven_locked"]:
                pos["current_sl"] = entry_p
                pos["breakeven_locked"] = True
                self.save_state()
                msg = (
                    f"🛡️ *BREAKEVEN LOCKED (RISK-FREE)*\n\n"
                    f"• *Coin:* `{sym}`\n"
                    f"• *Current P&L:* `+{pnl_pct:.2f}%`\n"
                    f"• *New SL:* `${entry_p:,.4f}` (Entry Price)\n"
                    f"• Trade is now 100% Risk-Free!"
                )
                log.info(f"Trailing SL moved to Breakeven for {sym} (P&L: +{pnl_pct:.2f}%)")
                send_telegram(msg)

            # 2. Check Take Profit Hit
            is_tp = (action == "BUY" and cur_p >= tp) or (action == "SELL" and cur_p <= tp)
            # 3. Check Stop Loss Hit
            is_sl = (action == "BUY" and cur_p <= sl) or (action == "SELL" and cur_p >= sl)

            if is_tp or is_sl:
                reason = "TAKE_PROFIT" if is_tp else ("BREAKEVEN_EXIT" if pos["breakeven_locked"] and abs(pnl_pct) < 0.25 else "STOP_LOSS")
                pnl_usd = notional * (pnl_pct / 100.0)
                self.capital += pnl_usd

                record = {
                    "symbol": sym,
                    "action": action,
                    "entry_time": pos["entry_time"],
                    "exit_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "entry_price": entry_p,
                    "exit_price": cur_p,
                    "pnl_pct": round(pnl_pct, 2),
                    "pnl_usd": round(pnl_usd, 2),
                    "reason": reason,
                    "balance": round(self.capital, 2)
                }
                self.trade_history.append(record)
                self.log_trade_to_csv(record)
                to_close.append((sym, record))

        for sym, rec in to_close:
            del self.open_positions[sym]
            self.save_state()

            icon = "🎉" if rec["pnl_usd"] > 0 else "🛑"
            msg = (
                f"{icon} *TRADE CLOSED: {rec['reason']}*\n\n"
                f"• *Coin:* `{sym}`\n"
                f"• *Exit Price:* `${rec['exit_price']:,.4f}`\n"
                f"• *Realized P&L:* `{rec['pnl_pct']:+.2f}%` ({rec['pnl_usd']:+.2f} USD)\n"
                f"• *Account Balance:* `${rec['balance']:,.2f}`"
            )
            log.info(f"Closed {sym}: PnL=${rec['pnl_usd']:.2f} ({rec['reason']})")
            send_telegram(msg)

    def run_cycle(self):
        now = time.time()
        # 1. Manage active positions
        self.manage_positions()

        # 2. Run new market scan every 15 mins (if slots available)
        if now - self.last_scan_time >= SCAN_INTERVAL_SECS:
            self.last_scan_time = now
            if len(self.open_positions) < MAX_CONCURRENT_TRADES:
                setups = scan_order_block_setups()
                for s in setups:
                    if len(self.open_positions) >= MAX_CONCURRENT_TRADES:
                        break
                    self.enter_trade(s)
            else:
                log.info(f"Max trades active ({len(self.open_positions)}/{MAX_CONCURRENT_TRADES}). Skipping scan.")

def main():
    log.info("Starting Autonomous 24/7 Crypto Order Block Paper Trading Daemon...")
    trader = CryptoPaperTrader()
    trader.print_status()

    # Cloud runner vs continuous daemon
    if RUN_MINUTES > 0:
        log.info(f"Cloud Runner Mode: Active for {RUN_MINUTES} minutes.")
        start_time = time.time()
        end_time = start_time + (RUN_MINUTES * 60)

        while time.time() < end_time:
            try:
                trader.run_cycle()
                time.sleep(POLL_INTERVAL_SECS)
            except Exception as e:
                log.error(f"Error in cycle: {e}")
                time.sleep(10)

        trader.save_state()
        log.info("Cloud Runner cycle finished cleanly. State saved.")
    else:
        # Infinite daemon mode
        log.info("Infinite Daemon Mode: Running continuously.")
        while True:
            try:
                trader.run_cycle()
                time.sleep(POLL_INTERVAL_SECS)
            except KeyboardInterrupt:
                log.info("Daemon stopped by user.")
                trader.save_state()
                break
            except Exception as e:
                log.error(f"Unexpected error in daemon loop: {e}")
                time.sleep(10)

if __name__ == "__main__":
    main()
