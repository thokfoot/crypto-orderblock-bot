# USA Paper Bot — Per-Symbol Exit Walk-Forward (2026-10-17)

**Status:** PAPER SANDBOX only. No live capital. This note documents the change that
gave SPY and QQQ **different exit rules** in `usa_options_daemon.py`.

## TL;DR
The +50% upside cap (both the +0.6% spot take-profit *and* the EOD mark-to-market cap)
**hurts SPY but helps QQQ**.

| symbol | live (cap on) | cap-off (hold to EOD) | verdict |
|---|---:|---:|---|
| SPY | **−$85.70** | **+$219.60** | cap-off ROBUST (6/7 half-years) |
| QQQ | **+$769.10** | +$817.60 | cap NOT robust (recent 3 half-years favour live) |
| Both ($2,000) | +$683.40 (+34%) | — | hybrid ≈ +$988.70 (+49%) |

Applied config: **SPY = `tp_mode:"run_eod"` (cap-off)**, **QQQ = `tp_mode:"spot_tp"` (unchanged)**.
Principle: *fix the losing leg, do not touch the working leg.*

## Why (root cause)
The spread is only **$2 wide** (`strike_long = round(spot)`, `strike_short = +2`).
- Win needs **+0.6% spot** (≈ 2.3× the width) → capped reward **+50% of debit = +$60**.
- A mere **−0.3%** move wipes the $2 width → **up to −$120** (full debit).
- So risk:reward ≈ **2:1 against**; break-even needs a **>67% win rate**.
- SPY (broad, slow) rarely reaches +0.6% intraday → the cap mostly *denies* its winners
  while its red days still take the full debit. QQQ (fast) *does* reach +0.6% often →
  locking +50% protects it from afternoon reversals.

## Methodology
- **Pricing:** Black-Scholes calls (r=5%, q=0), `K1=round(S0)`, `K2=K1+2`, value =
  `(bs_call(K1) − bs_call(K2)) × 100`. σ from **^VIX/100** (fallback ^VXN). `DTE=2`.
- **Bars:** yfinance 1h (period `730d` → ~729 sessions) and 5m (`60d`), tz `America/New_York`.
- Entry = first bar ≥ 10:15 ET (1h bars land on 10:30), exit per rule else EOD ≤ 15:30.
- **Both modes realize the real BS value at the exit bar** — the *only* difference is the
  exit *trigger*, so the comparison is apples-to-apples.
  - `live`: exit at the first bar where High ≥ S0×1.006.
  - `cap-off (val2x)`: exit when spread value ≥ 2× debit, else EOD.
- Fee: **$1.30** round turn. NAV $1,000 per symbol.

## Walk-forward — SPY 729d/1h (per half-year)
| bucket | n | LIVE $ | cap-off $ | delta | winner |
|---|---:|---:|---:|---:|---|
| 2023-H2 | 29 | +60.1 | +83.7 | +23.5 | cap-off |
| 2024-H1 | 124 | +8.5 | +141.4 | +133.0 | cap-off |
| 2024-H2 | 128 | −24.0 | +66.9 | +91.0 | cap-off |
| 2025-H1 | 122 | +34.2 | +55.9 | +21.7 | cap-off |
| 2025-H2 | 128 | −31.4 | −6.6 | +24.8 | cap-off |
| 2026-H1 | 122 | −104.9 | −79.2 | +25.7 | cap-off |
| 2026-H2 | 76 | −28.1 | −42.5 | −14.4 | live |
| **TOTAL** | | **−85.7** | **+219.6** | **+305.3** | |

## Walk-forward — QQQ 729d/1h (per half-year)
| bucket | n | LIVE $ | cap-off $ | delta | winner |
|---|---:|---:|---:|---:|---|
| 2024-H2 | 128 | +164.2 | +144.4 | −19.8 | live |
| 2025-H2 | 128 | −44.3 | −7.7 | +36.7 | cap-off |
| 2026-H1 | 123 | +175.4 | +159.2 | −16.2 | live |
| 2026-H2 | 76 | +147.2 | +10.8 | −136.4 | live |
| **TOTAL** | | **+769.1** | **+817.6** | **+48.6** | |

## What was rejected
- **"Drop SPY, trade QQQ only"** — the largest single-day loss (−$225 on 2026-10-16) is
  SPY+QQQ *combined*; trading QQQ alone only **halves exposure** (and halves wins). Absolute
  2y profit falls $1,038 → $818, and %-drawdown worsens (46% vs 23%). Not a fix.
- **A stop-loss** — tested repeatedly, it never helped (e.g. QQQ 2y +84.5% → +51.9% with a
  −50% SL). The structure is defined-risk; a SL just locks in noise.
- **A blanket "remove the cap everywhere" rule** — half-right: it helps SPY, not QQQ.

## Caveats
- **BS + ^VIX proxy ≠ real intraday IV.** VIX is 30-day; real 0–2 DTE IV (and theta) is
  larger, so live theta bleed is likely *worse* than modelled.
- **`DTE=2` fixed** — real 0-DTE spreads behave differently near expiry.
- **Only $1.30 friction** modelled; real 0-DTE bid/ask can be wide.
- 2026-H2 is partial (76 sessions). PAPER only — not a live-capital green light.

## Scripts (run locally, yfinance)
`bs_bt2.py` (LIVE vs REAL), `bs_bt3.py` (exit-rule variants), `bs_bt4.py`
(SPY-only / QQQ-only / both × exit rules), `bs_bt5.py` (per-half-year walk-forward).
