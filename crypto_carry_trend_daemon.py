"""
crypto_carry_trend_daemon.py
============================
Autonomous PAPER paper-trading daemon for a two-engine crypto book.

DEPLOYED 2026-10-14 — replaces the 15m altcoin SMC order-block scalper
(``crypto_orderblock_daemon.py``), which was decommissioned after an
out-of-sample audit proved its edge structurally NEGATIVE:
  * live-rules replay ~1yr: 1,940 trades, 34.9% WR, net -$3,248
  * 10-config OOS tune sweep: every config negative in BOTH train and test
  * the coin universe was even survivorship-biased toward winners.

Two engines, $2,000 total, PAPER ONLY (Binance public REST, no keys, no orders):

  ENGINE 1 — FUNDING CARRY (delta-neutral), $1,000
     Long spot + short perp on BTCUSDT and ETHUSDT ($500 notional each).
     Collects the 8-hourly funding payment when fundingRate > 0 (shorts
     receive). Credited at each Binance settlement (00:00 / 08:00 / 16:00 UTC),
     idempotently via the settled fundingRate history.
     Verified edge: BTC 12.2%/yr Sharpe 10.6 maxDD -1.5% (86% of periods +),
     ETH 14.7%/yr. Out-of-sample 2024+ ~7%/yr on deployed capital.

  ENGINE 2 — TREND FOLLOWING (long/cash), $1,000
     BTCUSDT and ETHUSDT ($500 each), evaluated once per daily close (00:00 UTC).
     Hold long while Close > SMA50; flat (cash) otherwise.
     Verified edge: BTC+ETH 50/50 SMA50 [TEST] Sharpe 1.16, CAGR 41.9%,
     maxDD -30.3%. Robust across SMA 50/100/200.

Both engines are PAPER simulations from real public market data. No API keys,
no order placement, no real capital. Journals to
``data/crypto_paper_trades.csv`` and state to ``data/crypto_dashboard.json``.
"""

import os
import sys
import time
import json
import logging
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
import subprocess
import pandas as pd

# --------------------------------------------------------------------------- #
# CONFIGURATION
# --------------------------------------------------------------------------- #
INITIAL_CAPITAL = 2000.0
ENGINE1_CAPITAL = 1000.0          # funding carry sleeve
ENGINE2_CAPITAL = 1000.0          # trend sleeve
CARRY_NOTIONAL = {"BTCUSDT": 500.0, "ETHUSDT": 500.0}
TREND_ALLOC = {"BTCUSDT": 500.0, "ETHUSDT": 500.0}
SMA_PERIOD = 50
POLL_INTERVAL_SECS = int(os.environ.get("POLL_INTERVAL_SECS", "2"))
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "0"))
TELEGRAM_BOT_TOKEN = os.environ.get("CRYPTO_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
DASHBOARD_FILE = os.path.join(DATA_DIR, "crypto_dashboard.json")
CSV_FILE = os.path.join(DATA_DIR, "crypto_paper_trades.csv")
LOG_FILE = "crypto_carry_trend.log"

CSV_HEADER = "symbol,action,entry_time,exit_time,entry_price,exit_price,pnl_pct,fee_usd,pnl_usd,reason,balance"

try:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("CryptoCarryTrend")

STATUS_KEYBOARD = json.dumps({
    "keyboard": [[{"text": "📊 Status Summary"}, {"text": "💰 P&L Report"}]],
    "resize_keyboard": True,
    "is_persistent": True,
})


# --------------------------------------------------------------------------- #
# TELEGRAM
# --------------------------------------------------------------------------- #
def send_telegram(message: str, reply_markup=STATUS_KEYBOARD):
    if not TELEGRAM_BOT_TOKEN or "placeholder" in TELEGRAM_BOT_TOKEN:
        log.info(f"[TELEGRAM SIMULATED]:\n{message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload_dict = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        if reply_markup:
            payload_dict["reply_markup"] = reply_markup
        payload = urllib.parse.urlencode(payload_dict).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            pass
    except Exception:
        try:
            payload_plain = {"chat_id": TELEGRAM_CHAT_ID, "text": message.replace("*", "").replace("`", "")}
            if reply_markup:
                payload_plain["reply_markup"] = reply_markup
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data=urllib.parse.urlencode(payload_plain).encode("utf-8"),
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                pass
        except Exception as e2:
            log.warning(f"Telegram alert failed: {e2}")


def answer_callback_query(callback_query_id: str, text: str = ""):
    if not TELEGRAM_BOT_TOKEN or "placeholder" in TELEGRAM_BOT_TOKEN:
        return
    try:
        payload_dict = {"callback_query_id": callback_query_id}
        if text:
            payload_dict["text"] = text
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            data=urllib.parse.urlencode(payload_dict).encode("utf-8"),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            pass
    except Exception:
        pass


def push_git_sync(comment: str = "crypto-carry-trend-sync"):
    try:
        if not os.path.exists(".git"):
            return
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], capture_output=True, timeout=5)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], capture_output=True, timeout=5)
        subprocess.run(["git", "add", "data/crypto_dashboard.json", "data/crypto_paper_trades.csv", "crypto_dashboard.json"], capture_output=True, timeout=5)
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
# BINANCE API HELPERS (public REST, read-only)
# --------------------------------------------------------------------------- #
def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict) and "code" in data and isinstance(data["code"], int) and data["code"] < 0:
                    log.warning(f"Binance API error from {url}: {data}")
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return data
        except urllib.error.HTTPError as he:
            if he.code in (418, 429):
                time.sleep(2.5 * (attempt + 1))
            else:
                time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
    return None


