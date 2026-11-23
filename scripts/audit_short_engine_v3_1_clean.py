"""
audit_short_engine_v3_1_clean.py
STAGE 1: CLEAN BASELINE CORRECTION FOR SHORT ENGINE v3.1.

Audit Requirements Implemented:
1. Strict 20 consecutive, unique, completed 1-minute OR bars.
2. Minimum >= 3 valid constituents per sector.
3. Weakest sector ranking (breadth % red, avg move).
4. Point-in-time ATR(14) using strictly daily bars BEFORE the session date. No defaults/fallbacks.
5. Strict OR move between -0.70% and -1.80%. NO FALLBACK. If none qualifies -> NO_QUALIFIED_CANDIDATE.
6. Execution: ORL * 0.9997, gap-through entry fill, adverse entry & exit slippage (5 bps),
   SL gap-through exit handling, conservative entry-bar ambiguity handling.
7. Outputs short_engine_v3_1_clean.csv.
"""
import pickle
import pandas as pd
import numpy as np
from datetime import datetime

with open("nse_1m_full.pkl", "rb") as f:
    cache = pickle.load(f)

with open("nse_daily_atr.pkl", "rb") as f:
    daily_atrs = pickle.load(f)

NSE_SECTORS = {
    "Banking": ["HDFCBANK.NS", "ICICIBANK.NS", "KOTAKBANK.NS", "AXISBANK.NS", "SBIN.NS", "INDUSINDBK.NS"],
    "IT_Tech": ["TCS.NS", "INFY.NS", "HCLTECH.NS", "WIPRO.NS", "TECHM.NS"],
    "Oil_Gas": ["RELIANCE.NS", "ONGC.NS", "BPCL.NS", "IOC.NS", "HINDPETRO.NS"],
    "Automobile": ["MARUTI.NS", "M&M.NS", "BAJAJ-AUTO.NS", "EICHERMOT.NS", "HEROMOTOCO.NS"],
    "Pharma": ["SUNPHARMA.NS", "CIPLA.NS", "DRREDDY.NS", "DIVISLAB.NS", "APOLLOHOSP.NS"],
    "FMCG": ["HINDUNILVR.NS", "ITC.NS", "NESTLEIND.NS", "BRITANNIA.NS", "TATACONSUM.NS"],
    "MetalsCement": ["TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS", "GRASIM.NS", "ULTRACEMCO.NS"],
    "Consumer_Retail": ["TITAN.NS", "ASIANPAINT.NS", "TRENT.NS"],
    "Energy_Utilities": ["NTPC.NS", "POWERGRID.NS", "COALINDIA.NS"],
    "Infra_CapGoods": ["LT.NS", "ADANIENT.NS", "ADANIPORTS.NS", "BHARTIARTL.NS"]
}

nifty = cache["^NSEI"]
dates = sorted(list(set(nifty.index.strftime('%Y-%m-%d'))))

trades = []
SLIPPAGE_BPS = 0.0005  # 5 bps adverse slippage

