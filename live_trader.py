"""
LIVE INTRADAY PIVOT TRADER — top-10 (stock, strategy) combos from the backtest.

================================================================================
                ⚠⚠⚠   READ THIS BEFORE FLIPPING DRY_RUN OFF   ⚠⚠⚠
================================================================================

1. This bot can place REAL MONEY ORDERS through your Dhan account.
2. Past performance (the backtest) is not a guarantee of future returns.
3. Default config is `DRY_RUN = True` — it will only PRINT intended actions.
   To go live: set `DRY_RUN = False` AND set `I_UNDERSTAND_THE_RISKS = True`.
4. Default product type is `INTRADAY` (MIS). All positions are forcibly
   squared-off at 15:15 IST regardless of P&L.
5. Default per-trade notional is Rs 15,000 (< Rs 20K so brokerage cap rarely
   binds, gives meaningful position size). Tweak in CONFIG below.
6. Dhan's max ~5 orders/sec rate-limit applies; we self-throttle.

================================================================================
                        TRADE PLANS (auto-loaded from backtest)
================================================================================
At startup, the bot reads `results_intraday_dhan.csv` and selects the top-10
(stock, strategy) combos by backtest `total_ret` (filtered to bounce/fade
strategies that the live engine supports). If the CSV is missing or has
fewer than 10 eligible combos, it falls back to the hardcoded snapshot
below — same set the bot shipped with on 2026-06-13.

   #  Stock        Strategy                Side    Entry    Target   Stop
   1  POWERGRID    STD_R1_BOUNCE_SHORT     SHORT   std_R1   std_PP   std_R2
   2  HDFCBANK     FIB_S1_BOUNCE_LONG      LONG    fib_S1   fib_PP   fib_S2
   3  MARUTI       CAM_S3_SHORT            SHORT   cam_R3   cam_S3   cam_R4
   4  BPCL         FIB_S1_BOUNCE_LONG      LONG    fib_S1   fib_PP   fib_S2
   5  BPCL         FIB_R1_BOUNCE_SHORT     SHORT   fib_R1   fib_PP   fib_R2
   6  COALINDIA    CAM_L3_LONG             LONG    cam_S3   cam_R3   cam_S4
   7  BPCL         STD_R1_BOUNCE_SHORT     SHORT   std_R1   std_PP   std_R2
   8  KOTAKBANK    STD_S1_BOUNCE_LONG      LONG    std_S1   std_PP   std_S2
   9  NTPC         CAM_S3_SHORT            SHORT   cam_R3   cam_S3   cam_R4
  10  NESTLEIND    FIB_S1_BOUNCE_LONG      LONG    fib_S1   fib_PP   fib_S2

Weekly refresh workflow:
   1.  python backtest_intraday_dhan.py     # regenerates results_*.csv
   2.  python live_trader.py --once         # confirm the new top-10 prints
   3.  python live_trader.py                # run live (or dry-run)

================================================================================
                                 RUN-TIME FLOW
================================================================================
1. ~09:10 IST  Pre-market: fetch yesterday's daily OHLC for each stock,
                compute pivots, build today's 10 trade plans.
2.  09:15 IST  Market opens. Begin polling LTP for each of the 10 names every
                ~5s (one consolidated LTP request per cycle).
3.  intra-day  When LTP crosses the entry trigger:
                 - LONG  bounce: when LTP <= entry level
                 - SHORT bounce: when LTP >= entry level
                 -> place a LIMIT order at the entry level (MIS, DAY).
                After fill: place SL-M stop and a LIMIT target. First-to-fill
                wins; the other is cancelled.
4.  15:15 IST  Force-square-off any open position via MARKET order.
5.  15:25 IST  Cancel any unfilled pending orders, write trade log, exit.

================================================================================
"""

from __future__ import annotations
import os, sys, json, time, signal, logging, argparse
from datetime import datetime, date, timedelta, time as dtime
from dataclasses import dataclass, field
from typing import Optional

import requests
import pandas as pd
from dotenv import load_dotenv

# =============================================================================
# CONFIG — edit before going live
# =============================================================================
DRY_RUN                = True             # << set False to actually trade
I_UNDERSTAND_THE_RISKS = False            # << must also be True to go live
PER_TRADE_NOTIONAL_INR = 15_000           # static fallback notional/position
MAX_CONCURRENT_TRADES  = 6                # safety cap

