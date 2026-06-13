"""
Pivot-point strategies backtest for Nifty 50 (last 3 months, daily bars).

Data sourcing
-------------
Primary intent was Dhan v2 Historical/Intraday Data API. The provided account
returns DH-902 / HTTP 451 ("User has not subscribed to Data APIs") for every
data endpoint, while Trading endpoints (fundlimit, etc.) work. Since the
account lacks the Data API entitlement, OHLC is sourced from Yahoo Finance
(`yfinance`) as a fallback.  The Dhan scrip master is still used to validate
that every symbol is a tradable NSE_EQ instrument.

Strategies (8 total across 3 pivot systems)
-------------------------------------------
For every day t we compute pivots from day t-1's H/L/C and simulate the trade
on day t using day t's OHLC.

Standard pivots (Floor):
  S1 = 2*PP - H,   R1 = 2*PP - L
  S2 = PP - (H-L), R2 = PP + (H-L)
Fibonacci pivots:
  R1 = PP + 0.382*(H-L), S1 = PP - 0.382*(H-L)
  R2 = PP + 0.618*(H-L), S2 = PP - 0.618*(H-L)
Camarilla pivots (uses prior close C):
  R3 = C + 1.1*(H-L)/4,   S3 = C - 1.1*(H-L)/4
  R4 = C + 1.1*(H-L)/2,   S4 = C - 1.1*(H-L)/2

Strategies:
  STD_BOUNCE_LONG   : Low<=S1 -> long@S1, exit@Close.
  STD_BOUNCE_SHORT  : High>=R1 -> short@R1, exit@Close.
  STD_BREAK_LONG    : Close>R1(prev day's bar) -> long next-open, exit next Close.
  FIB_BOUNCE_LONG   : Low<=S1(fib) -> long@S1, exit@Close.
  FIB_BOUNCE_SHORT  : High>=R1(fib) -> short@R1, exit@Close.
  CAM_L3_LONG       : Low<=S3 -> long@S3, target=R3, stop=S4, fallback=Close.
  CAM_S3_SHORT      : High>=R3 -> short@R3, target=S3, stop=R4, fallback=Close.
  CAM_BREAK_LONG    : Close>R4 -> long next-open, exit next Close.

Round-trip cost = 10 bps (5 bps each side) applied to every trade.
"""

from __future__ import annotations
import os, sys, math, time, json, warnings
from datetime import date, timedelta, datetime
from dataclasses import dataclass

import pandas as pd
import numpy as np
import requests
import yfinance as yf
from dotenv import load_dotenv

warnings.filterwarnings("ignore")
load_dotenv()

ROUND_TRIP_COST = 0.0010  # 10 bps total per trade
LOOKBACK_DAYS   = 95      # ~3 months of calendar days; trims to ~62 trading days

# ---------------------------------------------------------------------------
# Nifty 50 universe (as of 2026; static list — validated against scrip master)
# ---------------------------------------------------------------------------
NIFTY50 = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","ITC","SBIN",
    "BHARTIARTL","LT","KOTAKBANK","BAJFINANCE","AXISBANK","ASIANPAINT","MARUTI",
    "HCLTECH","SUNPHARMA","ULTRACEMCO","TITAN","NESTLEIND","WIPRO","ONGC","NTPC",
    "POWERGRID","M&M","JSWSTEEL","TATAMOTORS","TATASTEEL","COALINDIA","INDUSINDBK",
    "BAJAJFINSV","HDFCLIFE","GRASIM","ADANIENT","ADANIPORTS","DRREDDY","EICHERMOT",
    "BRITANNIA","CIPLA","HEROMOTOCO","BPCL","APOLLOHOSP","TECHM","SBILIFE",
    "BAJAJ-AUTO","TATACONSUM","HINDALCO","LTIM","SHRIRAMFIN","TRENT",
]

# ---------------------------------------------------------------------------
# Dhan probe (informational) + scrip master
# ---------------------------------------------------------------------------
def dhan_probe() -> dict:
    """Confirm Dhan token state. Returns a status dict for the report."""
    out = {"data_api": False, "trading_api": False, "error": ""}
    token  = os.environ.get("DHAN_ACCESS_TOKEN", "")
    client = os.environ.get("DHAN_CLIENT_ID", "")
    if not token or not client:
        out["error"] = "Missing DHAN credentials in .env"
        return out
    H = {"access-token": token, "client-id": client,
         "Content-Type":"application/json", "Accept":"application/json"}
    try:
        r = requests.get("https://api.dhan.co/v2/fundlimit", headers=H, timeout=20)
        out["trading_api"] = r.status_code == 200
    except Exception as e:
        out["error"] = f"trading: {e}"
    try:
        r = requests.post(
            "https://api.dhan.co/v2/charts/historical", headers=H,
            json={"securityId":"2885","exchangeSegment":"NSE_EQ",
                  "instrument":"EQUITY","expiryCode":0,
                  "fromDate":"2026-03-01","toDate":"2026-06-10"},
            timeout=20,
        )
        out["data_api"] = r.status_code == 200
        if r.status_code != 200:
            try:
                out["error"] = r.json().get("errorMessage", r.text[:200])
            except Exception:
                out["error"] = r.text[:200]
    except Exception as e:
        out["error"] = f"data: {e}"
    return out


