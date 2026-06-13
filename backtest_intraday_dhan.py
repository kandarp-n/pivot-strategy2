"""
INTRADAY pivot-point backtest for Nifty 50 — last ~3 months — DHAN DATA API.

Granularity:
  Intraday bars  : 15-min (Dhan v2 /charts/intraday).
  Pivots         : computed once per day from the *previous trading day's*
                   H/L/C (synthesized by aggregating that day's 15-min bars
                   from Dhan v2 /charts/intraday), then applied to every
                   15-min bar of the *next* day.
  Holding        : strictly intraday — every position is force-flat at the
                   final 15-min bar of the trading day.

Strategies tested (10):
  STD_S1_BOUNCE_LONG     : low<=S1, entry@S1, target=PP,  stop=S2,  EOD exit
  STD_R1_BOUNCE_SHORT    : high>=R1, entry@R1, target=PP, stop=R2,  EOD exit
  STD_R1_BREAKOUT_LONG   : 15m close>R1 -> entry next-bar open, target=R2,
                           stop=PP, EOD exit
  STD_S1_BREAKDOWN_SHORT : 15m close<S1 -> entry next-bar open, target=S2,
                           stop=PP, EOD exit
  FIB_S1_BOUNCE_LONG     : same as STD_S1_BOUNCE_LONG with Fibonacci pivots
  FIB_R1_BOUNCE_SHORT    : same as STD_R1_BOUNCE_SHORT with Fibonacci pivots
  CAM_L3_LONG            : low<=S3, entry@S3, target=R3, stop=S4, EOD exit
  CAM_S3_SHORT           : high>=R3, entry@R3, target=S3, stop=R4, EOD exit
  CAM_R4_BREAKOUT_LONG   : 15m close>R4 -> entry next-bar open,
                           target=R4 + 0.5*(R4-S4), stop=R3, EOD exit
  CAM_S4_BREAKDOWN_SHORT : 15m close<S4 -> entry next-bar open,
                           target=S4 - 0.5*(R4-S4), stop=S3, EOD exit

Cost model — realistic Dhan intraday equity charges (NSE EQ):
  buy  leg ~0.0420% (brokerage 0.03% + exch 0.00297% + SEBI 0.0001%
                     + stamp duty 0.003% + 18% GST on brokerage/exch/SEBI)
  sell leg ~0.0640% (brokerage 0.03% + exch 0.00297% + SEBI 0.0001%
                     + STT 0.025% + 18% GST on brokerage/exch/SEBI)
  -> round-trip ~0.106% of turnover. Rs 20/order brokerage cap is NOT
     applied (results are pure percentages independent of position size; the
     cap would *reduce* effective brokerage above ~Rs 66,667/order).

Conservative intra-bar: when one 15m bar's range contains both target and
stop, we assume the stop fills first (worst-case).
"""

from __future__ import annotations
import os, time, json, warnings, sys
from datetime import date, timedelta, datetime
from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

warnings.filterwarnings("ignore")
load_dotenv()

ROUND_TRIP_COST = 0.0010  # legacy constant (no longer used) — see cost_* functions
TOKEN  = os.environ["DHAN_ACCESS_TOKEN"]
CLIENT = os.environ["DHAN_CLIENT_ID"]
HEADERS = {"access-token": TOKEN, "client-id": CLIENT,
           "Content-Type": "application/json", "Accept": "application/json"}

# ---------------------------------------------------------------------------
# Dhan intraday-equity cost model (NSE EQ)
#   Brokerage : 0.03% per executed order (cap of Rs 20/order ignored — keeps
#               results expressed in pure % space, which is conservative for
#               positions below ~Rs 66,667)
#   STT       : 0.025% on the SELL leg only (intraday delivery exempt rate)
#   Exch txn  : 0.00297% per leg (NSE EQ)
#   SEBI fee  : 0.0001% per leg
#   Stamp duty: 0.003% on the BUY leg only
#   GST 18%   : on (brokerage + exch txn + SEBI fee)
# Effective round-trip cost ~= 0.106% of traded value.
# ---------------------------------------------------------------------------
BROKERAGE_PCT = 0.0003     # 0.03% per leg
STT_SELL_PCT  = 0.00025    # 0.025% sell only
TXN_PCT       = 0.0000297  # 0.00297% per leg (NSE)
SEBI_PCT      = 0.000001   # 0.0001% per leg
STAMP_BUY_PCT = 0.00003    # 0.003% buy only
GST_RATE      = 0.18