# --- Margin / dynamic sizing ---------------------------------------------
# Set USE_DYNAMIC_SIZING = True to size each trade from your Dhan balance and
# Dhan's MIS leverage instead of using the fixed Rs PER_TRADE_NOTIONAL_INR.
#
# Effective per-trade notional (when USE_DYNAMIC_SIZING is True) =
#     available_balance * MAX_CAPITAL_DEPLOYED_PCT * LEVERAGE / MAX_CONCURRENT_TRADES
#
# Example (Rs 1.94L balance, 80% deployed, 5x leverage, 6 slots):
#     1.94L * 0.80 * 5 / 6  ~=  Rs 1.30L per trade
# That gives roughly Rs 7.8L of total intraday exposure — about 4x cash.
#
# WARNING: leverage scales BOTH gains AND losses. A 1% adverse move on a
# 5x-leveraged position is a 5% loss on your cash. Start at LEVERAGE = 1.0
# (cash only, full deployment) for the first live week.
USE_DYNAMIC_SIZING       = False          # keep PER_TRADE_NOTIONAL_INR by default
LEVERAGE                 = 1.0            # 1.0 = cash only; up to ~5.0 on Nifty 50 MIS
MAX_CAPITAL_DEPLOYED_PCT = 0.80           # buffer vs Dhan's hard balance limit
MARGIN_PRECHECK          = True           # call /v2/margincalculator before each order
# ------------------------------------------------------------------------

PRODUCT_TYPE           = "INTRADAY"        # MIS — force square-off by EOD
SQUARE_OFF_TIME        = dtime(15, 15)    # IST
HARD_STOP_TIME         = dtime(15, 25)    # IST — final cleanup
ENTRY_WINDOW_END       = dtime(14, 30)    # don't enter new trades after this
POLL_INTERVAL_SEC      = 10               # LTP poll cadence (intraday-fallback friendly)
ORDER_RATE_DELAY_SEC   = 0.25             # between any two order calls

# =============================================================================
# Logging
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"live_trader_{date.today().isoformat()}.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger("livetrader")

# =============================================================================
# Trade plan definitions — locked from the backtest top-10
# =============================================================================
@dataclass
class TradePlan:
    rank: int
    symbol: str
    strategy: str
    side: str             # "LONG" or "SHORT"
    entry_lvl_key: str    # column name in pivots dict
    target_lvl_key: str
    stop_lvl_key: str

    # populated at runtime
    security_id: str = ""
    qty: int = 0
    entry_price: float = 0.0
    target_price: float = 0.0
    stop_price: float = 0.0

    # state machine: PENDING -> ENTRY_PLACED -> FILLED -> EXITED / CANCELLED
    state: str = "PENDING"
    entry_order_id: Optional[str] = None
    target_order_id: Optional[str] = None
    stop_order_id: Optional[str] = None
    filled_at: Optional[float] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None


TOP10_HARDCODED: list[TradePlan] = [
    TradePlan(1,  "POWERGRID", "STD_R1_BOUNCE_SHORT", "SHORT", "std_R1", "std_PP", "std_R2"),
    TradePlan(2,  "HDFCBANK",  "FIB_S1_BOUNCE_LONG",  "LONG",  "fib_S1", "fib_PP", "fib_S2"),
    TradePlan(3,  "MARUTI",    "CAM_S3_SHORT",        "SHORT", "cam_R3", "cam_S3", "cam_R4"),
    TradePlan(4,  "BPCL",      "FIB_S1_BOUNCE_LONG",  "LONG",  "fib_S1", "fib_PP", "fib_S2"),
    TradePlan(5,  "BPCL",      "FIB_R1_BOUNCE_SHORT", "SHORT", "fib_R1", "fib_PP", "fib_R2"),
    TradePlan(6,  "COALINDIA", "CAM_L3_LONG",         "LONG",  "cam_S3", "cam_R3", "cam_S4"),
    TradePlan(7,  "BPCL",      "STD_R1_BOUNCE_SHORT", "SHORT", "std_R1", "std_PP", "std_R2"),
    TradePlan(8,  "KOTAKBANK", "STD_S1_BOUNCE_LONG",  "LONG",  "std_S1", "std_PP", "std_S2"),
    TradePlan(9,  "NTPC",      "CAM_S3_SHORT",        "SHORT", "cam_R3", "cam_S3", "cam_R4"),
    TradePlan(10, "NESTLEIND", "FIB_S1_BOUNCE_LONG",  "LONG",  "fib_S1", "fib_PP", "fib_S2"),
]


# Strategy name -> (side, entry_lvl_key, target_lvl_key, stop_lvl_key).
# Used to auto-rebuild TOP10 from a fresh results CSV. Only bounce / fade
# strategies are listed here because the live engine currently implements
# limit-style entries; breakout / breakdown variants need a different entry
# trigger and are skipped during auto-load.
STRATEGY_MAP: dict[str, tuple[str, str, str, str]] = {
    "STD_S1_BOUNCE_LONG":  ("LONG",  "std_S1", "std_PP", "std_S2"),
    "STD_R1_BOUNCE_SHORT": ("SHORT", "std_R1", "std_PP", "std_R2"),
    "FIB_S1_BOUNCE_LONG":  ("LONG",  "fib_S1", "fib_PP", "fib_S2"),
    "FIB_R1_BOUNCE_SHORT": ("SHORT", "fib_R1", "fib_PP", "fib_R2"),
    "CAM_L3_LONG":         ("LONG",  "cam_S3", "cam_R3", "cam_S4"),
    "CAM_S3_SHORT":        ("SHORT", "cam_R3", "cam_S3", "cam_R4"),
}


