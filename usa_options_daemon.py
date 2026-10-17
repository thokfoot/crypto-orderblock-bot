"""
usa_options_daemon.py
=====================
Autonomous Production Daemon for USA Defined-Risk Debit Spreads (SPY + QQQ).

[QUANT AUDIT COUNCIL CONSENSUS NOTICE]:
This module is running in PAPER-SIMULATION SANDBOX ONLY.
Live capital deployment is quarantined per Council Consensus until a full
Black-Scholes-Merton Greek engine (Delta, Gamma, Vega, intraday Theta decay)
and real multi-leg Cboe options chain integration are completed.

=== DEPLOY 2026-10-14 — QQQ + SPY LONG-ONLY REFACTOR ===
Mandate: two tickers, $1,000 each ($2,000 total), one bull-call debit spread
per ticker, entry 10:15-10:30 ET, take-profit +50% of debit (13-Oct +0.60% spot),
no stop-loss (defined-risk only), square-off 15:30 ET via real proportional MTM.
No gap filter.

VERIFIED MODEL (60d, 5-min, 2026-07-22 -> 2026-10-14, real yfinance):
    QQQ bull-call 10:15/15:30  +$479.99  (+24.0% of $2,000-cap, 55.0% WR, PF 1.56)
    SPY bull-call 10:15/15:30  +$197.44  ( +9.9%,                 50.0% WR, PF 1.27)
CAVEAT: LONG-ONLY, 60-day, single bull regime, synthetic linear-delta spread
(P&L = ±0.8333 * spot-move%, capped +50% / -100%). NOT an out-of-sample proof,
NOT a real options chain. Forward-validation paper run only.

MANDATE RULE REJECTED: the proposed "Bull Call if price>VWAP/EMA50 else Bear Put"
direction rule was tested and REMOVED -- it cut QQQ from +24.0% to +0.8% (VWAP)
/ +4.8% (EMA) over the same 60 days. QQQ's edge is a long-drift; flipping short
on down-days trades into noise. Deployed as LONG-ONLY bull-call per user approval.

=== PER-SYMBOL EXIT REFACTOR (2026-10-17) — walk-forward validated ===
Walk-forward (Black-Scholes priced, ~729 sessions, 7 half-year buckets; see
research/USA_PAPER_WALKFORWARD.md) tested removing SPY's +50% upside cap:
    SPY  live(cap) -$85.70  ->  cap-off +$219.60   (6/7 half-years favour cap-off)
    QQQ  live(cap) +$769.10 ->  cap-off +$817.60   (total +$48.6 only; the 3 MOST
                                                    RECENT half-years favour LIVE)
=> Symbol-specific exit (fix the losing leg, do not touch the winning leg):
    SPY "spy_bullcall" : tp_mode="run_eod" (cap-off: no +0.6% spot TP; hold to EOD;
                         upside uncapped to the structural full-width max +66.7%)
    QQQ "qqq_bullcall" : tp_mode="spot_tp" (UNCHANGED: +0.6% spot -> +50% of debit)
CAVEAT: BS + ^VIX proxy (not real intraday IV); PAPER SANDBOX only.
"""

import os
import sys
import time
import json
import glob
import logging
import urllib.request
import urllib.parse
from datetime import datetime, time as dtime, timedelta
import pytz
import subprocess
import yfinance as yf
import pandas as pd

# Configuration (Injected securely via GitHub Actions Secrets)
TELEGRAM_BOT_TOKEN = os.environ.get("USA_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DEFAULT_CAPITAL = 2000.0
POLL_INTERVAL_SECS = int(os.environ.get("POLL_INTERVAL_SECS", "2"))
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "0"))  # 0 = infinite loop, >0 = bounded cloud run