def load_scrip_master(path: str = "scrip_master.csv") -> pd.DataFrame:
    if not os.path.exists(path):
        url = "https://images.dhan.co/api-data/api-scrip-master.csv"
        r = requests.get(url, timeout=120); r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    df = pd.read_csv(path, low_memory=False)
    nse_eq = df[(df["SEM_EXM_EXCH_ID"] == "NSE") &
                (df["SEM_INSTRUMENT_NAME"] == "EQUITY") &
                (df["SEM_SERIES"] == "EQ")].copy()
    nse_eq["SYMBOL"] = nse_eq["SEM_TRADING_SYMBOL"].astype(str).str.upper()
    return nse_eq[["SYMBOL", "SEM_SMST_SECURITY_ID"]].rename(
        columns={"SEM_SMST_SECURITY_ID": "security_id"}
    )


# ---------------------------------------------------------------------------
# OHLC fetch (Yahoo, NSE)
# ---------------------------------------------------------------------------
def fetch_ohlc(symbol: str, start: date, end: date) -> pd.DataFrame:
    yf_sym = symbol.replace("&", "%26") + ".NS"
    # yfinance handles `&` natively in some versions; try both.
    candidates = [symbol + ".NS", yf_sym]
    for s in candidates:
        try:
            df = yf.download(s, start=start.isoformat(),
                             end=(end + timedelta(days=1)).isoformat(),
                             progress=False, auto_adjust=False, threads=False)
        except Exception:
            df = pd.DataFrame()
        if df is not None and not df.empty:
            break
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open","high","low","close"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df


# ---------------------------------------------------------------------------
# Pivot calculation
# ---------------------------------------------------------------------------
def add_pivots(df: pd.DataFrame) -> pd.DataFrame:
    p = df.shift(1)  # use previous day's H/L/C
    H, L, C = p["high"], p["low"], p["close"]
    rng = H - L
    pp  = (H + L + C) / 3.0

    # Standard
    df["std_PP"] = pp
    df["std_R1"] = 2*pp - L
    df["std_S1"] = 2*pp - H
    df["std_R2"] = pp + rng
    df["std_S2"] = pp - rng

    # Fibonacci
    df["fib_PP"] = pp
    df["fib_R1"] = pp + 0.382 * rng
    df["fib_S1"] = pp - 0.382 * rng
    df["fib_R2"] = pp + 0.618 * rng
    df["fib_S2"] = pp - 0.618 * rng

    # Camarilla
    df["cam_R3"] = C + 1.1 * rng / 4.0
    df["cam_S3"] = C - 1.1 * rng / 4.0
    df["cam_R4"] = C + 1.1 * rng / 2.0
    df["cam_S4"] = C - 1.1 * rng / 2.0
    return df


# ---------------------------------------------------------------------------
# Strategies — return list of trades dicts with pct return (after costs)
# ---------------------------------------------------------------------------
def _ret_long(entry: float, exit_: float) -> float:
    return (exit_ / entry) - 1.0 - ROUND_TRIP_COST

def _ret_short(entry: float, exit_: float) -> float:
    return (entry / exit_) - 1.0 - ROUND_TRIP_COST


def strat_bounce_long(df: pd.DataFrame, lvl: str) -> list[dict]:
    trades = []
    for d, row in df.iterrows():
        s1 = row[lvl]
        if pd.isna(s1):
            continue
        if row["low"] <= s1 and row["open"] > s1:  # entry only if open > S1 (gap-down avoidance)
            entry = s1
            exit_ = row["close"]
            trades.append({"date": d, "entry": entry, "exit": exit_,
                           "ret": _ret_long(entry, exit_)})
    return trades


def strat_bounce_short(df: pd.DataFrame, lvl: str) -> list[dict]:
    trades = []
    for d, row in df.iterrows():
        r1 = row[lvl]
        if pd.isna(r1):
            continue
        if row["high"] >= r1 and row["open"] < r1:
            entry = r1
            exit_ = row["close"]
            trades.append({"date": d, "entry": entry, "exit": exit_,
                           "ret": _ret_short(entry, exit_)})
    return trades