def load_top10(results_csv: str = "results_intraday_dhan.csv",
               min_trades: int = 10) -> list[TradePlan]:
    """Auto-load the top-10 trade plans from the latest backtest results.

    Falls back to the hardcoded TOP10_HARDCODED list if the CSV is missing,
    unparseable, or yields fewer than 10 valid combinations.
    Only strategies present in STRATEGY_MAP are eligible (bounce / fade
    families). Breakout / breakdown variants are skipped because the live
    engine doesn't currently implement their next-bar-on-close trigger."""
    if not os.path.exists(results_csv):
        log.warning(f"   {results_csv} not found — using hardcoded TOP10")
        return [TradePlan(p.rank, p.symbol, p.strategy, p.side,
                          p.entry_lvl_key, p.target_lvl_key, p.stop_lvl_key)
                for p in TOP10_HARDCODED]
    try:
        df = pd.read_csv(results_csv)
        df = df[df["trades"] >= min_trades]
        df = df[df["strategy"].isin(STRATEGY_MAP.keys())]
        df = df.sort_values("total_ret", ascending=False).head(10).reset_index(drop=True)
        if len(df) < 10:
            log.warning(f"   {results_csv} only yielded {len(df)} eligible "
                        f"combos (need 10) — using hardcoded TOP10")
            return [TradePlan(p.rank, p.symbol, p.strategy, p.side,
                              p.entry_lvl_key, p.target_lvl_key, p.stop_lvl_key)
                    for p in TOP10_HARDCODED]
    except Exception as e:
        log.warning(f"   could not read {results_csv}: {e} — using hardcoded TOP10")
        return [TradePlan(p.rank, p.symbol, p.strategy, p.side,
                          p.entry_lvl_key, p.target_lvl_key, p.stop_lvl_key)
                for p in TOP10_HARDCODED]

    plans: list[TradePlan] = []
    for i, row in df.iterrows():
        side, ek, tk, sk = STRATEGY_MAP[row["strategy"]]
        plans.append(TradePlan(i + 1, row["stock"], row["strategy"],
                               side, ek, tk, sk))
    log.info(f"   loaded TOP10 from {results_csv} (latest backtest):")
    for p, r in zip(plans, df.itertuples()):
        log.info(f"     #{p.rank:>2} {p.symbol:<11} {p.strategy:<22} "
                 f"backtest_total={r.total_ret*100:+6.2f}%  "
                 f"win={r.win_rate*100:>4.1f}%  trades={int(r.trades)}")
    return plans


# Active list (auto-loaded at startup unless the CSV is missing/stale).
TOP10: list[TradePlan] = []  # populated by main() via load_top10()