def get_live_price(symbol: str):
    for url in (
        f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}",
        f"https://data-api.binance.vision/api/v3/ticker/price?symbol={symbol}",
    ):
        data = fetch_json(url)
        if data and isinstance(data, dict) and "price" in data:
            try:
                p = float(data["price"])
                if p > 0:
                    return p
            except Exception:
                continue
    return None


def get_klines_df(symbol: str, interval="1d", limit=80):
    for url in (
        f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
    ):
        data = fetch_json(url)
        if data and isinstance(data, list) and len(data) > 0:
            cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "q_vol", "trades", "tb_vol", "tb_qvol", "ignore"]
            df = pd.DataFrame(data, columns=cols)
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = df[c].astype(float)
            df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            return df
    return None


FUNDING_HOSTS = ("https://fapi.binance.com", "https://www.binance.com")
_funding_host = None


def get_funding_history(symbol: str, limit=20):
    """Settled funding-rate history (fields: fundingTime ms, fundingRate str).

    fapi.binance.com is geo-blocked (HTTP 451) from some CI runners (e.g. GitHub
    Actions), which silently returned no funding -> zero carry accrual. Fall back
    to www.binance.com/fapi (same futures API, reachable from US) and cache the
    first host that works so later polls stay cheap.
    """
    global _funding_host
    hosts = ([_funding_host] if _funding_host else []) + [h for h in FUNDING_HOSTS if h != _funding_host]
    for host in hosts:
        data = fetch_json(f"{host}/fapi/v1/fundingRate?symbol={symbol}&limit={limit}")
        if isinstance(data, list) and len(data) > 0:
            _funding_host = host
            return data
    return []


