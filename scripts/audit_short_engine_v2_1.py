"""
audit_short_engine_v2_1.py
Corrected Short Engine v2.1 Execution Simulator.
Fixes:
1. Directional bug on TP exit: Short buyback adverse slippage is tp * (1.0 + SLIPPAGE_BPS).
2. Gap-through entry: If candle Open < Trigger, fill is candle Open (not Trigger).
3. Same-candle ambiguity: Exits evaluated strictly starting from the candle AFTER the entry candle.
4. Timestamp sequence integrity: Verifies 20 strictly unique 1-min timestamps.
5. Generates short_engine_v2_1_trades.csv.
"""
import pickle
import pandas as pd
import numpy as np

with open("nse_1m_full.pkl", "rb") as f:
    cache = pickle.load(f)

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
SLIPPAGE_BPS = 0.0005  # 5 bps

for d in dates:
    n_day = nifty[nifty.index.strftime('%Y-%m-%d') == d]
    n_or = n_day.between_time("09:15", "09:34")
    # Verify exactly 20 unique completed bars
    if len(n_or) != 20 or len(set(n_or.index)) != 20:
        continue
    n_open = n_or["Open"].iloc[0]
    n_close = n_or["Close"].iloc[-1]
    n_move = ((n_close - n_open) / n_open) * 100

    sec_stats = []
    stock_map = {}

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
                o = s_or["Open"].iloc[0]
                c = s_or["Close"].iloc[-1]
                l = s_or["Low"].min()
                m = ((c - o) / o) * 100
                moves.append(m)
                info = {"sym": s, "move": m, "orl": l, "day": day_s}
                stock_map[s] = info
                valid_stocks.append(info)

        if len(valid_stocks) >= 3:
            breadth = (sum(1 for x in moves if x < 0) / len(moves)) * 100
            avg_m = np.mean(moves)
            sec_stats.append({"sec": sec, "breadth": breadth, "avg_m": avg_m, "stocks": valid_stocks})

    if not sec_stats:
        continue

    sec_stats.sort(key=lambda x: (x["breadth"], -x["avg_m"]), reverse=True)
    weakest_sec = sec_stats[0]

    cands = weakest_sec["stocks"]
    cands.sort(key=lambda x: x["move"])
    weakest_stock = cands[0]

    sym = weakest_stock["sym"]
    orl = weakest_stock["orl"]
    stock_or_pct = weakest_stock["move"]

    trig = round(orl * 0.9997, 2)
    sl = round(trig * 1.0100, 2)     # +1.00% Hard SL
    tp = round(trig * 0.9800, 2)     # -2.00% TP

    post = weakest_stock["day"].between_time("09:45", "15:29")
    trig_bars = post[post["Low"] <= trig]

    if trig_bars.empty:
        trades.append({
            "Date": d, "Benchmark_OR_pct": round(n_move, 2), "Sector": weakest_sec["sec"],
            "Symbol": sym.replace(".NS", ""), "Stock_OR_pct": round(stock_or_pct, 2),
            "ORL": orl, "Trigger": trig, "Actual_Fill": trig, "SL": sl, "TP": tp,
            "Exit": 0.0, "PnL": 0.00, "Status": "UNTRIGGERED", "Note": "Untriggered"
        })
    else:
        entry_idx = trig_bars.index[0]
        entry_bar = post.loc[entry_idx]

        # Fix A: Gap-through entry check
        # If the entry candle opened strictly below trigger, fill at candle Open (worse fill)
        if entry_bar["Open"] < trig:
            base_fill = float(entry_bar["Open"])
        else:
            base_fill = trig

        # Short fill adverse slippage: fills LOWER by 5 bps
        actual_fill = round(base_fill * (1.0 - SLIPPAGE_BPS), 2)

        # Fix B: Exits evaluated strictly on post-entry bars (excluding entry minute ambiguity)
        post_entry = post.loc[entry_idx:].iloc[1:]

        if post_entry.empty:
            # Entry on the very last bar of session
            exit_price = round(float(entry_bar["Close"]) * (1.0 + SLIPPAGE_BPS), 2)
            pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
            trades.append({
                "Date": d, "Benchmark_OR_pct": round(n_move, 2), "Sector": weakest_sec["sec"],
                "Symbol": sym.replace(".NS", ""), "Stock_OR_pct": round(stock_or_pct, 2),
                "ORL": orl, "Trigger": trig, "Actual_Fill": actual_fill, "SL": sl, "TP": tp,
                "Exit": exit_price, "PnL": pnl, "Status": "EOD EXIT", "Note": "Filled at 15:29"
            })
            continue

        sl_bars = post_entry[post_entry["High"] >= sl]
        tp_bars = post_entry[post_entry["Low"] <= tp]

        if not sl_bars.empty and not tp_bars.empty:
            if sl_bars.index[0] <= tp_bars.index[0]:
                # Adverse buyback slippage: pays HIGHER price (+ SLIPPAGE_BPS)
                exit_price = round(sl * (1.0 + SLIPPAGE_BPS), 2)
                pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
                status = "SL HIT"
            else:
                # Fix: Corrected TP exit slippage: Short buyback pays HIGHER price (+ SLIPPAGE_BPS)
                exit_price = round(tp * (1.0 + SLIPPAGE_BPS), 2)
                pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
                status = "TP HIT"
        elif not sl_bars.empty:
            exit_price = round(sl * (1.0 + SLIPPAGE_BPS), 2)
            pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
            status = "SL HIT"
        elif not tp_bars.empty:
            exit_price = round(tp * (1.0 + SLIPPAGE_BPS), 2)
            pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
            status = "TP HIT"
        else:
            last_c = float(post_entry["Close"].iloc[-1])
            exit_price = round(last_c * (1.0 + SLIPPAGE_BPS), 2)
            pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
            status = "EOD EXIT"

        trades.append({
            "Date": d, "Benchmark_OR_pct": round(n_move, 2), "Sector": weakest_sec["sec"],
            "Symbol": sym.replace(".NS", ""), "Stock_OR_pct": round(stock_or_pct, 2),
            "ORL": orl, "Trigger": trig, "Actual_Fill": actual_fill, "SL": sl, "TP": tp,
            "Exit": exit_price, "PnL": pnl, "Status": status, "Note": "Post-entry evaluated"
        })

df_trades = pd.DataFrame(trades)
df_trades.to_csv("short_engine_v2_1_trades.csv", index=False)

print("=" * 105)
print("  SHORT ENGINE v2.1 — CORRECTED SIMULATION (TP EXIT SLIPPAGE + GAP-THROUGH + NO SAME-BAR)")
print("=" * 105)
print(df_trades[["Date", "Benchmark_OR_pct", "Sector", "Symbol", "Trigger", "Actual_Fill", "Exit", "PnL", "Status"]].to_string(index=False))

exec_trades = df_trades[df_trades["Status"] != "UNTRIGGERED"]
wins = len(exec_trades[exec_trades["PnL"] > 0])
losses = len(exec_trades[exec_trades["PnL"] < 0])
total_pnl = exec_trades["PnL"].sum()
wr = (wins / len(exec_trades)) * 100 if len(exec_trades) > 0 else 0

print("\n" + "=" * 105)
print(f"Total Sessions: {len(df_trades)} | Executed Shorts: {len(exec_trades)} | Untriggered: {len(df_trades) - len(exec_trades)}")
print(f"Wins: {wins} | Losses: {losses} | Realized Win Rate: {wr:.1f}% | Corrected Net PnL: {total_pnl:+.2f}%")
print("Saved complete trade-level records to short_engine_v2_1_trades.csv")
print("=" * 105)
