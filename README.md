# Autonomous 24/7 Crypto Order Block Paper Trading Bot

Autonomous algorithmic trading daemon running 24/7 on cloud infrastructure (GitHub Actions / Microsoft Azure).

## 🚀 Overview
- **Strategy:** Institutional Smart Money Concepts (SMC) - Order Blocks (OB), Fair Value Gaps (FVG), & Liquidity Pool Sweeps.
- **Market:** Binance USD-M Perpetual Futures (top 35 high-volume liquid pairs).
- **Execution:** 100% Free Public REST API (no private exchange keys exposed or required for paper execution).
- **Risk Management:**
  - Dynamic ATR Stops (minimum 1.2x 15m ATR buffer against false wicks).
  - Minimum 1:2.0 Risk-to-Reward ratio per trade.
  - Automated Breakeven Trailing: Once position reaches +2.0% profit, SL is automatically moved to Entry price (zero downside risk).
  - Max 3 concurrent positions ($200 USD notional each, $2,000 initial virtual capital).
- **Notifications:** Real-time Telegram alerts via `@trade234_bot` (Chat ID `988970998`).

## 📊 Live Open Positions
- **RLCUSDT (Long):** Entry $1.0535 | SL $1.0535 (Breakeven Locked) | TP $1.1484
- **CFXUSDT (Long):** Entry $0.06613 | SL $0.06613 (Breakeven Locked) | TP $0.07229
- **ARBUSDT (Long):** Entry $0.19628 | SL $0.19344 | TP $0.20315

## ☁️ Cloud Architecture & Zero-Intervention Management
- Runs continuously on GitHub Actions runners (`ubuntu-latest`).
- Automatically syncs trade state (`data/crypto_dashboard.json` and `data/crypto_paper_trades.csv`) back to Git upon every execution.
- Can be inspected, managed, and triggered directly via GitHub CLI (`gh`).