# --------------------------------------------------------------------------- #
# DUAL-ENGINE PAPER TRADER
# --------------------------------------------------------------------------- #
class CarryTrendTrader:
    def __init__(self):
        self.funding_capital = ENGINE1_CAPITAL
        self.funding_accrued = 0.0
        self.funding_last_settlement_ms = 0
        self.carry_notional = dict(CARRY_NOTIONAL)

        self.trend_capital = ENGINE2_CAPITAL
        self.trend_positions = {
            s: {"in_position": False, "entry_price": None, "equity": float(TREND_ALLOC[s]), "last_price": None}
            for s in TREND_ALLOC
        }
        self.trend_last_eval_ms = {s: 0 for s in TREND_ALLOC}
        self.trend_regime = {s: "CASH" for s in TREND_ALLOC}

        self.last_update_id = 0
        self.last_inbox_check = 0.0
        self.load_state()

    # ---- persistence ------------------------------------------------------- #
    def load_state(self):
        for path in (DASHBOARD_FILE, "crypto_dashboard.json"):
            if os.path.exists(path) and os.path.getsize(path) > 5:
                try:
                    with open(path, "r") as f:
                        d = json.load(f)
                    self.funding_capital = float(d.get("funding_capital", ENGINE1_CAPITAL))
                    self.funding_accrued = float(d.get("funding_yield_accrued", 0.0))
                    self.funding_last_settlement_ms = int(d.get("funding_last_settlement_ms", 0))
                    self.carry_notional = d.get("carry_notional", dict(CARRY_NOTIONAL))
                    self.trend_capital = float(d.get("trend_capital", ENGINE2_CAPITAL))
                    tp = d.get("trend_positions")
                    if isinstance(tp, dict) and tp:
                        for s in self.trend_positions:
                            if s in tp and isinstance(tp[s], dict):
                                self.trend_positions[s].update(tp[s])
                    le = d.get("trend_last_eval_ms")
                    if isinstance(le, dict):
                        for s in self.trend_last_eval_ms:
                            self.trend_last_eval_ms[s] = int(le.get(s, 0))
                    tr = d.get("trend_regime")
                    if isinstance(tr, dict):
                        for s in self.trend_regime:
                            self.trend_regime[s] = tr.get(s, "CASH")
                    self.last_update_id = int(d.get("last_update_id", 0))
                    log.info(f"Loaded state from {path}: funding ${self.funding_accrued:.2f} accrual, trend regimes {self.trend_regime}")
                    return
                except Exception as e:
                    log.warning(f"Could not load state from {path}: {e}")
        log.info("No prior valid state; initializing fresh dual-engine book ($2,000).")

    def total_equity(self):
        eq = self.funding_capital + self.funding_accrued + self.trend_capital
        for s, p in self.trend_positions.items():
            if p.get("in_position") and p.get("entry_price") and p.get("last_price"):
                eq += p["equity"] * (p["last_price"] / p["entry_price"] - 1.0)
        return eq

    def save_state(self):
        data = {
            "capital": round(self.total_equity(), 2),
            "funding_capital": round(self.funding_capital, 2),
            "funding_yield_accrued": round(self.funding_accrued, 2),
            "funding_last_settlement_ms": self.funding_last_settlement_ms,
            "carry_notional": self.carry_notional,
            "trend_capital": round(self.trend_capital, 2),
            "trend_positions": self.trend_positions,
            "trend_regime": self.trend_regime,
            "trend_last_eval_ms": self.trend_last_eval_ms,
            # legacy/compat keys
            "open_positions": {},
            "total_trades": self._count_trades(),
            "last_update_id": self.last_update_id,
            "engine": "carry+trend v1",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        for path in (DASHBOARD_FILE, "crypto_dashboard.json"):
            try:
                dir_name = os.path.dirname(path)
                if dir_name:
                    os.makedirs(dir_name, exist_ok=True)
                temp_path = f"{path}.tmp.{os.getpid()}"
                with open(temp_path, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(temp_path, path)
            except Exception as e:
                log.warning(f"Could not atomically save to {path}: {e}")

    def _count_trades(self):
        if os.path.exists(CSV_FILE):
            try:
                return len(pd.read_csv(CSV_FILE))
            except Exception:
                return 0
        return 0

    def log_row(self, record):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            df = pd.DataFrame([record])
            header = not os.path.exists(CSV_FILE) or os.path.getsize(CSV_FILE) == 0
            df.to_csv(CSV_FILE, mode="a", index=False, header=header)
        except Exception as e:
            log.warning(f"Failed to journal row: {e}")

    # ---- ENGINE 1: funding carry ------------------------------------------ #
    def accrue_funding(self):
        now_ms = int(time.time() * 1000)
        total_new = 0.0
        for sym, notional in self.carry_notional.items():
            events = get_funding_history(sym)
            for ev in events:
                try:
                    ft = int(ev["fundingTime"])
                    rate = float(ev["fundingRate"])
                except Exception:
                    continue
                if self.funding_last_settlement_ms < ft <= now_ms:
                    accrual = notional * rate
                    total_new += accrual
                    self.funding_accrued += accrual
                    self.log_row({
                        "symbol": sym,
                        "action": "FUNDING_CARRY",
                        "entry_time": datetime.fromtimestamp(ft / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                        "exit_time": datetime.fromtimestamp(ft / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                        "entry_price": "",
                        "exit_price": "",
                        "pnl_pct": round(rate * 100, 6),
                        "fee_usd": 0.0,
                        "pnl_usd": round(accrual, 4),
                        "reason": "FUNDING_8H",
                        "balance": round(self.funding_capital + self.funding_accrued, 2),
                    })
        if now_ms > self.funding_last_settlement_ms:
            self.funding_last_settlement_ms = now_ms
        if total_new:
            log.info(f"Funding accrued this cycle: ${total_new:+.4f} (lifetime ${self.funding_accrued:+.4f})")

    # ---- ENGINE 2: SMA50 daily trend -------------------------------------- #
    def eval_trend(self):
        for sym, alloc in TREND_ALLOC.items():
            df = get_klines_df(sym, interval="1d", limit=SMA_PERIOD + 20)
            if df is None or len(df) < SMA_PERIOD + 1:
                log.warning(f"[{sym}] insufficient daily klines for trend eval.")
                continue
            closed = df.iloc[:-1]  # drop the in-progress candle
            last = closed.iloc[-1]
            bar_close_ms = int(last["close_time"])
            if bar_close_ms <= self.trend_last_eval_ms.get(sym, 0):
                continue
            sma = float(closed["close"].iloc[-SMA_PERIOD:].mean())
            last_close = float(last["close"])
            regime = "LONG" if last_close > sma else "CASH"
            pos = self.trend_positions[sym]
            pos["last_price"] = last_close

            if regime == "LONG" and not pos["in_position"]:
                pos["in_position"] = True
                pos["entry_price"] = last_close
                log.info(f"[{sym}] TREND ENTRY @ {last_close:.4f} (SMA{SMA_PERIOD}={sma:.4f})")
                self.log_row(self._trend_row(sym, "TREND_LONG", last_close, last_close, 0.0, "SMA50_CROSS_UP"))
                send_telegram(f"📈 *Crypto Trend* — {sym} LONG @ `${last_close:,.4f}` (Close>SMA50)")
            elif regime == "CASH" and pos["in_position"]:
                entry = pos.get("entry_price") or last_close
                ret = (last_close / entry - 1.0)
                pos["equity"] = pos["equity"] * (1.0 + ret)
                pos["in_position"] = False
                pos["entry_price"] = None
                log.info(f"[{sym}] TREND EXIT @ {last_close:.4f} (ret {ret*100:+.2f}%)")
                self.log_row(self._trend_row(sym, "TREND_EXIT", entry, last_close, ret * 100, "SMA50_CROSS_DOWN"))
                send_telegram(f"📉 *Crypto Trend* — {sym} EXIT @ `${last_close:,.4f}` ({ret*100:+.2f}%)")

            self.trend_regime[sym] = regime
            self.trend_last_eval_ms[sym] = bar_close_ms

    def _trend_row(self, sym, action, entry, exit_p, pnl_pct, reason):
        pos = self.trend_positions[sym]
        return {
            "symbol": sym,
            "action": action,
            "entry_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "exit_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "entry_price": round(float(entry), 6),
            "exit_price": round(float(exit_p), 6),
            "pnl_pct": round(float(pnl_pct), 4),
            "fee_usd": 0.0,
            "pnl_usd": round(pos["equity"] * float(pnl_pct) / 100.0, 4),
            "reason": reason,
            "balance": round(self.total_equity(), 2),
        }

    def mark_to_market(self):
        for sym, pos in self.trend_positions.items():
            p = get_live_price(sym)
            if p and p > 0:
                pos["last_price"] = p

    # ---- TELEGRAM ---------------------------------------------------------- #
    def send_status(self):
        self.mark_to_market()
        eq = self.total_equity()
        lines = [
            "🪙 *CRYPTO CARRY + TREND PAPER STATUS*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"💰 *Total Equity:* `${eq:,.2f} USD` (start $2,000)",
            f"💵 *Funding Accrued:* `${self.funding_accrued:+.4f} USD`",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "*Engine 1 — Funding Carry (delta-neutral):*",
        ]
        for s, n in self.carry_notional.items():
            lines.append(f"  • {s}: notional `${n:,.0f}` (long spot + short perp)")
        lines.append("*Engine 2 — SMA50 Trend (long/cash):*")
        for s, pos in self.trend_positions.items():
            tag = "🟢 LONG" if pos.get("in_position") else "⚪ CASH"
            mtm = ""
            if pos.get("in_position") and pos.get("entry_price") and pos.get("last_price"):
                r = (pos["last_price"] / pos["entry_price"] - 1.0) * 100
                mtm = f" · entry `${pos['entry_price']:,.4f}` ({r:+.2f}%)"
            lines.append(f"  • {s}: {tag}{mtm}")
        lines += ["━━━━━━━━━━━━━━━━━━━━━━", "👉 _Paper only. Tap buttons for refresh._"]
        send_telegram("\n".join(lines))

    def send_pnl_report(self):
        eq = self.total_equity()
        pnl = eq - INITIAL_CAPITAL
        lines = [
            "💰 *CRYPTO CARRY + TREND REALIZED REPORT*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"💎 *Net Banked Equity:* `${eq:,.2f}`",
            f"📈 *Total P&L:* `${pnl:+.2f}` ({(pnl/INITIAL_CAPITAL*100):+.2f}%)",
            f"💵 *Funding Accrued:* `${self.funding_accrued:+.4f}`",
            f"📊 *Trend Equity:* `${self.trend_capital:,.2f}`",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        send_telegram("\n".join(lines))

    def check_telegram_inbox(self):
        if not TELEGRAM_BOT_TOKEN or "placeholder" in TELEGRAM_BOT_TOKEN:
            return
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={self.last_update_id + 1}&limit=10&timeout=1"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for update in data.get("result", []):
                up_id = update.get("update_id", 0)
                if up_id > self.last_update_id:
                    self.last_update_id = up_id
                cb = update.get("callback_query")
                msg = update.get("message")
                if cb:
                    answer_callback_query(cb.get("id"))
                    text = cb.get("data", "")
                    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                elif msg:
                    text = msg.get("text", "").strip()
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                else:
                    continue
                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue
                cmd = text.strip().lower()
                if cmd in {"status", "summary", "/status", "/summary", "/start"} or cmd.startswith("/status"):
                    self.send_status()
                elif cmd in {"pnl", "/pnl", "profit", "report"} or cmd.startswith("/pnl"):
                    self.send_pnl_report()
        except Exception as e:
            log.warning(f"Telegram inbox notice: {e}")

    # ---- MAIN CYCLE -------------------------------------------------------- #
    def run_cycle(self):
        now = time.time()
        if now - self.last_inbox_check >= 3.0:
            self.last_inbox_check = now
            self.check_telegram_inbox()

        try:
            self.accrue_funding()
            self.eval_trend()
            self.save_state()
        except Exception as e:
            log.error(f"Cycle error: {e}")


def main():
    log.info("Starting Crypto Carry + Trend dual-engine PAPER daemon ($2,000)...")
    trader = CarryTrendTrader()
    trader.save_state()
    send_telegram("🚀 *Crypto Carry + Trend PAPER daemon started* ($2,000)\nEngine1: funding carry BTC/ETH · Engine2: SMA50 trend BTC/ETH")

    if RUN_MINUTES > 0:
        log.info(f"Cloud Runner Mode: Active for {RUN_MINUTES} minutes.")
        end_time = time.time() + (RUN_MINUTES * 60)
        while time.time() < end_time:
            try:
                trader.run_cycle()
                time.sleep(POLL_INTERVAL_SECS)
            except Exception as e:
                log.error(f"Error in cycle: {e}")
                time.sleep(10)
        trader.save_state()
        log.info("Cloud Runner finished cleanly. State saved.")
    else:
        log.info("Infinite Daemon Mode: running continuously.")
        while True:
            try:
                trader.run_cycle()
                time.sleep(POLL_INTERVAL_SECS)
            except KeyboardInterrupt:
                trader.save_state()
                break
            except Exception as e:
                log.error(f"Unexpected error in daemon loop: {e}")
                time.sleep(10)


if __name__ == "__main__":
    main()
