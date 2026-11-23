"""
audit_short_engine_v3_2_exits.py
STAGE 2: SHORT ENGINE EXIT OPTIMIZATION & DATA-INTEGRITY AUDIT

Implements:
1. Exact 20 consecutive 1-minute timestamps verification (09:15 to 09:34).
2. Explicit session logging: missing benchmarks/sectors logged as DATA_INVALID.
3. Point-in-time ATR(14) with >= 14 daily bars prior to current session date.
4. Correct peak-to-trough Drawdown formula including initial 0.0 equity peak.
5. Exit Optimization testing across 5 Fixed TP models + Trailing Exit:
   - Model A: 1.00% SL / 0.50% TP
   - Model B: 1.00% SL / 0.75% TP
   - Model C: 1.00% SL / 1.00% TP
   - Model D: 1.00% SL / 1.50% TP
   - Model E: 1.00% SL / 2.00% TP (Baseline)
   - Model F: Trailing Stop (Breakeven @ +0.50% MFE, trail 50 bps from peak low)
6. Computes MFE (Maximum Favorable Excursion) & MAE (Maximum Adverse Excursion).
7. Outputs detailed CSV: data/short_engine_v3_2_exits.csv.
"""

import pickle
import os
import pandas as pd
import numpy as np

# Load cache
data_dir = r"C:\Dev\Dev\Trading"
cache_path = os.path.join(data_dir, "nse_1m_full.pkl")
atr_path = os.path.join(data_dir, "nse_daily_atr.pkl")

with open(cache_path, "rb") as f:
    cache = pickle.load(f)

with open(atr_path, "rb") as f:
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

EXPECTED_OR_TIMES = [f"09:{m:02d}" for m in range(15, 35)]
SLIPPAGE_BPS = 0.0005  # 5 bps adverse slippage

def get_point_in_time_atr(sym, current_date):
    raw = daily_atrs.get(sym)
    if raw is None or not isinstance(raw, pd.DataFrame):
        return None
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

