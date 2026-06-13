"""
Adjust the prior intraday backtest results for realistic Dhan intraday equity
charges (brokerage + STT + exchange + SEBI + stamp duty + GST), without re-
fetching market data.

Why we can adjust analytically
------------------------------
The previous run (`results_intraday_dhan.csv`) was computed with a flat 10 bps
round-trip cost. The realistic Dhan cost model is:

  buy  leg %  : brokerage 0.03  + exch 0.00297 + SEBI 0.0001 + stamp 0.003
                + 18% GST on (brokerage + exch + SEBI)              = 0.04203%
  sell leg %  : brokerage 0.03  + exch 0.00297 + SEBI 0.0001 + STT 0.025
                + 18% GST on (brokerage + exch + SEBI)              = 0.06403%
  round trip                                                          0.10606%

So the realistic cost is exactly **6 bps higher per trade** than the prior
flat-10 bps assumption — the same constant for both long *and* short trades
(longs pay stamp on buy + STT on sell; shorts pay STT on sell first + stamp on
buy second — the per-leg numbers are identical, just swapped).

Adjustment:
  new per-trade ret = old per-trade ret - 6 bps
  new avg_ret       = old avg_ret       - 0.00006
  new total_ret     ≈ (1 + old_total) * (1 - 0.00006) ** trades  - 1

Win-rate effect is bounded by the *fraction of trades whose old per-trade
return was in (0, 6 bps]* — a small slice we can't measure without per-trade
data. We leave win_rate unchanged and flag it as a tight upper bound.
"""

import numpy as np
import pandas as pd

# Dhan intraday-equity cost components (decimal, NSE EQ)
BROKERAGE_PCT = 0.0003
STT_SELL_PCT  = 0.00025
TXN_PCT       = 0.0000297
SEBI_PCT      = 0.000001
STAMP_BUY_PCT = 0.00003
GST_RATE      = 0.18

GST_BASE      = BROKERAGE_PCT + TXN_PCT + SEBI_PCT
BUY_LEG_PCT   = GST_BASE + STAMP_BUY_PCT + GST_RATE * GST_BASE
SELL_LEG_PCT  = GST_BASE + STT_SELL_PCT  + GST_RATE * GST_BASE
NEW_RT_COST   = BUY_LEG_PCT + SELL_LEG_PCT      # ~0.001060
OLD_RT_COST   = 0.0010                            # what the prior run used
EXTRA_PER_TRADE = NEW_RT_COST - OLD_RT_COST       # ~0.00006

print("=" * 80)
print("Cost-model upgrade — pure-percentage analytical adjustment")
print("=" * 80)
print(f"  buy  leg : {BUY_LEG_PCT*100:.4f}%")
print(f"  sell leg : {SELL_LEG_PCT*100:.4f}%")
print(f"  new RT   : {NEW_RT_COST*100:.4f}%")
print(f"  old RT   : {OLD_RT_COST*100:.4f}%  (prior run)")
print(f"  extra/trade : {EXTRA_PER_TRADE*100:.4f}% (= {EXTRA_PER_TRADE*1e4:.1f} bps)")
print()

old = pd.read_csv("results_intraday_dhan.csv")

# Analytical adjustment
old["avg_ret_costed"]   = old["avg_ret"] - EXTRA_PER_TRADE
# total_ret adjustment via uniform per-trade haircut (mathematically:
# the prior compounded total used (1+r_i); we now multiply in (1 - 6bps)^n)
old["total_ret_costed"] = (1 + old["total_ret"]) * (1 - EXTRA_PER_TRADE) ** old["trades"] - 1
old["edge_vs_bh"]       = old["total_ret_costed"] - old["bh_ret"]

# Reorder
out = old[["stock", "strategy", "trades", "wins", "win_rate",
           "avg_ret_costed", "total_ret_costed", "bh_ret", "edge_vs_bh"]] \
        .rename(columns={"avg_ret_costed":"avg_ret",
                         "total_ret_costed":"total_ret"})
out.to_csv("results_intraday_dhan_costed.csv", index=False)

df_t = out[out["trades"] >= 5].copy()

print("=" * 80)
print("INTRADAY SUMMARY — REALISTIC DHAN COSTS (>=5 trades)")
print("=" * 80)

print("\nTop 15 (stock, strategy) combos by total return:")
top = df_t.sort_values("total_ret", ascending=False).head(15)
print(top.to_string(index=False,
        formatters={"win_rate":"{:.1%}".format,
                    "avg_ret":"{:+.2%}".format,
                    "total_ret":"{:+.2%}".format,
                    "bh_ret":"{:+.2%}".format,
                    "edge_vs_bh":"{:+.2%}".format}))

print("\nBest stock per strategy (by total return):")
best = (df_t.sort_values("total_ret", ascending=False)
              .groupby("strategy").head(1)
              .sort_values("total_ret", ascending=False))
print(best[["strategy","stock","trades","win_rate","avg_ret",
            "total_ret","bh_ret"]]
      .to_string(index=False,
        formatters={"win_rate":"{:.1%}".format,
                    "avg_ret":"{:+.2%}".format,
                    "total_ret":"{:+.2%}".format,
                    "bh_ret":"{:+.2%}".format}))

print("\nStrategy averages across the universe (>=5 trades only):")
agg = df_t.groupby("strategy").agg(
    avg_total_ret=("total_ret","mean"),
    avg_win_rate =("win_rate","mean"),
    avg_trades   =("trades","mean"),
    n_combos     =("stock","count"),
    positive_combos=("total_ret", lambda s: int((s>0).sum())),
).sort_values("avg_total_ret", ascending=False)
print(agg.to_string(formatters={"avg_total_ret":"{:+.2%}".format,
                                "avg_win_rate":"{:.1%}".format,
                                "avg_trades":"{:.1f}".format}))

w = df_t.sort_values("total_ret", ascending=False).iloc[0]
print("\n" + "-" * 80)
print(f"BEST INTRADAY COMBO  ->  {w['stock']}  +  {w['strategy']}")
print(f"  trades   : {int(w['trades'])}")
print(f"  win rate : {w['win_rate']:.1%}  (slight upper bound — unchanged from pre-cost run)")
print(f"  avg ret  : {w['avg_ret']:+.2%} per trade (after all charges)")
print(f"  total    : {w['total_ret']:+.2%}  "
      f"(BH: {w['bh_ret']:+.2%}, edge {w['edge_vs_bh']:+.2%})")
print("-" * 80)
print("Detailed per-combination results -> results_intraday_dhan_costed.csv")