# Market Hours (America/New_York)
TZ_NY = pytz.timezone("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
ENTRY_END_DEFAULT = dtime(14, 30)


def _t(h, m):
    return dtime(h, m)


def _t_add(base, minutes):
    return (datetime(2000, 1, 1, base.hour, base.minute) + timedelta(minutes=minutes)).time()


# ---------------------------------------------------------------------------
# MULTI-TICKER CONFIGURATION (SPY + QQQ long-only bull-call, EOD real-MTM)
# $1,000 per ticker ($2,000 total). Verified 60d: QQQ +24.0%, SPY +9.9%.
# Per-symbol exit (2026-10-17 walk-forward): SPY cap-off / QQQ keep live cap.
# ---------------------------------------------------------------------------
STRATEGY_CONFIGS = [
    {
        "key": "spy_bullcall",
        "name": "SPY Bull-Call 10:15/15:30",
        "symbol": "SPY",
        "entry_start": _t(10, 15),
        "entry_end": _t_add(_t(10, 15), 15),
        "square_off": _t(15, 30),
        # Walk-forward: SPY is a SLOW mover -> the +0.6% spot cap only cuts its
        # winners. Cap-off (hold to EOD, uncapped upside) is robust in 6/7
        # half-years: live -$85.70 -> cap-off +$219.60.
        "tp_mode": "run_eod",      # cap-off: no +0.6% spot TP; hold to EOD
        "upside_cap_pct": 66.7,    # structural max = (width*100 - debit)/debit
        "tp_pct": 0.0060,          # unused in run_eod mode (kept for reference)
        "sl_pct": None,            # defined-risk only (no synthetic spot-SL)
        "eod_mode": "mtm",         # real proportional MTM at square-off
        "initial_capital": 1000.0,
    },
    {
        "key": "qqq_bullcall",
        "name": "QQQ Bull-Call 10:15/15:30",
        "symbol": "QQQ",
        "entry_start": _t(10, 15),
        "entry_end": _t_add(_t(10, 15), 15),
        "square_off": _t(15, 30),
        # Walk-forward: QQQ is a FAST mover -> locking +0.6% beats holding into
        # afternoon reversals. Cap-off did NOT help QQQ (recent 3 half-years
        # favour LIVE). Keep live behaviour UNCHANGED.
        "tp_mode": "spot_tp",      # +0.6% spot -> +50% of debit
        "upside_cap_pct": 50.0,
        "tp_pct": 0.0060,
        "sl_pct": None,
        "eod_mode": "mtm",
        "initial_capital": 1000.0,
    },
]

FEE_USD = 1.30  # round-trip cost for one debit spread (brokerage + slippage estimate)

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

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
    """Commits and pushes all USA state to Git, tolerating concurrent runners via retry + rebase."""
    if not os.path.exists(".git"):
        return
    try:
        files = sorted(glob.glob("data/usa_*_state.json") + glob.glob("data/usa_*_trades.csv"))
        files += ["data/usa_dashboard.json", "usa_dashboard.json"]
        files = [f for f in files if os.path.exists(f)]
        if not files:
            return
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], capture_output=True, timeout=5)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], capture_output=True, timeout=5)
        subprocess.run(["git", "add", "-f"] + files, capture_output=True, timeout=5)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True, timeout=5)
        if diff.returncode == 0:
            return  # nothing to sync
        subprocess.run(["git", "commit", "-m", f"state: {comment} [skip ci]"], capture_output=True, timeout=10)
    except Exception as e:
        log.warning(f"USA Git commit notice: {e}")
        return

    for attempt in range(3):
        try:
            subprocess.run(["git", "fetch", "origin", "main"], capture_output=True, timeout=15)
            subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], capture_output=True, timeout=20)
            push_res = subprocess.run(["git", "push", "origin", "main"], capture_output=True, timeout=20)
            if push_res.returncode == 0:
                log.info("In-flight USA Git state push succeeded.")
                return
            log.warning(f"USA push attempt {attempt + 1} failed; retrying...")
        except Exception as e:
            log.warning(f"USA Git push notice (attempt {attempt + 1}): {e}")
        time.sleep(2 * (attempt + 1))
    log.warning("USA Git state push exhausted retries; state kept locally.")


