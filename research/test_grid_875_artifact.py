"""
research/test_grid_875_artifact.py
==================================
Documents the 875-Combination Parameter Grid Search on USA SPY Options
and mathematically explains why the "+77.1%" result is a synthetic Greeks artifact.

Audit Conclusion:
Awarding a full +$60 (+50%) credit whenever SPY moves +0.30% creates synthetic
"fake money" because real option delta at midday is only ~0.25-0.35 (actual gain ~$15-$25).
"""

import yfinance as yf
import pandas as pd
import numpy as np

def run_grid_audit():
    print("Fetching SPY 5m data for Grid Analysis...")
    df = yf.Ticker("SPY").history(period="60d", interval="5m")
    days = sorted(list(set(df.index.strftime('%Y-%m-%d'))))
    # Filter completed sessions
    today_str = pd.Timestamp.now(tz='America/New_York').strftime('%Y-%m-%d')
    days = [d for d in days if d < today_str]
    print(f"Analyzing {len(days)} Completed Trading Sessions ({days[0]} to {days[-1]})")

    COST = 120.0
    FEE = 1.30

    # Test the "Best Grid" combination reported:
    # Entry: 10:00 ET, TP: +0.30% (awarding full $60), SL: None, Exit: 15:00 ET
    trades = []
    for d in days:
        sub = df[df.index.strftime('%Y-%m-%d') == d]
        cands = sub[sub.index.strftime('%H:%M') >= '10:00']
        if cands.empty:
            continue
        e_bar = cands.iloc[0]
        e_spot = e_bar['Close']
        tp_spot = e_spot * 1.0030
        sess = sub[(sub.index >= e_bar.name) & (sub.index.strftime('%H:%M') <= '15:00')]

        hit_tp = False
        for idx, r in sess.iterrows():
            if r['High'] >= tp_spot:
                hit_tp = True
                break
        if hit_tp:
            # The artifact: granting full +50% ($60) on a tiny 0.30% move
            pnl = (COST * 0.50) - FEE
            reason = "TAKE_PROFIT_030"
        else:
            last_r = sess.iloc[-1]
            move = ((last_r['Close'] - e_spot) / e_spot) * 100.0
            spread_pct = min(50.0, move * (50.0 / 0.60)) if move >= 0 else max(-100.0, move * (50.0 / 0.60))
            pnl = (COST * (spread_pct / 100.0)) - FEE
            reason = "EXIT_1500"
        trades.append({"Date": d, "Reason": reason, "PnL": round(pnl, 2)})

    res = pd.DataFrame(trades)
    tp_hits = len(res[res['Reason'] == 'TAKE_PROFIT_030'])
    tot_pnl = res['PnL'].sum()

    print("\n=================================================================================")
    print("                     875-GRID ARTIFACT DEMONSTRATION                             ")
    print("=================================================================================")
    print(f"Total Completed Sessions: {len(res)}")
    print(f"Take-Profit (+0.30% move credited as +$60): {tp_hits} / {len(res)} ({tp_hits/len(res)*100:.1f}%)")
    print(f"Grid Total PnL: ${tot_pnl:+.2f} USD ({tot_pnl/2000.0*100:+.2f}% on $2,000 capital)")
    print("\nWhy This Is a Synthetic Greeks Artifact:")
    print("1. Real 773/775 spread delta at 12:00 PM is ~0.25 to 0.35.")
    print("2. A +0.30% ($2.30) underlying spot move only yields +$15 to +$25 in real options markets.")
    print("3. Granting +$60 (+50%) on a tiny 0.30% move inflates backtest PnL artificially.")
    print("4. Council & Peer Verdict: Reject grid mining; maintain realistic +0.60% TP target.")
    print("=================================================================================\n")

if __name__ == "__main__":
    run_grid_audit()
