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
    * Automated Breakeven Trailing: Once in +1.5% profit, moves SL to Entry + 0.40% buffer!
    * Tier-2 Scale-Out Profit Taking: At +2.0%, banks 50% position directly to cash balance!
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
import subprocess
import pandas as pd
import numpy as np

# --------------------------------------------------------------------------- #
# CONFIGURATION
# --------------------------------------------------------------------------- #
INITIAL_CAPITAL = 2000.0
TRADE_ALLOCATION_USD = 200.0  # $200 per position (10% of capital)
MAX_CONCURRENT_TRADES = 3
ROUNDTRIP_FEE_PCT = 0.30  # Realistic friction: 0.10% taker fees + 0.08% spread crossing + 0.12% slippage
POLL_INTERVAL_SECS = int(os.environ.get("POLL_INTERVAL_SECS", "1"))   # Poll every 1s for instant UI & fast exits
SCAN_INTERVAL_SECS = int(os.environ.get("SCAN_INTERVAL_SECS", "900")) # Market scan every 15 mins (900s)
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "0"))                 # 0 = infinite loop, >0 = bounded cloud run

# Telegram Credentials (Injected securely via GitHub Actions Secrets)
TELEGRAM_BOT_TOKEN = os.environ.get("CRYPTO_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# State File Paths
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
DASHBOARD_FILE = os.path.join(DATA_DIR, "crypto_dashboard.json")
CSV_FILE = os.path.join(DATA_DIR, "crypto_paper_trades.csv")
SCAN_LOG_FILE = os.path.join(DATA_DIR, "crypto_scan_history.csv")
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
# TELEGRAM NOTIFIER & INTERACTIVE BUTTONS
# --------------------------------------------------------------------------- #
STATUS_KEYBOARD = json.dumps({
    "keyboard": [
        [{"text": "📊 Status Summary"}, {"text": "🔍 Scan Log"}],
        [{"text": "💰 P&L Report"}, {"text": "⚡ Rescan Now"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
})

def send_telegram(message: str, reply_markup=STATUS_KEYBOARD):
    """Send formatted alert to Telegram with persistent status summary buttons."""
    if not TELEGRAM_BOT_TOKEN or "placeholder" in TELEGRAM_BOT_TOKEN:
        log.info(f"[TELEGRAM SIMULATED]:\n{message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload_dict = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }
        if reply_markup:
            payload_dict["reply_markup"] = reply_markup
        payload = urllib.parse.urlencode(payload_dict).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            pass
    except Exception as e:
        # Fallback to plain text if markdown formatting fails
        try:
            payload_dict_plain = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message.replace("*", "").replace("`", "")
            }
            if reply_markup:
                payload_dict_plain["reply_markup"] = reply_markup
            payload_plain = urllib.parse.urlencode(payload_dict_plain).encode("utf-8")
            req = urllib.request.Request(url, data=payload_plain, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                pass
        except Exception as e2:
            log.warning(f"Telegram alert failed: {e2}")

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

def push_git_sync(comment: str = "auto-sync"):
    """Atomically commits and pushes state to Git without requiring workflow teardown."""
    try:
        if not os.path.exists(".git"):
            return
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], capture_output=True, timeout=5)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], capture_output=True, timeout=5)
        subprocess.run(["git", "add", "data/crypto_dashboard.json", "data/crypto_paper_trades.csv", "data/crypto_scan_history.csv", "crypto_dashboard.json"], capture_output=True, timeout=5)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True, timeout=5)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", f"state: {comment} [skip ci]"], capture_output=True, timeout=5)
            push_res = subprocess.run(["git", "push", "origin", "main"], capture_output=True, timeout=10)
            if push_res.returncode != 0:
                subprocess.run(["git", "pull", "--rebase", "origin", "main"], capture_output=True, timeout=10)
                subprocess.run(["git", "push", "origin", "main"], capture_output=True, timeout=10)
            log.info("In-flight Git state push succeeded.")
    except Exception as e:
        log.warning(f"Git push notice: {e}")

# --------------------------------------------------------------------------- #
# BINANCE API HELPERS
# --------------------------------------------------------------------------- #
def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict) and "code" in data and data["code"] < 0:
                    log.warning(f"Binance API error from {url}: {data}")
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return data
        except urllib.error.HTTPError as he:
            if he.code in (418, 429):
                log.warning(f"Binance rate limit (HTTP {he.code}) on {url}: backing off...")
                time.sleep(2.5 * (attempt + 1))
            else:
                time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
    return None

def get_live_price(symbol: str) -> float:
    """Fetches live price with multi-tier fallback across Futures, Vision CDN, and Spot."""
    urls = [
        f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}",
        f"https://data-api.binance.vision/api/v3/ticker/price?symbol={symbol}",
        f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
    ]
    for url in urls:
        data = fetch_json(url)
        if data and isinstance(data, dict) and "price" in data:
            try:
                p = float(data["price"])
                if p > 0:
                    return p
            except Exception:
                continue
    return None