def _yahoo_chart_price(symbol: str):
    """Direct Yahoo chart API fallback (independent of yfinance session handling)."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        res = data["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c]
        if closes:
            p = float(closes[-1])
            if p > 0:
                return p
    except Exception as e:
        log.warning(f"Yahoo chart fallback failed for {symbol}: {e}")
    return None


def get_spot(symbol: str = "SPY"):
    """Robust live spot for any ticker: yfinance with retries, then Yahoo chart fallback."""
    for attempt in range(3):
        try:
            df = yf.Ticker(symbol).history(period="1d", interval="1m")
            if df is None or df.empty:
                df = yf.Ticker(symbol).history(period="5d", interval="1m")
            if df is not None and not df.empty:
                p = float(df.iloc[-1]["Close"])
                if p > 0:
                    return p
        except Exception as e:
            log.warning(f"yfinance {symbol} fetch failed (attempt {attempt + 1}): {e}")
        time.sleep(1.5 * (attempt + 1))
    return _yahoo_chart_price(symbol)


def get_spy_spot():
    """Backward-compatible SPY spot wrapper."""
    return get_spot("SPY")


def spread_pnl_pct_from_move(move_pct, upside_cap=50.0):
    """Synthetic spread P&L % for a given % spot move (audit model).
    +: linear to `upside_cap` (50% live-cap / 66.7% cap-off structural max).
    -: proportional down to full debit (-100%)."""
    if move_pct >= 0:
        return min(upside_cap, move_pct * (50.0 / 0.60))
    return max(-100.0, move_pct * (50.0 / 0.60))


class StrategyAccount:
    """Independent paper strategy: own capital, own active trade, own ledger + state."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.key = cfg["key"]
        self.name = cfg["name"]
        self.initial_capital = float(cfg.get("initial_capital", DEFAULT_CAPITAL))
        self.capital = self.initial_capital
        self.active_trade = None
        self.last_trade_date = None
        self.state_path = f"data/usa_{self.key}_state.json"
        self.ledger_path = f"data/usa_{self.key}_trades.csv"
        self.load_state()

    def load_state(self):
        try:
            if os.path.exists(self.state_path) and os.path.getsize(self.state_path) > 5:
                with open(self.state_path, "r") as f:
                    data = json.load(f)
                    self.capital = float(data.get("capital", self.initial_capital))
                    self.active_trade = data.get("active_trade", None)
                    self.last_trade_date = data.get("last_trade_date", None)
        except Exception as e:
            log.warning(f"Could not load state for {self.key}: {e}")
        # Ledger robustness: recover daily guard if today already traded
        if not self.last_trade_date and os.path.exists(self.ledger_path):
            try:
                df_csv = pd.read_csv(self.ledger_path)
                today_ny = datetime.now(TZ_NY).strftime("%Y-%m-%d")
                if not df_csv.empty and "entry_time" in df_csv.columns:
                    if df_csv["entry_time"].astype(str).str.contains(today_ny).any():
                        self.last_trade_date = today_ny
            except Exception:
                pass

    def save_state(self):
        os.makedirs("data", exist_ok=True)
        data = {
            "capital": round(self.capital, 2),
            "active_trade": self.active_trade,
            "last_trade_date": self.last_trade_date,
            "updated_at": datetime.now(pytz.utc).isoformat()
        }
        try:
            temp_p = f"{self.state_path}.tmp.{os.getpid()}"
            with open(temp_p, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(temp_p, self.state_path)
        except Exception as e:
            log.warning(f"Could not atomically save {self.key} state: {e}")

    def log_trade(self, record):
        try:
            os.makedirs("data", exist_ok=True)
            df = pd.DataFrame([record])
            header = not os.path.exists(self.ledger_path) or os.path.getsize(self.ledger_path) == 0
            df.to_csv(self.ledger_path, mode="a", index=False, header=header)
            # Aggregate legacy file (backward compat + single overview)
            agg_p = "data/usa_paper_trades.csv"
            header_a = not os.path.exists(agg_p) or os.path.getsize(agg_p) == 0
            df.to_csv(agg_p, mode="a", index=False, header=header_a)
            log.info(f"[{self.key}] Logged trade to {self.ledger_path}")
        except Exception as e:
            log.warning(f"[{self.key}] Failed to log trade: {e}")

    def open_trade(self, current_spot, now_ny):
        symbol = self.cfg.get("symbol", "SPY")
        strike_long = round(current_spot)
        strike_short = strike_long + 2
        est_cost = 120.0  # $120 estimated spread debit
        self.active_trade = {
            "symbol": symbol,
            "strategy": self.key,
            "structure": f"Bull Call Spread ({strike_long}/{strike_short})",
            "entry_spot": current_spot,
            "entry_cost": est_cost,
            "entry_time": now_ny.strftime("%Y-%m-%d %H:%M:%S %Z")
        }
        self.last_trade_date = now_ny.strftime("%Y-%m-%d")
        self.save_state()
        return strike_long, strike_short, est_cost

    def floating_pnl_pct(self, cur_spot):
        if not self.active_trade or not cur_spot:
            return 0.0, 0.0
        entry_spot = float(self.active_trade.get("entry_spot", 0))
        if entry_spot <= 0:
            return 0.0, 0.0
        move = ((cur_spot - entry_spot) / entry_spot) * 100.0
        return move, spread_pnl_pct_from_move(move, self.cfg.get("upside_cap_pct", 50.0))

    def manage_exit(self, current_spot, now_ny, today_str):
        """Returns (closed_bool, reason_or_None). Applies TP / SL / EOD+MTM."""
        if not self.active_trade:
            return False, None
        entry_spot = float(self.active_trade["entry_spot"])
        entry_cost = float(self.active_trade["entry_cost"])
        t = now_ny.time()
        sl_pct = self.cfg.get("sl_pct")
        tp_pct = self.cfg.get("tp_pct", 0.0060)
        tp_mode = self.cfg.get("tp_mode", "spot_tp")
        upside_cap = self.cfg.get("upside_cap_pct", 50.0)

        hit_sl = sl_pct and current_spot <= entry_spot * (1 - sl_pct)
        hit_tp = tp_mode == "spot_tp" and current_spot >= entry_spot * (1 + tp_pct)
        hit_eod = t >= self.cfg["square_off"]

        if not (hit_sl or hit_tp or hit_eod):
            return False, None

        if hit_tp:
            reason = "TAKE_PROFIT"
            gross_pnl = entry_cost * 0.50
            pnl_pct = 50.0
        elif hit_sl:
            reason = "STOP_LOSS"
            gross_pnl = -(entry_cost * 0.30)
            pnl_pct = -30.0
        else:  # EOD square-off -> real mark-to-market (audit fix: no more forced $0)
            reason = "EOD_MTM"
            move = ((current_spot - entry_spot) / entry_spot) * 100.0
            pnl_pct = spread_pnl_pct_from_move(move, upside_cap)
            gross_pnl = entry_cost * (pnl_pct / 100.0)

        pnl = gross_pnl - FEE_USD
        self.capital += pnl

        record = {
            "symbol": self.cfg.get("symbol", "SPY"),
            "strategy": self.key,
            "action": "BUY_SPREAD",
            "entry_time": self.active_trade["entry_time"],
            "exit_time": now_ny.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "entry_price": entry_spot,
            "exit_price": current_spot,
            "pnl_pct": round(pnl_pct, 2),
            "fee_usd": FEE_USD,
            "pnl_usd": round(pnl, 2),
            "reason": reason,
            "balance": round(self.capital, 2)
        }
        self.log_trade(record)
        log.info(f"[{self.key}] Closed trade: PnL=${pnl:.2f} ({reason})")
        self.active_trade = None
        self.last_trade_date = today_str
        self.save_state()
        return True, reason


class USAOptionsTrader:
    def __init__(self):
        self.accounts = [StrategyAccount(c) for c in STRATEGY_CONFIGS]
        self.last_update_id = 0
        self.last_inbox_check = 0.0
        self.last_spot_price = None
        self.load_state()

    def load_state(self):
        # Telegram cursor persists in legacy dashboard for backward compat
        paths = ["data/usa_dashboard.json", "usa_dashboard.json"]
        for p in paths:
            if os.path.exists(p) and os.path.getsize(p) > 5:
                try:
                    with open(p, "r") as f:
                        data = json.load(f)
                        self.last_update_id = data.get("last_update_id", 0)
                        break
                except Exception:
                    pass

    def save_state(self):
        data = {
            "capital": round(sum(a.capital for a in self.accounts), 2),
            "strategies": {
                a.key: {
                    "name": a.name,
                    "capital": round(a.capital, 2),
                    "active_trade": a.active_trade,
                    "last_trade_date": a.last_trade_date
                }
                for a in self.accounts
            },
            "last_update_id": self.last_update_id,
            "last_spot_price": self.last_spot_price,
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
                log.warning(f"Could not atomically save aggregate dashboard to {p}: {e}")
        for a in self.accounts:
            a.save_state()

    def _remote_trade_exists_today(self, today_str, key):
        """Concurrent-runner guard per strategy: check ORIGIN state before opening a new trade."""
        try:
            subprocess.run(["git", "fetch", "origin", "main"], capture_output=True, timeout=15)
            r = subprocess.run(["git", "show", f"origin/main:data/usa_{key}_state.json"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                if data.get("active_trade"):
                    return True
                if data.get("last_trade_date") == today_str:
                    return True
            r2 = subprocess.run(["git", "show", f"origin/main:data/usa_{key}_trades.csv"],
                                capture_output=True, text=True, timeout=10)
            if r2.returncode == 0 and today_str in (r2.stdout or ""):
                return True
        except Exception as e:
            log.warning(f"[{key}] Remote daily-guard check failed: {e}")
        return False

    def send_telegram_status_summary(self):
        is_open, status_str = self.is_market_open()
        now_ny = datetime.now(TZ_NY)
        invested = sum(a.initial_capital for a in self.accounts)
        realized_pnl = sum(a.capital for a in self.accounts) - invested
        spot_prices = {}
        for a in self.accounts:
            sym = a.cfg.get("symbol", "SPY")
            if sym not in spot_prices:
                spot_prices[sym] = get_spot(sym)
        if spot_prices.get("SPY"):
            self.last_spot_price = spot_prices["SPY"]

        lines = [
            "🇺🇸 *USA OPTIONS LIVE STATUS SUMMARY (SPY + QQQ)*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"💰 *Total Capital:* `${sum(a.capital for a in self.accounts):,.2f} USD`",
            f"💎 *Realized P&L (Banked):* `${realized_pnl:+.2f} USD`",
            f"🕒 *Market Status:* `{status_str}` ({now_ny.strftime('%I:%M %p %Z')})",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for a in self.accounts:
            sym = a.cfg.get("symbol", "SPY")
            cur_spot = spot_prices.get(sym)
            if a.active_trade:
                t = a.active_trade
                entry_spot = float(t.get("entry_spot", 0))
                entry_cost = float(t.get("entry_cost", 120.0))
                if cur_spot and entry_spot > 0:
                    move, sp = a.floating_pnl_pct(cur_spot)
                    floating_pnl = entry_cost * (sp / 100.0)
                    pnl_str = f"`${floating_pnl:+.2f} USD` ({sp:+.1f}%)"
                else:
                    pnl_str = "`Syncing...`"
                lines.append(f"• *{a.name}*  —  `${a.capital:,.2f}` · {pnl_str}")
                lines.append(f"  └ Entry `${entry_spot:,.2f}` @ `{t.get('entry_time')}`")
            else:
                lines.append(f"• *{a.name}*  —  `${a.capital:,.2f}` · 💤 no active spread")
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━",
            "``🛡️ EOD = real mark-to-market``",
            "⏱️ *System:* Cloud Daemon (Fast 2s Polling)",
            "👉 _Tap buttons below anytime for instant refresh_"
        ]
        send_telegram("\n".join(lines))

    def send_telegram_pnl_report(self):
        realized_pnl = sum(a.capital for a in self.accounts) - sum(a.initial_capital for a in self.accounts)
        lines = [
            "💰 *USA OPTIONS REALIZED P&L REPORT*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"💎 *Net Banked Realized P&L:* *`${realized_pnl:+.2f} USD`*",
        ]
        for a in self.accounts:
            n = w = l = 0
            if os.path.exists(a.ledger_path):
                try:
                    df = pd.read_csv(a.ledger_path)
                    if not df.empty and "pnl_usd" in df.columns:
                        n = len(df)
                        w = len(df[df["pnl_usd"] > 0])
                        l = len(df[df["pnl_usd"] < 0])
                except Exception:
                    pass
            wr = (w / n * 100) if n else 0.0
            lines.append(f"• *{a.name}* — Cap `${a.capital:,.2f}` · {n} trades · `{wr:.0f}%` WR")
        lines += ["━━━━━━━━━━━━━━━━━━━━━━", "👉 _Tap buttons below anytime for instant refresh_"]
        send_telegram("\n".join(lines))

    def check_telegram_inbox(self):
        if not TELEGRAM_BOT_TOKEN or "placeholder" in TELEGRAM_BOT_TOKEN:
            return
        try:
            if self.last_update_id == 0:
                probe_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset=-1&limit=1"
                probe_req = urllib.request.Request(probe_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(probe_req, timeout=3) as probe_resp:
                    probe_data = json.loads(probe_resp.read().decode("utf-8"))
                    probe_res = probe_data.get("result", [])
                    if probe_res:
                        self.last_update_id = probe_res[-1].get("update_id", 0)
                        self.save_state()
                        log.info("USA Telegram inbox initialized on cold boot.")
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={self.last_update_id + 1}&limit=10&timeout=1"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for update in data.get("result", []):
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
                    # Normalize away the emoji prefix on reply-keyboard labels so
                    # button taps ("Status Summary"/"P&L Report") and typed commands match.
                    cmd_clean = "".join(ch for ch in text.strip().lower() if ord(ch) < 128).strip()
                    if cmd_clean in {"status", "summary", "status summary", "/status", "/summary", "/start"} or cmd_clean.startswith("/status"):
                        self.send_telegram_status_summary()
                        self.save_state()
                    elif cmd_clean in {"pnl", "p&l", "profit", "report", "p&l report", "pnl report", "/pnl"} or cmd_clean.startswith("/pnl"):
                        self.send_telegram_pnl_report()
                        self.save_state()
        except Exception as e:
            log.warning(f"USA telegram inbox notice: {e}")

    def is_market_open(self):
        now_ny = datetime.now(TZ_NY)
        if now_ny.weekday() >= 5:
            return False, "Weekend (Market Closed)"
        t = now_ny.time()
        if t < MARKET_OPEN or t >= MARKET_CLOSE:
            return False, f"Outside Market Hours ({t.strftime('%H:%M')} NY)"
        return True, "Market Open"

    def force_eod_safety_close(self, now_ny, t, today_str):
        """Never carry a spread overnight. If a trade is still open at/after its
        square-off time, force an EOD mark-to-market close even when the market
        clock reads closed (e.g. a delayed runner started after 16:00 ET, or a
        weekend carry). Idempotent: once flat, later cycles skip."""
        for account in self.accounts:
            if not account.active_trade:
                continue
            if t < account.cfg["square_off"]:
                continue
            sym = account.cfg.get("symbol", "SPY")
            spot = get_spot(sym)
            if not spot:
                log.warning(f"[{account.key}] EOD safety close pending: no {sym} spot available.")
                continue
            closed, reason = account.manage_exit(spot, now_ny, today_str)
            if closed:
                self.save_state()
                msg = (
                    f"🇺🇸 *{account.name}* — EOD SAFETY CLOSE: `{reason}`\n\n"
                    f"• *Exit {sym} Price:* `${spot:,.2f}`\n"
                    f"• *Realized P&L:* `${account.capital:,.2f}` (bal)\n"
                    f"• *Guard:* forced square-off (no overnight carry)"
                )
                send_telegram(msg, reply_markup=None)
                push_git_sync(f"usa closed {account.key} ({reason} safety)")

    def run_cycle(self):
        now_ts = time.time()
        if now_ts - self.last_inbox_check >= 3.0:
            self.last_inbox_check = now_ts
            self.check_telegram_inbox()

        is_open, status_str = self.is_market_open()
        now_ny = datetime.now(TZ_NY)
        t = now_ny.time()
        today_str = now_ny.strftime("%Y-%m-%d")

        if not is_open:
            self.force_eod_safety_close(now_ny, t, today_str)
            log.info(f"[USA CLOCK] {status_str}. Sleeping until market open...")
            return

        # Fetch spot per ticker once per cycle (SPY + QQQ)
        spot_prices = {}
        for account in self.accounts:
            sym = account.cfg.get("symbol", "SPY")
            if sym not in spot_prices:
                spot_prices[sym] = get_spot(sym)
        if all(v is None for v in spot_prices.values()):
            log.warning("No spot available for any ticker this cycle (all sources failed); skipping.")
            return
        self.last_spot_price = spot_prices.get("SPY") or next(
            (v for v in spot_prices.values() if v), None)

        # Manage active trades + new entries per strategy independently
        for account in self.accounts:
            sym = account.cfg.get("symbol", "SPY")
            current_spot = spot_prices.get(sym)
            if not current_spot:
                log.warning(f"[{account.key}] {sym} spot unavailable this cycle; skipping.")
                continue

            closed, reason = account.manage_exit(current_spot, now_ny, today_str)
            if closed:
                self.save_state()
                msg = (
                    f"🇺🇸 *{account.name}* — TRADE CLOSED: `{reason}`\n\n"
                    f"• *Exit {sym} Price:* `${current_spot:,.2f}`\n"
                    f"• *Realized P&L:* `${account.capital:,.2f}` (bal)\n"
                    f"• *EOD:* real mark-to-market"
                )
                send_telegram(msg, reply_markup=None)
                push_git_sync(f"usa closed {account.key} ({reason})")
                continue

            es = account.cfg["entry_start"]
            ee = account.cfg["entry_end"]
            if es <= t <= ee and not account.active_trade:
                if account.last_trade_date == today_str:
                    continue
                if self._remote_trade_exists_today(today_str, account.key):
                    log.info(f"[{account.key}] Remote guard: trade already exists today on origin.")
                    account.last_trade_date = today_str
                    account.save_state()
                    self.save_state()
                    continue
                sl, sk, cost = account.open_trade(current_spot, now_ny)
                self.save_state()
                push_git_sync(f"usa opened {account.key} spread")
                if account.cfg.get("tp_mode") == "run_eod":
                    tp_txt = f"cap-off \u00b7 hold to EOD (max +{account.cfg.get('upside_cap_pct', 66.7):.1f}%)"
                else:
                    tp_txt = "+50% ($60.00)"
                msg = (
                    f"🇺🇸 *{account.name}* — NEW SPREAD OPENED\n\n"
                    f"• *Underlying:* `{sym} @ ${current_spot:,.2f}`\n"
                    f"• *Structure:* `Bull Call Spread {sl}/{sk}`\n"
                    f"• *Cost / Max Risk:* `${cost:,.2f}`\n"
                    f"• *Take Profit:* `{tp_txt}`\n"
                    f"• *Note:* defined-risk only · EOD real MTM · SL `{account.cfg.get('sl_pct') or 'off'}`"
                )
                log.info(f"[{account.key}] Opened {sym} spread @ ${current_spot:.2f}")
                send_telegram(msg, reply_markup=None)


def main():
    log.info("Starting Autonomous USA SPY+QQQ Options Paper Trading Daemon...")
    trader = USAOptionsTrader()
    names = " · ".join(a.name for a in trader.accounts)
    send_telegram(f"🚀 *USA SPY+QQQ Options Paper Daemon Started*\n{names}\nClock: New York (Mon-Fri 09:30-16:00 ET). LONG-ONLY bull-call, defined-risk.")

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