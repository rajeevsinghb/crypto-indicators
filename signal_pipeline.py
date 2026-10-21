"""
signal_pipeline.py  -  Minimal levels + signals system
=======================================================
Input  : Sheet2 (OHLCV.py ka output: coin, exchange, date, open, high, low, close, volume)
Output : Sheet "DB"          -> har coin ka context + levels (coin name ke saath)
         Sheet "Signals"     -> aaj ke saare signals + verification result
         Sheet "Tracker"     -> har signal ka outcome (OPEN/TP/SL/EXPIRED, R result)
         Sheet "Performance" -> signal type ke hisaab se win-rate / avg R
         Sheet "Sent_Log"    -> jo alerts bheje ja chuke (duplicate rokne ke liye)
         Telegram / Email -> sirf VERIFIED opportunities

Features (minimum set):
  Context : Trend (EMA20/50), Regime (RANGING/TRENDING/NEUTRAL), ADX, RSI, ATR%, Volume ratio
  Levels  : PDH/PDL, PWH/PWL, PMH/PML, PYH/PYL (prev year), Range High/Low + position%,
            S/R zones, Equal High / Equal Low
  Signals : BREAKOUT, RETEST (breakout ke baad), SWEEP_REVERSAL (fake breakout),
            RANGE_TRADE   -> LONG aur SHORT dono
  Filters : BTC regime (trend signals), Relative-Strength rank 7/21/30d (trend signals)
  Tracker : har signal ka TP/SL result -> "Tracker" aur "Performance" sheet

Env variables:
  GOOGLE_CREDENTIALS                (service account JSON)  [required]
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID                      [optional]
  EMAIL_USER, EMAIL_PASS, EMAIL_TO  (+ SMTP_HOST/SMTP_PORT) [optional]
  DRY_RUN=1  -> sheet/notification ke bina sirf print
"""

import os
import json
import time
import random
import smtplib
from email.message import EmailMessage

import numpy as np
import pandas as pd
import requests

# =========================
# CONFIG
# =========================
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "13DYY42q_PDLxinGkqT1rigsG2t8zzOioFBcL-suOQFU")
RAW_SHEET, DB_SHEET, SIGNAL_SHEET, LOG_SHEET = "Sheet2", "DB", "Signals", "Sent_Log"
TRACKER_SHEET, PERF_SHEET = "Tracker", "Performance"
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
LOCAL_CSV = os.environ.get("LOCAL_CSV")          # sirf testing ke liye

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]

# Universe filter
EXCLUDE = {"USDT", "USDC", "USDS", "USD1", "USDE", "USDG", "PYUSD", "USDY", "RLUSD",
           "DAI", "FDUSD", "TUSD", "USDD", "BUSD", "XAUT", "PAXG"}
PEG_RANGE_PCT = 0.03          # 90 din ki range 3% se kam => stable/pegged, skip
MIN_BARS = 60

# Indicators
ATR_N, RSI_N, ADX_N = 14, 14, 14
VOL_AVG_N = 20

# Levels
PIVOT_W = 3                   # swing pivot window (bars each side)
ZONE_LOOKBACK = 180
ZONE_TOL_ATR = 0.6            # itne ATR ke andar ke pivots ek zone
ZONE_MIN_TOUCH = 2
EQ_LOOKBACK = 60
EQ_TOL_ATR = 0.3              # equal high/low tolerance
RANGE_WINDOW = 30
RANGE_MAX_ATR = 8.0           # range width <= 8 ATR
ADX_RANGE_MAX = 25

# Signal rules
BO_MIN_VOL = 1.3              # breakout me volume >= 1.3x avg
WICK_MIN = 0.40               # sweep me wick >= 40% of candle

# Verification
MIN_SCORE = 60
MIN_RR = 1.5
MAX_RISK_PCT = 12.0
MIN_QUOTE_VOL = 2_000_000     # close*volume >= 2M USD (approx)
MAX_ALERTS = 15
SEND_EMPTY_SUMMARY = True     # "aaj koi opportunity nahi" bhi bheje

# Filters (trend-following signals par lagte hain: BREAKOUT, RETEST)
# NOTE: ye rules logic par based hain, research-proven nahi. Tracker se test karo.
TREND_SIGNALS = {"BREAKOUT", "RETEST"}
USE_BTC_FILTER = True         # LONG tab nahi jab BTC BEARISH, SHORT tab nahi jab BTC BULLISH
BTC_SYMBOL = "BTC"
USE_RS_FILTER = True          # LONG sirf top-RS, SHORT sirf bottom-RS coins
RS_WINDOWS = (7, 21, 30)      # days (crypto momentum research ~3 hafte ke aas-paas)
RS_LONG_MIN = 60              # RS percentile >= 60 for LONG
RS_SHORT_MAX = 40             # RS percentile <= 40 for SHORT
RS_MIN_UNIVERSE = 10          # isse kam coins ho to RS filter off

