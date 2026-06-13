# Pivot Points Backtest Plan

## Goal
Backtest pivot-point based trading strategies on Nifty 50 stocks for the last
3 months using Dhan APIs, and report the best (stock, strategy) combination.

## Approach
1. Load Dhan credentials from `.env`.
2. Resolve security IDs for Nifty 50 stocks via Dhan scrip master CSV.
3. Fetch ~3 months of **daily** OHLC for each stock (Dhan v2 historical API).
4. Compute pivots from the *previous* day for each bar:
   - Standard
   - Fibonacci
   - Camarilla
5. Simulate four strategies per pivot system:
   - **Bounce Long (Mean Reversion)**: long at S1 if Low <= S1, exit at Close.
   - **Breakout Long**: long if Close > R1, exit next day at Close.
   - **Camarilla L3 Long**: long at S3 if Low<=S3, target R3, stop S4, else Close.
   - **Camarilla S3 Short**: short at R3 if High>=R3, target S3, stop R4, else Close.
6. Aggregate net % return per (stock, strategy) over the 3-month window.
7. Print the top-N combinations and write a summary report.

## Deliverables
- `backtest.py` — runs full pipeline.
- Console summary + `results.csv` of all (stock, strategy) combinations.