# =============================================================================
# Dhan API client
# =============================================================================
class DhanClient:
    BASE = "https://api.dhan.co/v2"

    def __init__(self):
        load_dotenv()
        self.token  = os.environ["DHAN_ACCESS_TOKEN"]
        self.client = os.environ["DHAN_CLIENT_ID"]
        self.h = {"access-token": self.token, "client-id": self.client,
                  "Content-Type": "application/json", "Accept": "application/json"}
        self._last_call = 0.0

    def _throttle(self):
        wait = ORDER_RATE_DELAY_SEC - (time.time() - self._last_call)
        if wait > 0: time.sleep(wait)
        self._last_call = time.time()

    def fundlimit(self) -> dict:
        r = requests.get(f"{self.BASE}/fundlimit", headers=self.h, timeout=15)
        r.raise_for_status(); return r.json()

    def historical_daily(self, security_id: str, days: int = 10) -> pd.DataFrame:
        self._throttle()
        end = date.today()
        body = {"securityId": str(security_id), "exchangeSegment": "NSE_EQ",
                "instrument": "EQUITY", "expiryCode": 0, "oi": False,
                "fromDate": (end - timedelta(days=days)).isoformat(),
                "toDate":   end.isoformat()}
        r = requests.post(f"{self.BASE}/charts/historical",
                          headers=self.h, json=body, timeout=30)
        if r.status_code != 200:
            log.warning(f"   historical failed for sid={security_id}: "
                        f"{r.status_code} {r.text[:120]}")
            return pd.DataFrame()
        j = r.json()
        if not j or "timestamp" not in j or not j["timestamp"]:
            return pd.DataFrame()
        df = pd.DataFrame({"open": j["open"], "high": j["high"],
                           "low": j["low"], "close": j["close"],
                           "ts": pd.to_datetime(j["timestamp"], unit="s",
                                                utc=True)})
        df.index = df["ts"].dt.tz_convert("Asia/Kolkata").dt.normalize() \
                          .dt.tz_localize(None)
        return df[["open","high","low","close"]].sort_index()

    def intraday_15m(self, security_id: str, days: int = 5) -> pd.DataFrame:
        """Used as fallback to synthesize daily pivots if daily endpoint
        returns the known DH-905 quirk for some symbols."""
        self._throttle()
        end = date.today()
        body = {"securityId": str(security_id), "exchangeSegment": "NSE_EQ",
                "instrument": "EQUITY", "interval": "15",
                "fromDate": (end - timedelta(days=days)).isoformat(),
                "toDate":   end.isoformat()}
        r = requests.post(f"{self.BASE}/charts/intraday",
                          headers=self.h, json=body, timeout=30)
        if r.status_code != 200:
            log.warning(f"   intraday failed for sid={security_id}: "
                        f"{r.status_code} {r.text[:120]}")
            return pd.DataFrame()
        j = r.json()
        if not j or "timestamp" not in j or not j["timestamp"]:
            return pd.DataFrame()
        df = pd.DataFrame({"open": j["open"], "high": j["high"],
                           "low": j["low"], "close": j["close"],
                           "ts": pd.to_datetime(j["timestamp"], unit="s",
                                                utc=True)})
        df.index = df["ts"].dt.tz_convert("Asia/Kolkata")
        df["date"] = df.index.date
        return df

    def ltp(self, sid_to_symbol: dict[str, str]) -> dict[str, float]:
        """Single batched LTP request for all the day's tickers.
        Returns map symbol -> last price.
        Falls back to /charts/intraday 1-min bars if /marketfeed/ltp is not
        subscribed (Dhan returns 401-808 in that case)."""
        self._throttle()
        body = {"NSE_EQ": [int(s) for s in sid_to_symbol]}
        r = requests.post(f"{self.BASE}/marketfeed/ltp",
                          headers=self.h, json=body, timeout=15)
        if r.status_code == 200:
            j = r.json()
            out = {}
            try:
                inner = j.get("data", {}).get("NSE_EQ", {})
                for sid, sym in sid_to_symbol.items():
                    v = inner.get(str(sid)) or inner.get(int(sid))
                    if v and "last_price" in v:
                        out[sym] = float(v["last_price"])
            except Exception as e:
                log.warning(f"   ltp parse error: {e}")
            return out

        # 401 / 808 = no marketfeed subscription -> fall back to intraday bars
        try:
            err = r.json().get("data", {})
            err_msg = next(iter(err.values())) if isinstance(err, dict) and err else r.text[:120]
        except Exception:
            err_msg = r.text[:120]
        if not getattr(self, "_ltp_fallback_announced", False):
            log.warning(f"   /marketfeed/ltp returned {r.status_code}: {err_msg}")
            log.warning("   -> falling back to /charts/intraday 1-min bars "
                        "(LTP may be up to ~1 min stale)")
            self._ltp_fallback_announced = True
        return self._ltp_via_intraday(sid_to_symbol)

    def _ltp_via_intraday(self, sid_to_symbol: dict[str, str]) -> dict[str, float]:
        """Last-resort LTP source: read the close of the most recent 1-min bar
        for each security. Slower than /marketfeed/ltp but doesn't need the
        live-quote subscription."""
        out = {}
        today = date.today().isoformat()
        yday  = (date.today() - timedelta(days=1)).isoformat()
        for sid, sym in sid_to_symbol.items():
            self._throttle()
            body = {"securityId": str(sid), "exchangeSegment": "NSE_EQ",
                    "instrument": "EQUITY", "interval": "1",
                    "fromDate": yday, "toDate": today}
            try:
                r = requests.post(f"{self.BASE}/charts/intraday",
                                  headers=self.h, json=body, timeout=15)
                if r.status_code == 200:
                    j = r.json()
                    if j and j.get("close"):
                        out[sym] = float(j["close"][-1])
                elif r.status_code in (429, 805):
                    # rate-limited; skip this tick for this sid
                    pass
                else:
                    if not getattr(self, "_intraday_fallback_warned", set()).__contains__(sym):
                        log.warning(f"   intraday-ltp failed for {sym}: "
                                    f"{r.status_code} {r.text[:80]}")
                        self._intraday_fallback_warned = \
                            getattr(self, "_intraday_fallback_warned", set()) | {sym}
            except Exception as e:
                log.warning(f"   intraday-ltp exception for {sym}: {e}")
        return out

    # -------- ORDER CALLS (gated by DRY_RUN) --------
    def place_order(self, **kwargs) -> Optional[str]:
        body = {"dhanClientId": self.client, "validity": "DAY",
                "disclosedQuantity": 0, **kwargs}
        log.info(f"   ORDER -> {body['transactionType']:<4} "
                 f"{body['orderType']:<14} sid={body['securityId']} "
                 f"qty={body['quantity']} "
                 f"price={body.get('price',0)} trig={body.get('triggerPrice',0)}")
        if DRY_RUN or not I_UNDERSTAND_THE_RISKS:
            return f"DRY-{int(time.time()*1000)}"
        self._throttle()
        r = requests.post(f"{self.BASE}/orders", headers=self.h,
                          json=body, timeout=15)
        if r.status_code not in (200, 201):
            log.error(f"   place_order rejected: {r.status_code} {r.text[:200]}")
            return None
        return r.json().get("orderId")

    def cancel_order(self, order_id: str) -> bool:
        if not order_id or order_id.startswith("DRY-"):
            return True
        self._throttle()
        r = requests.delete(f"{self.BASE}/orders/{order_id}",
                            headers=self.h, timeout=15)
        if r.status_code in (200, 202): return True
        log.warning(f"   cancel_order {order_id}: {r.status_code} {r.text[:120]}")
        return False

    def order_status(self, order_id: str) -> dict:
        if not order_id or order_id.startswith("DRY-"):
            return {"orderStatus": "DRY"}
        self._throttle()
        r = requests.get(f"{self.BASE}/orders/{order_id}",
                         headers=self.h, timeout=15)
        if r.status_code != 200: return {}
        j = r.json()
        return j[0] if isinstance(j, list) and j else j

    def margin_required(self, security_id: str, txn: str, qty: int,
                        price: float) -> dict:
        """Return Dhan's margin breakdown for a hypothetical INTRADAY order.
        Keys we care about: totalMargin, availableBalance, leverage."""
        self._throttle()
        body = {"dhanClientId": self.client, "exchangeSegment": "NSE_EQ",
                "transactionType": txn, "quantity": int(qty),
                "productType": PRODUCT_TYPE, "securityId": str(security_id),
                "price": float(price)}
        r = requests.post(f"{self.BASE}/margincalculator", headers=self.h,
                          json=body, timeout=15)
        if r.status_code != 200:
            log.warning(f"   margincalculator {r.status_code}: {r.text[:120]}")
            return {}
        return r.json()


