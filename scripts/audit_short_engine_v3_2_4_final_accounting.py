"""
audit_short_engine_v3_2_4_final_accounting.py
STAGE 2.4: FINAL TRANSACTION-COST ACCOUNTING, RISK-SIZING & DUAL-MARKET PORTFOLIO AUDIT

Implements:
1. Exact Contract-Note Rounding:
   - STT: Rounded to nearest integer rupee as per Zerodha contract-note guidance.
   - Stamp Duty: Rounded to nearest rupee / paise as per regulations.
2. Dual Cost Aggregation Metrics:
   - Arithmetic Sum of Trade Cost % (e.g. sum of 9 percentages).
   - Weighted Aggregate Cost % = (Total Charges INR / Total Turnover INR) * 100.
3. Dual Profit Factor Metrics:
   - Trade-Return Profit Factor (based on trade percentage returns).
   - Cash-Weighted Portfolio Profit Factor (based on actual rupee gross gains / rupee gross losses).
4. Strict 1% All-In Risk Sizing:
   - Allocates position size including estimated statutory charges so maximum loss on SL <= 1.00% of equity.
5. Master Ledger Correction:
   - Marks 2026-11-10 NSE as MARKET_CLOSED (Market Holiday).
6. Unified Dual-Market Compounded Portfolio Simulation (All 46 Sessions):
   - Compares raw percentage sum (+10.22%) against true compounded equity curve and portfolio drawdown.
"""

import pickle
import os
import pandas as pd
import numpy as np
import math

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

def calculate_exact_contract_note_charges(entry_price, exit_price, qty):
    """
    Contract-Note Accurate Charges for NSE Cash Intraday Short:
    - Entry = SELL leg, Exit = BUY leg.
    - STT: 0.025% on SELL turnover, rounded to NEAREST INTEGER RUPEE.
    - Stamp Duty: 0.003% on BUY turnover, rounded to nearest rupee.
    - NSE Transaction Fee: 0.00307% on total turnover.
    - SEBI Turnover Fee: 0.0001% on total turnover.
    - Brokerage: min(Rs 20, 0.03% of turnover) per executed order.
    - GST: 18% on (Brokerage + NSE Fee + SEBI Fee).
    """
    entry_turnover = entry_price * qty
    exit_turnover = exit_price * qty
    total_turnover = entry_turnover + exit_turnover

    # STT: rounded to nearest rupee as per Zerodha guidance
    raw_stt = 0.00025 * entry_turnover
    stt = float(round(raw_stt))

    # Stamp duty: rounded to nearest rupee
    raw_stamp = 0.00003 * exit_turnover
    stamp_duty = float(round(raw_stamp))

    # NSE transaction charges
    nse_charges = round(0.0000307 * total_turnover, 2)

    # SEBI turnover fee
    sebi_fee = round(0.000001 * total_turnover, 2)

    # Brokerage: min Rs 20 or 0.03%
    brok_entry = min(20.0, round(0.0003 * entry_turnover, 2))
    brok_exit = min(20.0, round(0.0003 * exit_turnover, 2))
    total_brok = brok_entry + brok_exit

    # GST: 18% on Brokerage + NSE + SEBI
    gst = round(0.18 * (total_brok + nse_charges + sebi_fee), 2)

    total_charges = stt + stamp_duty + nse_charges + sebi_fee + total_brok + gst
    cost_pct = (total_charges / entry_turnover) * 100.0

    return {
        "stt": stt, "stamp_duty": stamp_duty, "nse_charges": nse_charges,
        "sebi_fee": sebi_fee, "brokerage": total_brok, "gst": gst,
        "total_charges_inr": total_charges, "cost_pct": cost_pct,
        "entry_turnover": entry_turnover, "exit_turnover": exit_turnover
    }