# Tracker
TRACK_MAX_BARS = 15           # itne din me TP/SL na mile to EXPIRED (close par exit)


# =========================
# SMALL HELPERS
# =========================
def sig(x):
    """6 significant digits (SHIB jaise tiny prices safe)."""
    if x is None:
        return ""
    try:
        if isinstance(x, (float, np.floating)):
            if np.isnan(x) or np.isinf(x):
                return ""
            return float(f"{x:.6g}")
        return x
    except Exception:
        return x


def _retry(fn, *a, tries=6, **k):
    for i in range(tries):
        try:
            return fn(*a, **k)
        except Exception as e:
            s = str(e)
            if any(c in s for c in ("429", "500", "503")) and i < tries - 1:
                time.sleep(min(5 * 2 ** i, 90) + random.random() * 3)
            else:
                raise


# =========================
# GOOGLE SHEETS
# =========================
def get_spreadsheet():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=SCOPES)
    gc = gspread.authorize(creds)
    return _retry(gc.open_by_key, SPREADSHEET_ID)


def get_or_create_ws(ss, title, rows=1000, cols=40):
    import gspread
    try:
        return _retry(ss.worksheet, title)
    except gspread.exceptions.WorksheetNotFound:
        return _retry(ss.add_worksheet, title=title, rows=rows, cols=cols)


def write_df(ws, df):
    from gspread_dataframe import set_with_dataframe
    _retry(ws.clear)
    time.sleep(1)
    out = df.astype(object).where(pd.notna(df), "")
    _retry(set_with_dataframe, ws, out, include_index=False, include_column_header=True)


def load_raw(ss):
    if LOCAL_CSV:
        return pd.read_csv(LOCAL_CSV)
    ws = _retry(ss.worksheet, RAW_SHEET)
    vals = _retry(ws.get_all_values)
    return pd.DataFrame(vals[1:], columns=vals[0])


def prepare_raw(raw):
    df = raw.copy()
    df["coin"] = df["coin"].astype(str).str.strip().str.upper()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close"])
    # Aaj ki (UTC) adhuri candle hatao -> sirf CLOSED candles
    today = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()
    df = df[df["date"] < today]
    df = df.drop_duplicates(["coin", "date"], keep="last").sort_values(["coin", "date"])
    return df, today


# =========================
# INDICATORS
# =========================
def _tr(df):
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"],
                      (df["high"] - pc).abs(),
                      (df["low"] - pc).abs()], axis=1).max(axis=1)