def simulate_trade_exit(post, trig, entry_idx, actual_fill, sl_pct, tp_pct, model_type="FIXED"):
    """
    Simulates short exit with conservative same-bar handling, adverse slippage, and gap-through.
    """
    entry_bar = post.loc[entry_idx]
    sl = round(trig * (1.0 + sl_pct), 2)
    tp = round(trig * (1.0 - tp_pct), 2) if tp_pct is not None else None

    # Check same-bar ambiguity on entry candle
    candle_high = float(entry_bar["High"])
    if candle_high >= sl:
        exit_price = round(sl * (1.0 + SLIPPAGE_BPS), 2)
        pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
        return {"exit_price": exit_price, "reason": "SL_HIT_SAME_BAR", "pnl": pnl, "status": "SL HIT", "bars_held": 1}

    post_entry = post.loc[entry_idx:].iloc[1:]
    if post_entry.empty:
        exit_price = round(float(entry_bar["Close"]) * (1.0 + SLIPPAGE_BPS), 2)
        pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
        return {"exit_price": exit_price, "reason": "EOD_EXIT_1529", "pnl": pnl, "status": "EOD EXIT", "bars_held": 1}

    if model_type == "TRAILING":
        # Trailing Exit:
        # If MFE reaches +0.50%, stop moved to breakeven (actual_fill).
        # If MFE reaches +0.75%, stop trailed to lowest_low * 1.0050.
        current_sl = sl
        lowest_low = float(entry_bar["Low"])
        be_active = False
        trail_active = False

        for i, (ts, bar) in enumerate(post_entry.iterrows(), start=2):
            b_high = float(bar["High"])
            b_low = float(bar["Low"])
            b_open = float(bar["Open"])

            if b_low < lowest_low:
                lowest_low = b_low

            current_mfe_pct = ((actual_fill - lowest_low) / actual_fill) * 100

            if current_mfe_pct >= 0.75:
                trail_active = True
                new_sl = round(lowest_low * 1.0050, 2)
                if new_sl < current_sl:
                    current_sl = new_sl
            elif current_mfe_pct >= 0.50:
                be_active = True
                if actual_fill < current_sl:
                    current_sl = actual_fill

            # Check stop hit
            if b_high >= current_sl:
                base_exit = b_open if b_open > current_sl else current_sl
                exit_price = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
                pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
                status_str = "TRAIL_STOP" if trail_active else ("BE_STOP" if be_active else "SL HIT")
                return {"exit_price": exit_price, "reason": status_str, "pnl": pnl, "status": status_str, "bars_held": i}

        # EOD Exit
        last_c = float(post_entry["Close"].iloc[-1])
        exit_price = round(last_c * (1.0 + SLIPPAGE_BPS), 2)
        pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
        return {"exit_price": exit_price, "reason": "EOD_EXIT_1529", "pnl": pnl, "status": "EOD EXIT", "bars_held": len(post_entry)+1}

    # Standard Fixed TP / SL Model
    sl_bars = post_entry[post_entry["High"] >= sl]
    tp_bars = post_entry[post_entry["Low"] <= tp]

    if not sl_bars.empty and not tp_bars.empty:
        if sl_bars.index[0] <= tp_bars.index[0]:
            hit_ts = sl_bars.index[0]
            hit_bar = post_entry.loc[hit_ts]
            base_exit = float(hit_bar["Open"]) if float(hit_bar["Open"]) > sl else sl
            exit_price = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
            pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
            bars_held = post_entry.index.get_loc(hit_ts) + 2
            return {"exit_price": exit_price, "reason": "SL_HIT", "pnl": pnl, "status": "SL HIT", "bars_held": bars_held}
        else:
            hit_ts = tp_bars.index[0]
            base_exit = tp
            exit_price = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
            pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
            bars_held = post_entry.index.get_loc(hit_ts) + 2
            return {"exit_price": exit_price, "reason": "TP_HIT", "pnl": pnl, "status": "TP HIT", "bars_held": bars_held}
    elif not sl_bars.empty:
        hit_ts = sl_bars.index[0]
        hit_bar = post_entry.loc[hit_ts]
        base_exit = float(hit_bar["Open"]) if float(hit_bar["Open"]) > sl else sl
        exit_price = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
        pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
        bars_held = post_entry.index.get_loc(hit_ts) + 2
        return {"exit_price": exit_price, "reason": "SL_HIT", "pnl": pnl, "status": "SL HIT", "bars_held": bars_held}
    elif not tp_bars.empty:
        hit_ts = tp_bars.index[0]
        base_exit = tp
        exit_price = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
        pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
        bars_held = post_entry.index.get_loc(hit_ts) + 2
        return {"exit_price": exit_price, "reason": "TP_HIT", "pnl": pnl, "status": "TP HIT", "bars_held": bars_held}
    else:
        last_c = float(post_entry["Close"].iloc[-1])
        exit_price = round(last_c * (1.0 + SLIPPAGE_BPS), 2)
        pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
        return {"exit_price": exit_price, "reason": "EOD_EXIT_1529", "pnl": pnl, "status": "EOD EXIT", "bars_held": len(post_entry)+1}

# Execute audit
audit_records = []
all_trades = []