def _gst_base() -> float:
    return BROKERAGE_PCT + TXN_PCT + SEBI_PCT

BUY_LEG_PCT  = BROKERAGE_PCT + TXN_PCT + SEBI_PCT + STAMP_BUY_PCT + GST_RATE * _gst_base()
SELL_LEG_PCT = BROKERAGE_PCT + TXN_PCT + SEBI_PCT + STT_SELL_PCT  + GST_RATE * _gst_base()
ROUND_TRIP_PCT_REF = BUY_LEG_PCT + SELL_LEG_PCT   # ~0.00106 (10.6 bps)

NIFTY50 = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","ITC","SBIN",
    "BHARTIARTL","LT","KOTAKBANK","BAJFINANCE","AXISBANK","ASIANPAINT","MARUTI",
    "HCLTECH","SUNPHARMA","ULTRACEMCO","TITAN","NESTLEIND","WIPRO","ONGC","NTPC",
    "POWERGRID","M&M","JSWSTEEL","TATAMOTORS","TATASTEEL","COALINDIA","INDUSINDBK",
    "BAJAJFINSV","HDFCLIFE","GRASIM","ADANIENT","ADANIPORTS","DRREDDY","EICHERMOT",
    "BRITANNIA","CIPLA","HEROMOTOCO","BPCL","APOLLOHOSP","TECHM","SBILIFE",
    "BAJAJ-AUTO","TATACONSUM","HINDALCO","LTIM","SHRIRAMFIN","TRENT",
]

END   = date(2026, 6, 11)
START = END - timedelta(days=95)


# ---------------------------------------------------------------------------
# Dhan scrip-master -> security_id map for NSE_EQ
# ---------------------------------------------------------------------------
def load_security_map(path: str = "scrip_master.csv") -> dict[str, str]:
    if not os.path.exists(path):
        url = "https://images.dhan.co/api-data/api-scrip-master.csv"
        r = requests.get(url, timeout=120); r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    df = pd.read_csv(path, low_memory=False)
    df = df[(df["SEM_EXM_EXCH_ID"] == "NSE") &
            (df["SEM_INSTRUMENT_NAME"] == "EQUITY") &
            (df["SEM_SERIES"] == "EQ")]
    return {str(s).upper(): str(int(sid))
            for s, sid in zip(df["SEM_TRADING_SYMBOL"], df["SEM_SMST_SECURITY_ID"])}


# ---------------------------------------------------------------------------
# Dhan data fetchers (with simple rate-limit handling)
# ---------------------------------------------------------------------------
def _post_with_retry(url: str, body: dict, max_retries: int = 8) -> dict | None:
    """Retry on 429/805 (rate limit) and also on transient 400/500/503.
    Dhan sometimes returns 400 'Missing required fields' under load."""
    delay = 1.0
    last_err = ""
    for attempt in range(max_retries):
        try:
            r = requests.post(url, headers=HEADERS, json=body, timeout=45)
        except Exception as e:
            last_err = str(e); time.sleep(delay); delay = min(delay * 1.7, 12); continue
        if r.status_code == 200:
            return r.json()
        try:    last_err = r.json().get("errorMessage", "")[:150]
        except: last_err = r.text[:150]
        if r.status_code in (400, 429, 500, 502, 503, 805):
            time.sleep(delay); delay = min(delay * 1.7, 12); continue
        print(f"   dhan {r.status_code}: {last_err}")
        return None
    print(f"   dhan giving up after {max_retries} retries: {last_err}")
    return None