# =============================================================================
# Pivot computation
# =============================================================================
def compute_pivots(daily: pd.DataFrame) -> dict[str, float]:
    """Pivots from the *most recent* completed daily bar."""
    if daily.empty: return {}
    p = daily.iloc[-1]
    H, L, C = p["high"], p["low"], p["close"]
    rng = H - L
    pp  = (H + L + C) / 3.0
    return {
        "std_PP": pp,
        "std_R1": 2*pp - L,  "std_S1": 2*pp - H,
        "std_R2": pp + rng,  "std_S2": pp - rng,
        "fib_PP": pp,
        "fib_R1": pp + 0.382 * rng, "fib_S1": pp - 0.382 * rng,
        "fib_R2": pp + 0.618 * rng, "fib_S2": pp - 0.618 * rng,
        "cam_R3": C + 1.1 * rng / 4.0, "cam_S3": C - 1.1 * rng / 4.0,
        "cam_R4": C + 1.1 * rng / 2.0, "cam_S4": C - 1.1 * rng / 2.0,
    }


def synthesize_yesterday_from_intraday(intra: pd.DataFrame) -> pd.DataFrame:
    if intra.empty: return pd.DataFrame()
    g = intra.groupby(intra["date"]).agg(open=("open","first"),
                                          high=("high","max"),
                                          low=("low","min"),
                                          close=("close","last"))
    g.index = pd.to_datetime(g.index)
    return g.sort_index().iloc[:-1] if len(g) > 1 else g  # exclude today


# =============================================================================
# Symbol resolution from Dhan scrip master
# =============================================================================
def load_security_map(path: str = "scrip_master.csv") -> dict[str, str]:
    if not os.path.exists(path):
        log.info("downloading scrip master ...")
        r = requests.get("https://images.dhan.co/api-data/api-scrip-master.csv",
                         timeout=120); r.raise_for_status()
        with open(path, "wb") as f: f.write(r.content)
    df = pd.read_csv(path, low_memory=False)
    df = df[(df["SEM_EXM_EXCH_ID"] == "NSE") &
            (df["SEM_INSTRUMENT_NAME"] == "EQUITY") &
            (df["SEM_SERIES"] == "EQ")]
    return {str(s).upper(): str(int(sid))
            for s, sid in zip(df["SEM_TRADING_SYMBOL"],
                              df["SEM_SMST_SECURITY_ID"])}