for d in dates:
    n_day = nifty[nifty.index.strftime('%Y-%m-%d') == d]
    n_or = n_day.between_time("09:15", "09:34")
    
    # Integrity Check: exactly 20 consecutive timestamps
    if list(n_or.index.strftime('%H:%M')) != EXPECTED_OR_TIMES:
        audit_records.append({
            "Date": d, "Status": "DATA_INVALID", "Reason": f"Benchmark missing OR bars ({len(n_or)} bars present)"
        })
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
            
            # Consecutive 20-min timestamp check
            if list(s_or.index.strftime('%H:%M')) == EXPECTED_OR_TIMES:
                o = float(s_or["Open"].iloc[0])
                c = float(s_or["Close"].iloc[-1])
                h = float(s_or["High"].max())
                l = float(s_or["Low"].min())
                m = ((c - o) / o) * 100
                atr_pct = get_point_in_time_atr(s, d)

                moves.append(m)
                valid_stocks.append({
                    "sym": s, "move": m, "orh": h, "orl": l, "atr": atr_pct,
                    "day": day_s, "or_bars": s_or
                })

        if len(valid_stocks) >= 3:
            breadth = (sum(1 for x in moves if x < 0) / len(moves)) * 100
            avg_m = np.mean(moves)
            sec_stats.append({"sec": sec, "breadth": breadth, "avg_m": avg_m, "stocks": valid_stocks})

    if not sec_stats:
        audit_records.append({
            "Date": d, "Status": "NO_QUALIFIED_SECTOR", "Reason": "No sector had >= 3 valid stocks"
        })
        continue

    sec_stats.sort(key=lambda x: (x["breadth"], -x["avg_m"]), reverse=True)
    weakest_sec = sec_stats[0]

    # Sweet-spot filter
    qual_cands = [
        s for s in weakest_sec["stocks"]
        if s["atr"] is not None and s["atr"] >= 2.50 and (-1.80 <= s["move"] <= -0.70)
    ]

    if not qual_cands:
        audit_records.append({
            "Date": d, "Sector": weakest_sec["sec"], "Symbol": "NO_QUALIFIED_STOCK",
            "Benchmark_OR%": round(n_move, 2), "Status": "NO_CANDIDATE",
            "Reason": "Filtered: No stock met ATR>=2.5% & Move [-0.70%, -1.80%]"
        })
        continue

    qual_cands.sort(key=lambda x: x["move"])
    selected = qual_cands[0]
    sym = selected["sym"]
    orh = selected["orh"]
    orl = selected["orl"]
    stock_or_pct = selected["move"]
    atr_val = selected["atr"]

    trig = round(orl * 0.9997, 2)
    post = selected["day"].between_time("09:45", "15:29")
    trig_bars = post[post["Low"] <= trig]

    if trig_bars.empty:
        audit_records.append({
            "Date": d, "Sector": weakest_sec["sec"], "Symbol": sym.replace(".NS", ""),
            "Benchmark_OR%": round(n_move, 2), "Stock_OR%": round(stock_or_pct, 2),
            "ATR14%": round(atr_val, 2), "ORH": orh, "ORL": orl, "Trigger": trig,
            "Status": "UNTRIGGERED", "Reason": "Price stayed above ORL trigger"
        })
        continue

    entry_idx = trig_bars.index[0]
    entry_bar = post.loc[entry_idx]
    entry_open = float(entry_bar["Open"])
    base_fill = entry_open if entry_open < trig else trig
    actual_fill = round(base_fill * (1.0 - SLIPPAGE_BPS), 2)
    entry_slip = round(base_fill - actual_fill, 2)

    # Calculate MFE & MAE across all post-entry bars
    post_all = post.loc[entry_idx:]
    max_price = float(post_all["High"].max())
    min_price = float(post_all["Low"].min())

    # For short trade:
    # Adverse movement = price rising above entry
    mae_pct = round(((max_price - actual_fill) / actual_fill) * 100, 2)
    # Favorable movement = price falling below entry
    mfe_pct = round(((actual_fill - min_price) / actual_fill) * 100, 2)

    # Simulate Exits:
    res_a = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, 0.0050, "FIXED")
    res_b = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, 0.0075, "FIXED")
    res_c = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, 0.0100, "FIXED")
    res_d = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, 0.0150, "FIXED")
    res_e = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, 0.0200, "FIXED")  # Baseline
    res_f = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, None, "TRAILING")

    record = {
        "Date": d, "Sector": weakest_sec["sec"], "Symbol": sym.replace(".NS", ""),
        "Benchmark_OR%": round(n_move, 2), "Stock_OR%": round(stock_or_pct, 2),
        "ATR14%": round(atr_val, 2), "ORH": orh, "ORL": orl, "Trigger": trig,
        "Actual_Entry": actual_fill, "Entry_Time": entry_idx.strftime('%H:%M'),
        "MAE%": mae_pct, "MFE%": mfe_pct,
        # Model A: 1% SL / 0.50% TP
        "A_PnL": res_a["pnl"], "A_Status": res_a["status"],
        # Model B: 1% SL / 0.75% TP
        "B_PnL": res_b["pnl"], "B_Status": res_b["status"],
        # Model C: 1% SL / 1.00% TP
        "C_PnL": res_c["pnl"], "C_Status": res_c["status"],
        # Model D: 1% SL / 1.50% TP
        "D_PnL": res_d["pnl"], "D_Status": res_d["status"],
        # Model E: 1% SL / 2.00% TP (Baseline)
        "E_PnL": res_e["pnl"], "E_Status": res_e["status"],
        # Model F: Trailing Stop
        "F_PnL": res_f["pnl"], "F_Status": res_f["status"],
        "Status": "EXECUTED"
    }
    all_trades.append(record)
    audit_records.append(record)