def synthesize_daily(intraday: pd.DataFrame) -> pd.DataFrame:
    """Build daily OHLC by aggregating the 15-min bars of each trading day."""
    if intraday.empty:
        return pd.DataFrame()
    g = intraday.groupby(intraday["date"])
    daily = pd.DataFrame({
        "open":  g["open"].first(),
        "high":  g["high"].max(),
        "low":   g["low"].min(),
        "close": g["close"].last(),
    })
    daily.index = pd.to_datetime(daily.index)
    return daily.sort_index()


def fetch_intraday_dhan(security_id: str, symbol: str | None = None) -> pd.DataFrame:
    """Dhan caps intraday windows at ~5 days per call -> chunk requests.
    Caches the stitched DataFrame to disk so cost-model tweaks don't refetch."""
    cache_dir = "cache_intraday"
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(
        cache_dir, f"{security_id}_{START.isoformat()}_{END.isoformat()}.parquet"
    )
    if os.path.exists(cache_path):
        try:
            df = pd.read_parquet(cache_path)
            if not df.empty:
                df.index = pd.to_datetime(df.index, utc=True).tz_convert("Asia/Kolkata")
                df["date"] = df.index.date
                return df
        except Exception:
            pass

    out_frames = []
    chunk_start = START
    while chunk_start <= END:
        chunk_end = min(chunk_start + timedelta(days=5), END)
        body = {"securityId": str(security_id), "exchangeSegment": "NSE_EQ",
                "instrument": "EQUITY", "interval": "15",
                "fromDate": chunk_start.isoformat(),
                "toDate":   chunk_end.isoformat()}
        j = _post_with_retry("https://api.dhan.co/v2/charts/intraday", body)
        if j and j.get("timestamp"):
            df = pd.DataFrame({
                "open": j["open"], "high": j["high"],
                "low":  j["low"],  "close": j["close"],
                "ts":   pd.to_datetime(j["timestamp"], unit="s", utc=True),
            })
            df.index = df["ts"].dt.tz_convert("Asia/Kolkata")
            out_frames.append(df[["open","high","low","close"]])
        chunk_start = chunk_end + timedelta(days=1)
        time.sleep(0.6)
    if not out_frames:
        return pd.DataFrame()
    df = pd.concat(out_frames).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    try:
        df.copy().assign(_idx=df.index.tz_convert("UTC")).reset_index(drop=True) \
          .set_index("_idx").to_parquet(cache_path)
    except Exception as e:
        print(f"   cache write failed: {e}")
    df["date"] = df.index.date
    return df


# ---------------------------------------------------------------------------
# Pivot calculation
# ---------------------------------------------------------------------------
def daily_pivots(daily: pd.DataFrame) -> pd.DataFrame:
    p = daily.shift(1)
    H, L, C = p["high"], p["low"], p["close"]
    rng = H - L
    pp  = (H + L + C) / 3.0
    out = pd.DataFrame(index=daily.index)
    out["std_PP"] = pp
    out["std_R1"] = 2*pp - L
    out["std_S1"] = 2*pp - H
    out["std_R2"] = pp + rng
    out["std_S2"] = pp - rng
    out["fib_PP"] = pp
    out["fib_R1"] = pp + 0.382 * rng
    out["fib_S1"] = pp - 0.382 * rng
    out["fib_R2"] = pp + 0.618 * rng
    out["fib_S2"] = pp - 0.618 * rng
    out["cam_R3"] = C + 1.1 * rng / 4.0
    out["cam_S3"] = C - 1.1 * rng / 4.0
    out["cam_R4"] = C + 1.1 * rng / 2.0
    out["cam_S4"] = C - 1.1 * rng / 2.0
    return out


# ---------------------------------------------------------------------------
# Trade engine
# ---------------------------------------------------------------------------
def _ret_long(entry, exit_):
    """Net % return on a LONG round trip after Dhan intraday charges.
    Buy leg pays brokerage+exch+SEBI+stamp+GST on entry value.
    Sell leg pays brokerage+exch+SEBI+STT+GST on exit value."""
    buy_cost  = BUY_LEG_PCT  * entry
    sell_cost = SELL_LEG_PCT * exit_
    gross_pnl = exit_ - entry
    return (gross_pnl - buy_cost - sell_cost) / entry