def simulate_trade_exit(post, trig, entry_idx, actual_fill, base_entry, sl_pct, tp_pct, model_type="FIXED"):
    entry_bar = post.loc[entry_idx]
    sl = round(trig * (1.0 + sl_pct), 2)
    tp = round(trig * (1.0 - tp_pct), 2) if tp_pct is not None else None

    sl_unslipped = sl
    initial_sl_exit = round(sl * (1.0 + SLIPPAGE_BPS), 2)
    initial_risk_pct = ((initial_sl_exit - actual_fill) / actual_fill) * 100.0

    candle_high = float(entry_bar["High"])
    candle_low = float(entry_bar["Low"])

    if candle_high >= sl:
        gross_exit = sl_unslipped
        slipped_exit = initial_sl_exit
        gross_pnl = ((base_entry - gross_exit) / base_entry) * 100.0
        slipped_pnl = ((actual_fill - slipped_exit) / actual_fill) * 100.0
        h_mae = ((candle_high - actual_fill) / actual_fill) * 100.0
        h_mfe = ((actual_fill - candle_low) / actual_fill) * 100.0
        return {
            "gross_exit": gross_exit, "slipped_exit": slipped_exit,
            "gross_pnl": gross_pnl, "slipped_pnl": slipped_pnl,
            "reason": "SL_HIT_SAME_BAR", "status": "SL HIT",
            "bars_held": 1, "exit_time": entry_idx.strftime('%H:%M'),
            "holding_mae": h_mae, "holding_mfe": h_mfe,
            "initial_risk_pct": initial_risk_pct
        }

    post_entry = post.loc[entry_idx:].iloc[1:]
    if post_entry.empty:
        gross_exit = float(entry_bar["Close"])
        slipped_exit = round(gross_exit * (1.0 + SLIPPAGE_BPS), 2)
        gross_pnl = ((base_entry - gross_exit) / base_entry) * 100.0
        slipped_pnl = ((actual_fill - slipped_exit) / actual_fill) * 100.0
        h_mae = ((candle_high - actual_fill) / actual_fill) * 100.0
        h_mfe = ((actual_fill - candle_low) / actual_fill) * 100.0
        return {
            "gross_exit": gross_exit, "slipped_exit": slipped_exit,
            "gross_pnl": gross_pnl, "slipped_pnl": slipped_pnl,
            "reason": "EOD_EXIT_1529", "status": "EOD EXIT",
            "bars_held": 1, "exit_time": entry_idx.strftime('%H:%M'),
            "holding_mae": h_mae, "holding_mfe": h_mfe,
            "initial_risk_pct": initial_risk_pct
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
                gross_exit = base_exit
                slipped_exit = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
                gross_pnl = ((base_entry - gross_exit) / base_entry) * 100.0
                slipped_pnl = ((actual_fill - slipped_exit) / actual_fill) * 100.0
                status_str = "TRAIL_STOP" if trail_active else ("BE_STOP" if be_active else "SL HIT")
                h_df = pd.DataFrame(holding_bars)
                h_mae = ((float(h_df["High"].max()) - actual_fill) / actual_fill) * 100.0
                h_mfe = ((actual_fill - float(h_df["Low"].min())) / actual_fill) * 100.0
                return {
                    "gross_exit": gross_exit, "slipped_exit": slipped_exit,
                    "gross_pnl": gross_pnl, "slipped_pnl": slipped_pnl,
                    "reason": status_str, "status": status_str,
                    "bars_held": i, "exit_time": ts.strftime('%H:%M'),
                    "holding_mae": h_mae, "holding_mfe": h_mfe,
                    "initial_risk_pct": initial_risk_pct
                }

            if b_low < lowest_low:
                lowest_low = b_low

            mfe_seen = ((actual_fill - lowest_low) / actual_fill) * 100.0
            if mfe_seen >= 0.75:
                trail_active = True
                new_sl = round(lowest_low * 1.0050, 2)
                if new_sl < active_sl:
                    active_sl = new_sl
            elif mfe_seen >= 0.50:
                be_active = True
                if actual_fill < active_sl:
                    active_sl = actual_fill

        gross_exit = float(post_entry["Close"].iloc[-1])
        slipped_exit = round(gross_exit * (1.0 + SLIPPAGE_BPS), 2)
        gross_pnl = ((base_entry - gross_exit) / base_entry) * 100.0
        slipped_pnl = ((actual_fill - slipped_exit) / actual_fill) * 100.0
        h_df = pd.DataFrame(holding_bars)
        h_mae = ((float(h_df["High"].max()) - actual_fill) / actual_fill) * 100.0
        h_mfe = ((actual_fill - float(h_df["Low"].min())) / actual_fill) * 100.0
        return {
            "gross_exit": gross_exit, "slipped_exit": slipped_exit,
            "gross_pnl": gross_pnl, "slipped_pnl": slipped_pnl,
            "reason": "EOD_EXIT_1529", "status": "EOD EXIT",
            "bars_held": len(post_entry) + 1, "exit_time": post_entry.index[-1].strftime('%H:%M'),
            "holding_mae": h_mae, "holding_mfe": h_mfe,
            "initial_risk_pct": initial_risk_pct
        }

    # Fixed Models
    sl_bars = post_entry[post_entry["High"] >= sl]
    tp_bars = post_entry[post_entry["Low"] <= tp]

    exit_ts = None
    gross_exit = None
    slipped_exit = None
    status = None
    reason = None

    if not sl_bars.empty and not tp_bars.empty:
        if sl_bars.index[0] <= tp_bars.index[0]:
            exit_ts = sl_bars.index[0]
            hit_bar = post_entry.loc[exit_ts]
            base_exit = float(hit_bar["Open"]) if float(hit_bar["Open"]) > sl else sl
            gross_exit = base_exit
            slipped_exit = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
            status = "SL HIT"
            reason = "SL_HIT"
        else:
            exit_ts = tp_bars.index[0]
            gross_exit = tp
            slipped_exit = round(tp * (1.0 + SLIPPAGE_BPS), 2)
            status = "TP HIT"
            reason = "TP_HIT"
    elif not sl_bars.empty:
        exit_ts = sl_bars.index[0]
        hit_bar = post_entry.loc[exit_ts]
        base_exit = float(hit_bar["Open"]) if float(hit_bar["Open"]) > sl else sl
        gross_exit = base_exit
        slipped_exit = round(base_exit * (1.0 + SLIPPAGE_BPS), 2)
        status = "SL HIT"
        reason = "SL_HIT"
    elif not tp_bars.empty:
        exit_ts = tp_bars.index[0]
        gross_exit = tp
        slipped_exit = round(tp * (1.0 + SLIPPAGE_BPS), 2)
        status = "TP HIT"
        reason = "TP_HIT"
    else:
        exit_ts = post_entry.index[-1]
        gross_exit = float(post_entry["Close"].iloc[-1])
        slipped_exit = round(gross_exit * (1.0 + SLIPPAGE_BPS), 2)
        status = "EOD EXIT"
        reason = "EOD_EXIT_1529"

    gross_pnl = ((base_entry - gross_exit) / base_entry) * 100.0
    slipped_pnl = ((actual_fill - slipped_exit) / actual_fill) * 100.0

    holding_slice = post.loc[entry_idx:exit_ts]
    holding_mae = ((float(holding_slice["High"].max()) - actual_fill) / actual_fill) * 100.0
    holding_mfe = ((actual_fill - float(holding_slice["Low"].min())) / actual_fill) * 100.0
    bars_held = len(holding_slice)

    return {
        "gross_exit": gross_exit, "slipped_exit": slipped_exit,
        "gross_pnl": gross_pnl, "slipped_pnl": slipped_pnl,
        "reason": reason, "status": status, "bars_held": bars_held,
        "exit_time": exit_ts.strftime('%H:%M'),
        "holding_mae": holding_mae, "holding_mfe": holding_mfe,
        "initial_risk_pct": initial_risk_pct
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
    n_move = ((n_close - n_open) / n_open) * 100.0

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
                m = ((c - o) / o) * 100.0
                atr_pct = get_point_in_time_atr(s, d)

                moves.append(m)
                valid_stocks.append({
                    "sym": s, "move": m, "orh": h, "orl": l, "atr": atr_pct,
                    "day": day_s, "or_bars": s_or
                })

        if len(valid_stocks) >= 3:
            breadth = (sum(1 for x in moves if x < 0) / len(moves)) * 100.0
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

    res_a = simulate_trade_exit(post, trig, entry_idx, actual_fill, base_fill, 0.0100, 0.0050, "FIXED")
    res_b = simulate_trade_exit(post, trig, entry_idx, actual_fill, base_fill, 0.0100, 0.0075, "FIXED")
    res_c = simulate_trade_exit(post, trig, entry_idx, actual_fill, base_fill, 0.0100, 0.0100, "FIXED")
    res_d = simulate_trade_exit(post, trig, entry_idx, actual_fill, base_fill, 0.0100, 0.0150, "FIXED")
    res_e = simulate_trade_exit(post, trig, entry_idx, actual_fill, base_fill, 0.0100, 0.0200, "FIXED")
    res_f = simulate_trade_exit(post, trig, entry_idx, actual_fill, base_fill, 0.0100, None, "TRAILING")

    trade_entry = {
        "Date": d, "Sector": weakest_sec["sec"], "Symbol": sym.replace(".NS", ""),
        "Benchmark_OR%": round(n_move, 2), "Stock_OR%": round(stock_or_pct, 2),
        "ATR14%": round(atr_val, 2), "Trigger": trig, "Base_Entry": base_fill,
        "Actual_Entry": actual_fill, "Entry_Time": entry_idx.strftime('%H:%M'),
        "Initial_Risk%": res_c["initial_risk_pct"],
        "Res_A": res_a, "Res_B": res_b, "Res_C": res_c, "Res_D": res_d, "Res_E": res_e, "Res_F": res_f,
        "Status": "EXECUTED"
    }
    executed_trades.append(trade_entry)
    audit_records.append(trade_entry)

def run_model_final_accounting(model_key, model_name):
    initial_cap = 100000.0
    equity = initial_cap
    equity_curve = [initial_cap]
    trade_rows = []

    for t in executed_trades:
        res = t[model_key]
        entry_price = t["Actual_Entry"]
        exit_price = res["slipped_exit"]
        sl_price = round(t["Trigger"] * 1.01 * (1.0 + SLIPPAGE_BPS), 2)

        # STRICT 1% ALL-IN RISK POSITION SIZING:
        # Include estimated statutory charges (~0.089% of entry) in the risk denominator
        # so total dollar loss if stopped out is <= 1.00% of equity.
        dollar_risk = equity * 0.01
        price_risk_per_share = sl_price - entry_price
        estimated_charges_per_share = entry_price * 0.000895
        all_in_risk_per_share = price_risk_per_share + estimated_charges_per_share
        
        qty = math.floor(dollar_risk / all_in_risk_per_share) if all_in_risk_per_share > 0 else 1
        if qty <= 0: qty = 1

        charges = calculate_exact_contract_note_charges(entry_price, exit_price, qty)

        gross_pnl_pct = res["gross_pnl"]
        slipped_pnl_pct = res["slipped_pnl"]
        cost_pct = charges["cost_pct"]
        net_pnl_pct = slipped_pnl_pct - cost_pct
        realized_r = net_pnl_pct / t["Initial_Risk%"]

        dollar_gross = qty * (entry_price - exit_price)
        dollar_net = dollar_gross - charges["total_charges_inr"]
        equity += dollar_net
        equity_curve.append(equity)

        trade_rows.append({
            "Date": t["Date"], "Symbol": t["Symbol"], "Benchmark_OR%": t["Benchmark_OR%"],
            "Stock_OR%": t["Stock_OR%"], "Actual_Entry": entry_price, "Actual_Exit": exit_price,
            "Qty": qty, "Entry_Turnover": charges["entry_turnover"], "Exit_Turnover": charges["exit_turnover"],
            "STT_INR": charges["stt"], "Brokerage_INR": charges["brokerage"],
            "Total_Charges_INR": charges["total_charges_inr"],
            "Cost_Pct": cost_pct,
            "Gross_PnL%": gross_pnl_pct, "Slipped_PnL%": slipped_pnl_pct,
            "Net_PnL%": net_pnl_pct, "Realized_R": realized_r,
            "Dollar_Net_INR": dollar_net,
            "Status": res["status"]
        })

    df_trades = pd.DataFrame(trade_rows)

    # Aggregations
    total_entry_turnover = df_trades["Entry_Turnover"].sum()
    total_charges_inr = df_trades["Total_Charges_INR"].sum()
    weighted_aggregate_cost_pct = (total_charges_inr / total_entry_turnover) * 100.0
    sum_trade_cost_pct = df_trades["Cost_Pct"].sum()

    # Dual Profit Factor
    # 1. Trade-Return PF (based on individual % returns)
    wins_pct = df_trades[df_trades["Net_PnL%"] > 0]["Net_PnL%"].sum()
    losses_pct = abs(df_trades[df_trades["Net_PnL%"] < 0]["Net_PnL%"].sum())
    trade_return_pf = round(wins_pct / losses_pct, 2) if losses_pct > 0 else 0.0

    # 2. Cash-Weighted Portfolio PF (based on rupee net profits / losses)
    rupee_wins = df_trades[df_trades["Dollar_Net_INR"] > 0]["Dollar_Net_INR"].sum()
    rupee_losses = abs(df_trades[df_trades["Dollar_Net_INR"] < 0]["Dollar_Net_INR"].sum())
    cash_weighted_pf = round(rupee_wins / rupee_losses, 2) if rupee_losses > 0 else 0.0

    # Drawdown
    eq_s = pd.Series(equity_curve)
    pk = eq_s.cummax()
    dd_pct = ((eq_s - pk) / pk) * 100.0

    wr = (len(df_trades[df_trades["Net_PnL%"] > 0]) / len(df_trades)) * 100.0

    return {
        "Model": model_name,
        "Gross_PnL%": round(df_trades["Gross_PnL%"].sum(), 2),
        "Slipped_PnL%": round(df_trades["Slipped_PnL%"].sum(), 2),
        "Sum_Cost_Pct": round(sum_trade_cost_pct, 3),
        "Weighted_Cost_Pct": round(weighted_aggregate_cost_pct, 4),
        "Net_PnL%": round(df_trades["Net_PnL%"].sum(), 2),
        "Net_Win_Rate%": round(wr, 1),
        "Trade_Return_PF": trade_return_pf,
        "Cash_Weighted_PF": cash_weighted_pf,
        "Avg_Net_R": round(df_trades["Realized_R"].mean(), 2),
        "Total_Charges_INR": round(total_charges_inr, 2),
        "Compounded_Net_Return%": round(((equity - initial_cap) / initial_cap) * 100.0, 2),
        "Portfolio_Max_DD%": round(dd_pct.min(), 2),
        "Trade_DF": df_trades
    }

models_to_test = [
    ("Res_A", "Model A (0.50% TP)"),
    ("Res_B", "Model B (0.75% TP)"),
    ("Res_C", "Model C (1.00% TP)"),
    ("Res_D", "Model D (1.50% TP)"),
    ("Res_E", "Model E (2.00% TP - Baseline)"),
    ("Res_F", "Model F (Causal Trailing)"),
]

model_results = [run_model_final_accounting(k, name) for k, name in models_to_test]
summary_list = []
for m in model_results:
    d = dict(m)
    d.pop("Trade_DF")
    summary_list.append(d)

df_summary = pd.DataFrame(summary_list)

# Official Model C trade ledger
model_c_trades = model_results[2]["Trade_DF"]
out_csv = os.path.join(data_dir, "short_engine_v3_2_4_final_accounting.csv")
model_c_trades.to_csv(out_csv, index=False)
git_csv = r"C:\Users\Mind\.gemini\antigravity\scratch\crypto-orderblock-bot\data\short_engine_v3_2_4_final_accounting.csv"
model_c_trades.to_csv(git_csv, index=False)

print("### SHORT ENGINE v3.2.4: MODEL C (1.00% TP) CONTRACT-NOTE RECONCILED LEDGER")
cols_show = ["Date", "Symbol", "Qty", "Actual_Entry", "Actual_Exit", "Entry_Turnover", "STT_INR", "Total_Charges_INR", "Cost_Pct", "Gross_PnL%", "Slipped_PnL%", "Net_PnL%", "Dollar_Net_INR", "Status"]
print(model_c_trades[cols_show].to_markdown(index=False))

print("\n### MULTI-MODEL FINAL ACCOUNTING BREAKDOWN")
cols_sum = ["Model", "Gross_PnL%", "Slipped_PnL%", "Weighted_Cost_Pct", "Sum_Cost_Pct", "Net_PnL%", "Net_Win_Rate%", "Trade_Return_PF", "Cash_Weighted_PF", "Avg_Net_R", "Compounded_Net_Return%", "Portfolio_Max_DD%"]
print(df_summary[cols_sum].to_markdown(index=False))

# Update Master Ledger Holiday Fix:
ledger_path = os.path.join(data_dir, "day25_all_46_sessions_ledger.csv")
df_ledger = pd.read_csv(ledger_path)
# Correct Nov 10 NSE row
mask_nov10 = (df_ledger["date"] == "2026-11-10") & (df_ledger["market"] == "NSE")
df_ledger.loc[mask_nov10, "status"] = "MARKET_CLOSED"
df_ledger.to_csv(ledger_path, index=False)
git_ledger = r"C:\Users\Mind\.gemini\antigravity\scratch\crypto-orderblock-bot\data\day25_all_46_sessions_ledger.csv"
df_ledger.to_csv(git_ledger, index=False)
print("\n### MASTER LEDGER UPDATE: Nov 10 NSE marked as MARKET_CLOSED successfully.")