def calc_atr(df, n=ATR_N):
    return _tr(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def calc_rsi(close, n=RSI_N):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    r = 100 - 100 / (1 + rs)
    return r.fillna(100).where(up.notna(), np.nan)


def calc_adx(df, n=ADX_N):
    up, dn = df["high"].diff(), -df["low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    atr = _tr(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    pdi = 100 * plus.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr
    mdi = 100 * minus.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


# =========================
# LEVELS
# =========================
def period_levels(df, ref):
    """PDH/PDL, PWH/PWL, PMH/PML jo 'ref' date se pehle complete ho chuke."""
    out = {k: np.nan for k in ["PDH", "PDL", "PWH", "PWL", "PMH", "PML", "PYH", "PYL"]}
    sub = df[df["date"] < ref]
    if sub.empty:
        return out
    last = sub.iloc[-1]
    if last["date"] == ref - pd.Timedelta(days=1):
        out["PDH"], out["PDL"] = last["high"], last["low"]
    for code, freq in (("W", "W"), ("M", "M"), ("Y", "Y")):
        per = sub["date"].dt.to_period(freq)
        prev = per[per < pd.Period(ref, freq)]
        if prev.empty:
            continue
        if code == "Y":   # pichla saal tabhi valid jab data us saal ke shuru se ho
            if sub["date"].min() > pd.Timestamp(year=prev.max().year, month=1, day=5):
                continue
        sel = sub[per == prev.max()]
        out[f"P{code}H"], out[f"P{code}L"] = sel["high"].max(), sel["low"].min()
    return out


def pivot_positions(series, w, kind):
    roll = series.rolling(2 * w + 1, center=True)
    ext = roll.max() if kind == "H" else roll.min()
    return np.where(series.values == ext.values)[0]


def cluster_zones(prices, tol, min_touch):
    prices = sorted(prices)
    groups, cur = [], []
    for p in prices:
        if cur and p - np.mean(cur) > tol:
            groups.append(cur)
            cur = []
        cur.append(p)
    if cur:
        groups.append(cur)
    return [(float(np.mean(g)), len(g)) for g in groups if len(g) >= min_touch]


def find_equal(hist, kind, tol, ref_close):
    col = "high" if kind == "H" else "low"
    vals = hist[col].values
    pos = [p for p in pivot_positions(hist[col], PIVOT_W, kind) if p >= len(hist) - EQ_LOOKBACK]
    found = []
    for a in range(len(pos)):
        for b in range(a + 1, len(pos)):
            i, j = pos[a], pos[b]
            if j - i < PIVOT_W or abs(vals[i] - vals[j]) > tol:
                continue
            lvl = (vals[i] + vals[j]) / 2
            after = vals[j + 1:]
            if len(after):
                if kind == "H" and after.max() > lvl + tol:
                    continue
                if kind == "L" and after.min() < lvl - tol:
                    continue
            found.append(lvl)
    if not found:
        return np.nan
    return min(found, key=lambda x: abs(x - ref_close))


def _runs(mask):
    m = np.asarray(mask, dtype=bool)
    return int(((m[1:] & ~m[:-1]).sum()) + (1 if len(m) and m[0] else 0))


def detect_range(hist, atr_h, adx_h):
    w = hist.tail(RANGE_WINDOW)
    hi, lo = float(w["high"].max()), float(w["low"].min())
    tol = 0.5 * atr_h
    t_hi = _runs(w["high"].values >= hi - tol)
    t_lo = _runs(w["low"].values <= lo + tol)
    width = hi - lo
    is_range = bool(adx_h < ADX_RANGE_MAX and width <= RANGE_MAX_ATR * atr_h
                    and t_hi >= 2 and t_lo >= 2)
    return {"high": hi, "low": lo, "touch_hi": t_hi, "touch_lo": t_lo, "is_range": is_range}


# =========================
# SIGNAL ENGINE
# =========================
def evaluate(side, X):
    """
    side = +1 (LONG) / -1 (SHORT).
    SHORT ke liye prices ko mirror (x -1) karke wahi LONG logic chalate hain,
    isliye long aur short ke rules hamesha symmetric rehte hain.
    """
    s = side
    atr = X["atr"]
    P = lambda x: s * x
    Pc, Po = P(X["c"]), P(X["o"])
    Ph, Pl = (X["h"], X["l"]) if s == 1 else (-X["l"], -X["h"])
    Pprev = P(X["prev_close"])
    rsi = X["rsi"] if s == 1 else 100 - X["rsi"]
    rsi_prev = X["rsi_prev"] if s == 1 else 100 - X["rsi_prev"]
    rng_c = max(X["h"] - X["l"], 1e-12)
    body_pos = (Pc - Pl) / rng_c                       # close candle ke upar (favourable side) kitna
    low_wick = (min(Po, Pc) - Pl) / rng_c              # favourable side ka rejection wick
    aligned = X["trend_sign"] == s
    brk = X["high_levels"] if s == 1 else X["low_levels"]
    hold = X["low_levels"] if s == 1 else X["high_levels"]
    all_p = [P(p) for _, p, st in X["high_levels"] + X["low_levels"] if st >= 3]   # TP ke liye sirf strong levels
    cands = []

    def finish(kind, name, strength, sl_p, tp_p, score, why, level_price):
        risk = max(Pc - sl_p, 0.5 * atr)
        sl_p = Pc - risk
        if tp_p is None:
            obst = [x for x in all_p if x > Pc + 0.3 * atr]
            tp_p = min(obst) if obst else Pc + 2.5 * risk
        rr = (tp_p - Pc) / risk
        cands.append({
            "signal": kind, "side": "LONG" if s == 1 else "SHORT",
            "level_name": name, "level": level_price, "entry": X["c"],
            "sl": s * sl_p, "tp": s * tp_p, "rr": rr,
            "risk_pct": risk / X["c"] * 100, "score": int(min(score, 100)),
            "reasons": "; ".join(why)})

    # ---- 1) BREAKOUT (close level ke paar + volume + strong candle)
    if X["vol_ratio"] >= BO_MIN_VOL and Pc > Po and body_pos >= 0.6:
        hit = [(P(p), n, st, p) for n, p, st in brk if Pprev <= P(p) and Pc > P(p) + 0.1 * atr]
        if hit:
            L, n, st, raw_p = max(hit, key=lambda t: t[0])
            sc, why = 40, [f"Close broke {n}", f"Volume {X['vol_ratio']:.1f}x avg"]
            sc += 15 if X["vol_ratio"] >= 2 else 8
            if aligned: sc += 15; why.append("Trend aligned")
            if X["is_range"] and n.startswith("Range"): sc += 10; why.append("Range breakout")
            if 55 <= rsi <= 78: sc += 10; why.append(f"Momentum RSI {X['rsi']:.0f}")
            if st >= 3: sc += 10; why.append("Strong level")
            if body_pos >= 0.8: sc += 5
            finish("BREAKOUT", n, st, L - 0.5 * atr, None, sc, why, raw_p)

    # ---- 2) RETEST (pichle 5 din me break hua level wapas test hua aur hold hua)
    cl = [P(x) for x in X["hist_close"][-5:]]
    pre = P(X["hist_close"][-6]) if len(X["hist_close"]) >= 6 else None
    if pre is not None and Pc > Po:
        for n, p, st in brk:
            L = P(p)
            if pre > L:
                continue
            k = next((i for i, v in enumerate(cl) if v > L + 0.1 * atr), None)
            if k is None or not all(v > L for v in cl[k:]):
                continue
            if Pl <= L + 0.3 * atr and Pl >= L - 0.5 * atr and Pc > L:
                sc, why = 45, [f"Retest of broken {n}"]
                if low_wick >= 0.3: sc += 15; why.append("Rejection wick")
                if aligned: sc += 15; why.append("Trend aligned")
                if X["vol_ratio"] >= 1: sc += 10
                if 45 <= rsi <= 70: sc += 10
                if st >= 3: sc += 5
                finish("RETEST", n, st, min(Pl, L) - 0.4 * atr, None, sc, why, p)
                break

    # ---- 3) SWEEP REVERSAL (level ke paar wick, wapas andar close)
    if low_wick >= WICK_MIN:
        hits = [(P(p), n, st, p) for n, p, st in hold
                if Pl < P(p) - 0.05 * atr and Pc > P(p)]
        if hits:
            L, n, st, raw_p = min(hits, key=lambda t: abs(t[0] - Pc))
            sc, why = 45, [f"Liquidity sweep of {n}", f"Wick {low_wick*100:.0f}% of candle"]
            if X["vol_ratio"] >= 1.2: sc += 10; why.append(f"Volume {X['vol_ratio']:.1f}x")
            if rsi <= 40: sc += 15; why.append(f"RSI stretched {X['rsi']:.0f}")
            if rsi > rsi_prev: sc += 5; why.append("RSI turning")
            if st >= 3: sc += 10; why.append("Strong level")
            if X["is_range"]: sc += 10; why.append("Range regime")
            if Pc > Po: sc += 5
            finish("SWEEP_REVERSAL", n, st, min(Pl, L) - 0.3 * atr, None, sc, why, raw_p)

    # ---- 4) RANGE TRADE (range ke edge se mean-reversion)
    if X["is_range"]:
        r = X["range"]
        edge = P(r["low"]) if s == 1 else P(r["high"])
        opp = P(r["high"]) if s == 1 else P(r["low"])
        width = max(abs(opp - edge), 1e-12)
        pos = (Pc - edge) / width * 100
        touches = r["touch_lo"] if s == 1 else r["touch_hi"]
        if pos <= 20 and Pc > Po and Pl <= edge + 0.5 * atr and rsi <= 45:
            sc, why = 40, [f"At range {'low' if s == 1 else 'high'} ({pos:.0f}% of range)"]
            if touches >= 3: sc += 15; why.append(f"{touches} touches")
            if rsi <= 38: sc += 10; why.append(f"RSI {X['rsi']:.0f}")
            if low_wick >= 0.3: sc += 10; why.append("Rejection wick")
            if X["vol_ratio"] >= 1: sc += 5
            if X["adx_prev"] < 20: sc += 10; why.append("Low ADX")
            finish("RANGE_TRADE", "Range Low" if s == 1 else "Range High", touches,
                   min(Pl, edge) - 0.5 * atr, opp - 0.25 * atr, sc, why,
                   r["low"] if s == 1 else r["high"])
    return cands


def verify(sg, X, fresh, sent_keys):
    fails = []
    if sg["signal"] in TREND_SIGNALS:
        b = X.get("btc_sign")
        if USE_BTC_FILTER and b is not None and X["coin"] != BTC_SYMBOL:
            if sg["side"] == "LONG" and b == -1: fails.append("BTC bearish")
            if sg["side"] == "SHORT" and b == 1: fails.append("BTC bullish")
        r = X.get("rs_pct")
        if USE_RS_FILTER and r is not None:
            if sg["side"] == "LONG" and r < RS_LONG_MIN: fails.append(f"RS {r:.0f}<{RS_LONG_MIN}")
            if sg["side"] == "SHORT" and r > RS_SHORT_MAX: fails.append(f"RS {r:.0f}>{RS_SHORT_MAX}")
    if not fresh: fails.append("stale data")
    if X["n_bars"] < MIN_BARS: fails.append("short history")
    if X["c"] * X["v"] < MIN_QUOTE_VOL: fails.append("low liquidity")
    if sg["score"] < MIN_SCORE: fails.append(f"score<{MIN_SCORE}")
    if sg["rr"] < MIN_RR: fails.append(f"RR<{MIN_RR}")
    if sg["risk_pct"] > MAX_RISK_PCT: fails.append(f"risk>{MAX_RISK_PCT}%")
    if sg["key"] in sent_keys: fails.append("already sent")
    return fails


# =========================
# UNIVERSE HELPERS (pegged / RS rank / BTC regime)
# =========================
def is_pegged(df):
    last90 = df["close"].tail(90)
    return len(last90) > 0 and (last90.max() - last90.min()) / last90.mean() < PEG_RANGE_PCT


def ret_pct(df, n):
    c = df["close"].values
    return float((c[-1] / c[-1 - n] - 1) * 100) if len(c) > n else np.nan


def trend_sign_of(df):
    e20 = df["close"].ewm(span=20, adjust=False).mean().iloc[-1]
    e50 = df["close"].ewm(span=50, adjust=False).mean().iloc[-1]
    c = df["close"].iloc[-1]
    return 1 if (e20 > e50 and c > e50) else -1 if (e20 < e50 and c < e50) else 0


def compute_rs(coin_dfs):
    """Har window ka percentile rank, phir average (0-100). Returns {coin: pct}."""
    rets = {c: [ret_pct(g, n) for n in RS_WINDOWS] for c, g in coin_dfs.items()}
    tab = pd.DataFrame(rets, index=RS_WINDOWS).T.dropna()
    if len(tab) < RS_MIN_UNIVERSE:
        return {}
    return (tab.rank(pct=True).mean(axis=1) * 100).to_dict()


# =========================
# PER-COIN ANALYSIS
# =========================
def analyze_coin(coin, df, today, sent_keys, rs_pct=None, btc_sign=None):
    """df: sirf us coin ki CLOSED daily candles (sorted)."""
    df = df.reset_index(drop=True)
    if len(df) < MIN_BARS:
        return None, []
    if is_pegged(df):
        return None, []

    atr_s, rsi_s, adx_s = calc_atr(df), calc_rsi(df["close"]), calc_adx(df)
    ema20 = df["close"].ewm(span=20, adjust=False).mean()
    ema50 = df["close"].ewm(span=50, adjust=False).mean()
    t, hist = df.iloc[-1], df.iloc[:-1]
    atr_t, atr_h = atr_s.iloc[-1], atr_s.iloc[-2]
    if np.isnan(atr_h) or np.isnan(atr_t) or atr_h <= 0:
        return None, []

    c = float(t["close"])
    trend_sign = 1 if (ema20.iloc[-1] > ema50.iloc[-1] and c > ema50.iloc[-1]) else \
                -1 if (ema20.iloc[-1] < ema50.iloc[-1] and c < ema50.iloc[-1]) else 0
    trend = {1: "BULLISH", -1: "BEARISH", 0: "NEUTRAL"}[trend_sign]
    adx_h = float(adx_s.iloc[-2]) if not np.isnan(adx_s.iloc[-2]) else 0.0

    rng = detect_range(hist, atr_h, adx_h)
    regime = "RANGING" if rng["is_range"] else ("TRENDING" if adx_h >= ADX_RANGE_MAX else "NEUTRAL")

    plv = period_levels(hist, t["date"])                       # signal ke liye (T se pehle ke levels)
    plv_today = period_levels(df, t["date"] + pd.Timedelta(days=1))   # aaj ke liye display

    # swing pivots -> S/R zones
    z = hist.tail(ZONE_LOOKBACK).reset_index(drop=True)
    piv = [z["high"].values[i] for i in pivot_positions(z["high"], PIVOT_W, "H")] + \
          [z["low"].values[i] for i in pivot_positions(z["low"], PIVOT_W, "L")]
    zones = cluster_zones(piv, ZONE_TOL_ATR * atr_h, ZONE_MIN_TOUCH)
    prev_close = float(hist["close"].iloc[-1])
    sup = max([p for p, _ in zones if p < c], default=np.nan)
    res = min([p for p, _ in zones if p > c], default=np.nan)

    eqh = find_equal(hist, "H", EQ_TOL_ATR * atr_h, prev_close)
    eql = find_equal(hist, "L", EQ_TOL_ATR * atr_h, prev_close)

    rn = "Range" if rng["is_range"] else "20D"
    high_levels = [(f"{rn} High", rng["high"], rng["touch_hi"])]
    low_levels = [(f"{rn} Low", rng["low"], rng["touch_lo"])]
    for nm, key, bucket in (("Prev Week High", "PWH", high_levels), ("Prev Month High", "PMH", high_levels),
                            ("Prev Year High", "PYH", high_levels),
                            ("Prev Week Low", "PWL", low_levels), ("Prev Month Low", "PML", low_levels),
                            ("Prev Year Low", "PYL", low_levels)):
        if not np.isnan(plv[key]):
            bucket.append((nm, float(plv[key]), 3))
    if not np.isnan(eqh): high_levels.append(("Equal High", float(eqh), 3))
    if not np.isnan(eql): low_levels.append(("Equal Low", float(eql), 3))
    for p, n in zones:
        high_levels.append(("S/R Zone", p, n))
        low_levels.append(("S/R Zone", p, n))

    vol_avg = hist["volume"].tail(VOL_AVG_N).mean()
    X = {
        "o": float(t["open"]), "h": float(t["high"]), "l": float(t["low"]), "c": c,
        "v": float(t["volume"]), "prev_close": prev_close, "atr": float(atr_h),
        "rsi": float(rsi_s.iloc[-1]), "rsi_prev": float(rsi_s.iloc[-2]),
        "adx_prev": adx_h, "vol_ratio": float(t["volume"] / vol_avg) if vol_avg > 0 else 0.0,
        "trend_sign": trend_sign, "is_range": rng["is_range"], "range": rng,
        "high_levels": high_levels, "low_levels": low_levels,
        "hist_close": hist["close"].values.tolist(), "n_bars": len(df),
        "coin": coin, "rs_pct": rs_pct, "btc_sign": btc_sign,
    }

    fresh = t["date"] == today - pd.Timedelta(days=1)
    cands = evaluate(1, X) + evaluate(-1, X)
    date_s = t["date"].strftime("%Y-%m-%d")
    exch = t.get("exchange", "")
    signals = []
    for sg in cands:
        sg.update({"coin": coin, "date": date_s, "exchange": exch,
                   "key": f"{date_s}|{coin}|{sg['signal']}|{sg['side']}"})
        fails = verify(sg, X, fresh, sent_keys)
        sg["verified"] = "YES" if not fails else "NO"
        sg["fail_reasons"] = ", ".join(fails)
        signals.append(sg)
    best = max(signals, key=lambda g: (g["verified"] == "YES", g["score"]), default=None)

    prev_c = float(df["close"].iloc[-2])
    width = rng["high"] - rng["low"]
    row = {
        "coin": coin, "date": date_s, "exchange": exch, "close": c,
        "pct_change": (c / prev_c - 1) * 100, "trend": trend, "regime": regime,
        "adx": float(adx_s.iloc[-1]), "rsi": X["rsi"], "atr_pct": float(atr_t) / c * 100,
        "vol_ratio": X["vol_ratio"],
        "PDH": plv_today["PDH"], "PDL": plv_today["PDL"], "PWH": plv_today["PWH"],
        "PWL": plv_today["PWL"], "PMH": plv_today["PMH"], "PML": plv_today["PML"],
        "PYH": plv_today["PYH"], "PYL": plv_today["PYL"],
        "rs_rank": rs_pct if rs_pct is not None else np.nan, "ret_21d_pct": ret_pct(df, 21),
        "range_high": rng["high"], "range_low": rng["low"],
        "range_pos_pct": (c - rng["low"]) / width * 100 if width > 0 else np.nan,
        "nearest_support": sup, "nearest_resistance": res,
        "equal_high": eqh, "equal_low": eql,
        "signal": f"{best['side']} {best['signal']}" if best else "NONE",
        "score": best["score"] if best else "", "verified": best["verified"] if best else "",
    }
    return row, signals


# =========================
# NOTIFICATIONS
# =========================
def fmt_signal(g):
    icon = "🟢" if g["side"] == "LONG" else "🔴"
    return (f"{icon} {g['side']} | {g['coin']} | {g['signal']}\n"
            f"Level: {g['level_name']} @ {sig(g['level'])}\n"
            f"Entry {sig(g['entry'])} | SL {sig(g['sl'])} | TP {sig(g['tp'])}\n"
            f"RR {g['rr']:.1f} | Risk {g['risk_pct']:.1f}% | Score {g['score']}\n"
            f"Why: {g['reasons']}")


def build_message(sigs, date_s, extra=""):
    if not sigs:
        return [f"Crypto scan {date_s}: aaj koi verified opportunity nahi.{extra}"]
    head = f"Crypto opportunities {date_s} ({len(sigs)} verified){extra}\n"
    foot = "\nNot financial advice. Chart check karke hi trade lo."
    blocks, cur = [], head
    for g in sigs:
        b = "\n" + fmt_signal(g) + "\n"
        if len(cur) + len(b) > 3800:
            blocks.append(cur)
            cur = ""
        cur += b
    blocks.append(cur + foot)
    return blocks


def send_telegram(parts):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    try:
        for p in parts:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data={"chat_id": chat, "text": p}, timeout=30)
            r.raise_for_status()
        print("Telegram sent")
        return True
    except Exception as e:
        print("Telegram failed:", e)
        return False


def send_email(parts, subject):
    user, pwd, to = os.environ.get("EMAIL_USER"), os.environ.get("EMAIL_PASS"), os.environ.get("EMAIL_TO")
    if not (user and pwd and to):
        return False
    try:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, user, to
        msg.set_content("\n".join(parts))
        with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"),
                              int(os.environ.get("SMTP_PORT", "465"))) as s:
            s.login(user, pwd)
            s.send_message(msg)
        print("Email sent")
        return True
    except Exception as e:
        print("Email failed:", e)
        return False


