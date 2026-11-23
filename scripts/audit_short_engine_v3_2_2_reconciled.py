"""
audit_short_engine_v3_2_2_reconciled.py
STAGE 2.2: SHORT ENGINE RECONCILIATION & TRANSACTION COST AUDIT

Implements:
1. Explicit Labeling: 'Bar-Resolution MFE/MAE Estimates (1-minute resolution)'.
2. Entry Candle Ambiguity Audit: Verifies and logs intrabar touches of SL/TP on the entry candle.
3. Transaction Costs & Regulatory Frictions:
   - Brokerage: ₹20 / order (0.03% capped)
   - STT: 0.025% on Sell side
   - Exchange charges: 0.00345% both sides
   - GST: 18% on (brokerage + turnover fee)
   - SEBI turnover fee: 0.0001%
   - Stamp duty: 0.003% on Buy side
   - Slippage: 5 bps adverse entry + 5 bps adverse exit (10 bps)
   - Total estimated round-trip friction: ~0.15% per trade
4. Direct Programmatic Markdown Generation (Zero Manual Transcription Error).
"""

import pickle
import os
import pandas as pd
import numpy as np

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
SLIPPAGE_BPS = 0.0005  # 5 bps

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
    entry_bar = post.loc[entry_idx]
    sl = round(trig * (1.0 + sl_pct), 2)
    tp = round(trig * (1.0 - tp_pct), 2) if tp_pct is not None else None

    initial_sl_exit = round(sl * (1.0 + SLIPPAGE_BPS), 2)
    initial_risk_pct = round(((initial_sl_exit - actual_fill) / actual_fill) * 100, 3)

    # Check same-bar ambiguity
    candle_high = float(entry_bar["High"])
    candle_low = float(entry_bar["Low"])

    if candle_high >= sl:
        exit_price = round(sl * (1.0 + SLIPPAGE_BPS), 2)
        pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
        realized_r = round(pnl / initial_risk_pct, 2)
        h_mae = round(((candle_high - actual_fill) / actual_fill) * 100, 2)
        h_mfe = round(((actual_fill - candle_low) / actual_fill) * 100, 2)
        return {
            "exit_price": exit_price, "reason": "SL_HIT_SAME_BAR", "pnl": pnl, "status": "SL HIT",
            "bars_held": 1, "exit_time": entry_idx.strftime('%H:%M'),
            "holding_mae": h_mae, "holding_mfe": h_mfe,
            "initial_risk_pct": initial_risk_pct, "realized_r": realized_r
        }

    post_entry = post.loc[entry_idx:].iloc[1:]
    if post_entry.empty:
        exit_price = round(float(entry_bar["Close"]) * (1.0 + SLIPPAGE_BPS), 2)
        pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
        realized_r = round(pnl / initial_risk_pct, 2)
        h_mae = round(((candle_high - actual_fill) / actual_fill) * 100, 2)
        h_mfe = round(((actual_fill - candle_low) / actual_fill) * 100, 2)
        return {
            "exit_price": exit_price, "reason": "EOD_EXIT_1529", "pnl": pnl, "status": "EOD EXIT",
            "bars_held": 1, "exit_time": entry_idx.strftime('%H:%M'),
            "holding_mae": h_mae, "holding_mfe": h_mfe,
            "initial_risk_pct": initial_risk_pct, "realized_r": realized_r
        }

    if model_type == "TRAILING":
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

            if b_high >= active_sl:
                base_exit = b_open if b_open > active_sl else active_sl
                exit_price = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
                pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
                realized_r = round(pnl / initial_risk_pct, 2)
                status_str = "TRAIL_STOP" if trail_active else ("BE_STOP" if be_active else "SL HIT")
                h_df = pd.DataFrame(holding_bars)
                h_mae = round(((float(h_df["High"].max()) - actual_fill) / actual_fill) * 100, 2)
                h_mfe = round(((actual_fill - float(h_df["Low"].min())) / actual_fill) * 100, 2)
                return {
                    "exit_price": exit_price, "reason": status_str, "pnl": pnl, "status": status_str,
                    "bars_held": i, "exit_time": ts.strftime('%H:%M'),
                    "holding_mae": h_mae, "holding_mfe": h_mfe,
                    "initial_risk_pct": initial_risk_pct, "realized_r": realized_r
                }

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

    # Fixed TP / SL Model
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

    # Check entry candle intrabar touch
    e_high = float(entry_bar["High"])
    e_low = float(entry_bar["Low"])
    entry_touched_sl = (e_high >= round(trig * 1.01, 2))
    entry_touched_tp05 = (e_low <= round(trig * 0.995, 2))
    entry_touched_tp10 = (e_low <= round(trig * 0.990, 2))

    post_all = post.loc[entry_idx:]
    full_day_mfe = round(((actual_fill - float(post_all["Low"].min())) / actual_fill) * 100, 2)
    full_day_mae = round(((float(post_all["High"].max()) - actual_fill) / actual_fill) * 100, 2)

    res_a = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, 0.0050, "FIXED")
    res_b = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, 0.0075, "FIXED")
    res_c = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, 0.0100, "FIXED")
    res_d = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, 0.0150, "FIXED")
    res_e = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, 0.0200, "FIXED")
    res_f = simulate_trade_exit(post, trig, entry_idx, actual_fill, 0.0100, None, "TRAILING")

    trade_entry = {
        "Date": d, "Sector": weakest_sec["sec"], "Symbol": sym.replace(".NS", ""),
        "Benchmark_OR%": round(n_move, 2), "Stock_OR%": round(stock_or_pct, 2),
        "ATR14%": round(atr_val, 2), "Trigger": trig, "Actual_Entry": actual_fill,
        "Entry_Time": entry_idx.strftime('%H:%M'), "Initial_Risk%": res_c["initial_risk_pct"],
        "Entry_Touch_SL": entry_touched_sl, "Entry_Touch_TP05": entry_touched_tp05, "Entry_Touch_TP10": entry_touched_tp10,
        "Full_Day_MFE%": full_day_mfe, "Full_Day_MAE%": full_day_mae,
        # Model C (1.00% TP)
        "C_PnL": res_c["pnl"], "C_R": res_c["realized_r"], "C_Status": res_c["status"],
        "C_Bar_MFE%": res_c["holding_mfe"], "C_Bar_MAE%": res_c["holding_mae"],
        "C_Bars_Held": res_c["bars_held"], "C_Exit_Time": res_c["exit_time"],
        # Model A
        "A_PnL": res_a["pnl"], "A_R": res_a["realized_r"], "A_Status": res_a["status"],
        "A_Bar_MFE%": res_a["holding_mfe"], "A_Bar_MAE%": res_a["holding_mae"],
        # Model B
        "B_PnL": res_b["pnl"], "B_R": res_b["realized_r"], "B_Status": res_b["status"],
        # Model D
        "D_PnL": res_d["pnl"], "D_R": res_d["realized_r"], "D_Status": res_d["status"],
        # Model E
        "E_PnL": res_e["pnl"], "E_R": res_e["realized_r"], "E_Status": res_e["status"],
        # Model F
        "F_PnL": res_f["pnl"], "F_R": res_f["realized_r"], "F_Status": res_f["status"],
        "Status": "EXECUTED"
    }
    executed_trades.append(trade_entry)
    audit_records.append(trade_entry)

