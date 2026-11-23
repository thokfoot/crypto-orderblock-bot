"""
audit_short_engine_v3_2_1_integrity.py
STAGE 2.1: SHORT ENGINE EXIT SIMULATION INTEGRITY FIX & AUDIT

Implements all 4 Peer 2 Audit Corrections:
1. Trade-Specific Holding-Period MFE & MAE:
   - Excursions strictly calculated during the actual trade holding period (from entry to exit bar).
   - Full-session post-entry MFE is recorded separately as potential post-exit path.
2. Trailing-Stop Intra-Candle Lookahead Elimination (Model F):
   - Current candle evaluated against existing active stop carried over from candle t-1.
   - Stop update occurs ONLY after candle t completes without stopping out.
   - Tightened stop activates strictly starting on candle t+1.
3. Trade-Specific Risk (R) Denominator:
   - Exact initial risk calculated per trade:
     Initial Risk Pct = ((initial_sl_with_slippage - actual_fill) / actual_fill) * 100
     Realized R = Trade_PnL / Initial_Risk_Pct
4. Compounded Portfolio Equity Curve & True Portfolio Drawdown:
   - Evaluates a standardized portfolio (Initial Capital = 100,000, Risk = 1.0% of equity per trade).
   - Exact position sizing: Capital * 0.01 / (Initial_Risk_Pct / 100).
   - Compounded account equity curve, peak-to-trough portfolio drawdown.
"""

import pickle
import os
import pandas as pd
import numpy as np

