import yfinance as yf
import pandas as pd
import numpy as np

def run_1mo_backtest():
    print("Fetching 1-Month 2-Minute SPY data...")
    df = yf.Ticker("SPY").history(period="1mo", interval="2m")
    days = sorted(list(set(df.index.strftime('%Y-%m-%d'))))
    print(f"Total Trading Days in 1 Month: {len(days)} ({days[0]} to {days[-1]})\n")

    # -------------------------------------------------------------
    # 1. BASELINE STRATEGY: Current Production Code
    # Blind Bull Call Spread at 10:00 AM ET, TP +0.60%, SL -0.30%, EOD 15:45
    # -------------------------------------------------------------
    COST = 120.0
    FEE = 1.30
    
    baseline_trades = []
    
    for d in days:
        day_df = df[df.index.strftime('%Y-%m-%d') == d]
        # Filter from 10:00 AM ET
        entry_candidates = day_df[day_df.index.strftime('%H:%M') >= '10:00']
        if entry_candidates.empty:
            continue
            
        entry_bar = entry_candidates.iloc[0]
        entry_time = entry_bar.name.strftime('%H:%M')
        entry_spot = entry_bar['Close']
        
        tp_spot = entry_spot * 1.0060
        sl_spot = entry_spot * 0.9970
        
        # Track until 15:45
        session = day_df[(day_df.index >= entry_bar.name) & (day_df.index.strftime('%H:%M') <= '15:45')]
        
        exit_time = None
        exit_spot = None
        reason = None
        pnl_pct = 0.0
        pnl_usd = 0.0
        
        for idx, row in session.iterrows():
            t_str = idx.strftime('%H:%M')
            # Check SL first (conservative intrabar order)
            if row['Low'] <= sl_spot:
                exit_time = t_str
                exit_spot = sl_spot
                reason = "STOP_LOSS"
                pnl_pct = -30.0
                pnl_usd = -(COST * 0.30) - FEE
                break
            # Check TP
            if row['High'] >= tp_spot:
                exit_time = t_str
                exit_spot = tp_spot
                reason = "TAKE_PROFIT"
                pnl_pct = +50.0
                pnl_usd = (COST * 0.50) - FEE
                break
            # EOD Square-off at 15:45
            if t_str >= '15:45':
                exit_time = t_str
                exit_spot = row['Close']
                reason = "EOD_SQUARE_OFF"
                # Code rule: flat PnL 0% - fee
                pnl_pct = 0.0
                pnl_usd = 0.0 - FEE
                break
                
        if not reason and not session.empty:
            last_row = session.iloc[-1]
            exit_time = last_row.name.strftime('%H:%M')
            exit_spot = last_row['Close']
            reason = "EOD_SQUARE_OFF"
            pnl_pct = 0.0
            pnl_usd = 0.0 - FEE
            
        # Also compute true mark-to-market PnL at exit
        spot_move = ((exit_spot - entry_spot) / entry_spot) * 100.0
        if spot_move >= 0:
            mtm_spread_pct = min(50.0, spot_move * (50.0 / 0.60))
        else:
            mtm_spread_pct = max(-30.0, spot_move * (30.0 / 0.30))
        mtm_pnl_usd = (COST * (mtm_spread_pct / 100.0)) - FEE
            
        baseline_trades.append({
            "Date": d,
            "Entry_Time": entry_time,
            "Entry_Spot": round(entry_spot, 2),
            "Exit_Time": exit_time,
            "Exit_Spot": round(exit_spot, 2),
            "Spot_Move_%": round(spot_move, 2),
            "Reason": reason,
            "PnL_Rule_USD": round(pnl_usd, 2),
            "PnL_MTM_USD": round(mtm_pnl_usd, 2)
        })

    b_df = pd.DataFrame(baseline_trades)
    
    print("=========================================================================================")
    print("                    1-MONTH BACKTEST: CURRENT PRODUCTION BOT STRATEGY                    ")
    print("                     (Blind Bull Call Spread @ 10:00 AM ET, Max 1 Trade/Day)            ")
    print("=========================================================================================")
    print(b_df.to_string(index=False))
    
    total_trades = len(b_df)
    tp_cnt = len(b_df[b_df['Reason'] == 'TAKE_PROFIT'])
    sl_cnt = len(b_df[b_df['Reason'] == 'STOP_LOSS'])
    eod_cnt = len(b_df[b_df['Reason'] == 'EOD_SQUARE_OFF'])
    
    rule_pnl = b_df['PnL_Rule_USD'].sum()
    mtm_pnl = b_df['PnL_MTM_USD'].sum()
    
    gross_wins = b_df[b_df['PnL_Rule_USD'] > 0]['PnL_Rule_USD'].sum()
    gross_losses = abs(b_df[b_df['PnL_Rule_USD'] < 0]['PnL_Rule_USD'].sum())
    pf = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 999.0
    
    print("\n------------------------- PERFORMANCE SUMMARY -------------------------")
    print(f"Total Trading Days:       {total_trades}")
    print(f"Take Profit (+50% / $60): {tp_cnt} ({tp_cnt/total_trades*100:.1f}%)")
    print(f"Stop Loss (-30% / -$36):  {sl_cnt} ({sl_cnt/total_trades*100:.1f}%)")
    print(f"EOD Square-Off (Flat):    {eod_cnt} ({eod_cnt/total_trades*100:.1f}%)")
    print(f"Win Rate (TPs / Total):   {tp_cnt/total_trades*100:.1f}%")
    print(f"Profit Factor:            {pf}")
    print(f"Total Banked P&L (Rule):  ${rule_pnl:+.2f} USD ({rule_pnl/2000.0*100:+.2f}% on $2,000 capital)")
    print(f"Total Banked P&L (MTM):   ${mtm_pnl:+.2f} USD ({mtm_pnl/2000.0*100:+.2f}% on $2,000 capital)")
    print("------------------------------------------------------------------------\n")

    # -------------------------------------------------------------
    # 2. ENHANCED STRATEGY: VWAP / Trend Filter
    # If 10:00 AM Spot > VWAP -> Bull Call Spread
    # If 10:00 AM Spot < VWAP -> Bear Put Spread
    # -------------------------------------------------------------
    enhanced_trades = []
    
    for d in days:
        day_df = df[df.index.strftime('%Y-%m-%d') == d]
        
        # Calculate session VWAP up to 10:00 AM
        am_bars = day_df[(day_df.index.strftime('%H:%M') >= '09:30') & (day_df.index.strftime('%H:%M') <= '10:00')]
        if am_bars.empty:
            continue
            
        typical_price = (am_bars['High'] + am_bars['Low'] + am_bars['Close']) / 3.0
        cum_vp = (typical_price * am_bars['Volume']).sum()
        cum_vol = am_bars['Volume'].sum()
        vwap_1000 = cum_vp / cum_vol if cum_vol > 0 else am_bars.iloc[-1]['Close']
        
        entry_bar = day_df[day_df.index.strftime('%H:%M') >= '10:00'].iloc[0]
        entry_time = entry_bar.name.strftime('%H:%M')
        entry_spot = entry_bar['Close']
        
        # Determine Direction
        if entry_spot >= vwap_1000:
            direction = "BULL_CALL"
            tp_spot = entry_spot * 1.0060
            sl_spot = entry_spot * 0.9970
        else:
            direction = "BEAR_PUT"
            tp_spot = entry_spot * 0.9940  # Down 0.60%
            sl_spot = entry_spot * 1.0030  # Up 0.30%
            
        session = day_df[(day_df.index >= entry_bar.name) & (day_df.index.strftime('%H:%M') <= '15:45')]
        
        exit_time = None
        exit_spot = None
        reason = None
        pnl_usd = 0.0
        
        for idx, row in session.iterrows():
            t_str = idx.strftime('%H:%M')
            if direction == "BULL_CALL":
                if row['Low'] <= sl_spot:
                    exit_time, exit_spot, reason = t_str, sl_spot, "STOP_LOSS"
                    pnl_usd = -(COST * 0.30) - FEE
                    break
                if row['High'] >= tp_spot:
                    exit_time, exit_spot, reason = t_str, tp_spot, "TAKE_PROFIT"
                    pnl_usd = (COST * 0.50) - FEE
                    break
            else: # BEAR_PUT
                if row['High'] >= sl_spot:
                    exit_time, exit_spot, reason = t_str, sl_spot, "STOP_LOSS"
                    pnl_usd = -(COST * 0.30) - FEE
                    break
                if row['Low'] <= tp_spot:
                    exit_time, exit_spot, reason = t_str, tp_spot, "TAKE_PROFIT"
                    pnl_usd = (COST * 0.50) - FEE
                    break
                    
            if t_str >= '15:45':
                exit_time, exit_spot, reason = t_str, row['Close'], "EOD_SQUARE_OFF"
                pnl_usd = 0.0 - FEE
                break
                
        if not reason and not session.empty:
            last_row = session.iloc[-1]
            exit_time, exit_spot, reason = last_row.name.strftime('%H:%M'), last_row['Close'], "EOD_SQUARE_OFF"
            pnl_usd = 0.0 - FEE

        enhanced_trades.append({
            "Date": d,
            "Direction": direction,
            "Entry_Spot": round(entry_spot, 2),
            "VWAP_1000": round(vwap_1000, 2),
            "Exit_Time": exit_time,
            "Exit_Spot": round(exit_spot, 2),
            "Reason": reason,
            "PnL_USD": round(pnl_usd, 2)
        })

    e_df = pd.DataFrame(enhanced_trades)
    print("=========================================================================================")
    print("               1-MONTH BACKTEST: ENHANCED DIRECTIONAL STRATEGY (VWAP FILTER)             ")
    print("       (Bull Call if Spot > VWAP, Bear Put if Spot < VWAP @ 10:00 AM ET)                 ")
    print("=========================================================================================")
    print(e_df.to_string(index=False))
    
    e_total = len(e_df)
    e_tp = len(e_df[e_df['Reason'] == 'TAKE_PROFIT'])
    e_sl = len(e_df[e_df['Reason'] == 'STOP_LOSS'])
    e_eod = len(e_df[e_df['Reason'] == 'EOD_SQUARE_OFF'])
    e_pnl = e_df['PnL_USD'].sum()
    
    e_wins = e_df[e_df['PnL_USD'] > 0]['PnL_USD'].sum()
    e_losses = abs(e_df[e_df['PnL_USD'] < 0]['PnL_USD'].sum())
    e_pf = round(e_wins / e_losses, 2) if e_losses > 0 else 999.0
    
    print("\n-------------------- ENHANCED STRATEGY PERFORMANCE --------------------")
    print(f"Total Trading Days:       {e_total}")
    print(f"Take Profit (+50% / $60): {e_tp} ({e_tp/e_total*100:.1f}%)")
    print(f"Stop Loss (-30% / -$36):  {e_sl} ({e_sl/e_total*100:.1f}%)")
    print(f"EOD Square-Off (Flat):    {e_eod} ({e_eod/e_total*100:.1f}%)")
    print(f"Win Rate (TPs / Total):   {e_tp/e_total*100:.1f}%")
    print(f"Profit Factor:            {e_pf}")
    print(f"Total Banked P&L:         ${e_pnl:+.2f} USD ({e_pnl/2000.0*100:+.2f}% on $2,000 capital)")
    print("------------------------------------------------------------------------\n")

if __name__ == "__main__":
    run_1mo_backtest()
