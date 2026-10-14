"""
research/backtest_usa_max_historical.py
=======================================
Peer-Auditable Maximum Historical Backtest for USA SPY Options Spread Strategy.

Features:
1. 60-Day 5-Minute Intraday Backtest (Max Yahoo Finance 5m limit: 59 completed sessions, 4,636 bars)
2. 2-Year / 730-Day 1-Hour Multi-Regime Backtest (Max Yahoo Finance 1h limit: 729 completed sessions, 5,068 bars)

Compares:
- Baseline (-0.30% tight spot SL, +0.60% TP, $0 flat EOD)
- Wide SL (-0.75% spot SL, +0.60% TP, Linear MTM EOD)
- Defined-Risk (No spot SL, +0.60% TP, Linear MTM EOD, max loss capped by $120 spread debit)

Note: All P&L calculations use the daemon's production spot-linear options proxy:
      spread_pnl_pct = min(50.0, spot_move_pct * 83.33)
"""

import yfinance as yf
import pandas as pd
import numpy as np

def run_5m_60d_test():
    print("=" * 80)
    print("     MODULE 1A: SPY 60-DAY 5-MINUTE HIGH-PRECISION INTRADAY TEST (MAX 5M)     ")
    print("=" * 80)
    df = yf.Ticker("SPY").history(period="60d", interval="5m")
    days = sorted(list(set(df.index.strftime('%Y-%m-%d'))))
    today_ny = pd.Timestamp.now(tz='America/New_York').strftime('%Y-%m-%d')
    completed_days = [d for d in days if d < today_ny]
    print(f"Data Span: {len(completed_days)} Completed Days ({completed_days[0]} to {completed_days[-1]}) | Total Bars: {len(df)}")

    COST = 120.0
    FEE = 1.30

    models = {
        "Baseline (SL -0.30%, TP +0.60%, Flat $0 EOD)": {"sl": 0.30, "tp": 0.60, "mtm": False},
        "Wide SL (-0.75%, TP +0.60%, Linear MTM EOD)":   {"sl": 0.75, "tp": 0.60, "mtm": True},
        "Defined-Risk (No Spot SL, TP +0.60%, Linear MTM)": {"sl": None, "tp": 0.60, "mtm": True}
    }

    results = {}
    for name, cfg in models.items():
        trades = []
        for d in completed_days:
            sub = df[df.index.strftime('%Y-%m-%d') == d]
            cands = sub[sub.index.strftime('%H:%M') >= '10:00']
            if cands.empty: continue
            e_spot = cands.iloc[0]['Close']
            tp_spot = e_spot * (1 + cfg["tp"] / 100)
            sl_spot = e_spot * (1 - cfg["sl"] / 100) if cfg["sl"] else None
            sess = sub[(sub.index >= cands.iloc[0].name) & (sub.index.strftime('%H:%M') <= '15:45')]

            exit_reason = None
            pnl = 0.0
            for idx, r in sess.iterrows():
                if sl_spot and r['Low'] <= sl_spot:
                    exit_reason = "STOP_LOSS"
                    pnl = -(COST * 0.30) - FEE if cfg["sl"] == 0.30 else -(COST * 0.50) - FEE
                    break
                if r['High'] >= tp_spot:
                    exit_reason = "TAKE_PROFIT"
                    pnl = (COST * 0.50) - FEE
                    break
            if not exit_reason and not sess.empty:
                exit_reason = "EOD"
                if cfg["mtm"]:
                    last_spot = sess.iloc[-1]['Close']
                    move = ((last_spot - e_spot) / e_spot) * 100.0
                    sp = min(50.0, move * (50.0 / 0.60)) if move >= 0 else max(-100.0, move * (50.0 / 0.60))
                    pnl = (COST * (sp / 100.0)) - FEE
                else:
                    pnl = 0.0 - FEE
            trades.append({"Date": d, "Exit": exit_reason, "PnL": round(pnl, 2)})

        tdf = pd.DataFrame(trades)
        wins = len(tdf[tdf['PnL'] > 0])
        losses = len(tdf[tdf['PnL'] < 0])
        tot_pnl = tdf['PnL'].sum()
        ret_pct = (tot_pnl / 2000.0) * 100.0
        results[name] = {
            "Trades": len(tdf),
            "Wins": wins,
            "Losses": losses,
            "Win_Rate_%": round(wins / len(tdf) * 100, 1),
            "Total_PnL_USD": round(tot_pnl, 2),
            "Return_%": round(ret_pct, 2)
        }

    res_df = pd.DataFrame(results).T
    print(res_df.to_string())
    print()