# Paths
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
    Simulates short exit with strictly causal candle evaluation:
    - Intra-candle lookahead eliminated for trailing stops.
    - Excursion (MFE/MAE) calculated strictly over trade holding bars.
    """
    entry_bar = post.loc[entry_idx]
    sl = round(trig * (1.0 + sl_pct), 2)
    tp = round(trig * (1.0 - tp_pct), 2) if tp_pct is not None else None

    # Initial SL execution with adverse slippage
    initial_sl_exit = round(sl * (1.0 + SLIPPAGE_BPS), 2)
    initial_risk_pct = round(((initial_sl_exit - actual_fill) / actual_fill) * 100, 3)

    # Entry-candle ambiguity check:
    # If entry candle touches SL within the same minute:
    candle_high = float(entry_bar["High"])
    candle_low = float(entry_bar["Low"])

    if candle_high >= sl:
        exit_price = round(sl * (1.0 + SLIPPAGE_BPS), 2)
        pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
        realized_r = round(pnl / initial_risk_pct, 2)
        # On entry bar, adverse excursion is at least to SL
        holding_mae = round(((candle_high - actual_fill) / actual_fill) * 100, 2)
        holding_mfe = round(((actual_fill - candle_low) / actual_fill) * 100, 2)
        return {
            "exit_price": exit_price, "reason": "SL_HIT_SAME_BAR", "pnl": pnl, "status": "SL HIT",
            "bars_held": 1, "exit_time": entry_idx.strftime('%H:%M'),
            "holding_mae": holding_mae, "holding_mfe": holding_mfe,
            "initial_risk_pct": initial_risk_pct, "realized_r": realized_r
        }

    post_entry = post.loc[entry_idx:].iloc[1:]
    if post_entry.empty:
        exit_price = round(float(entry_bar["Close"]) * (1.0 + SLIPPAGE_BPS), 2)
        pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
        realized_r = round(pnl / initial_risk_pct, 2)
        holding_mae = round(((candle_high - actual_fill) / actual_fill) * 100, 2)
        holding_mfe = round(((actual_fill - candle_low) / actual_fill) * 100, 2)
        return {
            "exit_price": exit_price, "reason": "EOD_EXIT_1529", "pnl": pnl, "status": "EOD EXIT",
            "bars_held": 1, "exit_time": entry_idx.strftime('%H:%M'),
            "holding_mae": holding_mae, "holding_mfe": holding_mfe,
            "initial_risk_pct": initial_risk_pct, "realized_r": realized_r
        }

    if model_type == "TRAILING":
        # Strictly Causal Trailing Stop:
        # active_sl is carried into candle t.
        # Candle t is checked against active_sl first.
        # Only if candle t survives, update lowest_low and compute new stop for candle t+1.
        active_sl = sl
        lowest_low = candle_low
        be_active = False
        trail_active = False
        holding_bars = [entry_bar]

        for i, (ts, bar) in enumerate(post_entry.iterrows(), start=2):
            holding_bars.append(bar)
            b_high = float(bar["High"])
            b_low = float(bar["Low"])
            b_open = float(bar["Open"])

            # 1. Test existing active stop on candle t
            if b_high >= active_sl:
                base_exit = b_open if b_open > active_sl else active_sl
                exit_price = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
                pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
                realized_r = round(pnl / initial_risk_pct, 2)
                status_str = "TRAIL_STOP" if trail_active else ("BE_STOP" if be_active else "SL HIT")
                
                # Excursions during holding period
                h_df = pd.DataFrame(holding_bars)
                h_mae = round(((float(h_df["High"].max()) - actual_fill) / actual_fill) * 100, 2)
                h_mfe = round(((actual_fill - float(h_df["Low"].min())) / actual_fill) * 100, 2)
                
                return {
                    "exit_price": exit_price, "reason": status_str, "pnl": pnl, "status": status_str,
                    "bars_held": i, "exit_time": ts.strftime('%H:%M'),
                    "holding_mae": h_mae, "holding_mfe": h_mfe,
                    "initial_risk_pct": initial_risk_pct, "realized_r": realized_r
                }

            # 2. Candle t survived. Now update favorable extreme and calculate stop for candle t+1
            if b_low < lowest_low:
                lowest_low = b_low

            mfe_seen = ((actual_fill - lowest_low) / actual_fill) * 100

            if mfe_seen >= 0.75:
                trail_active = True
                new_sl = round(lowest_low * 1.0050, 2)
                if new_sl < active_sl:
                    active_sl = new_sl
            elif mfe_seen >= 0.50:
                be_active = True
                if actual_fill < active_sl:
                    active_sl = actual_fill

        # EOD exit at 15:29
        last_c = float(post_entry["Close"].iloc[-1])
        exit_price = round(last_c * (1.0 + SLIPPAGE_BPS), 2)
        pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
        realized_r = round(pnl / initial_risk_pct, 2)
        h_df = pd.DataFrame(holding_bars)
        h_mae = round(((float(h_df["High"].max()) - actual_fill) / actual_fill) * 100, 2)
        h_mfe = round(((actual_fill - float(h_df["Low"].min())) / actual_fill) * 100, 2)
        return {
            "exit_price": exit_price, "reason": "EOD_EXIT_1529", "pnl": pnl, "status": "EOD EXIT",
            "bars_held": len(post_entry) + 1, "exit_time": post_entry.index[-1].strftime('%H:%M'),
            "holding_mae": h_mae, "holding_mfe": h_mfe,
            "initial_risk_pct": initial_risk_pct, "realized_r": realized_r
        }

    # Standard Fixed TP / SL Model
    sl_bars = post_entry[post_entry["High"] >= sl]
    tp_bars = post_entry[post_entry["Low"] <= tp]

    exit_ts = None
    exit_price = None
    status = None
    reason = None

    if not sl_bars.empty and not tp_bars.empty:
        if sl_bars.index[0] <= tp_bars.index[0]:
            exit_ts = sl_bars.index[0]
            hit_bar = post_entry.loc[exit_ts]
            base_exit = float(hit_bar["Open"]) if float(hit_bar["Open"]) > sl else sl
            exit_price = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
            status = "SL HIT"
            reason = "SL_HIT"
        else:
            exit_ts = tp_bars.index[0]
            base_exit = tp
            exit_price = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
            status = "TP HIT"
            reason = "TP_HIT"
    elif not sl_bars.empty:
        exit_ts = sl_bars.index[0]
        hit_bar = post_entry.loc[exit_ts]
        base_exit = float(hit_bar["Open"]) if float(hit_bar["Open"]) > sl else sl
        exit_price = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
        status = "SL HIT"
        reason = "SL_HIT"
    elif not tp_bars.empty:
        exit_ts = tp_bars.index[0]
        base_exit = tp
        exit_price = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
        status = "TP HIT"
        reason = "TP_HIT"
    else:
        exit_ts = post_entry.index[-1]
        last_c = float(post_entry["Close"].iloc[-1])
        exit_price = round(last_c * (1.0 + SLIPPAGE_BPS), 2)
        status = "EOD EXIT"
        reason = "EOD_EXIT_1529"

    pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
    realized_r = round(pnl / initial_risk_pct, 2)

    # Slice trade holding period strictly between entry_idx and exit_ts
    holding_slice = post.loc[entry_idx:exit_ts]
    holding_mae = round(((float(holding_slice["High"].max()) - actual_fill) / actual_fill) * 100, 2)
    holding_mfe = round(((actual_fill - float(holding_slice["Low"].min())) / actual_fill) * 100, 2)
    bars_held = len(holding_slice)

    return {
        "exit_price": exit_price, "reason": reason, "pnl": pnl, "status": status,
        "bars_held": bars_held, "exit_time": exit_ts.strftime('%H:%M'),
        "holding_mae": holding_mae, "holding_mfe": holding_mfe,
        "initial_risk_pct": initial_risk_pct, "realized_r": realized_r
    }

# Execute audit
audit_records = []
executed_trades = []

for d in dates:
    n_day = nifty[nifty.index.strftime('%Y-%m-%d') == d]
    n_or = n_day.between_time("09:15", "09:34")
    
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

    # Full session potential excursion (if held to EOD without exit)
    post_all = post.loc[entry_idx:]
    full_day_mfe = round(((actual_fill - float(post_all["Low"].min())) / actual_fill) * 100, 2)
    full_day_mae = round(((float(post_all["High"].max()) - actual_fill) / actual_fill) * 100, 2)

    # Simulate Exits with strict integrity
    res_a = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, 0.0050, "FIXED")
    res_b = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, 0.0075, "FIXED")
    res_c = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, 0.0100, "FIXED")
    res_d = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, 0.0150, "FIXED")
    res_e = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, 0.0200, "FIXED")  # Baseline
    res_f = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, None, "TRAILING")

    trade_entry = {
        "Date": d, "Sector": weakest_sec["sec"], "Symbol": sym.replace(".NS", ""),
        "Benchmark_OR%": round(n_move, 2), "Stock_OR%": round(stock_or_pct, 2),
        "ATR14%": round(atr_val, 2), "ORH": orh, "ORL": orl, "Trigger": trig,
        "Actual_Entry": actual_fill, "Entry_Time": entry_idx.strftime('%H:%M'),
        "Initial_Risk%": res_c["initial_risk_pct"],
        "Full_Day_MFE%": full_day_mfe, "Full_Day_MAE%": full_day_mae,
        # Model A: 0.50% TP
        "A_PnL": res_a["pnl"], "A_R": res_a["realized_r"], "A_Status": res_a["status"],
        "A_Hold_MFE": res_a["holding_mfe"], "A_Hold_MAE": res_a["holding_mae"],
        # Model B: 0.75% TP
        "B_PnL": res_b["pnl"], "B_R": res_b["realized_r"], "B_Status": res_b["status"],
        "B_Hold_MFE": res_b["holding_mfe"], "B_Hold_MAE": res_b["holding_mae"],
        # Model C: 1.00% TP
        "C_PnL": res_c["pnl"], "C_R": res_c["realized_r"], "C_Status": res_c["status"],
        "C_Hold_MFE": res_c["holding_mfe"], "C_Hold_MAE": res_c["holding_mae"],
        # Model D: 1.50% TP
        "D_PnL": res_d["pnl"], "D_R": res_d["realized_r"], "D_Status": res_d["status"],
        "D_Hold_MFE": res_d["holding_mfe"], "D_Hold_MAE": res_d["holding_mae"],
        # Model E: 2.00% TP (Baseline)
        "E_PnL": res_e["pnl"], "E_R": res_e["realized_r"], "E_Status": res_e["status"],
        "E_Hold_MFE": res_e["holding_mfe"], "E_Hold_MAE": res_e["holding_mae"],
        # Model F: Causal Trailing
        "F_PnL": res_f["pnl"], "F_R": res_f["realized_r"], "F_Status": res_f["status"],
        "F_Hold_MFE": res_f["holding_mfe"], "F_Hold_MAE": res_f["holding_mae"],
        "Status": "EXECUTED"
    }
    executed_trades.append(trade_entry)
    audit_records.append(trade_entry)

# Save files
df_audit = pd.DataFrame(audit_records)
csv_path_dev = os.path.join(data_dir, "short_engine_v3_2_1_integrity.csv")
df_audit.to_csv(csv_path_dev, index=False)

git_csv_path = r"C:\Users\Mind\.gemini\antigravity\scratch\crypto-orderblock-bot\data\short_engine_v3_2_1_integrity.csv"
os.makedirs(os.path.dirname(git_csv_path), exist_ok=True)
df_audit.to_csv(git_csv_path, index=False)

df_exec = pd.DataFrame(executed_trades)

print("=" * 135)
print("  SHORT ENGINE v3.2.1: INTEGRITY-AUDITED EXECUTED TRADES (N = 9)")
print("=" * 135)
cols_show = ["Date", "Symbol", "Stock_OR%", "Actual_Entry", "Initial_Risk%", "Full_Day_MFE%", "A_PnL", "A_R", "B_PnL", "B_R", "C_PnL", "C_R", "E_PnL", "E_R", "F_PnL", "F_R"]
print(df_exec[cols_show].to_string(index=False))

# Portfolio Drawdown & Compounded Growth Simulation:
# Initial Capital = 100,000, Risk per trade = 1.0% of portfolio equity
def compute_compounded_metrics(name, pnl_col, r_col, status_col):
    initial_cap = 100000.0
    equity = initial_cap
    equity_curve = [initial_cap]
    
    for _, row in df_exec.iterrows():
        # Trade-specific risk in %
        risk_pct = row["Initial_Risk%"] / 100.0
        # Dollar risk = 1% of current equity
        dollar_risk = equity * 0.01
        position_size = dollar_risk / risk_pct
        trade_dollar_pnl = position_size * (row[pnl_col] / 100.0)
        equity += trade_dollar_pnl
        equity_curve.append(equity)

    eq_series = pd.Series(equity_curve)
    peak = eq_series.cummax()
    dd_dollar = eq_series - peak
    dd_pct = (dd_dollar / peak) * 100.0
    max_portfolio_dd_pct = dd_pct.min()
    final_equity = equity
    portfolio_net_return_pct = ((final_equity - initial_cap) / initial_cap) * 100.0

    # Trade stats
    pnl = df_exec[pnl_col]
    r_vals = df_exec[r_col]
    wins = df_exec[pnl > 0]
    losses = df_exec[pnl < 0]
    w_cnt = len(wins)
    l_cnt = len(losses)
    wr = (w_cnt / len(df_exec)) * 100.0 if len(df_exec) > 0 else 0.0
    gp = wins[pnl_col].sum()
    gl = abs(losses[pnl_col].sum())
    pf = round(gp / gl, 2) if gl > 0 else (np.inf if gp > 0 else 0.0)
    avg_r = r_vals.mean()
    total_raw_pnl = pnl.sum()

    tp_hits = len(df_exec[df_exec[status_col] == "TP HIT"])
    sl_hits = len(df_exec[df_exec[status_col].str.contains("SL", na=False)])
    eod_hits = len(df_exec[df_exec[status_col].str.contains("EOD|TRAIL|BE", na=False)])

    return {
        "Model": name, "TP": tp_hits, "SL": sl_hits, "EOD/Trail": eod_hits,
        "W/L": f"{w_cnt}W / {l_cnt}L", "Win_Rate%": round(wr, 1),
        "Profit_Factor": pf, "Avg_R": round(avg_r, 2), "Sum_Trade_PnL%": round(total_raw_pnl, 2),
        "Compounded_Return%": round(portfolio_net_return_pct, 2),
        "Portfolio_Max_DD%": round(max_portfolio_dd_pct, 2)
    }

models = [
    ("Model A (0.50% TP)", "A_PnL", "A_R", "A_Status"),
    ("Model B (0.75% TP)", "B_PnL", "B_R", "B_Status"),
    ("Model C (1.00% TP)", "C_PnL", "C_R", "C_Status"),
    ("Model D (1.50% TP)", "D_PnL", "D_R", "D_Status"),
    ("Model E (2.00% TP - Baseline)", "E_PnL", "E_R", "E_Status"),
    ("Model F (Causal Trailing)", "F_PnL", "F_R", "F_Status"),
]

summary_rows = [compute_compounded_metrics(m[0], m[1], m[2], m[3]) for m in models]
df_summary = pd.DataFrame(summary_rows)

print("\n" + "=" * 135)
print("  SHORT ENGINE v3.2.1 SUMMARY: COMPARED WITH TRADE-SPECIFIC R & 1% RISK COMPOUNDED PORTFOLIO")
print("=" * 135)
print(df_summary.to_string(index=False))
print("=" * 135)