# =========================
# MAIN
# =========================
DB_COLS = ["coin", "date", "exchange", "close", "pct_change", "trend", "regime", "adx", "rsi",
           "atr_pct", "vol_ratio", "rs_rank", "ret_21d_pct", "PDH", "PDL", "PWH", "PWL", "PMH", "PML",
           "PYH", "PYL", "range_high", "range_low", "range_pos_pct", "nearest_support",
           "nearest_resistance", "equal_high", "equal_low", "signal", "score", "verified"]
SIG_COLS = ["coin", "date", "side", "signal", "level_name", "level", "entry", "sl", "tp", "rr",
            "risk_pct", "score", "verified", "fail_reasons", "reasons", "exchange"]
TRK_COLS = ["key", "date", "coin", "side", "signal", "verified", "fail_reasons", "entry", "sl", "tp",
            "rr", "score", "status", "exit_date", "exit_price", "r_result", "bars"]
TRK_NUM = ["entry", "sl", "tp", "rr", "score", "exit_price", "r_result", "bars"]
PERF_COLS = ["signal", "verified", "closed", "wins", "win_rate_pct", "avg_R", "open"]


def run(raw, sent_keys):
    df, today = prepare_raw(raw)
    coin_dfs = {c: g.reset_index(drop=True) for c, g in df.groupby("coin")
                if c not in EXCLUDE and len(g) >= MIN_BARS and not is_pegged(g)}

    rs = compute_rs(coin_dfs) if USE_RS_FILTER else {}
    btc_sign = trend_sign_of(coin_dfs[BTC_SYMBOL]) if (USE_BTC_FILTER and BTC_SYMBOL in coin_dfs) else None

    rows, all_sigs = [], []
    for coin, g in coin_dfs.items():
        row, sigs = analyze_coin(coin, g, today, sent_keys, rs.get(coin), btc_sign)
        if row is None:
            continue
        rows.append(row)
        all_sigs += sigs
    db = pd.DataFrame(rows, columns=DB_COLS)
    sg = pd.DataFrame(all_sigs)
    if not sg.empty:
        sg = sg.sort_values(["verified", "score"], ascending=[False, False])
    btc_txt = {1: "BULLISH", -1: "BEARISH", 0: "NEUTRAL", None: "n/a"}[btc_sign]
    print(f"Coins analysed: {len(db)} | signals: {len(sg)} | BTC regime: {btc_txt} | RS filter: {'on' if rs else 'off'}")
    return db, sg, all_sigs, today, df, btc_txt