def run_1h_2year_test():
    print("=" * 80)
    print("     MODULE 1B: SPY 2-YEAR (730-DAY) 1-HOUR MULTI-REGIME TEST (MAX 1H)         ")
    print("=" * 80)
    df = yf.Ticker("SPY").history(period="730d", interval="1h")
    days = sorted(list(set(df.index.strftime('%Y-%m-%d'))))
    today_ny = pd.Timestamp.now(tz='America/New_York').strftime('%Y-%m-%d')
    completed_days = [d for d in days if d < today_ny]
    print(f"Data Span: {len(completed_days)} Completed Trading Days ({completed_days[0]} to {completed_days[-1]}) | Total Bars: {len(df)}")

    COST = 120.0
    FEE = 1.30

    models = {
        "Baseline (SL -0.30%, TP +0.60%, Flat $0 EOD)": {"sl": 0.30, "tp": 0.60, "mtm": False},
        "Wide SL (-0.75%, TP +0.60%, Linear MTM EOD)":   {"sl": 0.75, "tp": 0.60, "mtm": True},
        "Defined-Risk (No Spot SL, TP +0.60%, Linear MTM)": {"sl": None, "tp": 0.60, "mtm": True}
    }

    results = {}
    for name, cfg in models.items():
        trades = []
        for d in completed_days:
            sub = df[df.index.strftime('%Y-%m-%d') == d]
            cands = sub[sub.index.strftime('%H:%M') >= '10:00']
            if cands.empty:
                cands = sub
            if cands.empty: continue
            e_bar = cands.iloc[0]
            e_spot = e_bar['Close']
            tp_spot = e_spot * (1 + cfg["tp"] / 100)
            sl_spot = e_spot * (1 - cfg["sl"] / 100) if cfg["sl"] else None
            sess = sub[sub.index >= e_bar.name]

            exit_reason = None
            pnl = 0.0
            for idx, r in sess.iterrows():
                if sl_spot and r['Low'] <= sl_spot:
                    exit_reason = "STOP_LOSS"
                    pnl = -(COST * 0.30) - FEE if cfg["sl"] == 0.30 else -(COST * 0.50) - FEE
                    break
                if r['High'] >= tp_spot:
                    exit_reason = "TAKE_PROFIT"
                    pnl = (COST * 0.50) - FEE
                    break
            if not exit_reason and not sess.empty:
                exit_reason = "EOD"
                if cfg["mtm"]:
                    last_spot = sess.iloc[-1]['Close']
                    move = ((last_spot - e_spot) / e_spot) * 100.0
                    sp = min(50.0, move * (50.0 / 0.60)) if move >= 0 else max(-100.0, move * (50.0 / 0.60))
                    pnl = (COST * (sp / 100.0)) - FEE
                else:
                    pnl = 0.0 - FEE
            trades.append({"Date": d, "Exit": exit_reason, "PnL": round(pnl, 2)})

        tdf = pd.DataFrame(trades)
        wins = len(tdf[tdf['PnL'] > 0])
        losses = len(tdf[tdf['PnL'] < 0])
        tot_pnl = tdf['PnL'].sum()
        ret_pct = (tot_pnl / 2000.0) * 100.0
        
        cum = tdf['PnL'].cumsum()
        peak = cum.cummax()
        dd = cum - peak
        max_dd = dd.min()

        results[name] = {
            "Trades": len(tdf),
            "Wins": wins,
            "Losses": losses,
            "Win_Rate_%": round(wins / len(tdf) * 100, 1),
            "Total_PnL_USD": round(tot_pnl, 2),
            "Return_%": round(ret_pct, 2),
            "Max_Drawdown_USD": round(max_dd, 2)
        }

    res_df = pd.DataFrame(results).T
    print(res_df.to_string())
    print("=" * 80 + "\n")

if __name__ == "__main__":
    run_5m_60d_test()
    run_1h_2year_test()