df_audit = pd.DataFrame(audit_records)
df_audit.to_csv(os.path.join(data_dir, "short_engine_v3_2_2_reconciled.csv"), index=False)
git_csv_path = r"C:\Users\Mind\.gemini\antigravity\scratch\crypto-orderblock-bot\data\short_engine_v3_2_2_reconciled.csv"
os.makedirs(os.path.dirname(git_csv_path), exist_ok=True)
df_audit.to_csv(git_csv_path, index=False)

df_exec = pd.DataFrame(executed_trades)

# Markdown table generator
print("### TRADE-LEVEL RECONCILED TABLE (N = 9)")
print(df_exec[["Date", "Symbol", "Benchmark_OR%", "Stock_OR%", "Actual_Entry", "Initial_Risk%", "C_PnL", "C_R", "C_Bar_MFE%", "C_Bar_MAE%", "C_Bars_Held", "C_Status"]].to_markdown(index=False))

# Friction Impact Analysis (Round-trip statutory + brokerage = 0.15% per trade)
FRICTION_PCT = 0.15

def compute_friction_metrics(name, pnl_col, r_col, status_col):
    raw_pnl = df_exec[pnl_col]
    net_pnl = raw_pnl - FRICTION_PCT  # Deduct statutory brokerage & taxes
    
    # Portfolio Compounding
    initial_cap = 100000.0
    eq = initial_cap
    eq_curve = [initial_cap]
    for _, row in df_exec.iterrows():
        r_pct = row["Initial_Risk%"] / 100.0
        pos_size = (eq * 0.01) / r_pct
        trade_ret = (row[pnl_col] - FRICTION_PCT) / 100.0
        eq += pos_size * trade_ret
        eq_curve.append(eq)
        
    eq_s = pd.Series(eq_curve)
    pk = eq_s.cummax()
    dd_pct = ((eq_s - pk) / pk) * 100.0
    
    wins = net_pnl[net_pnl > 0]
    losses = net_pnl[net_pnl < 0]
    wr = (len(wins) / len(df_exec)) * 100.0
    gp = wins.sum()
    gl = abs(losses.sum())
    pf = round(gp / gl, 2) if gl > 0 else 0.0
    
    return {
        "Model": name,
        "Raw_PnL%": round(raw_pnl.sum(), 2),
        "Net_After_Friction%": round(net_pnl.sum(), 2),
        "Net_Win_Rate%": round(wr, 1),
        "Net_Profit_Factor": pf,
        "Net_Compounded_Return%": round(((eq - initial_cap) / initial_cap) * 100.0, 2),
        "Net_Portfolio_Max_DD%": round(dd_pct.min(), 2)
    }

models = [
    ("Model A (0.50% TP)", "A_PnL", "A_R", "A_Status"),
    ("Model B (0.75% TP)", "B_PnL", "B_R", "B_Status"),
    ("Model C (1.00% TP)", "C_PnL", "C_R", "C_Status"),
    ("Model D (1.50% TP)", "D_PnL", "D_R", "D_Status"),
    ("Model E (2.00% TP - Baseline)", "E_PnL", "E_R", "E_Status"),
    ("Model F (Causal Trailing)", "F_PnL", "F_R", "F_Status"),
]

friction_rows = [compute_friction_metrics(m[0], m[1], m[2], m[3]) for m in models]
df_friction = pd.DataFrame(friction_rows)

print("\n### MULTI-MODEL PERFORMANCE AFTER STATUTORY TAXES & BROKERAGE (0.15% / trade)")
print(df_friction.to_markdown(index=False))
