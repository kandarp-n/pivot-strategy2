# Pivot-Point Trading Strategies — Nifty 50 (Dhan)

End-to-end research → live-trading pipeline for **pivot-point based intraday
strategies on the Nifty 50**, using the Dhan v2 APIs.

The pipeline does three things:

1. **Backtest** ten pivot-point strategies (Standard, Fibonacci, Camarilla
   bounce / breakout / breakdown variants) on every Nifty 50 stock for the
   last ~3 months of 15-minute Dhan bars.
2. **Cost the results** with the real Indian intraday-equity charge stack
   (Dhan brokerage + STT + exchange + SEBI + stamp duty + GST).
3. **Live-trade** the top-10 (stock, strategy) combinations against your Dhan
   account, with a strict dry-run safety interlock.

---

## Repository layout

```
backtest.py                    # Daily-bar backtest (legacy, Yahoo fallback)
backtest_intraday_dhan.py      # Primary intraday backtest (Dhan 15-min bars)
apply_realistic_costs.py       # Re-cost prior intraday results analytically
live_trader.py                 # Live trading bot (DRY_RUN by default)

results.csv                    # Daily-bar backtest results
results_intraday_dhan.csv      # Intraday results, flat 0.10% cost
results_intraday_dhan_costed.csv  # Intraday results, full Dhan cost stack

plan.md                        # Design notes
.gitignore                     # excludes .env, caches, logs, large CSVs
.env.example                   # template for your Dhan credentials
```

`scrip_master.csv` (~30 MB) and `cache_intraday/` are regenerated at run-time
and are deliberately not tracked.

---

## Setup

1. **Create a `.env`** file in the repo root using `.env.example` as a template:

   ```
   DHAN_CLIENT_ID=<your dhan client id>
   DHAN_ACCESS_TOKEN=<your dhan v2 access token>
   ```

   The `.gitignore` excludes `.env` so the token never reaches GitHub. **If
   you ever paste your token into a tracked file by accident, rotate it
   immediately from the Dhan portal.**

2. **Install Python deps** (Python 3.11+):

   ```powershell
   pip install pandas numpy requests python-dotenv yfinance pyarrow
   ```

3. **Required Dhan subscriptions** for the full pipeline:
   - **Trading API** — for live order placement.
   - **Historical Data API** — for the backtest and pivot computation.
   - **Live Market Quote API** — *recommended* for live trading. Without it
     the bot transparently falls back to the most-recent 1-minute bar from
     the Historical Data API (~ ≤60 s stale).

---

## Running the backtest

```powershell
python backtest_intraday_dhan.py     # full 3-month intraday backtest
python apply_realistic_costs.py      # re-cost prior results with Dhan charges
```

The intraday script caches its 15-min bars under `cache_intraday/` so re-runs
after parameter tweaks finish in seconds.

### Latest top-10 result (after Dhan brokerage + STT + GST)

| # | Stock | Strategy | Trades | Win % | Net | BH | Edge |
|--:|---|---|--:|--:|--:|--:|--:|
| 1 | POWERGRID | STD_R1_BOUNCE_SHORT | 29 | 72.4% | +18.77% | +0.53% | +18.24% |
| 2 | HDFCBANK | FIB_S1_BOUNCE_LONG | 38 | 71.1% | +18.09% | −9.66% | +27.75% |
| 3 | MARUTI | CAM_S3_SHORT | 44 | 56.8% | +17.89% | −4.80% | +22.68% |
| 4 | BPCL | FIB_R1_BOUNCE_SHORT | 29 | 58.6% | +16.26% | −15.95% | +32.21% |
| 5 | BPCL | FIB_S1_BOUNCE_LONG | 39 | 64.1% | +15.90% | −15.95% | +31.85% |
| 6 | COALINDIA | CAM_L3_LONG | 36 | 58.3% | +15.29% | +1.59% | +13.69% |
| 7 | BPCL | STD_R1_BOUNCE_SHORT | 24 | 62.5% | +14.39% | −15.95% | +30.34% |
| 8 | KOTAKBANK | STD_S1_BOUNCE_LONG | 33 | 72.7% | +13.69% | +0.67% | +13.02% |
| 9 | NTPC | CAM_S3_SHORT | 46 | 60.9% | +13.31% | −5.63% | +18.94% |
| 10 | NESTLEIND | FIB_S1_BOUNCE_LONG | 35 | 71.4% | +12.12% | +15.73% | −3.61% |

Cost model: brokerage 0.03 % + STT 0.025 % (sell) + exchange 0.00297 % + SEBI
0.0001 % + stamp duty 0.003 % (buy) + 18 % GST → **~0.106 % round-trip**.

---

## Live trading (`live_trader.py`)

```powershell
# Pre-market sanity check (fetches today's pivots, prints the plan, no orders)
python live_trader.py --once

# Full session (DRY_RUN by default — logs intended orders only)
python live_trader.py
```

### Keeping the strategies fresh

The bot **auto-loads its top-10 (stock, strategy) list from
`results_intraday_dhan.csv`** at every startup. To refresh after a regime
shift, just regenerate the results CSV:

```powershell
# 1. Re-run the backtest on the most recent ~3 months of data
python backtest_intraday_dhan.py

# 2. Verify the new top-10 prints correctly during pre-market
python live_trader.py --once

# 3. Run live (or dry-run)
python live_trader.py
```

If `results_intraday_dhan.csv` is missing or has fewer than 10 eligible
combos, the bot falls back to a hardcoded snapshot of the top-10 from
2026-06-13 — so a missing/corrupt CSV won't take you offline.

> The auto-loader only considers **bounce / fade** strategies (the live
> engine implements limit-style entries). Breakout/breakdown variants are
> filtered out even if they crack the top 10.

### Going live — read this first

The script will refuse to send real orders unless **both** of these flags
near the top of `live_trader.py` are flipped:

```python
DRY_RUN                = False    # default True
I_UNDERSTAND_THE_RISKS = True     # default False
```

Other knobs in the same block:

```python
PER_TRADE_NOTIONAL_INR = 15_000   # rupees per position
MAX_CONCURRENT_TRADES  = 6        # cap on simultaneous open positions
PRODUCT_TYPE           = "INTRADAY"   # MIS — exchange auto-square-off
SQUARE_OFF_TIME        = dtime(15, 15)   # IST
ENTRY_WINDOW_END       = dtime(14, 30)   # no new entries after
POLL_INTERVAL_SEC      = 10              # LTP poll cadence
```

### Daily flow

1. **Pre-market** — fetch yesterday's daily H/L/C from Dhan, compute pivots,
   build the day's 10 trade plans (entry / target / stop / qty).
2. **Market open (09:15)** — poll LTP every 10 s. When LTP crosses the entry
   trigger:
   - LONG bounce: `LTP <= S-level`
   - SHORT bounce: `LTP >= R-level`

   place a **LIMIT** order at the entry level (MIS, DAY).
3. **After fill** — arm a bracket: a **LIMIT** target + **SL-M** stop.
   First-to-fill wins; the other is cancelled.
4. **15:15 IST** — force-square-off any remaining open position via MARKET.
5. **15:25 IST** — cancel any unfilled pending orders, write the trade log.

### Disclaimers

- Past performance (the backtest) is **not** a guarantee of future returns.
- The Indian market regime can shift; re-run the backtest weekly.
- Start with smaller notional (e.g. Rs 5,000 per trade) for the first
  real-money sessions and watch the bot live.
- **Not investment advice. Use at your own risk.**

---

## License

Personal-use research project. Provided as-is.