def strat_breakout_long(df: pd.DataFrame, lvl: str) -> list[dict]:
    """If Close[t] > level[t] (computed from t-1), enter at next day's open,
    exit next day's close."""
    trades = []
    rows = list(df.iterrows())
    for i in range(len(rows) - 1):
        d, row = rows[i]
        nd, nxt = rows[i + 1]
        lv = row[lvl]
        if pd.isna(lv):
            continue
        if row["close"] > lv:
            entry = nxt["open"]
            exit_ = nxt["close"]
            if entry > 0:
                trades.append({"date": nd, "entry": entry, "exit": exit_,
                               "ret": _ret_long(entry, exit_)})
    return trades


def strat_cam_l3_long(df: pd.DataFrame) -> list[dict]:
    trades = []
    for d, row in df.iterrows():
        s3, r3, s4 = row["cam_S3"], row["cam_R3"], row["cam_S4"]
        if pd.isna(s3):
            continue
        if row["low"] <= s3 and row["open"] > s3:
            entry = s3
            # Determine outcome: if low also <= s4 -> stop hit (worst case)
            if row["low"] <= s4:
                exit_ = s4
            elif row["high"] >= r3:
                exit_ = r3
            else:
                exit_ = row["close"]
            trades.append({"date": d, "entry": entry, "exit": exit_,
                           "ret": _ret_long(entry, exit_)})
    return trades


def strat_cam_s3_short(df: pd.DataFrame) -> list[dict]:
    trades = []
    for d, row in df.iterrows():
        s3, r3, r4 = row["cam_S3"], row["cam_R3"], row["cam_R4"]
        if pd.isna(r3):
            continue
        if row["high"] >= r3 and row["open"] < r3:
            entry = r3
            if row["high"] >= r4:
                exit_ = r4   # stop hit
            elif row["low"] <= s3:
                exit_ = s3   # target hit
            else:
                exit_ = row["close"]
            trades.append({"date": d, "entry": entry, "exit": exit_,
                           "ret": _ret_short(entry, exit_)})
    return trades


def strat_cam_break_long(df: pd.DataFrame) -> list[dict]:
    trades = []
    rows = list(df.iterrows())
    for i in range(len(rows) - 1):
        d, row = rows[i]
        nd, nxt = rows[i + 1]
        r4 = row["cam_R4"]
        if pd.isna(r4):
            continue
        if row["close"] > r4:
            entry = nxt["open"]; exit_ = nxt["close"]
            if entry > 0:
                trades.append({"date": nd, "entry": entry, "exit": exit_,
                               "ret": _ret_long(entry, exit_)})
    return trades


STRATEGIES = {
    "STD_BOUNCE_LONG":  lambda df: strat_bounce_long(df, "std_S1"),
    "STD_BOUNCE_SHORT": lambda df: strat_bounce_short(df, "std_R1"),
    "STD_BREAK_LONG":   lambda df: strat_breakout_long(df, "std_R1"),
    "FIB_BOUNCE_LONG":  lambda df: strat_bounce_long(df, "fib_S1"),
    "FIB_BOUNCE_SHORT": lambda df: strat_bounce_short(df, "fib_R1"),
    "CAM_L3_LONG":      strat_cam_l3_long,
    "CAM_S3_SHORT":     strat_cam_s3_short,
    "CAM_BREAK_LONG":   strat_cam_break_long,
}


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------
@dataclass
class Result:
    stock: str
    strategy: str
    trades: int
    wins: int
    win_rate: float
    avg_ret: float        # arithmetic mean trade return
    total_ret: float      # compounded total return (sum-of-1+r product)
    bh_ret: float         # buy-and-hold over the same window for context

    def row(self):
        return self.__dict__