def _ret_short(entry, exit_):
    """Net % return on an intraday SHORT (sell-then-buy) round trip.
    Sell leg (entry) pays brokerage+exch+SEBI+STT+GST.
    Buy leg  (exit ) pays brokerage+exch+SEBI+stamp+GST.
    Returns are normalised against the sell-side notional."""
    sell_cost = SELL_LEG_PCT * entry
    buy_cost  = BUY_LEG_PCT  * exit_
    gross_pnl = entry - exit_
    return (gross_pnl - sell_cost - buy_cost) / entry


def _walk_long(bars, start_i, target, stop):
    for j in range(start_i, len(bars)):
        hi = float(bars["high"].iloc[j]); lo = float(bars["low"].iloc[j])
        if lo <= stop:   return stop, "stop"
        if hi >= target: return target, "target"
    return float(bars["close"].iloc[-1]), "eod"


def _walk_short(bars, start_i, target, stop):
    for j in range(start_i, len(bars)):
        hi = float(bars["high"].iloc[j]); lo = float(bars["low"].iloc[j])
        if hi >= stop:   return stop, "stop"
        if lo <= target: return target, "target"
    return float(bars["close"].iloc[-1]), "eod"


def trades_bounce_long(bars, lvl_entry, target, stop):
    if any(pd.isna(x) for x in (lvl_entry, target, stop)): return []
    if lvl_entry <= stop or target <= lvl_entry:           return []
    for i in range(len(bars)):
        lo = float(bars["low"].iloc[i])
        hi = float(bars["high"].iloc[i])
        op = float(bars["open"].iloc[i])
        if lo <= lvl_entry:
            entry = min(op, lvl_entry) if op < lvl_entry else lvl_entry
            if lo <= stop:
                return [{"entry": entry, "exit": stop, "ret": _ret_long(entry, stop), "outcome": "stop"}]
            if hi >= target:
                return [{"entry": entry, "exit": target, "ret": _ret_long(entry, target), "outcome": "target"}]
            ex, oc = _walk_long(bars, i + 1, target, stop)
            return [{"entry": entry, "exit": ex, "ret": _ret_long(entry, ex), "outcome": oc}]
    return []


def trades_bounce_short(bars, lvl_entry, target, stop):
    if any(pd.isna(x) for x in (lvl_entry, target, stop)): return []
    if lvl_entry >= stop or target >= lvl_entry:           return []
    for i in range(len(bars)):
        hi = float(bars["high"].iloc[i])
        lo = float(bars["low"].iloc[i])
        op = float(bars["open"].iloc[i])
        if hi >= lvl_entry:
            entry = max(op, lvl_entry) if op > lvl_entry else lvl_entry
            if hi >= stop:
                return [{"entry": entry, "exit": stop, "ret": _ret_short(entry, stop), "outcome": "stop"}]
            if lo <= target:
                return [{"entry": entry, "exit": target, "ret": _ret_short(entry, target), "outcome": "target"}]
            ex, oc = _walk_short(bars, i + 1, target, stop)
            return [{"entry": entry, "exit": ex, "ret": _ret_short(entry, ex), "outcome": oc}]
    return []


def trades_breakout_long(bars, breakout_lvl, target, stop):
    if any(pd.isna(x) for x in (breakout_lvl, target, stop)): return []
    if target <= breakout_lvl or stop >= breakout_lvl:        return []
    for i in range(len(bars) - 1):
        if float(bars["close"].iloc[i]) > breakout_lvl:
            entry = float(bars["open"].iloc[i + 1])
            ex, oc = _walk_long(bars, i + 1, target, stop)
            return [{"entry": entry, "exit": ex, "ret": _ret_long(entry, ex), "outcome": oc}]
    return []


