"""
audit_short_engine_v2.py
Corrected Short Engine v2 Baseline Audit.
Strict constraints enforced:
1. Exactly 20 completed 1-minute OR bars (len == 20, 09:15-09:34).
2. Minimum 3 valid constituents per sector.
3. Conservative execution: 5 bps slippage penalty on entry and exit.
4. Same-bar ambiguity: If SL and TP are touched in the same minute, assume SL hit first.
5. Generates trade-level CSV: short_engine_v2_trades.csv.
"""
import pickle
import pandas as pd
import numpy as np

# Load exchange cache
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

SLIPPAGE_BPS = 0.0005  # 5 bps realistic execution penalty

for d in dates:
    n_day = nifty[nifty.index.strftime('%Y-%m-%d') == d]
    n_or = n_day.between_time("09:15", "09:34")
    # Strict 20 completed bars rule
    if len(n_or) != 20:
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
            # Strict 20 completed bars rule
            if len(s_or) == 20:
                o = s_or["Open"].iloc[0]
                c = s_or["Close"].iloc[-1]
                l = s_or["Low"].min()
                m = ((c - o) / o) * 100

                # Compute VWAP for OR
                vol = s_or["Volume"] if "Volume" in s_or.columns else pd.Series(1, index=s_or.index)
                typical_price = (s_or["High"] + s_or["Low"] + s_or["Close"]) / 3.0
                vwap = float((typical_price * vol).sum() / vol.sum()) if vol.sum() > 0 else c

                moves.append(m)
                info = {"sym": s, "move": m, "orl": l, "vwap": round(vwap, 2), "day": day_s, "or_bars": s_or}
                stock_map[s] = info
                valid_stocks.append(info)

        # Enforce minimum 3 valid constituents per sector
        if len(valid_stocks) >= 3:
            breadth = (sum(1 for x in moves if x < 0) / len(moves)) * 100
            avg_m = np.mean(moves)
            sec_stats.append({"sec": sec, "breadth": breadth, "avg_m": avg_m, "stocks": valid_stocks})

    if not sec_stats:
        continue

    # Rank sectors: Weakest sector has highest % red and lowest average move
    sec_stats.sort(key=lambda x: (x["breadth"], -x["avg_m"]), reverse=True)
    weakest_sec = sec_stats[0]

    # Select weakest qualified stock in weakest sector
    cands = weakest_sec["stocks"]
    cands.sort(key=lambda x: x["move"])
    weakest_stock = cands[0]

    sym = weakest_stock["sym"]
    orl = weakest_stock["orl"]
    stock_or_pct = weakest_stock["move"]
    vwap_val = weakest_stock["vwap"]

    # Entry trigger: ORL * 0.9997
    trig = round(orl * 0.9997, 2)
    # Realistic actual fill with 5 bps slippage (fills LOWER on short entry)
    actual_fill = round(trig * (1.0 - SLIPPAGE_BPS), 2)

    # Risk Levels (relative to trigger)
    sl = round(trig * 1.0100, 2)     # +1.00% Hard SL
    tp = round(trig * 0.9800, 2)     # -2.00% TP (2:1 reward)

    post = weakest_stock["day"].between_time("09:45", "15:29")
    trig_bars = post[post["Low"] <= trig]

    if trig_bars.empty:
        trades.append({
            "Date": d, "Benchmark_OR_pct": round(n_move, 2), "Sector": weakest_sec["sec"],
            "Symbol": sym.replace(".NS", ""), "Stock_OR_pct": round(stock_or_pct, 2),
            "ORL": orl, "VWAP": vwap_val, "RVOL": "1.0x", "Entry": trig, "Actual_Fill": trig,
            "SL": sl, "TP": tp, "Exit": 0.0, "PnL": 0.00, "Status": "UNTRIGGERED"
        })
    else:
        t_entry = trig_bars.index[0]
        in_t = post.loc[t_entry:]

        # Intraday bars checking SL and TP
        sl_bars = in_t[in_t["High"] >= sl]
        tp_bars = in_t[in_t["Low"] <= tp]

        if not sl_bars.empty and not tp_bars.empty:
            # Conservative tie-break: if same minute, assume SL was hit first!
            if sl_bars.index[0] <= tp_bars.index[0]:
                exit_price = round(sl * (1.0 + SLIPPAGE_BPS), 2)
                pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
                status = "SL HIT"
            else:
                exit_price = round(tp * (1.0 - SLIPPAGE_BPS), 2)
                pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
                status = "TP HIT"
        elif not sl_bars.empty:
            exit_price = round(sl * (1.0 + SLIPPAGE_BPS), 2)
            pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
            status = "SL HIT"
        elif not tp_bars.empty:
            exit_price = round(tp * (1.0 - SLIPPAGE_BPS), 2)
            pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
            status = "TP HIT"
        else:
            last_c = float(in_t["Close"].iloc[-1])
            exit_price = round(last_c * (1.0 + SLIPPAGE_BPS), 2)
            pnl = round(((actual_fill - exit_price) / actual_fill) * 100, 2)
            status = "EOD EXIT"

        trades.append({
            "Date": d, "Benchmark_OR_pct": round(n_move, 2), "Sector": weakest_sec["sec"],
            "Symbol": sym.replace(".NS", ""), "Stock_OR_pct": round(stock_or_pct, 2),
            "ORL": orl, "VWAP": vwap_val, "RVOL": "1.0x", "Entry": trig, "Actual_Fill": actual_fill,
            "SL": sl, "TP": tp, "Exit": exit_price, "PnL": pnl, "Status": status
        })

df_trades = pd.DataFrame(trades)
df_trades.to_csv("short_engine_v2_trades.csv", index=False)

print("=" * 105)
print("  SHORT ENGINE v2 — DETERMINISTIC BASELINE AUDIT (EXACTLY 20 BARS, CONSERVATIVE SLIPPAGE)")
print("=" * 105)
print(df_trades[["Date", "Benchmark_OR_pct", "Sector", "Symbol", "Entry", "Actual_Fill", "Exit", "PnL", "Status"]].to_string(index=False))

exec_trades = df_trades[df_trades["Status"] != "UNTRIGGERED"]
wins = len(exec_trades[exec_trades["PnL"] > 0])
losses = len(exec_trades[exec_trades["PnL"] < 0])
total_pnl = exec_trades["PnL"].sum()
wr = (wins / len(exec_trades)) * 100 if len(exec_trades) > 0 else 0

print("\n" + "=" * 105)
print(f"Total Sessions: {len(df_trades)} | Executed Shorts: {len(exec_trades)} | Untriggered: {len(df_trades) - len(exec_trades)}")
print(f"Wins: {wins} | Losses: {losses} | Realized Win Rate: {wr:.1f}% | Slippage-Adjusted Net PnL: {total_pnl:+.2f}%")
print("Saved complete trade-level records to short_engine_v2_trades.csv")
print("=" * 105)