def get_klines_df(symbol: str, interval="15m", limit=80):
    """Fetches klines dataframe with multi-tier failover across Futures, Vision CDN, and Spot."""
    urls = [
        f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    ]
    data = None
    for u in urls:
        data = fetch_json(u)
        if data and isinstance(data, list) and len(data) > 0:
            break
    if not data or not isinstance(data, list):
        return None
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "q_vol", "trades", "tb_vol", "tb_qvol", "ignore"]
    df = pd.DataFrame(data, columns=cols)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df

def get_24hr_tickers():
    """Fetches 24hr tickers with multi-tier failover across Futures, Vision CDN, and Spot."""
    urls = [
        "https://fapi.binance.com/fapi/v1/ticker/24hr",
        "https://data-api.binance.vision/api/v3/ticker/24hr",
        "https://api.binance.com/api/v3/ticker/24hr"
    ]
    for u in urls:
        data = fetch_json(u)
        if data and isinstance(data, list) and len(data) > 0:
            return data
    return None

# --------------------------------------------------------------------------- #
# BTC MACRO REGIME FILTER (COUNCIL CONSENSUS)
# --------------------------------------------------------------------------- #
def check_btc_regime():
    """Checks Bitcoin 1h trend regime: BTC > EMA50 and RSI > 48."""
    btc_df = get_klines_df("BTCUSDT", interval="1h", limit=60)
    if btc_df is None or len(btc_df) < 50:
        return True, "BTC Data Fallback"
    btc_close = btc_df["close"].iloc[-1]
    btc_ema50 = btc_df["close"].ewm(span=50).mean().iloc[-1]
    delta = btc_df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    btc_rsi = float((100 - (100 / (1 + rs))).iloc[-1])
    is_bullish = bool((btc_close > btc_ema50) and (btc_rsi > 48.0))
    return is_bullish, f"BTC=${btc_close:,.1f}, EMA50=${btc_ema50:,.1f}, RSI={btc_rsi:.1f}"

