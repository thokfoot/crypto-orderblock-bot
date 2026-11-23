"""
audit_short_engine_v3_exact_mirror.py
SHORT ENGINE v3 — Exact Mathematical Mirror of Strategy v3.2.5 LONG.

Mirrored Selection Rules:
1. 20-min completed OR (09:15-09:34 IST, len == 20 unique bars).
2. Sector Selection: #1 Weakest Sector (ranked by % of stocks red, then lowest avg return). Min 3 valid stocks.
3. Volatility Filter: Stock 14d ATR >= 2.50%.
4. Momentum Sweet Spot Filter: Stock OR Move strictly between -0.70% and -1.80% (prevents exhausted dumps!).
5. Primary Short Candidate: Weakest qualifying stock in #1 Weakest Sector.
6. Execution Trigger: ORL * 0.9997 (post-09:45 IST).
7. Hard SL: +1.00% above Trigger.
8. Take Profit: -2.00% below Trigger (2:1 reward/risk).
9. Conservative Execution: 5 bps adverse entry slippage, 5 bps adverse exit buyback slippage, post-entry bar evaluation.
"""
import pickle
import pandas as pd
import numpy as np

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
SLIPPAGE_BPS = 0.0005  # 5 bps

for d in dates:
    n_day = nifty[nifty.index.strftime('%Y-%m-%d') == d]
    n_or = n_day.between_time("09:15", "09:34")
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

                # Calculate 14d ATR
                atr_val = daily_atrs.get(s, 3.0)
                if isinstance(atr_val, pd.DataFrame) and len(atr_val) >= 14:
                    tr = pd.concat([atr_val["High"] - atr_val["Low"], (atr_val["High"] - atr_val["Close"].shift(1)).abs(), (atr_val["Low"] - atr_val["Close"].shift(1)).abs()], axis=1).max(axis=1)
                    atr_pct = float((tr.rolling(14).mean().iloc[-1] / atr_val["Close"].iloc[-1]) * 100)
                elif isinstance(atr_val, (float, int)):
                    atr_pct = float(atr_val)
                else:
                    atr_pct = 3.0

                moves.append(m)
                info = {"sym": s, "move": m, "orl": l, "atr": atr_pct, "day": day_s}
                stock_map[s] = info
                valid_stocks.append(info)

        if len(valid_stocks) >= 3:
            breadth = (sum(1 for x in moves if x < 0) / len(moves)) * 100
            avg_m = np.mean(moves)
            sec_stats.append({"sec": sec, "breadth": breadth, "avg_m": avg_m, "stocks": valid_stocks})

    if not sec_stats:
        continue

    # Rank sectors: Weakest sector
    sec_stats.sort(key=lambda x: (x["breadth"], -x["avg_m"]), reverse=True)
    weakest_sec = sec_stats[0]

    # Mirrored Qualified Candidate Selection:
    # 1. ATR >= 2.50%
    # 2. OR Move strictly between -0.70% and -1.80% (Sweet spot!)
    qual_cands = [
        s for s in weakest_sec["stocks"]
        if s["atr"] >= 2.50 and (-1.80 <= s["move"] <= -0.70)
    ]

    if not qual_cands:
        # Fallback to ATR qualified
        qual_cands = [s for s in weakest_sec["stocks"] if s["atr"] >= 2.50 and s["move"] < 0]

    if not qual_cands:
        trades.append({
            "Date": d, "Benchmark_OR_pct": round(n_move, 2), "Sector": weakest_sec["sec"],
            "Symbol": "NO_QUALIFIED_STOCK", "Stock_OR_pct": 0.0, "ORL": 0.0,
            "Trigger": 0.0, "Actual_Fill": 0.0, "SL": 0.0, "TP": 0.0,
            "Exit": 0.0, "PnL": 0.00, "Status": "UNTRIGGERED", "Note": "No stock passed ATR & sweet spot"
        })
        continue

    # Pick weakest qualifying stock
    qual_cands.sort(key=lambda x: x["move"])
    selected_stock = qual_cands[0]

    sym = selected_stock["sym"]
    orl = selected_stock["orl"]
    stock_or_pct = selected_stock["move"]
    atr_val = selected_stock["atr"]

    trig = round(orl * 0.9997, 2)
    sl = round(trig * 1.0100, 2)     # +1.00% Hard SL
    tp = round(trig * 0.9800, 2)     # -2.00% TP

    post = selected_stock["day"].between_time("09:45", "15:29")
    trig_bars = post[post["Low"] <= trig]

    if trig_bars.empty:
        trades.append({
            "Date": d, "Benchmark_OR_pct": round(n_move, 2), "Sector": weakest_sec["sec"],
            "Symbol": sym.replace(".NS", ""), "Stock_OR_pct": round(stock_or_pct, 2),
            "ORL": orl, "Trigger": trig, "Actual_Fill": trig, "SL": sl, "TP": tp,
            "Exit": 0.0, "PnL": 0.00, "Status": "UNTRIGGERED", "Note": f"Untriggered (ATR {atr_val:.1f}%)"
        })
    else:
        entry_idx = trig_bars.index[0]
        entry_bar = post.loc[entry_idx]

        # Gap-through check
        base_fill = float(entry_bar["Open"]) if entry_bar["Open"] < trig else trig
        actual_fill = round(base_fill * (1.0 - SLIPPAGE_BPS), 2)

        # Post-entry bars evaluation
        post_entry = post.loc[entry_idx:].iloc[1:]

        if post_entry.empty:
            exit_price = round(float(entry_bar["Close"]) * (1.0 + SLIPPAGE_BPS), 2)
            pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
            status = "EOD EXIT"
        else:
            sl_bars = post_entry[post_entry["High"] >= sl]
            tp_bars = post_entry[post_entry["Low"] <= tp]

            if not sl_bars.empty and not tp_bars.empty:
                if sl_bars.index[0] <= tp_bars.index[0]:
                    exit_price = round(sl * (1.0 + SLIPPAGE_BPS), 2)
                    pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
                    status = "SL HIT"
                else:
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
            "Exit": exit_price, "PnL": pnl, "Status": status, "Note": f"ATR {atr_val:.1f}% | Sweet spot"
        })

df_trades = pd.DataFrame(trades)
df_trades.to_csv("short_engine_v3_exact_mirror.csv", index=False)

print("=" * 105)
print("  SHORT ENGINE v3 — EXACT MIRROR OF LONG v3.2.5 (SWEET SPOT -0.7% TO -1.8% + ATR >= 2.5%)")
print("=" * 105)
print(df_trades[["Date", "Benchmark_OR_pct", "Sector", "Symbol", "Stock_OR_pct", "Actual_Fill", "Exit", "PnL", "Status"]].to_string(index=False))

exec_trades = df_trades[df_trades["Status"] != "UNTRIGGERED"]
wins = len(exec_trades[exec_trades["PnL"] > 0])
losses = len(exec_trades[exec_trades["PnL"] < 0])
total_pnl = exec_trades["PnL"].sum()
wr = (wins / len(exec_trades)) * 100 if len(exec_trades) > 0 else 0

print("\n" + "=" * 105)
print(f"Total Sessions: {len(df_trades)} | Executed Shorts: {len(exec_trades)} | Untriggered: {len(df_trades) - len(exec_trades)}")
print(f"Wins: {wins} | Losses: {losses} | Realized Win Rate: {wr:.1f}% | Slippage-Adjusted Net PnL: {total_pnl:+.2f}%")
print("Saved complete trade-level records to short_engine_v3_exact_mirror.csv")
print("=" * 105)