# =========================
# OUTCOME TRACKER
# =========================
def empty_tracker():
    return pd.DataFrame(columns=TRK_COLS)


def load_tracker(ss):
    ws = get_or_create_ws(ss, TRACKER_SHEET, cols=len(TRK_COLS) + 2)
    vals = _retry(ws.get_all_values)
    if len(vals) < 2:
        return empty_tracker()
    tr = pd.DataFrame(vals[1:], columns=vals[0]).reindex(columns=TRK_COLS)
    for c in TRK_NUM:
        tr[c] = pd.to_numeric(tr[c], errors="coerce")
    return tr


def update_tracker(tr, new_sigs, df_all):
    """Naye signals add karo, OPEN signals ko aage ki candles se resolve karo.
    Entry = signal candle ka close. Same candle me SL aur TP dono hit => SL maana jata hai (conservative)."""
    tr = tr.copy().astype({c: object for c in TRK_COLS if c not in TRK_NUM})
    have = set(tr["key"].astype(str)) if len(tr) else set()
    add = []
    for g in new_sigs:
        if g["key"] in have:
            continue
        add.append({"key": g["key"], "date": g["date"], "coin": g["coin"], "side": g["side"],
                    "signal": g["signal"], "verified": g["verified"], "fail_reasons": g["fail_reasons"],
                    "entry": g["entry"], "sl": g["sl"], "tp": g["tp"], "rr": g["rr"],
                    "score": g["score"], "status": "OPEN", "exit_date": "", "exit_price": np.nan,
                    "r_result": np.nan, "bars": np.nan})
    if add:
        tr = pd.concat([tr, pd.DataFrame(add, columns=TRK_COLS)], ignore_index=True)

    by_coin = {c: g for c, g in df_all.groupby("coin")}
    for idx in tr.index[tr["status"] == "OPEN"]:
        r = tr.loc[idx]
        g = by_coin.get(r["coin"])
        if g is None or pd.isna(r["entry"]) or pd.isna(r["sl"]) or pd.isna(r["tp"]):
            continue
        bars = g[g["date"] > pd.Timestamp(r["date"])].head(TRACK_MAX_BARS)
        side = 1 if r["side"] == "LONG" else -1
        risk = abs(r["entry"] - r["sl"])
        if risk <= 0:
            continue
        done = None
        for n, b in enumerate(bars.itertuples(), start=1):
            hit_sl = b.low <= r["sl"] if side == 1 else b.high >= r["sl"]
            hit_tp = b.high >= r["tp"] if side == 1 else b.low <= r["tp"]
            if hit_sl:
                done = ("SL", b.date, r["sl"], -1.0, n); break
            if hit_tp:
                done = ("TP", b.date, r["tp"], abs(r["tp"] - r["entry"]) / risk, n); break
        if done is None and len(bars) >= TRACK_MAX_BARS:
            last = bars.iloc[-1]
            done = ("EXPIRED", last["date"], last["close"],
                    side * (last["close"] - r["entry"]) / risk, len(bars))
        if done:
            st, d, px, rr_, n = done
            tr.loc[idx, ["status", "exit_date", "exit_price", "r_result", "bars"]] = \
                [st, pd.Timestamp(d).strftime("%Y-%m-%d"), px, round(float(rr_), 2), n]
    return tr