def run_backtest(symbol: str, df: pd.DataFrame) -> list[Result]:
    out = []
    if df.empty or len(df) < 3:
        return out
    df = add_pivots(df.copy())
    bh = df["close"].iloc[-1] / df["close"].iloc[0] - 1
    for name, fn in STRATEGIES.items():
        trades = fn(df)
        n = len(trades)
        wins = sum(1 for t in trades if t["ret"] > 0)
        wr   = wins / n if n else 0.0
        avg  = float(np.mean([t["ret"] for t in trades])) if n else 0.0
        compounded = float(np.prod([1 + t["ret"] for t in trades]) - 1) if n else 0.0
        out.append(Result(symbol, name, n, wins, wr, avg, compounded, bh))
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def main():
    print("=" * 78)
    print("Pivot-point backtest — Nifty 50 — last 3 months")
    print("=" * 78)

    probe = dhan_probe()
    print(f"Dhan trading_api={probe['trading_api']} data_api={probe['data_api']}")
    if not probe["data_api"]:
        print(f"  -> Data API blocked: {probe['error']}")
        print("  -> Falling back to Yahoo Finance for OHLC; "
              "Dhan scrip master still used for symbol validation.")

    master = load_scrip_master()
    universe = master[master["SYMBOL"].isin([s.upper() for s in NIFTY50])]
    missing = sorted(set(s.upper() for s in NIFTY50) - set(universe["SYMBOL"]))
    if missing:
        print(f"Symbols not found in Dhan scrip master: {missing}")
    print(f"Validated {len(universe)} / {len(NIFTY50)} Nifty 50 symbols.")

    end = date(2026, 6, 11)
    start = end - timedelta(days=LOOKBACK_DAYS)
    print(f"Window: {start} -> {end}")
    print()

    all_results: list[Result] = []
    bh_lookup: dict[str, float] = {}
    for i, sym in enumerate(NIFTY50, 1):
        df = fetch_ohlc(sym, start, end)
        if df.empty:
            print(f"[{i:2d}/{len(NIFTY50)}] {sym:<12} no data")
            continue
        df = df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
        results = run_backtest(sym, df)
        all_results.extend(results)
        if results:
            bh_lookup[sym] = results[0].bh_ret
        print(f"[{i:2d}/{len(NIFTY50)}] {sym:<12} bars={len(df):3d} "
              f"trades={sum(r.trades for r in results):3d} "
              f"BH={results[0].bh_ret*100:+6.2f}%")

    df_res = pd.DataFrame([r.row() for r in all_results])
    df_res.to_csv("results.csv", index=False)

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)

    # 1. Best (stock, strategy) overall
    df_traded = df_res[df_res["trades"] >= 3].copy()  # need ≥3 trades for meaning
    if df_traded.empty:
        print("No (stock, strategy) combination produced >=3 trades.")
        return
    df_traded["edge_vs_bh"] = df_traded["total_ret"] - df_traded["bh_ret"]

    print("\nTop 15 (stock, strategy) combinations by total return:")
    top = df_traded.sort_values("total_ret", ascending=False).head(15)
    print(top[["stock","strategy","trades","wins","win_rate",
              "avg_ret","total_ret","bh_ret","edge_vs_bh"]]
          .to_string(index=False,
                     formatters={"win_rate":"{:.1%}".format,
                                 "avg_ret":"{:+.2%}".format,
                                 "total_ret":"{:+.2%}".format,
                                 "bh_ret":"{:+.2%}".format,
                                 "edge_vs_bh":"{:+.2%}".format}))

    # 2. Best per strategy
    print("\nBest stock per strategy (by total return):")
    best_per_strat = (df_traded.sort_values("total_ret", ascending=False)
                                .groupby("strategy").head(1)
                                .sort_values("total_ret", ascending=False))
    print(best_per_strat[["strategy","stock","trades","win_rate",
                          "avg_ret","total_ret","bh_ret"]]
          .to_string(index=False,
                     formatters={"win_rate":"{:.1%}".format,
                                 "avg_ret":"{:+.2%}".format,
                                 "total_ret":"{:+.2%}".format,
                                 "bh_ret":"{:+.2%}".format}))

    # 3. Strategy-level averages
    print("\nStrategy averages across the universe (>=3 trades only):")
    agg = df_traded.groupby("strategy").agg(
        avg_total_ret=("total_ret","mean"),
        avg_win_rate=("win_rate","mean"),
        avg_trades=("trades","mean"),
        n_combos=("stock","count"),
        positive_combos=("total_ret", lambda s: int((s>0).sum())),
    ).sort_values("avg_total_ret", ascending=False)
    print(agg.to_string(formatters={"avg_total_ret":"{:+.2%}".format,
                                    "avg_win_rate":"{:.1%}".format,
                                    "avg_trades":"{:.1f}".format}))

    winner = df_traded.sort_values("total_ret", ascending=False).iloc[0]
    print("\n" + "-" * 78)
    print(f"BEST COMBO  ->  {winner['stock']}  +  {winner['strategy']}")
    print(f"  trades   : {int(winner['trades'])}")
    print(f"  win rate : {winner['win_rate']:.1%}")
    print(f"  avg ret  : {winner['avg_ret']:+.2%} per trade")
    print(f"  total    : {winner['total_ret']:+.2%}  "
          f"(buy-and-hold: {winner['bh_ret']:+.2%}, "
          f"edge {winner['edge_vs_bh']:+.2%})")
    print("-" * 78)
    print("Detailed per-combination results written to results.csv")


if __name__ == "__main__":
    main()