def get_point_in_time_atr(sym, current_date):
    """Calculates 14-day ATR strictly using completed daily bars BEFORE current_date."""
    raw = daily_atrs.get(sym)
    if raw is None or not isinstance(raw, pd.DataFrame):
        return None
    # Filter strictly prior to session date
    prior_daily = raw[raw.index.strftime('%Y-%m-%d') < current_date]
    if len(prior_daily) < 14:
        return None
    h = prior_daily["High"]
    l = prior_daily["Low"]
    c = prior_daily["Close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean().iloc[-1]
    last_c = c.iloc[-1]
    if last_c <= 0:
        return None
    return float((atr14 / last_c) * 100)

for d in dates:
    n_day = nifty[nifty.index.strftime('%Y-%m-%d') == d]
    n_or = n_day.between_time("09:15", "09:34")
    if len(n_or) != 20 or len(set(n_or.index)) != 20:
        continue
    n_open = n_or["Open"].iloc[0]
    n_close = n_or["Close"].iloc[-1]
    n_move = ((n_close - n_open) / n_open) * 100

    sec_stats = []

    for sec, syms in NSE_SECTORS.items():
        moves = []
        valid_stocks = []
        for s in syms:
            df = cache.get(s)
            if df is None:
                continue
            day_s = df[df.index.strftime('%Y-%m-%d') == d]
            s_or = day_s.between_time("09:15", "09:34")
            if len(s_or) == 20 and len(set(s_or.index)) == 20:
                o = float(s_or["Open"].iloc[0])
                c = float(s_or["Close"].iloc[-1])
                h = float(s_or["High"].max())
                l = float(s_or["Low"].min())
                m = ((c - o) / o) * 100

                # Point-in-time ATR(14)
                atr_pct = get_point_in_time_atr(s, d)

                moves.append(m)
                info = {
                    "sym": s, "move": m, "orh": h, "orl": l, "atr": atr_pct,
                    "day": day_s, "or_bars": s_or
                }
                valid_stocks.append(info)

        if len(valid_stocks) >= 3:
            breadth = (sum(1 for x in moves if x < 0) / len(moves)) * 100
            avg_m = np.mean(moves)
            sec_stats.append({"sec": sec, "breadth": breadth, "avg_m": avg_m, "stocks": valid_stocks})

    if not sec_stats:
        continue

    # Rank weakest sector
    sec_stats.sort(key=lambda x: (x["breadth"], -x["avg_m"]), reverse=True)
    weakest_sec = sec_stats[0]

    # STRICT CANDIDATE SELECTION:
    # 1. Point-in-time ATR >= 2.50% (Non-null)
    # 2. OR Move strictly between -0.70% and -1.80%
    # NO FALLBACK ALLOWED!
    qual_cands = [
        s for s in weakest_sec["stocks"]
        if s["atr"] is not None and s["atr"] >= 2.50 and (-1.80 <= s["move"] <= -0.70)
    ]

    if not qual_cands:
        trades.append({
            "Date": d, "Sector": weakest_sec["sec"], "Symbol": "NO_QUALIFIED_STOCK",
            "Benchmark_OR%": round(n_move, 2), "Stock_OR%": 0.0, "ATR14%": 0.0,
            "ORH": 0.0, "ORL": 0.0, "Trigger": 0.0, "Actual_Entry": 0.0, "SL": 0.0, "TP": 0.0,
            "Actual_Exit": 0.0, "Exit_Reason": "NO_CANDIDATE", "Entry_Slippage": 0.0, "Exit_Slippage": 0.0,
            "PnL": 0.00, "Status": "UNTRIGGERED",
            "Validation_Notes": "Filtered: No stock met ATR>=2.5% and OR Move [-0.70%, -1.80%]"
        })
        continue

    # Weakest qualifying stock
    qual_cands.sort(key=lambda x: x["move"])
    selected = qual_cands[0]

    sym = selected["sym"]
    orh = selected["orh"]
    orl = selected["orl"]
    stock_or_pct = selected["move"]
    atr_val = selected["atr"]

    trig = round(orl * 0.9997, 2)
    sl = round(trig * 1.0100, 2)     # +1.00% Hard SL
    tp = round(trig * 0.9800, 2)     # -2.00% TP (2:1 R:R)

    post = selected["day"].between_time("09:45", "15:29")
    trig_bars = post[post["Low"] <= trig]

    if trig_bars.empty:
        trades.append({
            "Date": d, "Sector": weakest_sec["sec"], "Symbol": sym.replace(".NS", ""),
            "Benchmark_OR%": round(n_move, 2), "Stock_OR%": round(stock_or_pct, 2),
            "ATR14%": round(atr_val, 2), "ORH": orh, "ORL": orl, "Trigger": trig,
            "Actual_Entry": 0.0, "SL": sl, "TP": tp, "Actual_Exit": 0.0,
            "Exit_Reason": "NEVER_TRIGGERED", "Entry_Slippage": 0.0, "Exit_Slippage": 0.0,
            "PnL": 0.00, "Status": "UNTRIGGERED", "Validation_Notes": "Price stayed above ORL trigger"
        })
    else:
        entry_idx = trig_bars.index[0]
        entry_bar = post.loc[entry_idx]

        # Gap-through entry execution
        entry_open = float(entry_bar["Open"])
        base_fill = entry_open if entry_open < trig else trig
        # Adverse short entry slippage (fills lower)
        actual_fill = round(base_fill * (1.0 - SLIPPAGE_BPS), 2)
        entry_slip = round(base_fill - actual_fill, 2)

        # Conservative entry-candle ambiguity check
        # If entry candle touches SL or TP within the same minute:
        candle_high = float(entry_bar["High"])
        candle_low = float(entry_bar["Low"])

        if candle_high >= sl:
            # Conservative worst-case: stopped out on entry candle
            exit_price = round(sl * (1.0 + SLIPPAGE_BPS), 2)
            exit_slip = round(exit_price - sl, 2)
            pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
            trades.append({
                "Date": d, "Sector": weakest_sec["sec"], "Symbol": sym.replace(".NS", ""),
                "Benchmark_OR%": round(n_move, 2), "Stock_OR%": round(stock_or_pct, 2),
                "ATR14%": round(atr_val, 2), "ORH": orh, "ORL": orl, "Trigger": trig,
                "Actual_Entry": actual_fill, "SL": sl, "TP": tp, "Actual_Exit": exit_price,
                "Exit_Reason": "SL_HIT_SAME_BAR", "Entry_Slippage": entry_slip, "Exit_Slippage": exit_slip,
                "PnL": pnl, "Status": "SL HIT", "Validation_Notes": "Conservative: Ambiguous entry candle hit SL"
            })
            continue

        # Post-entry bars evaluation
        post_entry = post.loc[entry_idx:].iloc[1:]

        if post_entry.empty:
            exit_price = round(float(entry_bar["Close"]) * (1.0 + SLIPPAGE_BPS), 2)
            exit_slip = round(exit_price - float(entry_bar["Close"]), 2)
            pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
            trades.append({
                "Date": d, "Sector": weakest_sec["sec"], "Symbol": sym.replace(".NS", ""),
                "Benchmark_OR%": round(n_move, 2), "Stock_OR%": round(stock_or_pct, 2),
                "ATR14%": round(atr_val, 2), "ORH": orh, "ORL": orl, "Trigger": trig,
                "Actual_Entry": actual_fill, "SL": sl, "TP": tp, "Actual_Exit": exit_price,
                "Exit_Reason": "EOD_EXIT_1529", "Entry_Slippage": entry_slip, "Exit_Slippage": exit_slip,
                "PnL": pnl, "Status": "EOD EXIT", "Validation_Notes": "Triggered on last bar of session"
            })
            continue

        sl_bars = post_entry[post_entry["High"] >= sl]
        tp_bars = post_entry[post_entry["Low"] <= tp]

        if not sl_bars.empty and not tp_bars.empty:
            if sl_bars.index[0] <= tp_bars.index[0]:
                hit_bar = post_entry.loc[sl_bars.index[0]]
                # SL gap-through check
                base_exit = float(hit_bar["Open"]) if float(hit_bar["Open"]) > sl else sl
                exit_price = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
                exit_slip = round(exit_price - base_exit, 2)
                pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
                status = "SL HIT"
                reason = "SL_HIT"
            else:
                base_exit = tp
                exit_price = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
                exit_slip = round(exit_price - base_exit, 2)
                pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
                status = "TP HIT"
                reason = "TP_HIT"
        elif not sl_bars.empty:
            hit_bar = post_entry.loc[sl_bars.index[0]]
            base_exit = float(hit_bar["Open"]) if float(hit_bar["Open"]) > sl else sl
            exit_price = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
            exit_slip = round(exit_price - base_exit, 2)
            pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
            status = "SL HIT"
            reason = "SL_HIT"
        elif not tp_bars.empty:
            base_exit = tp
            exit_price = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
            exit_slip = round(exit_price - base_exit, 2)
            pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
            status = "TP HIT"
            reason = "TP_HIT"
        else:
            last_c = float(post_entry["Close"].iloc[-1])
            exit_price = round(last_c * (1.0 + SLIPPAGE_BPS), 2)
            exit_slip = round(exit_price - last_c, 2)
            pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
            status = "EOD EXIT"
            reason = "EOD_EXIT_1529"

        trades.append({
            "Date": d, "Sector": weakest_sec["sec"], "Symbol": sym.replace(".NS", ""),
            "Benchmark_OR%": round(n_move, 2), "Stock_OR%": round(stock_or_pct, 2),
            "ATR14%": round(atr_val, 2), "ORH": orh, "ORL": orl, "Trigger": trig,
            "Actual_Entry": actual_fill, "SL": sl, "TP": tp, "Actual_Exit": exit_price,
            "Exit_Reason": reason, "Entry_Slippage": entry_slip, "Exit_Slippage": exit_slip,
            "PnL": pnl, "Status": status, "Validation_Notes": "Clean post-entry execution"
        })

df_clean = pd.DataFrame(trades)
df_clean.to_csv("short_engine_v3_1_clean.csv", index=False)

print("=" * 115)
print("  SHORT ENGINE v3.1 CLEAN BASELINE (POINT-IN-TIME ATR, STRICT SWEET SPOT, ZERO FALLBACK)")
print("=" * 115)
print(df_clean[["Date", "Benchmark_OR%", "Sector", "Symbol", "Stock_OR%", "ATR14%", "Actual_Entry", "Actual_Exit", "PnL", "Status", "Exit_Reason"]].to_string(index=False))

# Detailed metrics
exec_trades = df_clean[df_clean["Status"] != "UNTRIGGERED"]
no_candidates = len(df_clean[df_clean["Symbol"] == "NO_QUALIFIED_STOCK"])
untrig_stocks = len(df_clean[(df_clean["Status"] == "UNTRIGGERED") & (df_clean["Symbol"] != "NO_QUALIFIED_STOCK")])

wins = exec_trades[exec_trades["PnL"] > 0]
losses = exec_trades[exec_trades["PnL"] < 0]

win_count = len(wins)
loss_count = len(losses)
total_pnl = exec_trades["PnL"].sum()
wr = (win_count / len(exec_trades)) * 100 if len(exec_trades) > 0 else 0

gross_profit = wins["PnL"].sum()
gross_loss = abs(losses["PnL"].sum())
profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else np.nan

# Average R per trade (Risk = 1.05% with slippage)
avg_r = (exec_trades["PnL"] / 1.05).mean() if len(exec_trades) > 0 else 0.0

# Max Drawdown
cum_pnl = exec_trades["PnL"].cumsum()
running_max = cum_pnl.cummax()
drawdown = cum_pnl - running_max
max_dd = drawdown.min() if not drawdown.empty else 0.0

print("\n" + "=" * 115)
print("  SHORT ENGINE v3.1 CLEAN METRICS REPORT")
print("=" * 115)
print(f"Total Sessions Audited:        {len(df_clean)}")
print(f"No-Candidate Filtered Days:    {no_candidates}")
print(f"Qualified Untriggered Setups:  {untrig_stocks}")
print(f"Executed Trades:               {len(exec_trades)}")
print(f"Wins / Losses:                 {win_count}W / {loss_count}L")
print(f"Realized Win Rate:             {wr:.1f}%")
print(f"Profit Factor:                 {profit_factor}")
print(f"Average Expectancy per Trade:  {avg_r:+.2f}R ({exec_trades['PnL'].mean():+.2f}%)")
print(f"Maximum Drawdown:              {max_dd:.2f}%")
print(f"Total Slippage-Adjusted P&L:   {total_pnl:+.2f}%")
print("=" * 115)
