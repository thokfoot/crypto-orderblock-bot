"""
scratch/run_max_crypto_backtest.py
==================================
Maximum Historical Backtest for Binance 15m Crypto Order Block (SMC) Strategy.
Paginates ~10,000 bars (~104 days / ~3.5 months) of continuous 15m klines from Binance Public Futures.

Simulates the exact production state machine:
- $2,000 initial virtual capital
- $200 position size (max 3 concurrent positions)
- 15m Order Block (BOS, volume spike > 1.25x MA20, unmitigated 0-tap freshness)
- 1.2x ATR stop buffer
- BTC 1H Macro Regime Filter (EMA50 + RSI > 48)
- Breakeven lock @ +1.5% -> Entry + 0.40% buffer
- Scale-Out @ +2.0% -> 50% banked to balance, remaining trailed with Chandelier (peak - 2.5x ATR)
- 0.30% roundtrip taker fee + slippage friction deduction
"""

import os
import sys
import time
import json
import urllib.request
import pandas as pd
import numpy as np
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "scratch", "crypto_cache")
os.makedirs(DATA_DIR, exist_ok=True)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT", "NEARUSDT", "SUIUSDT", "ARBUSDT"]
BARS_TARGET = 9000  # ~94 days of 15m data (~3.1 months)

def fetch_klines(symbol: str, interval: str, target_bars: int = 9000) -> pd.DataFrame:
    cache_file = os.path.join(DATA_DIR, f"{symbol}_{interval}_{target_bars}.csv")
    if os.path.exists(cache_file):
        print(f"Loading {symbol} ({interval}) from cache...")
        df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
        return df

    print(f"Fetching {symbol} ({interval}) from Binance Futures ({target_bars} bars)...")
    all_klines = []
    end_time = None
    calls = int(np.ceil(target_bars / 1000))

    for i in range(calls):
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit=1000"
        if end_time:
            url += f"&endTime={end_time}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not data:
                break
            all_klines = data + all_klines
            end_time = data[0][0] - 1
            time.sleep(0.08)
        except Exception as e:
            print(f"Error fetching {symbol}: {e}")
            break

    if not all_klines:
        return pd.DataFrame()

    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", 
            "quote_vol", "trades", "taker_base", "taker_quote", "ignore"]
    df = pd.DataFrame(all_klines, columns=cols)
    for col in ["open", "high", "low", "close", "volume", "quote_vol"]:
        df[col] = df[col].astype(float)
    df["datetime"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
    df.set_index("datetime", inplace=True)
    df.to_csv(cache_file)
    print(f"  -> Saved {len(df)} bars ({df.index[0]} to {df.index[-1]})")
    return df

def run_crypto_smc_backtest():
    print("="*80)
    print("      MODULE 2: BINANCE 15M CRYPTO ORDER BLOCK (SMC) MAXIMUM BACKTEST         ")
    print("="*80)

    # 1. Fetch BTC 1H data for Macro Regime Filter
    btc_1h = fetch_klines("BTCUSDT", "1h", target_bars=2500)
    btc_1h["ema50"] = btc_1h["close"].ewm(span=50).mean()
    delta = btc_1h["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    btc_1h["rsi"] = 100 - (100 / (1 + rs))

    # Helper function for BTC regime at a given timestamp
    def is_btc_bullish_at(dt):
        sub = btc_1h[btc_1h.index <= dt]
        if len(sub) < 50:
            return True
        last_row = sub.iloc[-1]
        return bool(last_row["close"] > last_row["ema50"] and last_row["rsi"] > 48.0)

    # 2. Fetch 15m data for all universe symbols
    symbol_data = {}
    for s in SYMBOLS:
        df = fetch_klines(s, "15m", target_bars=BARS_TARGET)
        if not df.empty:
            # Precompute ATR and Volume MA
            df["tr"] = np.maximum(
                df["high"] - df["low"],
                np.maximum(abs(df["high"] - df["close"].shift(1)), abs(df["low"] - df["close"].shift(1)))
            )
            df["atr14"] = df["tr"].rolling(14).mean()
            df["vol_ma20"] = df["volume"].rolling(20).mean()
            symbol_data[s] = df

    # Find common date range
    start_dt = max(df.index[80] for df in symbol_data.values())
    end_dt = min(df.index[-1] for df in symbol_data.values())
    print(f"\nBacktest Date Range: {start_dt.strftime('%Y-%m-%d %H:%M UTC')} to {end_dt.strftime('%Y-%m-%d %H:%M UTC')}")
    days_span = (end_dt - start_dt).total_seconds() / 86400
    print(f"Total Span: {days_span:.1f} Days (~{days_span/30.4:.1f} Months) | Universe: {len(symbol_data)} Pairs")

    # Build chronological timeline of 15m timestamps
    all_timestamps = sorted(list(symbol_data["BTCUSDT"].loc[start_dt:end_dt].index))

    # Simulation State
    INITIAL_CAPITAL = 2000.0
    capital = INITIAL_CAPITAL
    ALLOCATION = 200.0
    MAX_TRADES = 3
    FEE_PCT = 0.30  # 0.10% taker fee + 0.20% slippage/spread

    open_positions = {}
    closed_trades = []

    for idx, cur_dt in enumerate(all_timestamps):
        btc_bullish = is_btc_bullish_at(cur_dt)

        # -------------------------------------------------------------
        # STEP A: Manage Open Positions (Intra-bar check)
        # -------------------------------------------------------------
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            df_sym = symbol_data[sym]
            if cur_dt not in df_sym.index:
                continue
            bar = df_sym.loc[cur_dt]
            action = pos["action"]
            entry_p = pos["entry_price"]
            tp = pos["tp"]
            sl = pos["current_sl"]
            atr = pos["atr"]

            # High-water mark update
            if action == "BUY":
                if bar["high"] > pos["peak_price"]:
                    pos["peak_price"] = bar["high"]
                peak_pnl = ((pos["peak_price"] - entry_p) / entry_p) * 100.0
            else:
                if bar["low"] < pos["peak_price"]:
                    pos["peak_price"] = bar["low"]
                peak_pnl = ((entry_p - pos["peak_price"]) / entry_p) * 100.0

            # 1. Breakeven Lock at +1.5% peak gain -> Entry + 0.40% buffer
            if peak_pnl >= 1.5 and not pos["breakeven_locked"] and not pos["scaled_out"]:
                be_floor = entry_p * (1.0040 if action == "BUY" else 0.9960)
                pos["current_sl"] = max(pos["current_sl"], be_floor) if action == "BUY" else min(pos["current_sl"], be_floor)
                pos["breakeven_locked"] = True

            # 2. Scale-Out at +2.0% -> bank 50% to cash
            cur_pnl_high = ((bar["high"] - entry_p) / entry_p * 100.0) if action == "BUY" else ((entry_p - bar["low"]) / entry_p * 100.0)
            if cur_pnl_high >= 2.0 and not pos["scaled_out"]:
                partial_size = pos["notional_usd"] * 0.5
                fee_usd = partial_size * (FEE_PCT / 100.0)
                partial_pnl = (partial_size * (2.0 / 100.0)) - fee_usd
                capital += partial_pnl
                pos["notional_usd"] -= partial_size
                pos["scaled_out"] = True
                be_floor = entry_p * (1.0040 if action == "BUY" else 0.9960)
                pos["current_sl"] = max(pos["current_sl"], be_floor) if action == "BUY" else min(pos["current_sl"], be_floor)

            # 3. Dynamic Chandelier Stop on remaining 50% runner
            if pos["scaled_out"]:
                if action == "BUY":
                    ch_sl = pos["peak_price"] - (2.5 * atr)
                    be_floor = entry_p * 1.0040
                    if peak_pnl >= 5.0:
                        be_floor = max(be_floor, entry_p * 1.0250)
                    pos["current_sl"] = max(pos["current_sl"], max(be_floor, ch_sl))
                else:
                    ch_sl = pos["peak_price"] + (2.5 * atr)
                    be_floor = entry_p * 0.9960
                    if peak_pnl >= 5.0:
                        be_floor = min(be_floor, entry_p * 0.9750)
                    pos["current_sl"] = min(pos["current_sl"], min(be_floor, ch_sl))

            # 4. Check TP or SL Hit on this bar
            cur_sl = pos["current_sl"]
            hit_tp = (bar["high"] >= tp) if action == "BUY" else (bar["low"] <= tp)
            hit_sl = (bar["low"] <= cur_sl) if action == "BUY" else (bar["high"] >= cur_sl)

            if hit_tp or hit_sl:
                if hit_tp:
                    exit_p = tp
                    reason = "TAKE_PROFIT"
                else:
                    exit_p = cur_sl
                    if pos["scaled_out"]:
                        reason = "RUNNER_TRAIL"
                    elif pos["breakeven_locked"]:
                        reason = "BREAKEVEN"
                    else:
                        reason = "STOP_LOSS"

                pnl_pct = ((exit_p - entry_p) / entry_p * 100.0) if action == "BUY" else ((entry_p - exit_p) / entry_p * 100.0)
                fee_usd = pos["notional_usd"] * (FEE_PCT / 100.0)
                pnl_usd = (pos["notional_usd"] * (pnl_pct / 100.0)) - fee_usd
                capital += pnl_usd

                closed_trades.append({
                    "symbol": sym,
                    "action": action,
                    "entry_time": pos["entry_time"],
                    "exit_time": cur_dt,
                    "entry_price": entry_p,
                    "exit_price": exit_p,
                    "pnl_pct": round(pnl_pct, 2),
                    "pnl_usd": round(pnl_usd, 2),
                    "reason": reason,
                    "capital": round(capital, 2)
                })
                del open_positions[sym]

        # -------------------------------------------------------------
        # STEP B: Scan for New Setups if slots available
        # -------------------------------------------------------------
        if len(open_positions) < MAX_TRADES:
            for sym in SYMBOLS:
                if len(open_positions) >= MAX_TRADES:
                    break
                if sym in open_positions:
                    continue

                df_sym = symbol_data[sym]
                if cur_dt not in df_sym.index:
                    continue
                loc_idx = df_sym.index.get_loc(cur_dt)
                if loc_idx < 60:
                    continue

                sub = df_sym.iloc[loc_idx - 60 : loc_idx + 1]
                cur_p = sub.iloc[-1]["close"]
                atr_15m = sub.iloc[-1]["atr14"]
                vol_ma = sub["vol_ma20"]
                recent_bsl = sub["high"].iloc[-35:].max()
                recent_ssl = sub["low"].iloc[-35:].min()

                # Search Bullish OB
                found_setup = None
                for i in range(len(sub) - 30, len(sub) - 3):
                    c_ob = sub.iloc[i]
                    c1 = sub.iloc[i + 1]
                    c2 = sub.iloc[i + 2]

                    if c_ob["close"] < c_ob["open"] and c1["close"] > c1["open"]:
                        if c1["volume"] > vol_ma.iloc[i + 1] * 1.25 or c2["volume"] > vol_ma.iloc[i + 2] * 1.25:
                            if c2["low"] >= c_ob["high"] * 0.999 and c1["close"] > c_ob["high"]:
                                ob_top = c_ob["high"]
                                ob_bottom = c_ob["low"]

                                intermediate = sub.iloc[i + 3 : len(sub) - 1]
                                prior_taps = 0
                                prior_breaks = False
                                for _, b in intermediate.iterrows():
                                    if b["low"] <= ob_top and b["high"] >= ob_bottom:
                                        prior_taps += 1
                                    if b["close"] < ob_bottom:
                                        prior_breaks = True
                                        break
                                if prior_breaks or prior_taps >= 1:
                                    continue

                                dist_pct = ((cur_p - ob_top) / ob_top) * 100.0
                                if -0.5 <= dist_pct <= 2.0:
                                    structural_sl = ob_bottom * 0.996
                                    min_atr_sl = cur_p - (1.2 * atr_15m)
                                    final_sl = min(structural_sl, min_atr_sl)
                                    tp = max(recent_bsl, cur_p + (2.5 * atr_15m))

                                    risk = cur_p - final_sl
                                    reward = tp - cur_p
                                    rr = reward / (risk + 1e-9)
                                    risk_pct = (risk / cur_p) * 100.0
                                    reward_pct = (reward / cur_p) * 100.0

                                    if rr >= 2.0 and 1.0 <= risk_pct <= 4.5 and reward_pct >= 2.5:
                                        if btc_bullish:
                                            found_setup = {
                                                "symbol": sym,
                                                "action": "BUY",
                                                "entry_price": cur_p,
                                                "current_sl": final_sl,
                                                "tp": tp,
                                                "atr": atr_15m,
                                                "peak_price": cur_p,
                                                "notional_usd": ALLOCATION,
                                                "entry_time": cur_dt,
                                                "scaled_out": False,
                                                "breakeven_locked": False
                                            }
                                            break

                # Search Bearish OB if no bullish setup found
                if not found_setup:
                    for i in range(len(sub) - 30, len(sub) - 3):
                        c_ob = sub.iloc[i]
                        c1 = sub.iloc[i + 1]
                        c2 = sub.iloc[i + 2]

                        if c_ob["close"] > c_ob["open"] and c1["close"] < c1["open"]:
                            if c1["volume"] > vol_ma.iloc[i + 1] * 1.25 or c2["volume"] > vol_ma.iloc[i + 2] * 1.25:
                                if c2["high"] <= c_ob["low"] * 1.001 and c1["close"] < c_ob["low"]:
                                    ob_top = c_ob["high"]
                                    ob_bottom = c_ob["low"]

                                    intermediate = sub.iloc[i + 3 : len(sub) - 1]
                                    prior_taps = 0
                                    prior_breaks = False
                                    for _, b in intermediate.iterrows():
                                        if b["high"] >= ob_bottom and b["low"] <= ob_top:
                                            prior_taps += 1
                                        if b["close"] > ob_top:
                                            prior_breaks = True
                                            break
                                    if prior_breaks or prior_taps >= 1:
                                        continue

                                    dist_pct = ((ob_bottom - cur_p) / ob_bottom) * 100.0
                                    if -0.5 <= dist_pct <= 2.0:
                                        structural_sl = ob_top * 1.004
                                        min_atr_sl = cur_p + (1.2 * atr_15m)
                                        final_sl = max(structural_sl, min_atr_sl)
                                        tp = min(recent_ssl, cur_p - (2.5 * atr_15m))

                                        risk = final_sl - cur_p
                                        reward = cur_p - tp
                                        rr = reward / (risk + 1e-9)
                                        risk_pct = (risk / cur_p) * 100.0
                                        reward_pct = (reward / cur_p) * 100.0

                                        if rr >= 2.0 and 1.0 <= risk_pct <= 4.5 and reward_pct >= 2.5:
                                            if not btc_bullish:
                                                found_setup = {
                                                    "symbol": sym,
                                                    "action": "SELL",
                                                    "entry_price": cur_p,
                                                    "current_sl": final_sl,
                                                    "tp": tp,
                                                    "atr": atr_15m,
                                                    "peak_price": cur_p,
                                                    "notional_usd": ALLOCATION,
                                                    "entry_time": cur_dt,
                                                    "scaled_out": False,
                                                    "breakeven_locked": False
                                                }
                                                break

                if found_setup:
                    open_positions[sym] = found_setup

    # -------------------------------------------------------------
    # Performance Reporting
    # -------------------------------------------------------------
    tdf = pd.DataFrame(closed_trades)
    print("\n" + "="*80)
    print("                    CRYPTO SMC BACKTEST RESULTS SUMMARY                       ")
    print("="*80)
    if tdf.empty:
        print("No closed trades generated.")
        return

    wins = len(tdf[tdf["pnl_usd"] > 0])
    losses = len(tdf[tdf["pnl_usd"] < 0])
    scratches = len(tdf[tdf["pnl_usd"] == 0])
    tot_pnl = tdf["pnl_usd"].sum()
    ret_pct = ((capital - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100.0

    gross_profit = tdf[tdf["pnl_usd"] > 0]["pnl_usd"].sum()
    gross_loss = abs(tdf[tdf["pnl_usd"] < 0]["pnl_usd"].sum())
    profit_factor = round(gross_profit / (gross_loss + 1e-9), 2)

    cum = tdf["pnl_usd"].cumsum()
    peak = cum.cummax()
    dd = cum - peak
    max_dd = dd.min()

    reason_counts = tdf["reason"].value_counts().to_dict()

    print(f"Total Closed Trades: {len(tdf)}")
    print(f"Wins: {wins} | Losses: {losses} | Scratches: {scratches}")
    print(f"Win Rate: {wins/len(tdf)*100:.1f}%")
    print(f"Gross Profit: +${gross_profit:.2f} | Gross Loss: -${gross_loss:.2f}")
    print(f"Profit Factor: {profit_factor}")
    print(f"Net Banked P&L: {tot_pnl:+.2f} USD ({ret_pct:+.2f}%)")
    print(f"Final Balance: ${capital:,.2f} USD (Initial: ${INITIAL_CAPITAL:,.2f})")
    print(f"Max Drawdown: ${max_dd:.2f} USD ({max_dd/INITIAL_CAPITAL*100:.2f}%)")
    print(f"Exit Breakdown: {reason_counts}")
    print("="*80)

    # Save trades to csv for auditing
    csv_out = os.path.join(DATA_DIR, "crypto_backtest_closed_trades.csv")
    tdf.to_csv(csv_out, index=False)
    print(f"Detailed closed trades saved to {csv_out}")

if __name__ == "__main__":
    run_crypto_smc_backtest()