# Save to CSV
df_exits = pd.DataFrame(audit_records)
output_path = os.path.join(data_dir, "short_engine_v3_2_exits.csv")
df_exits.to_csv(output_path, index=False)

# Also save into crypto-orderblock-bot data folder
git_data_path = r"C:\Users\Mind\.gemini\antigravity\scratch\crypto-orderblock-bot\data\short_engine_v3_2_exits.csv"
os.makedirs(os.path.dirname(git_data_path), exist_ok=True)
df_exits.to_csv(git_data_path, index=False)

# Analyze Performance across Models
df_exec = pd.DataFrame(all_trades)

print("=" * 125)
print("  SHORT ENGINE v3.2: EXECUTED TRADES WITH MFE, MAE & MULTI-MODEL EXIT EVALUATION")
print("=" * 125)
cols_show = ["Date", "Symbol", "Benchmark_OR%", "Stock_OR%", "Actual_Entry", "MAE%", "MFE%", "A_PnL", "B_PnL", "C_PnL", "D_PnL", "E_PnL", "F_PnL"]
print(df_exec[cols_show].to_string(index=False))

def compute_model_metrics(name, pnl_col, status_col):
    pnl = df_exec[pnl_col]
    wins = df_exec[pnl > 0]
    losses = df_exec[pnl < 0]
    w_cnt = len(wins)
    l_cnt = len(losses)
    wr = (w_cnt / len(df_exec)) * 100 if len(df_exec) > 0 else 0
    total_pnl = pnl.sum()
    gp = wins[pnl_col].sum()
    gl = abs(losses[pnl_col].sum())
    pf = round(gp / gl, 2) if gl > 0 else (np.inf if gp > 0 else 0.0)
    avg_r = (pnl / 1.05).mean()
    
    # Correct Peak-to-Trough Drawdown from 0.0 initial equity peak
    cum_pnl = pnl.cumsum()
    equity_curve = pd.Series([0.0] + list(cum_pnl))
    running_max = equity_curve.cummax()
    drawdown = equity_curve - running_max
    max_dd = drawdown.min()
    
    tp_hits = len(df_exec[df_exec[status_col] == "TP HIT"])
    sl_hits = len(df_exec[df_exec[status_col].str.contains("SL", na=False)])
    eod_exits = len(df_exec[df_exec[status_col].str.contains("EOD|TRAIL|BE", na=False)])

    return {
        "Model": name, "TP_Hits": tp_hits, "SL_Hits": sl_hits, "EOD/Trail": eod_exits,
        "W/L": f"{w_cnt}W / {l_cnt}L", "Win_Rate%": round(wr, 1),
        "Profit_Factor": pf, "Avg_R": round(avg_r, 2), "Net_PnL%": round(total_pnl, 2),
        "Max_Drawdown%": round(max_dd, 2)
    }

models = [
    ("Model A (0.50% TP)", "A_PnL", "A_Status"),
    ("Model B (0.75% TP)", "B_PnL", "B_Status"),
    ("Model C (1.00% TP)", "C_PnL", "C_Status"),
    ("Model D (1.50% TP)", "D_PnL", "D_Status"),
    ("Model E (2.00% TP - Baseline)", "E_PnL", "E_Status"),
    ("Model F (MFE Trailing)", "F_PnL", "F_Status"),
]

summary_rows = [compute_model_metrics(m[0], m[1], m[2]) for m in models]
df_summary = pd.DataFrame(summary_rows)

print("\n" + "=" * 125)
print("  SHORT ENGINE EXIT EXPERIMENTS SUMMARY (N = 9 EXECUTED TRADES)")
print("=" * 125)
print(df_summary.to_string(index=False))
print("=" * 125)