def trades_breakdown_short(bars, breakdown_lvl, target, stop):
    if any(pd.isna(x) for x in (breakdown_lvl, target, stop)): return []
    if target >= breakdown_lvl or stop <= breakdown_lvl:       return []
    for i in range(len(bars) - 1):
        if float(bars["close"].iloc[i]) < breakdown_lvl:
            entry = float(bars["open"].iloc[i + 1])
            ex, oc = _walk_short(bars, i + 1, target, stop)
            return [{"entry": entry, "exit": ex, "ret": _ret_short(entry, ex), "outcome": oc}]
    return []


def run_day_strategies(bars, piv):
    out = {}
    out["STD_S1_BOUNCE_LONG"]     = trades_bounce_long (bars, piv["std_S1"], piv["std_PP"], piv["std_S2"])
    out["STD_R1_BOUNCE_SHORT"]    = trades_bounce_short(bars, piv["std_R1"], piv["std_PP"], piv["std_R2"])
    out["STD_R1_BREAKOUT_LONG"]   = trades_breakout_long  (bars, piv["std_R1"], piv["std_R2"], piv["std_PP"])
    out["STD_S1_BREAKDOWN_SHORT"] = trades_breakdown_short(bars, piv["std_S1"], piv["std_S2"], piv["std_PP"])
    out["FIB_S1_BOUNCE_LONG"]     = trades_bounce_long (bars, piv["fib_S1"], piv["fib_PP"], piv["fib_S2"])
    out["FIB_R1_BOUNCE_SHORT"]    = trades_bounce_short(bars, piv["fib_R1"], piv["fib_PP"], piv["fib_R2"])
    out["CAM_L3_LONG"]            = trades_bounce_long (bars, piv["cam_S3"], piv["cam_R3"], piv["cam_S4"])
    out["CAM_S3_SHORT"]           = trades_bounce_short(bars, piv["cam_R3"], piv["cam_S3"], piv["cam_R4"])
    cam_ext_up   = piv["cam_R4"] + (piv["cam_R4"] - piv["cam_S4"]) * 0.5
    cam_ext_down = piv["cam_S4"] - (piv["cam_R4"] - piv["cam_S4"]) * 0.5
    out["CAM_R4_BREAKOUT_LONG"]   = trades_breakout_long  (bars, piv["cam_R4"], cam_ext_up,   piv["cam_R3"])
    out["CAM_S4_BREAKDOWN_SHORT"] = trades_breakdown_short(bars, piv["cam_S4"], cam_ext_down, piv["cam_S3"])
    return out


# ---------------------------------------------------------------------------
@dataclass
class Result:
    stock: str; strategy: str
    trades: int; wins: int
    win_rate: float; avg_ret: float
    total_ret: float; bh_ret: float
    def row(self): return self.__dict__


def backtest_symbol(symbol, intraday, daily):
    if intraday.empty or daily.empty:
        return []
    piv = daily_pivots(daily)
    grouped: dict[str, list[dict]] = {}
    for d in sorted(intraday["date"].unique()):
        if pd.Timestamp(d) not in piv.index:
            continue
        row = piv.loc[pd.Timestamp(d)]
        if row.isna().all():
            continue
        bars = intraday[intraday["date"] == d]
        if len(bars) < 5:
            continue
        for k, v in run_day_strategies(bars, row).items():
            grouped.setdefault(k, []).extend(v)
    bh = float(intraday["close"].iloc[-1] / intraday["open"].iloc[0] - 1)
    out = []
    for k, trades in grouped.items():
        n = len(trades)
        wins = sum(1 for t in trades if t["ret"] > 0)
        wr   = wins / n if n else 0.0
        avg  = float(np.mean([t["ret"] for t in trades])) if n else 0.0
        comp = float(np.prod([1 + t["ret"] for t in trades]) - 1) if n else 0.0
        out.append(Result(symbol, k, n, wins, wr, avg, comp, bh))
    return out