def build_performance(tr):
    if tr.empty:
        return pd.DataFrame(columns=PERF_COLS)
    t = tr.copy()
    t["r_result"] = pd.to_numeric(t["r_result"], errors="coerce")
    out = []
    for (sgn, ver), g in t.groupby(["signal", "verified"]):
        closed = g[g["status"].isin(["TP", "SL", "EXPIRED"])]
        wins = int((closed["r_result"] > 0).sum())
        out.append({"signal": sgn, "verified": ver, "closed": len(closed), "wins": wins,
                    "win_rate_pct": round(wins / len(closed) * 100, 1) if len(closed) else "",
                    "avg_R": round(float(closed["r_result"].mean()), 2) if len(closed) else "",
                    "open": int((g["status"] == "OPEN").sum())})
    return pd.DataFrame(out, columns=PERF_COLS).sort_values(["verified", "signal"], ascending=[False, True])


def perf_line(tr):
    if tr.empty:
        return ""
    t = tr[(tr["verified"] == "YES") & tr["status"].isin(["TP", "SL", "EXPIRED"])]
    if len(t) < 5:
        return ""
    r = pd.to_numeric(t["r_result"], errors="coerce")
    return f"\nTracker (verified, closed={len(t)}): win-rate {(r > 0).mean() * 100:.0f}% | avg R {r.mean():.2f}"