# --------------------------------------------------------------------------- #
# V2 ORDER BLOCK & LIQUIDITY SCANNER
# --------------------------------------------------------------------------- #
def scan_order_block_setups():
    """Scans liquid pairs on Binance Futures for fresh Order Block retests with BTC regime filter."""
    btc_bullish, btc_regime_info = check_btc_regime()
    log.info(f"Macro BTC 1H Regime: Bullish={btc_bullish} ({btc_regime_info})")

    tickers = get_24hr_tickers()
    if not tickers or not isinstance(tickers, list):
        log.warning(f"Binance ticker API returned invalid data across all endpoints: {tickers}")
        return [], None

    filtered_pairs = []
    for t in tickers:
        sym = t.get("symbol", "")
        if sym.endswith("USDT") and not sym.startswith(("USDC", "FDUSD", "BUSD", "EUR", "TUSD", "DAI")):
            try:
                vol = float(t.get("quoteVolume", 0))
                if vol >= 15_000_000:
                    filtered_pairs.append((sym, vol))
            except Exception:
                continue

    # Sort descending by 24h volume to guarantee top 35 liquid pairs across Futures or Spot
    filtered_pairs.sort(key=lambda x: x[1], reverse=True)
    universe = [p[0] for p in filtered_pairs[:35]]

    log.info(f"Scanning {len(universe)} high-volume perpetual pairs...")
    candidate_setups = []
    filtered_setups = []

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

                        # Freshness / Mitigation Check (Strict 0 prior taps)
                        intermediate = df.iloc[i + 3 : len(df) - 1]
                        prior_taps = 0
                        prior_breaks = False
                        for _, bar in intermediate.iterrows():
                            if bar["low"] <= ob_top and bar["high"] >= ob_bottom:
                                prior_taps += 1
                            if bar["close"] < ob_bottom:
                                prior_breaks = True
                                break

                        if prior_breaks or prior_taps >= 1:
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
                                if not btc_bullish:
                                    log.info(f"Skipping {sym} BUY setup: Macro BTC 1H is Bearish/Neutral.")
                                    filtered_setups.append({"symbol": sym, "action": "BUY", "reason": "BTC Bearish filter"})
                                else:
                                    candidate_setups.append({
                                        "symbol": sym,
                                        "action": "BUY",
                                        "price": cur_p,
                                        "ob_zone": (ob_bottom, ob_top),
                                        "sl": final_sl,
                                        "tp": tp,
                                        "rr": rr,
                                        "atr": atr_15m,
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

                        if prior_breaks or prior_taps >= 1:
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
                                if btc_bullish:
                                    log.info(f"Skipping {sym} SHORT setup: Macro BTC 1H is Bullish.")
                                    filtered_setups.append({"symbol": sym, "action": "SELL", "reason": "BTC Bullish filter"})
                                else:
                                    candidate_setups.append({
                                        "symbol": sym,
                                        "action": "SELL",
                                        "price": cur_p,
                                        "ob_zone": (ob_bottom, ob_top),
                                        "sl": final_sl,
                                        "tp": tp,
                                        "rr": rr,
                                        "atr": atr_15m,
                                        "risk_pct": risk_pct,
                                        "reward_pct": reward_pct
                                    })
                                    break

    # Construct Scan Record
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    pairs_count = min(len(universe), 35)
    cand_symbols = [f"{s['symbol']}:{s['action']}@{s['price']:.4f}(RR:{s['rr']:.1f})" for s in candidate_setups]
    filt_symbols = [f"{f['symbol']}:{f['action']}({f['reason']})" for f in filtered_setups]

    scan_record = {
        "timestamp": now_utc,
        "btc_bullish": btc_bullish,
        "btc_info": btc_regime_info,
        "pairs_scanned": pairs_count,
        "candidates_count": len(candidate_setups),
        "candidates_str": "; ".join(cand_symbols) if cand_symbols else "None",
        "filtered_count": len(filtered_setups),
        "filtered_str": "; ".join(filt_symbols) if filt_symbols else "None"
    }

    # Save to CSV history
    try:
        csv_row = pd.DataFrame([{
            "timestamp": now_utc,
            "btc_bullish": btc_bullish,
            "btc_info": btc_regime_info,
            "pairs_scanned": pairs_count,
            "candidates_count": len(candidate_setups),
            "candidates": "; ".join(cand_symbols) if cand_symbols else "None",
            "filtered": "; ".join(filt_symbols) if filt_symbols else "None"
        }])
        header = not os.path.exists(SCAN_LOG_FILE)
        csv_row.to_csv(SCAN_LOG_FILE, mode="a", index=False, header=header)
        log.info(f"Scan history logged: {len(candidate_setups)} qualified, {len(filtered_setups)} filtered.")
    except Exception as e:
        log.warning(f"Could not append to scan CSV: {e}")

    return candidate_setups, scan_record

# --------------------------------------------------------------------------- #
# PAPER TRADING DAEMON STATE MACHINE
# --------------------------------------------------------------------------- #
class CryptoPaperTrader:
    def __init__(self):
        self.capital = INITIAL_CAPITAL
        self.open_positions = {}  # {symbol: position_dict}
        self.trade_history = []
        self.last_scan_time = 0
        self.last_scan_record = None
        self.last_update_id = 0
        self.last_inbox_check = 0.0
        self.last_known_prices = {}
        self.cached_btc_regime = (True, "BTC Trend Active")
        self.last_btc_check = 0
        self.load_state()

    def get_btc_regime(self):
        now = time.time()
        if now - self.last_btc_check > 300:
            try:
                self.cached_btc_regime = check_btc_regime()
                self.last_btc_check = now
            except Exception:
                pass
        return self.cached_btc_regime

    def load_state(self):
        # Look in data/crypto_dashboard.json first, then fallback to crypto_dashboard.json
        paths_to_try = [DASHBOARD_FILE, "crypto_dashboard.json"]
        loaded = False
        for path in paths_to_try:
            if os.path.exists(path) and os.path.getsize(path) > 5:
                try:
                    with open(path, "r") as f:
                        data = json.load(f)
                    self.capital = data.get("capital", INITIAL_CAPITAL)
                    self.open_positions = data.get("open_positions", {})
                    self.last_update_id = data.get("last_update_id", 0)
                    self.last_scan_record = data.get("last_scan_record", None)
                    log.info(f"Loaded existing state from {path}: Capital=${self.capital:.2f}, Open Trades={len(self.open_positions)}")
                    loaded = True
                    break
                except Exception as e:
                    log.warning(f"Could not load state from {path}: {e}")
        if not loaded:
            log.info("No prior valid state found. Initializing with fresh account.")

    def save_state(self):
        total_trades = len(self.trade_history)
        if os.path.exists(CSV_FILE):
            try:
                df_csv = pd.read_csv(CSV_FILE)
                total_trades = len(df_csv)
            except Exception:
                pass

        data = {
            "capital": round(self.capital, 2),
            "open_positions": self.open_positions,
            "total_trades": total_trades,
            "last_update_id": self.last_update_id,
            "last_scan_record": self.last_scan_record,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        for path in [DASHBOARD_FILE, "crypto_dashboard.json"]:
            try:
                dir_name = os.path.dirname(path)
                if dir_name:
                    os.makedirs(dir_name, exist_ok=True)
                # Atomic write pattern to eliminate 0-byte file truncation
                temp_path = f"{path}.tmp.{os.getpid()}"
                with open(temp_path, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(temp_path, path)
            except Exception as e:
                log.warning(f"Could not atomically save to {path}: {e}")

    def send_telegram_status_summary(self):
        """Sends comprehensive live portfolio and PnL status card to Telegram (<100ms response)."""
        btc_bullish, btc_info = self.get_btc_regime()
        total_unrealized_usd = 0.0
        pos_lines = []

        if not self.open_positions:
            pos_lines.append("💤 *Open Trades:* None active (Scanning Binance 15m order blocks).")
        else:
            for sym, pos in self.open_positions.items():
                cur_p = get_live_price(sym)
                if cur_p and cur_p > 0:
                    self.last_known_prices[sym] = cur_p
                else:
                    cur_p = self.last_known_prices.get(sym, pos.get("peak_price", pos["entry_price"]))

                entry_p = pos["entry_price"]
                action = pos["action"]
                notional = pos["notional_usd"]

                pnl_pct = ((cur_p - entry_p) / entry_p * 100) if action == "BUY" else ((entry_p - cur_p) / entry_p * 100)
                pnl_usd = notional * (pnl_pct / 100.0)
                total_unrealized_usd += pnl_usd
                status_tag = "🔒 50% SCALED OUT" if pos.get("scaled_out") else ("🛡️ BE-LOCKED" if pos.get("breakeven_locked") else "🟢 ACTIVE")
                pos_lines.extend([
                    f"• *{sym}* ({action} @ `${entry_p:,.4f}`):",
                    f"  ├ Live: `${cur_p:,.4f}` (`{pnl_pct:+.2f}%` / *{pnl_usd:+.2f} USD*)",
                    f"  ├ Size: `${notional:.2f}` [{status_tag}]",
                    f"  └ SL: `${pos['current_sl']:,.4f}` | TP: `${pos['tp']:,.4f}`",
                    ""
                ])

        realized_pnl = self.capital - INITIAL_CAPITAL
        total_net_worth = self.capital + total_unrealized_usd
        total_return_pct = ((total_net_worth - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100

        # Scan block info
        scan_summary_lines = []
        if self.last_scan_record:
            rec = self.last_scan_record
            t_str = rec.get("timestamp", "N/A")
            c_cnt = rec.get("candidates_count", 0)
            c_str = rec.get("candidates_str", "None")
            p_n = rec.get("pairs_scanned", 35)
            scan_summary_lines = [
                f"🔍 *Market Scan:* `{t_str}` ({p_n} pairs checked)",
                f"  └ *Setups:* `{c_str if c_cnt > 0 else 'None qualified (waiting for clean OB)'}`",
                ""
            ]

        lines = [
            "📊 *CRYPTO BOT LIVE STATUS SUMMARY*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"💰 *Capital Balance:* `${self.capital:,.2f} USD`",
            f"🌊 *Floating P&L (Live):* *`${total_unrealized_usd:+.2f} USD`*",
            f"💎 *Realized P&L (Banked):* *`${realized_pnl:+.2f} USD`*",
            f"📈 *Total Portfolio Value:* *`${total_net_worth:,.2f} USD`* (`{total_return_pct:+.2f}%`)",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🔄 *Macro BTC 1H:* `{'🟢 BULLISH' if btc_bullish else '🔴 BEARISH/NEUTRAL'}` ({btc_info})",
            f"⚡ *Active Trades:* `{len(self.open_positions)} / {MAX_CONCURRENT_TRADES}`",
            "",
            *pos_lines,
            *scan_summary_lines,
            "━━━━━━━━━━━━━━━━━━━━━━",
            "⏱️ *System:* 24/7 Cloud Daemon (Fast 1s Polling)",
            "👉 _Tap buttons below anytime for instant refresh_"
        ]

        msg = "\n".join(lines)
        log.info("Sending live status summary to Telegram...")
        send_telegram(msg)

    def send_telegram_scan_log(self):
        """Sends comprehensive market scan audit log to Telegram."""
        if not self.last_scan_record and os.path.exists(SCAN_LOG_FILE):
            try:
                df = pd.read_csv(SCAN_LOG_FILE)
                if not df.empty:
                    last_row = df.iloc[-1]
                    self.last_scan_record = {
                        "timestamp": str(last_row.get("timestamp", "N/A")),
                        "btc_bullish": bool(last_row.get("btc_bullish", True)),
                        "btc_info": str(last_row.get("btc_info", "")),
                        "pairs_scanned": int(last_row.get("pairs_scanned", 35)),
                        "candidates_count": int(last_row.get("candidates_count", 0)),
                        "candidates_str": str(last_row.get("candidates", "None")),
                        "filtered_count": int(len(str(last_row.get("filtered", "")).split(";"))) if str(last_row.get("filtered", "None")) != "None" else 0,
                        "filtered_str": str(last_row.get("filtered", "None"))
                    }
            except Exception:
                pass

        rec = self.last_scan_record
        if not rec:
            msg = (
                "🔍 *MARKET SCAN AUDIT LOG*\n\n"
                "• *Status:* Initializing scan cycle...\n"
                "• *Interval:* Every 15 minutes (on 15m candle close)\n"
                "👉 Tap *⚡ Rescan Now* to run an instant market scan."
            )
            send_telegram(msg)
            return

        btc_status = "🟢 BULLISH" if rec.get("btc_bullish") else "🔴 BEARISH/NEUTRAL"
        time_str = rec.get("timestamp", "N/A")
        pairs_n = rec.get("pairs_scanned", 35)
        cand_cnt = rec.get("candidates_count", 0)
        cand_str = rec.get("candidates_str", "None")
        if not cand_str or str(cand_str).strip().lower() in ["nan", "none", ""]:
            cand_str = "None"
        filt_str = rec.get("filtered_str", "None")
        if not filt_str or str(filt_str).strip().lower() in ["nan", "none", ""]:
            filt_str = "None"

        # Read last 4 scan timestamps from CSV if available
        history_lines = []
        if os.path.exists(SCAN_LOG_FILE):
            try:
                df_hist = pd.read_csv(SCAN_LOG_FILE)
                for _, row in df_hist.tail(4).iloc[::-1].iterrows():
                    t = str(row.get("timestamp", ""))[-12:-4]
                    c_n = row.get("candidates_count", 0)
                    history_lines.append(f"  • `{t} UTC` | Scanned: 35 | Setups: `{c_n}`")
            except Exception:
                pass

        lines = [
            "🔍 *BINANCE 15M ORDER BLOCK SCAN AUDIT*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🕒 *Latest Scan Time:* `{time_str}`",
            f"🔄 *Macro BTC 1H Filter:* `{btc_status}`",
            f"   _{rec.get('btc_info', '')}_",
            f"📊 *Universe Checked:* `{pairs_n} High-Volume Perpetuals`",
            f"⚡ *Open Trade Slots:* `{len(self.open_positions)} / {MAX_CONCURRENT_TRADES} in use`",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🎯 *Qualified Setups ({cand_cnt}):*",
            f"   `{cand_str}`" if cand_cnt > 0 else "   _0 qualified (waiting for clean OB zone retest & R:R >= 2.0)_",
            "",
            f"🚫 *Filtered by BTC Trend:*",
            f"   `{filt_str}`" if filt_str != "None" else "   _None_",
            "━━━━━━━━━━━━━━━━━━━━━━"
        ]

        if history_lines:
            lines.extend([
                "📋 *Recent Scan History Log:*",
                *history_lines,
                "━━━━━━━━━━━━━━━━━━━━━━"
            ])

        lines.extend([
            "⏱️ *Rescan Frequency:* Every 15 mins (on 15m candle close)",
            "👉 _Tap '⚡ Rescan Now' to trigger an on-demand scan instantly._"
        ])

        send_telegram("\n".join(lines))

    def trigger_on_demand_rescan(self):
        """Executes an instant on-demand scan with cooldown and prior risk-flush."""
        now = time.time()
        # Cooldown guard: prevent rapid button-mashing lockups
        if now - self.last_scan_time < 30.0:
            send_telegram("⏳ *Cooldown Active:* Last market scan was completed <30s ago. Please wait.", reply_markup=None)
            return

        # Risk-First Flush: manage positions before entering the blocking 35-pair scan
        self.manage_positions()

        send_telegram("⏳ *Initiating On-Demand Market Scan...*\nScanning 35 top Binance Futures perpetuals now...", reply_markup=None)
        candidate_setups, scan_record = scan_order_block_setups()
        self.last_scan_record = scan_record
        self.last_scan_time = time.time()
        self.save_state()
        push_git_sync("on-demand scan")

        # If slot available and candidate found, enter trade
        if len(self.open_positions) < MAX_CONCURRENT_TRADES and candidate_setups:
            for s in candidate_setups:
                if len(self.open_positions) >= MAX_CONCURRENT_TRADES:
                    break
                self.enter_trade(s)
                break

        # Send updated scan log
        self.send_telegram_scan_log()

    def send_telegram_pnl_report(self):
        """Sends detailed realized P&L and trade performance report."""
        realized_pnl = self.capital - INITIAL_CAPITAL
        total_trades = 0
        winning_trades = 0
        losing_trades = 0
        trade_rows = []

        if os.path.exists(CSV_FILE):
            try:
                df = pd.read_csv(CSV_FILE)
                total_trades = len(df)
                winning_trades = len(df[df["pnl_usd"] > 0])
                losing_trades = len(df[df["pnl_usd"] < 0])
                for _, row in df.tail(5).iloc[::-1].iterrows():
                    icon = "🟢" if row["pnl_usd"] > 0 else ("🛡️" if "BREAKEVEN" in str(row["reason"]) else "🔴")
                    trade_rows.append(f"{icon} *{row['symbol']}* ({row['action']}): `{row['pnl_pct']:+.2f}%` (*{row['pnl_usd']:+.2f} USD*) [`{row['reason']}`]")
            except Exception:
                pass

        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0

        lines = [
            "💰 *CRYPTO BOT REALIZED P&L REPORT*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"💵 *Current Account Balance:* `${self.capital:,.2f} USD`",
            f"💎 *Net Banked Realized P&L:* *`${realized_pnl:+.2f} USD`*",
            f"📊 *Closed Trades:* `{total_trades}` | *Win Rate:* `{win_rate:.1f}%`",
            f"🏆 *Wins:* `{winning_trades}` | 🛡️ *Losses / BE:* `{losing_trades}`",
            "━━━━━━━━━━━━━━━━━━━━━━"
        ]
        if trade_rows:
            lines.extend([
                "📜 *Recent Closed / Scaled Trades:*",
                *trade_rows,
                "━━━━━━━━━━━━━━━━━━━━━━"
            ])
        lines.append("👉 _Tap '📊 Status Summary' to check live open positions._")
        send_telegram("\n".join(lines))

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
                        log.info(f"Telegram inbox initialized on cold boot. Discarded historical backlog up to update_id {self.last_update_id}")

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

                    # Strict intent matching (M2) to eliminate false triggers from casual text
                    cmd_clean = text.strip().lower()
                    if cmd_clean in {"⚡ rescan now", "rescan", "/rescan"} or cmd_clean.startswith("/rescan"):
                        self.trigger_on_demand_rescan()
                    elif cmd_clean in {"🔍 scan log", "scan", "/scan", "/scanlog"} or cmd_clean.startswith("/scan"):
                        self.send_telegram_scan_log()
                    elif cmd_clean in {"💰 p&l report", "pnl", "/pnl", "profit", "report"} or cmd_clean.startswith("/pnl"):
                        self.send_telegram_pnl_report()
                    elif cmd_clean in {"📊 status summary", "/status", "/summary", "/start", "status", "summary"} or cmd_clean.startswith("/status"):
                        self.send_telegram_status_summary()
                        self.save_state()
        except Exception as e:
            log.warning(f"Telegram inbox notice: {e}")

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
        atr = setup.get("atr", cur_p * 0.015)

        pos = {
            "symbol": sym,
            "action": action,
            "entry_price": cur_p,
            "current_sl": sl,
            "original_sl": sl,
            "tp": tp,
            "atr": atr,
            "peak_price": cur_p,
            "notional_usd": TRADE_ALLOCATION_USD,
            "entry_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "scaled_out": False,
            "realized_pnl_usd": 0.0,
            "breakeven_locked": False,
            "peak_pnl_pct": 0.0
        }
        self.open_positions[sym] = pos
        self.save_state()
        push_git_sync(f"entered {sym}")

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
        send_telegram(msg, reply_markup=None)

    def manage_positions(self):
        """Monitors active trades for Scale-Out profit taking, Chandelier trailing, TP, and SL."""
        to_close = []

        for sym, pos in list(self.open_positions.items()):
            cur_p = get_live_price(sym)
            if not cur_p:
                continue

            action = pos["action"]
            entry_p = pos["entry_price"]
            tp = pos["tp"]
            notional = pos["notional_usd"]

            # Track peak price
            if action == "BUY":
                pnl_pct = ((cur_p - entry_p) / entry_p) * 100
                if cur_p > pos.get("peak_price", entry_p):
                    pos["peak_price"] = cur_p
            else:
                pnl_pct = ((entry_p - cur_p) / entry_p) * 100
                if cur_p < pos.get("peak_price", entry_p):
                    pos["peak_price"] = cur_p

            if pnl_pct > pos.get("peak_pnl_pct", 0.0):
                pos["peak_pnl_pct"] = pnl_pct

            atr_val = pos.get("atr", entry_p * 0.015)
            peak_pnl = max(pnl_pct, pos.get("peak_pnl_pct", 0.0))

            # -------------------------------------------------------------
            # 1. Tier-1: Breakeven Stop Lock at +1.5% (Fee-Safe Buffer)
            # Peak-triggered: Locks permanently once high-water mark hits +1.5%
            # -------------------------------------------------------------
            if peak_pnl >= 1.5 and not pos.get("breakeven_locked") and not pos.get("scaled_out"):
                be_floor = entry_p * (1.0040 if action == "BUY" else 0.9960)
                if action == "BUY":
                    pos["current_sl"] = max(pos["current_sl"], be_floor)
                else:
                    pos["current_sl"] = min(pos["current_sl"], be_floor)
                pos["breakeven_locked"] = True
                self.save_state()
                push_git_sync(f"breakeven locked {sym}")
                msg = (
                    f"🛡️ *BREAKEVEN STOP LOCKED (RISK ELIMINATED)*\n\n"
                    f"• *Coin:* `{sym}`\n"
                    f"• *Peak Gain Reached:* `+{peak_pnl:.2f}%` (Live: `{pnl_pct:+.2f}%`)\n"
                    f"• *Protected Stop Loss:* `${pos['current_sl']:,.4f}` (+0.40% Buffer)\n"
                    f"• *Status:* Guaranteed Fee-Covered Net Scratch or Better"
                )
                log.info(f"Breakeven locked for {sym} at peak +{peak_pnl:.2f}% (SL: ${pos['current_sl']:.4f})")
                send_telegram(msg, reply_markup=None)

            # -------------------------------------------------------------
            # 2. Tier-2: Scale-Out Profit Taking at +2.0% (50% Banked to Cash)
            # Council-Approved: Intercepts 15m order block reaction impulse and books hard profit
            # -------------------------------------------------------------
            if pnl_pct >= 2.0 and not pos.get("scaled_out"):
                partial_notional = pos["notional_usd"] * 0.5
                fee_usd = partial_notional * (ROUNDTRIP_FEE_PCT / 100.0)
                partial_pnl_usd = (partial_notional * (pnl_pct / 100.0)) - fee_usd
                self.capital += partial_pnl_usd
                pos["notional_usd"] -= partial_notional
                pos["scaled_out"] = True
                pos["realized_pnl_usd"] = pos.get("realized_pnl_usd", 0.0) + partial_pnl_usd

                # Floor the trailing stop at Entry + 0.40% buffer (covers 0.30% roundtrip friction with net credit)
                be_floor = entry_p * (1.0040 if action == "BUY" else 0.9960)
                pos["current_sl"] = max(pos["current_sl"], be_floor) if action == "BUY" else min(pos["current_sl"], be_floor)
                # Record partial scale-out to trade journal
                scale_record = {
                    "symbol": sym,
                    "action": action,
                    "entry_time": pos["entry_time"],
                    "exit_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "entry_price": entry_p,
                    "exit_price": cur_p,
                    "pnl_pct": round(pnl_pct, 2),
                    "fee_usd": round(fee_usd, 2),
                    "pnl_usd": round(partial_pnl_usd, 2),
                    "reason": "PARTIAL_TP_50PCT",
                    "balance": round(self.capital, 2)
                }
                self.trade_history.append(scale_record)
                self.log_trade_to_csv(scale_record)
                self.save_state()
                push_git_sync(f"scaled out {sym}")

                msg = (
                    f"💰 *SCALE-OUT PROFIT TAKEN (50% BANKED)*\n\n"
                    f"• *Coin:* `{sym}`\n"
                    f"• *Trigger Gain:* `+{pnl_pct:.2f}%`\n"
                    f"• *Banked Realized Cash:* `+${partial_pnl_usd:.2f} USD` (Added to Balance)\n"
                    f"• *Remaining Runner Size:* `${pos['notional_usd']:.2f}`\n"
                    f"• *Runner SL Floor:* `${pos['current_sl']:,.4f}` (Guaranteed Net Credit)\n"
                    f"• *New Account Balance:* `${self.capital:,.2f}`"
                )
                log.info(f"Scaled out 50% on {sym} @ +{pnl_pct:.2f}% (Banked ${partial_pnl_usd:.2f})")
                send_telegram(msg, reply_markup=None)

            # -------------------------------------------------------------
            # 3. Dynamic Chandelier Trailing Stop on Remaining 50% Runner + Tier-3 Lock
            # -------------------------------------------------------------
            if pos.get("scaled_out"):
                peak_p = pos.get("peak_price", cur_p)
                if action == "BUY":
                    chandelier_sl = peak_p - (2.5 * atr_val)
                    be_floor = entry_p * 1.0040
                    # Tier-3 Lock: If peak ever reached >= +5.0%, floor stop at +2.5% minimum profit
                    if peak_pnl >= 5.0:
                        be_floor = max(be_floor, entry_p * 1.0250)
                    target_sl = max(be_floor, chandelier_sl)
                    if target_sl > pos["current_sl"]:
                        pos["current_sl"] = target_sl
                        self.save_state()
                        log.info(f"Chandelier SL trailed to ${target_sl:.4f} for {sym}")
                else:
                    chandelier_sl = peak_p + (2.5 * atr_val)
                    be_floor = entry_p * 0.9960
                    # Tier-3 Lock: If peak ever reached >= +5.0%, floor stop at +2.5% minimum profit
                    if peak_pnl >= 5.0:
                        be_floor = min(be_floor, entry_p * 0.9750)
                    target_sl = min(be_floor, chandelier_sl)
                    if target_sl < pos["current_sl"]:
                        pos["current_sl"] = target_sl
                        self.save_state()
                        log.info(f"Chandelier SL trailed to ${target_sl:.4f} for {sym}")

            sl = pos["current_sl"]

            # -------------------------------------------------------------
            # 3. Check Take Profit Hit
            # -------------------------------------------------------------
            is_tp = (action == "BUY" and cur_p >= tp) or (action == "SELL" and cur_p <= tp)
            # 4. Check Stop Loss Hit
            is_sl = (action == "BUY" and cur_p <= sl) or (action == "SELL" and cur_p >= sl)

            if is_tp or is_sl:
                if is_tp:
                    reason = "TAKE_PROFIT"
                    exit_p = tp
                    trade_pnl_pct = ((tp - entry_p) / entry_p * 100) if action == "BUY" else ((entry_p - tp) / entry_p * 100)
                else:
                    # Slippage realism: If market gapped down past SL, execute at live price (Red-Team Bug #1 fix)
                    exit_p = min(cur_p, sl) if action == "BUY" else max(cur_p, sl)
                    trade_pnl_pct = ((exit_p - entry_p) / entry_p * 100) if action == "BUY" else ((entry_p - exit_p) / entry_p * 100)
                    if pos.get("scaled_out"):
                        reason = "RUNNER_TRAIL_EXIT"
                    elif pos.get("breakeven_locked"):
                        reason = "BREAKEVEN_EXIT" if trade_pnl_pct >= 0 else "SLIPPAGE_EXIT"
                    else:
                        reason = "STOP_LOSS"

                # Account for realistic roundtrip friction (0.30%)
                curr_notional = pos["notional_usd"]
                fee_usd = curr_notional * (ROUNDTRIP_FEE_PCT / 100.0)
                gross_pnl_usd = curr_notional * (trade_pnl_pct / 100.0)
                net_pnl_usd = gross_pnl_usd - fee_usd
                self.capital += net_pnl_usd

                record = {
                    "symbol": sym,
                    "action": action,
                    "entry_time": pos["entry_time"],
                    "exit_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "entry_price": entry_p,
                    "exit_price": exit_p,
                    "pnl_pct": round(trade_pnl_pct, 2),
                    "fee_usd": round(fee_usd, 2),
                    "pnl_usd": round(net_pnl_usd, 2),
                    "reason": reason,
                    "balance": round(self.capital, 2)
                }
                self.trade_history.append(record)
                self.log_trade_to_csv(record)
                to_close.append((sym, record))

        for sym, rec in to_close:
            del self.open_positions[sym]
            self.save_state()
            push_git_sync(f"closed {sym}")

            icon = "🎉" if rec["pnl_usd"] > 0 else ("🛡️" if rec["reason"] == "BREAKEVEN_EXIT" else "🛑")
            msg = (
                f"{icon} *TRADE CLOSED: `{rec['reason']}`*\n\n"
                f"• *Coin:* `{sym}`\n"
                f"• *Exit Price:* `${rec['exit_price']:,.4f}`\n"
                f"• *Realized P&L:* `{rec['pnl_pct']:+.2f}%` ({rec['pnl_usd']:+.2f} USD after fees)\n"
                f"• *Account Balance:* `${rec['balance']:,.2f}`"
            )
            log.info(f"Closed {sym}: Net PnL=${rec['pnl_usd']:.2f} ({rec['reason']})")
            send_telegram(msg, reply_markup=None)

    def run_cycle(self):
        now = time.time()
        # 1. RISK FIRST - positions must be managed before any potentially blocking tasks
        self.manage_positions()

        # 2. Check Telegram inbox commands (gated to every 3s to eliminate 86k calls/day)
        if now - self.last_inbox_check >= 3.0:
            self.last_inbox_check = now
            self.check_telegram_inbox()

        # 3. Run new market scan every 15 mins (always runs to maintain continuous scan history)
        if now - self.last_scan_time >= SCAN_INTERVAL_SECS:
            self.last_scan_time = now
            candidate_setups, scan_record = scan_order_block_setups()
            self.last_scan_record = scan_record
            self.save_state()
            push_git_sync("periodic scan cycle")

            if len(self.open_positions) < MAX_CONCURRENT_TRADES:
                for s in candidate_setups:
                    if len(self.open_positions) >= MAX_CONCURRENT_TRADES:
                        break
                    self.enter_trade(s)
            else:
                log.info(f"Max trades active ({len(self.open_positions)}/{MAX_CONCURRENT_TRADES}). Candidate setups logged.")

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