# ---------------------------------------------------------------------------
def main():
    print("=" * 80)
    print("INTRADAY pivot-point backtest — Nifty 50 — last ~3 months — DHAN DATA API")
    print("=" * 80)
    print(f"Window: {START} -> {END}    bars: 15-min")
    print(f"Cost model: Dhan intraday equity (NSE EQ)")
    print(f"  buy-leg  : {BUY_LEG_PCT*100:.4f}% "
          f"(brokerage 0.03% + exch 0.00297% + SEBI 0.0001% + stamp 0.003% + GST)")
    print(f"  sell-leg : {SELL_LEG_PCT*100:.4f}% "
          f"(brokerage 0.03% + exch 0.00297% + SEBI 0.0001% + STT 0.025% + GST)")
    print(f"  round-trip ~{ROUND_TRIP_PCT_REF*100:.4f}% of turnover "
          f"(Rs 20/order cap NOT applied)")
    print()

    sec_map = load_security_map()
    missing = [s for s in NIFTY50 if s.upper() not in sec_map]
    if missing:
        print(f"Symbols not in Dhan scrip master: {missing}")

    all_results: list[Result] = []
    for i, sym in enumerate(NIFTY50, 1):
        sid = sec_map.get(sym.upper())
        if not sid:
            print(f"[{i:2d}/{len(NIFTY50)}] {sym:<12} no security_id"); continue
        try:
            intra = fetch_intraday_dhan(sid)
        except Exception as e:
            print(f"[{i:2d}/{len(NIFTY50)}] {sym:<12} fetch error: {e}"); continue
        if intra.empty:
            print(f"[{i:2d}/{len(NIFTY50)}] {sym:<12} sid={sid} no intraday data"); continue
        daily = synthesize_daily(intra)
        results = backtest_symbol(sym, intra, daily)
        all_results.extend(results)
        n_days = intra["date"].nunique(); n_bars = len(intra)
        n_tr   = sum(r.trades for r in results)
        bh     = results[0].bh_ret if results else 0.0
        print(f"[{i:2d}/{len(NIFTY50)}] {sym:<12} sid={sid:<8} days={n_days:3d} "
              f"bars={n_bars:5d} trades={n_tr:4d} BH={bh*100:+6.2f}%")

    df = pd.DataFrame([r.row() for r in all_results])
    df.to_csv("results_intraday_dhan.csv", index=False)

    print("\n" + "=" * 80)
    print("INTRADAY SUMMARY (Dhan Data API)")
    print("=" * 80)

    df_t = df[df["trades"] >= 5].copy()
    if df_t.empty:
        print("No (stock, strategy) combo produced >=5 trades."); return
    df_t["edge_vs_bh"] = df_t["total_ret"] - df_t["bh_ret"]

    print("\nTop 15 (stock, strategy) combos by total return:")
    top = df_t.sort_values("total_ret", ascending=False).head(15)
    print(top[["stock","strategy","trades","wins","win_rate",
              "avg_ret","total_ret","bh_ret","edge_vs_bh"]]
          .to_string(index=False,
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
        avg_win_rate=("win_rate","mean"),
        avg_trades=("trades","mean"),
        n_combos=("stock","count"),
        positive_combos=("total_ret", lambda s: int((s>0).sum())),
    ).sort_values("avg_total_ret", ascending=False)
    print(agg.to_string(formatters={"avg_total_ret":"{:+.2%}".format,
                                    "avg_win_rate":"{:.1%}".format,
                                    "avg_trades":"{:.1f}".format}))

    w = df_t.sort_values("total_ret", ascending=False).iloc[0]
    print("\n" + "-" * 80)
    print(f"BEST INTRADAY COMBO  ->  {w['stock']}  +  {w['strategy']}")
    print(f"  trades   : {int(w['trades'])}")
    print(f"  win rate : {w['win_rate']:.1%}")
    print(f"  avg ret  : {w['avg_ret']:+.2%} per trade")
    print(f"  total    : {w['total_ret']:+.2%}  "
          f"(BH: {w['bh_ret']:+.2%}, edge {w['edge_vs_bh']:+.2%})")
    print("-" * 80)
    print("Detailed per-combination results -> results_intraday_dhan.csv")


if __name__ == "__main__":
    main()