# =============================================================================
# Pre-market: build today's plans
# =============================================================================
def build_todays_plans(client: DhanClient,
                       sec_map: dict[str, str],
                       available_balance: float = 0.0) -> list[TradePlan]:
    """Compute today's pivots, sizing each plan's quantity based on either
    PER_TRADE_NOTIONAL_INR (static) or the user's available balance times
    Dhan MIS leverage (dynamic)."""
    if USE_DYNAMIC_SIZING and available_balance > 0:
        per_trade_notional = (available_balance
                              * MAX_CAPITAL_DEPLOYED_PCT
                              * LEVERAGE
                              / MAX_CONCURRENT_TRADES)
        log.info(f"  dynamic sizing  : per_trade_notional = Rs {per_trade_notional:,.0f} "
                 f"(balance Rs {available_balance:,.0f} x "
                 f"{MAX_CAPITAL_DEPLOYED_PCT*100:.0f}% x "
                 f"{LEVERAGE:.1f}x / {MAX_CONCURRENT_TRADES} slots)")
    else:
        per_trade_notional = PER_TRADE_NOTIONAL_INR
        log.info(f"  static sizing   : per_trade_notional = Rs {per_trade_notional:,.0f}")

    out = []
    seen_pivots: dict[str, dict[str, float]] = {}
    for plan in TOP10:
        sid = sec_map.get(plan.symbol.upper())
        if not sid:
            log.error(f"#{plan.rank} {plan.symbol}: no security_id, skipping")
            continue
        plan.security_id = sid

        if plan.symbol not in seen_pivots:
            daily = client.historical_daily(sid, days=10)
            if daily.empty or len(daily) < 2:
                log.warning(f"   daily endpoint empty for {plan.symbol}; "
                            f"falling back to intraday-synthesized daily")
                intra = client.intraday_15m(sid, days=5)
                daily = synthesize_yesterday_from_intraday(intra)
            if daily.empty:
                log.error(f"#{plan.rank} {plan.symbol}: cannot compute pivots")
                continue
            seen_pivots[plan.symbol] = compute_pivots(daily)
            log.info(f"   pivots for {plan.symbol}: "
                     f"PP={seen_pivots[plan.symbol]['std_PP']:.2f}  "
                     f"R1={seen_pivots[plan.symbol]['std_R1']:.2f}  "
                     f"S1={seen_pivots[plan.symbol]['std_S1']:.2f}")

        piv = seen_pivots[plan.symbol]
        plan.entry_price  = round(piv[plan.entry_lvl_key],  2)
        plan.target_price = round(piv[plan.target_lvl_key], 2)
        plan.stop_price   = round(piv[plan.stop_lvl_key],   2)
        if plan.side == "LONG" and not (plan.target_price > plan.entry_price > plan.stop_price):
            log.warning(f"#{plan.rank} {plan.symbol}: invalid LONG levels "
                        f"E={plan.entry_price} T={plan.target_price} S={plan.stop_price}; skipping")
            continue
        if plan.side == "SHORT" and not (plan.stop_price > plan.entry_price > plan.target_price):
            log.warning(f"#{plan.rank} {plan.symbol}: invalid SHORT levels "
                        f"E={plan.entry_price} T={plan.target_price} S={plan.stop_price}; skipping")
            continue

        plan.qty = max(1, int(per_trade_notional // plan.entry_price))
        out.append(plan)

    log.info("=" * 100)
    log.info("TODAY'S TRADE PLANS")
    log.info(f"  {'#':>2} {'Stock':<11} {'Strategy':<22} {'Side':<5} "
             f"{'Entry':>9} {'Target':>9} {'Stop':>9} {'Qty':>5} "
             f"{'Notional':>10} {'Margin':>9}")
    total_margin_top6 = 0.0
    for i, p in enumerate(out):
        notional = p.qty * p.entry_price
        margin_str = "?"
        margin_val = 0.0
        if MARGIN_PRECHECK:
            txn = "BUY" if p.side == "LONG" else "SELL"
            m = client.margin_required(p.security_id, txn, p.qty, p.entry_price)
            if m:
                margin_val = float(m.get("totalMargin", 0))
                margin_str = f"{margin_val:>9,.0f}"
        log.info(f"  {p.rank:>2} {p.symbol:<11} {p.strategy:<22} {p.side:<5} "
                 f"{p.entry_price:>9.2f} {p.target_price:>9.2f} "
                 f"{p.stop_price:>9.2f} {p.qty:>5} "
                 f"{notional:>10,.0f} {margin_str}")
        if i < MAX_CONCURRENT_TRADES:
            total_margin_top6 += margin_val
    log.info("-" * 100)
    if MARGIN_PRECHECK and total_margin_top6 > 0 and available_balance > 0:
        pct = total_margin_top6 / available_balance * 100
        log.info(f"  if top {MAX_CONCURRENT_TRADES} all fill: total margin = "
                 f"Rs {total_margin_top6:,.0f}  ({pct:.1f}% of balance "
                 f"Rs {available_balance:,.0f})")
        if pct > 95:
            log.warning(f"  >> margin usage is high; consider lowering "
                        f"MAX_CAPITAL_DEPLOYED_PCT or LEVERAGE")
    log.info("=" * 100)
    return out


# =============================================================================
# Trading loop
# =============================================================================
def now_ist() -> datetime:
    return datetime.now()


def in_session() -> bool:
    t = now_ist().time()
    return dtime(9, 15) <= t <= HARD_STOP_TIME


def can_enter() -> bool:
    return now_ist().time() < ENTRY_WINDOW_END


def square_off_time_reached() -> bool:
    return now_ist().time() >= SQUARE_OFF_TIME


def open_count(plans: list[TradePlan]) -> int:
    return sum(1 for p in plans if p.state == "FILLED")


def maybe_enter(client: DhanClient, plan: TradePlan, ltp: float):
    """Place limit entry if LTP has crossed the entry trigger."""
    triggered = (
        (plan.side == "LONG"  and ltp <= plan.entry_price) or
        (plan.side == "SHORT" and ltp >= plan.entry_price)
    )
    if not triggered: return

    log.info(f"#{plan.rank} {plan.symbol} {plan.side} TRIGGERED "
             f"@ LTP={ltp:.2f} entry={plan.entry_price:.2f}")
    txn = "BUY" if plan.side == "LONG" else "SELL"
    oid = client.place_order(
        transactionType=txn, exchangeSegment="NSE_EQ",
        productType=PRODUCT_TYPE, orderType="LIMIT",
        securityId=plan.security_id, quantity=plan.qty,
        price=plan.entry_price, triggerPrice=0.0)
    if oid:
        plan.entry_order_id = oid
        plan.state = "ENTRY_PLACED"
        log.info(f"   entry order placed: {oid}")


def maybe_arm_exits(client: DhanClient, plan: TradePlan):
    """If entry order filled, place target (limit) and stop (SL-M) bracket."""
    if plan.state != "ENTRY_PLACED": return
    st = client.order_status(plan.entry_order_id)
    status = (st.get("orderStatus") or "").upper()
    if status in ("DRY", "TRADED", "EXECUTED", "FILLED", "COMPLETE"):
        plan.state = "FILLED"
        plan.filled_at = time.time()
        log.info(f"#{plan.rank} {plan.symbol} ENTRY FILLED — arming bracket "
                 f"target={plan.target_price} stop={plan.stop_price}")
        # opposite side for exits
        opp = "SELL" if plan.side == "LONG" else "BUY"
        plan.target_order_id = client.place_order(
            transactionType=opp, exchangeSegment="NSE_EQ",
            productType=PRODUCT_TYPE, orderType="LIMIT",
            securityId=plan.security_id, quantity=plan.qty,
            price=plan.target_price, triggerPrice=0.0)
        plan.stop_order_id = client.place_order(
            transactionType=opp, exchangeSegment="NSE_EQ",
            productType=PRODUCT_TYPE, orderType="STOP_LOSS_MARKET",
            securityId=plan.security_id, quantity=plan.qty,
            price=0.0, triggerPrice=plan.stop_price)
    elif status in ("CANCELLED", "REJECTED"):
        log.warning(f"#{plan.rank} {plan.symbol} entry "
                    f"{status} — abandoning")
        plan.state = "CANCELLED"


def maybe_finalize(client: DhanClient, plan: TradePlan):
    """If either target or stop filled, cancel the other."""
    if plan.state != "FILLED": return
    t_st = client.order_status(plan.target_order_id).get("orderStatus","").upper()
    s_st = client.order_status(plan.stop_order_id).get("orderStatus","").upper()
    if t_st in ("TRADED","EXECUTED","FILLED","COMPLETE","DRY"):
        client.cancel_order(plan.stop_order_id)
        plan.state = "EXITED"; plan.exit_reason = "target"
        plan.exit_price = plan.target_price
        log.info(f"#{plan.rank} {plan.symbol} EXIT @ TARGET {plan.target_price}")
    elif s_st in ("TRADED","EXECUTED","FILLED","COMPLETE"):
        client.cancel_order(plan.target_order_id)
        plan.state = "EXITED"; plan.exit_reason = "stop"
        plan.exit_price = plan.stop_price
        log.info(f"#{plan.rank} {plan.symbol} EXIT @ STOP {plan.stop_price}")


def force_squareoff(client: DhanClient, plans: list[TradePlan]):
    log.warning("=" * 78)
    log.warning("SQUARE-OFF TIME REACHED — flattening any open positions")
    log.warning("=" * 78)
    for plan in plans:
        if plan.state == "ENTRY_PLACED" and plan.entry_order_id:
            client.cancel_order(plan.entry_order_id)
            plan.state = "CANCELLED"
            log.info(f"#{plan.rank} {plan.symbol} unfilled entry cancelled")
        elif plan.state == "FILLED":
            if plan.target_order_id: client.cancel_order(plan.target_order_id)
            if plan.stop_order_id:   client.cancel_order(plan.stop_order_id)
            opp = "SELL" if plan.side == "LONG" else "BUY"
            client.place_order(
                transactionType=opp, exchangeSegment="NSE_EQ",
                productType=PRODUCT_TYPE, orderType="MARKET",
                securityId=plan.security_id, quantity=plan.qty,
                price=0.0, triggerPrice=0.0)
            plan.state = "EXITED"; plan.exit_reason = "eod"
            log.info(f"#{plan.rank} {plan.symbol} squared-off MARKET")


def write_trade_log(plans: list[TradePlan]):
    rows = []
    for p in plans:
        rows.append({
            "rank": p.rank, "stock": p.symbol, "strategy": p.strategy,
            "side": p.side, "qty": p.qty,
            "entry": p.entry_price, "target": p.target_price, "stop": p.stop_price,
            "state": p.state, "exit_price": p.exit_price,
            "exit_reason": p.exit_reason,
            "entry_order_id": p.entry_order_id,
            "target_order_id": p.target_order_id,
            "stop_order_id": p.stop_order_id,
        })
    df = pd.DataFrame(rows)
    fname = f"trades_{date.today().isoformat()}.csv"
    df.to_csv(fname, index=False)
    log.info(f"trade log written -> {fname}")


# =============================================================================
# Main
# =============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true",
                        help="Run pre-market plan + one tick of the loop, then exit. "
                             "Useful for pre-market verification.")
    args = parser.parse_args()

    log.info("=" * 78)
    log.info(f"LIVE INTRADAY PIVOT TRADER  —  DRY_RUN={DRY_RUN}  "
             f"I_UNDERSTAND_THE_RISKS={I_UNDERSTAND_THE_RISKS}")
    log.info(f"  per-trade notional = Rs {PER_TRADE_NOTIONAL_INR:,}")
    log.info(f"  max concurrent     = {MAX_CONCURRENT_TRADES}")
    log.info(f"  product            = {PRODUCT_TYPE}")
    log.info(f"  dynamic sizing     = {USE_DYNAMIC_SIZING}  "
             f"(leverage {LEVERAGE:.1f}x, deploy {MAX_CAPITAL_DEPLOYED_PCT*100:.0f}%)")
    log.info(f"  margin precheck    = {MARGIN_PRECHECK}")
    if (not DRY_RUN) and (not I_UNDERSTAND_THE_RISKS):
        log.error("DRY_RUN=False but I_UNDERSTAND_THE_RISKS=False -> aborting")
        sys.exit(2)
    log.info("=" * 78)

    client = DhanClient()

    # sanity: token + funds
    available_balance = 0.0
    try:
        f = client.fundlimit()
        available_balance = float(f.get("availabelBalance", 0) or 0)
        log.info(f"Available balance: Rs {available_balance:,.2f}")
    except Exception as e:
        log.error(f"Dhan fundlimit failed — token expired? {e}")
        sys.exit(2)

    sec_map = load_security_map()

    # Auto-load latest top-10 from the backtest results CSV.
    global TOP10
    TOP10 = load_top10()
    plans   = build_todays_plans(client, sec_map, available_balance)
    if not plans:
        log.error("No valid trade plans for today; exiting"); return

    sid_to_sym = {p.security_id: p.symbol for p in plans}
    # graceful shutdown
    stop = {"flag": False}
    def _sigint(_s, _f):
        log.warning("CTRL+C received — flagging stop"); stop["flag"] = True
    signal.signal(signal.SIGINT, _sigint)

    log.info("Entering polling loop...")
    while in_session() and not stop["flag"]:
        try:
            ltps = client.ltp(sid_to_sym)
        except Exception as e:
            log.warning(f"   ltp call error: {e}; sleeping"); ltps = {}

        for plan in plans:
            try:
                if plan.state == "PENDING" and can_enter() \
                        and open_count(plans) < MAX_CONCURRENT_TRADES:
                    ltp = ltps.get(plan.symbol)
                    if ltp is not None:
                        maybe_enter(client, plan, ltp)
                if plan.state == "ENTRY_PLACED":
                    maybe_arm_exits(client, plan)
                if plan.state == "FILLED":
                    maybe_finalize(client, plan)
            except Exception as e:
                log.exception(f"#{plan.rank} {plan.symbol} loop error: {e}")

        if square_off_time_reached():
            force_squareoff(client, plans)
            break

        if args.once:
            log.info("--once flag set; exiting after first tick"); break
        time.sleep(POLL_INTERVAL_SEC)

    write_trade_log(plans)
    # summary
    log.info("=" * 78)
    log.info("END-OF-DAY SUMMARY")
    for p in plans:
        log.info(f"  #{p.rank:>2} {p.symbol:<11} {p.strategy:<22} "
                 f"{p.side:<5} state={p.state:<13} "
                 f"exit={p.exit_price} reason={p.exit_reason}")
    log.info("=" * 78)


if __name__ == "__main__":
    main()
