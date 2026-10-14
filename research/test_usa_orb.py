import yfinance as yf
import pandas as pd
import numpy as np

# Load 1-month 2m data
df = yf.Ticker("SPY").history(period="1mo", interval="2m")
days = sorted(list(set(df.index.strftime('%Y-%m-%d'))))
COST = 120.0
FEE = 1.30

print(f"=== TESTING QUANTITATIVE VARIANTS ACROSS {len(days)} TRADING DAYS ===")

def test_orb_strategy(sl_pct=0.30, tp_pct=0.60, use_trail=False):
    """
    Opening Range Breakout (ORB) (09:30 - 10:00 ET):
    - If SPY breaks above 10:00 AM High -> Buy Bull Call Spread
    - If SPY breaks below 10:00 AM Low -> Buy Bear Put Spread
    - If no breakout -> No trade (Flat / Zero Risk)
    """
    trades = []
    for d in days:
        day_df = df[df.index.strftime('%Y-%m-%d') == d]
        orb_bars = day_df[(day_df.index.strftime('%H:%M') >= '09:30') & (day_df.index.strftime('%H:%M') < '10:00')]
        if orb_bars.empty:
            continue
        orb_high = orb_bars['High'].max()
        orb_low = orb_bars['Low'].min()
        
        post_orb = day_df[(day_df.index.strftime('%H:%M') >= '10:00') & (day_df.index.strftime('%H:%M') <= '15:45')]
        
        entered = False
        direction = None
        entry_spot = None
        entry_time = None
        
        for idx, row in post_orb.iterrows():
            t_str = idx.strftime('%H:%M')
            if not entered:
                if row['Close'] > orb_high:
                    entered = True
                    direction = "BULL_CALL"
                    entry_spot = row['Close']
                    entry_time = t_str
                    entry_idx = idx
                    break
                elif row['Close'] < orb_low:
                    entered = True
                    direction = "BEAR_PUT"
                    entry_spot = row['Close']
                    entry_time = t_str
                    entry_idx = idx
                    break
                    
        if not entered:
            # Clean skip on chop days!
            trades.append({"Date": d, "Direction": "NO_TRADE", "Reason": "CHOP_NO_BREAKOUT", "PnL_USD": 0.0})
            continue
            
        # Manage trade
        trade_bars = day_df[(day_df.index >= entry_idx) & (day_df.index.strftime('%H:%M') <= '15:45')]
        exit_time = None
        exit_spot = None
        reason = None
        pnl_usd = 0.0
        
        tp_spot = entry_spot * (1 + tp_pct/100) if direction == "BULL_CALL" else entry_spot * (1 - tp_pct/100)
        sl_spot = entry_spot * (1 - sl_pct/100) if direction == "BULL_CALL" else entry_spot * (1 + sl_pct/100)
        
        for idx, row in trade_bars.iterrows():
            t_str = idx.strftime('%H:%M')
            if direction == "BULL_CALL":
                if row['Low'] <= sl_spot:
                    reason = "STOP_LOSS"
                    pnl_usd = -(COST * 0.30) - FEE
                    break
                if row['High'] >= tp_spot:
                    reason = "TAKE_PROFIT"
                    pnl_usd = (COST * 0.50) - FEE
                    break
            else:
                if row['High'] >= sl_spot:
                    reason = "STOP_LOSS"
                    pnl_usd = -(COST * 0.30) - FEE
                    break
                if row['Low'] <= tp_spot:
                    reason = "TAKE_PROFIT"
                    pnl_usd = (COST * 0.50) - FEE
                    break
            if t_str >= '15:45':
                reason = "EOD_SQUARE_OFF"
                pnl_usd = 0.0 - FEE
                break
                
        if not reason:
            reason = "EOD_SQUARE_OFF"
            pnl_usd = 0.0 - FEE
            
        trades.append({"Date": d, "Direction": direction, "Reason": reason, "PnL_USD": round(pnl_usd, 2)})
        
    res = pd.DataFrame(trades)
    tot_pnl = res['PnL_USD'].sum()
    t_cnt = len(res[res['Direction'] != 'NO_TRADE'])
    wins = len(res[res['Reason'] == 'TAKE_PROFIT'])
    losses = len(res[res['Reason'] == 'STOP_LOSS'])
    eods = len(res[res['Reason'] == 'EOD_SQUARE_OFF'])
    skips = len(res[res['Direction'] == 'NO_TRADE'])
    win_rate = (wins / t_cnt * 100) if t_cnt > 0 else 0
    return {
        "Config": f"ORB SL={sl_pct}% TP={tp_pct}%",
        "Total_Days": len(days),
        "Traded_Days": t_cnt,
        "Skips (Chop)": skips,
        "Wins (TP)": wins,
        "Losses (SL)": losses,
        "EOD Exits": eods,
        "Win_Rate_%": round(win_rate, 1),
        "Total_PnL_USD": round(tot_pnl, 2)
    }

configs = [
    (0.30, 0.60),
    (0.40, 0.60),
    (0.50, 0.50),
    (0.30, 0.40),
    (0.40, 0.40)
]

summary = []
for sl, tp in configs:
    summary.append(test_orb_strategy(sl, tp))

s_df = pd.DataFrame(summary)
print("\n=== OPENING RANGE BREAKOUT (ORB) VARIANT MATRIX ===")
print(s_df.to_string(index=False))