# =========================
# MAIN
# =========================
def main():
    ss = None if (DRY_RUN and LOCAL_CSV) else get_spreadsheet()
    raw = load_raw(ss)

    sent_keys, log_ws, tr = set(), None, empty_tracker()
    if ss is not None:
        log_ws = get_or_create_ws(ss, LOG_SHEET, cols=8)
        vals = _retry(log_ws.get_all_values)
        if not vals:
            _retry(log_ws.append_row, ["key", "date", "coin", "signal", "side", "sent_at_utc"])
        sent_keys = {r[0] for r in vals[1:] if r}
        tr = load_tracker(ss)

    db, sg, all_sigs, today, df_all, btc_txt = run(raw, sent_keys)
    date_s = (today - pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    tr = update_tracker(tr, all_sigs, df_all)
    perf = build_performance(tr)

    verified = sorted([g for g in all_sigs if g["verified"] == "YES"],
                      key=lambda g: g["score"], reverse=True)[:MAX_ALERTS]

    for col in db.columns:                                    # sheet ke liye clean numbers
        if db[col].dtype.kind == "f":
            db[col] = db[col].map(sig)
    sg_out = pd.DataFrame(columns=SIG_COLS) if sg.empty else sg[SIG_COLS].copy()
    for col in sg_out.columns:
        if sg_out[col].dtype.kind == "f":
            sg_out[col] = sg_out[col].map(sig)
    tr_out = tr.copy()
    for col in TRK_NUM:
        tr_out[col] = tr_out[col].map(sig)

    if DRY_RUN:
        print(db.to_string(max_rows=60))
        print(sg_out.to_string(max_rows=60))
        print(perf.to_string())
    if ss is not None and not DRY_RUN:
        for title, frame, ncols in ((DB_SHEET, db, len(DB_COLS)), (SIGNAL_SHEET, sg_out, len(SIG_COLS)),
                                    (TRACKER_SHEET, tr_out, len(TRK_COLS)), (PERF_SHEET, perf, len(PERF_COLS))):
            write_df(get_or_create_ws(ss, title, cols=ncols + 2), frame)
            time.sleep(2)

    if verified or SEND_EMPTY_SUMMARY:
        extra = f"\nBTC regime: {btc_txt}" + perf_line(tr)
        parts = build_message(verified, date_s, extra)
        print("\n".join(parts))
        if not DRY_RUN:
            ok = send_telegram(parts)
            ok = send_email(parts, f"Crypto opportunities {date_s}: {len(verified)} verified") or ok
            if not ok:
                print("WARNING: koi notification channel configured/working nahi.")
            if ok and verified and log_ws is not None:
                now = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M")
                _retry(log_ws.append_rows,
                       [[g["key"], g["date"], g["coin"], g["signal"], g["side"], now] for g in verified],
                       value_input_option="RAW")


if __name__ == "__main__":
    main()
