# =========================
# INSTALL (run this once in terminal, not needed inside script)
# pip install pandas-ta gspread gspread-dataframe google-auth pandas
# =========================
# NOTE (pandas pin): don't pin pandas==2.2.2 alongside pandas-ta in the
# same install - causes a hard ResolutionImpossible error. Leave pandas
# unpinned; any "incompatible" warning from unrelated packages is cosmetic.

# =========================
# IMPORTS
# =========================

import os
import pandas as pd
import pandas_ta as ta
import numpy as np
import gspread

from google.oauth2.service_account import Credentials
from gspread_dataframe import get_as_dataframe
from gspread_dataframe import set_with_dataframe

# =========================
# GOOGLE AUTH (Service Account - works on GitHub/local/server)
# =========================
#
# Colab's "auth.authenticate_user()" only works inside a Colab notebook
# (browser popup auth). On GitHub Actions / local / any server, we use
# a Service Account JSON key instead.
#
# SETUP STEPS (one-time):
# 1. Google Cloud Console -> IAM & Admin -> Service Accounts -> Create.
# 2. Enable "Google Sheets API" and "Google Drive API" for the project.
# 3. Create a JSON key for that service account, download it.
# 4. Open your Google Sheet -> Share -> add the service account's
#    email (xxxx@xxxx.iam.gserviceaccount.com) as Editor.
# 5. Save the JSON key as credentials.json next to this script
#    (add it to .gitignore - never commit it), OR set its full JSON
#    content as the GOOGLE_CREDENTIALS_JSON environment variable
#    (recommended for GitHub Actions secrets).

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_gspread_client():
    """
    Tries to load credentials from:
    1. GOOGLE_CREDENTIALS_JSON env var (paste full JSON content as a secret)
    2. credentials.json file in the same folder (local use)
    """
    creds_json_env = os.environ.get("GOOGLE_CREDENTIALS")

    if creds_json_env:
        import json
        creds_dict = json.loads(creds_json_env)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)

    return gspread.authorize(creds)


gc = get_gspread_client()

# =========================
# SETTINGS
# =========================

SPREADSHEET_ID = "13DYY42q_PDLxinGkqT1rigsG2t8zzOioFBcL-suOQFU"

# ----------------------------------
# ANALYSIS MODE - must be defined before OUTPUT_SHEET below
# ----------------------------------
# "LATEST" -> current behavior: analyze the most recent available
#             candle for each coin (daily monitoring snapshot).
# "RANGE"  -> analyze a specific historical window. The code truncates
#             each coin's data to TO_DATE (so nothing "from the future"
#             leaks into the indicators), and treats TO_DATE's bar as
#             the "current" bar. FROM_DATE is mainly informational here
#             since all rolling indicators (ATR, RSI, Z-scores, etc.)
#             still need their own lookback BEFORE TO_DATE to compute -
#             so make sure your raw sheet has enough history before
#             FROM_DATE (ideally 200+ days before FROM_DATE for EMA200).

ANALYSIS_MODE = "LATEST"   # "LATEST" or "RANGE"

# Only used when ANALYSIS_MODE = "RANGE". Format: "YYYY-MM-DD"
FROM_DATE = "2026-03-01"
TO_DATE = "2026-03-31"

RAW_SHEET = "Sheet2"
OUTPUT_SHEET = "DB" if ANALYSIS_MODE == "LATEST" else "DB_RANGE"
# NOTE: RANGE mode writes to "DB_RANGE" (auto-created if missing) so it
# never overwrites your daily LATEST snapshot in "DB". Make sure a
# worksheet/tab named "DB_RANGE" exists in the spreadsheet, or create
# one once manually before running in RANGE mode.

# ----------------------------------
# TIMEZONE SETTING (FIX #1)
# ----------------------------------
# IMPORTANT: set this to whatever timezone your raw "date" column's
# daily candle close is anchored to (most exchanges = UTC).
# On GitHub Actions / most servers the clock is UTC by default, but if
# your sheet dates are IST-anchored, change this to "Asia/Kolkata".

DATA_TIMEZONE = "UTC"

# ----------------------------------
# ENABLE / DISABLE
# ----------------------------------

ENABLE_ATR7 = True
ENABLE_ATR14 = True
ENABLE_ATR14_Z = True
ENABLE_ATR14_HALFLIFE = True

ENABLE_RSI = True

ENABLE_EMA20 = True
ENABLE_EMA50 = True
ENABLE_EMA200 = True

ENABLE_ADX = True
ENABLE_OBV = True
ENABLE_VOLUME_Z = True

ENABLE_BB = True
ENABLE_DONCHIAN = True

ENABLE_BOS_CHOCH = True
ENABLE_PERIOD_LEVELS = True   # PDH/PDL, PWH/PWL, PMH/PML

ENABLE_PERCENTILE = True
ENABLE_CROSS_SECTIONAL_RANK = True

ENABLE_DATA_VALIDATION = True   # FIX #5
ENABLE_CONFIDENCE_SCORE = True  # FIX #6 (bonus)

# ----------------------------------
# PERIOD SETTINGS
# ----------------------------------

ATR7_PERIOD = 7
ATR14_PERIOD = 14

RSI_PERIOD = 14

EMA20_PERIOD = 20
EMA50_PERIOD = 50
EMA200_PERIOD = 200

ADX_PERIOD = 14

BB_PERIOD = 20
BB_STD = 2

DONCHIAN_PERIOD = 20

SWING_LOOKBACK = 5          # fractal swing detection window (bars each side)

# FIX (BOS/CHoCH too strict): real-run output showed BOS/CHoCH firing on
# only 1 of 45 coins. Loosened both knobs below so genuine structure
# breaks aren't filtered out along with the noise.
MIN_SWING_CONFIRMATIONS = 2  # was 3 -> too strict, almost never had enough agreement
BOS_NOISE_ATR_MULT = 0.15    # was 0.25 -> was filtering out real breaks, not just noise

Z_LOOKBACK = 50             # window used for Z-score / percentile / regime filtering
REGIME_ADX_THRESHOLD = 25   # ADX >= this => "trending", else "ranging"
MIN_REGIME_SAMPLES = 5      # below this, Z-score falls back to full window

NSD = "NSD"   # No Sufficient Data

# =========================
# OPEN SHEET
# =========================

spreadsheet = gc.open_by_key(
    SPREADSHEET_ID
)

raw_sheet = spreadsheet.worksheet(
    RAW_SHEET
)

try:
    db_sheet = spreadsheet.worksheet(
        OUTPUT_SHEET
    )
except gspread.exceptions.WorksheetNotFound:
    print(f"Worksheet '{OUTPUT_SHEET}' not found, creating it...")
    db_sheet = spreadsheet.add_worksheet(
        title=OUTPUT_SHEET, rows=1000, cols=50
    )

# =========================
# READ RAW DATA
# =========================

df = get_as_dataframe(
    raw_sheet,
    evaluate_formulas=True
)

df = df.dropna(how="all")

# =========================
# CLEAN DATA
# =========================

df.columns = [
    str(c).strip().lower()
    for c in df.columns
]

required_cols = [
    "coin",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume"
]

for col in required_cols:

    if col not in df.columns:

        raise Exception(
            f"Missing Column: {col}"
        )

df["date"] = pd.to_datetime(
    df["date"]
)

for col in [
    "open",
    "high",
    "low",
    "close",
    "volume"
]:

    df[col] = pd.to_numeric(
        df[col],
        errors="coerce"
    )

df = df.sort_values(
    ["coin", "date"]
)

# =========================
# FIX #5: DATA VALIDATION LAYER
# =========================
# Detects bad rows (negative/zero prices, OHLC inconsistency, duplicate
# dates, large gaps) BEFORE indicators run, so a single bad row doesn't
# silently corrupt a whole coin's calculations.

validation_log = []

def validate_coin_data(coin_name, coin_df):
    issues = []

    # Negative or zero price/volume
    bad_price = coin_df[
        (coin_df[["open", "high", "low", "close"]] <= 0).any(axis=1)
    ]
    if len(bad_price) > 0:
        issues.append(f"{len(bad_price)} row(s) with non-positive OHLC")

    bad_vol = coin_df[coin_df["volume"] < 0]
    if len(bad_vol) > 0:
        issues.append(f"{len(bad_vol)} row(s) with negative volume")

    # OHLC consistency: high must be >= low, open/close within [low,high]
    inconsistent = coin_df[
        (coin_df["high"] < coin_df["low"]) |
        (coin_df["close"] > coin_df["high"]) |
        (coin_df["close"] < coin_df["low"]) |
        (coin_df["open"] > coin_df["high"]) |
        (coin_df["open"] < coin_df["low"])
    ]
    if len(inconsistent) > 0:
        issues.append(f"{len(inconsistent)} row(s) with OHLC inconsistency")

    # Duplicate dates
    dupes = coin_df["date"].duplicated().sum()
    if dupes > 0:
        issues.append(f"{dupes} duplicate date(s)")

    # Large gaps (missing days) - only warn, don't block
    if len(coin_df) > 1:
        diffs = coin_df["date"].sort_values().diff().dropna()
        max_gap = diffs.max()
        if pd.notna(max_gap) and max_gap > pd.Timedelta(days=3):
            issues.append(f"data gap of {max_gap.days} day(s) detected")

    return issues


if ENABLE_DATA_VALIDATION:

    for coin_name in df["coin"].dropna().unique():
        coin_subset = df[df["coin"] == coin_name]
        issues = validate_coin_data(coin_name, coin_subset)

        if issues:
            validation_log.append({
                "coin": coin_name,
                "issues": "; ".join(issues)
            })

    # Drop fully invalid rows (non-positive price) so they don't poison
    # rolling calculations downstream. Duplicates: keep last occurrence.
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
    df = df[df["volume"] >= 0]
    df = df.drop_duplicates(subset=["coin", "date"], keep="last")
    df = df.sort_values(["coin", "date"])

    if validation_log:
        print("\n⚠️  DATA VALIDATION WARNINGS:")
        for entry in validation_log:
            print(f"  - {entry['coin']}: {entry['issues']}")
        print()

# =========================
# HELPER FUNCTIONS
# =========================

def robust_zscore(series, lookback):
    """
    Standard (non-conditional) Robust Z-Score using Median + MAD.
    Returns the z-score of the LAST value in `series` using the
    trailing `lookback` window.
    """
    s = series.dropna().tail(lookback)

    if len(s) < lookback:
        return None

    median = s.median()
    mad = (s - median).abs().median()

    if mad == 0:
        return 0.0

    z = (s.iloc[-1] - median) / (1.4826 * mad)
    return float(z)


def regime_conditional_robust_zscore(value_series, regime_series, lookback):
    """
    Robust Z-Score where Median/MAD are computed ONLY from historical
    rows that share the SAME regime label as the current (last) row.

    FIX #3: returns a tuple (z, method) where method is
    "regime" if same-regime samples were sufficient,
    "fallback" if it had to use the full window, or
    "insufficient_data" if there isn't even enough history for the
    full lookback window. This is surfaced in the output so you know
    which Z-scores to trust more.
    """
    vals = value_series.tail(lookback)
    regimes = regime_series.tail(lookback)

    if len(vals) < lookback or vals.isna().any():
        return None, "insufficient_data"

    current_regime = regimes.iloc[-1]
    mask = regimes == current_regime

    same_regime_vals = vals[mask]

    method = "regime"

    if len(same_regime_vals) < MIN_REGIME_SAMPLES:
        # not enough same-regime samples -> fall back to full window
        same_regime_vals = vals
        method = "fallback"

    median = same_regime_vals.median()
    mad = (same_regime_vals - median).abs().median()

    if mad == 0:
        return 0.0, method

    z = (vals.iloc[-1] - median) / (1.4826 * mad)
    return float(z), method


def percentile_rank(series, lookback):
    """
    Percentile rank (0-100) of the last value within the trailing window.
    """
    s = series.dropna().tail(lookback)

    if len(s) < lookback:
        return None

    current = s.iloc[-1]
    pct = (s <= current).sum() / len(s) * 100
    return round(float(pct), 2)


def mean_reversion_half_life(series, lookback):
    """
    Estimates half-life (in periods) of mean reversion using
    lag-1 autocorrelation: half_life = ln(0.5) / ln(rho)

    FIX #4: handles the negative-autocorrelation case by reporting it
    explicitly instead of just returning NSD, so you know WHY it
    couldn't compute a half-life (oscillating vs no reversion signal
    at all are different things).
    """
    s = series.dropna().tail(lookback)

    if len(s) < lookback:
        return None, "insufficient_data"

    rho = s.autocorr(lag=1)

    if rho is None:
        return None, "undefined"

    if rho <= 0:
        return None, "oscillating_or_no_reversion"

    if rho >= 1:
        return None, "non_stationary"

    hl = np.log(0.5) / np.log(rho)
    return round(float(hl), 2), "ok"


def detect_swings(high, low, window):
    """
    Simple fractal swing high/low detection.
    A bar is a swing high if its high is the max in [-window, +window].
    A bar is a swing low if its low is the min in [-window, +window].
    """
    swing_high = pd.Series(False, index=high.index)
    swing_low = pd.Series(False, index=low.index)

    for i in range(window, len(high) - window):

        h_window = high.iloc[i - window: i + window + 1]
        l_window = low.iloc[i - window: i + window + 1]

        if high.iloc[i] == h_window.max():
            swing_high.iloc[i] = True

        if low.iloc[i] == l_window.min():
            swing_low.iloc[i] = True

    return swing_high, swing_low


def detect_bos_choch(coin_df, window, atr_series=None, ema_trend=None):
    """
    Returns ('BOS_BULL' / 'BOS_BEAR' / 'CHoCH_BULL' / 'CHoCH_BEAR' / 'NONE')
    for the LATEST bar.

    FIX (redesigned, real-run calibrated):
    Previous version determined "uptrend/downtrend" from agreement
    between only 2-3 recent swing-point diffs. That's a very narrow
    condition (often just 1 diff value to agree on) and across two
    live runs only fired on 1-2 of 45 coins - clearly under-detecting
    real structure breaks, not just filtering noise.

    New approach:
    - Trend direction now comes from `ema_trend` (BULLISH/BEARISH),
      which is already computed elsewhere from EMA20/50/200 - a much
      more stable, less noise-sensitive trend signal than 2-3 swing
      diffs.
    - BOS = price breaks the most recent confirmed swing high (in an
      uptrend) or swing low (in a downtrend), continuing the trend.
    - CHoCH = price breaks the most recent confirmed swing low (in an
      uptrend) or swing high (in a downtrend), signaling a possible
      reversal.
    - Still requires the break to exceed BOS_NOISE_ATR_MULT * ATR14,
      filtering out tiny noise breaks.
    """
    if len(coin_df) < (window * 2 + 10):
        return NSD

    if ema_trend not in ("BULLISH", "BEARISH"):
        return "NONE"

    high = coin_df["high"].reset_index(drop=True)
    low = coin_df["low"].reset_index(drop=True)
    close = coin_df["close"].reset_index(drop=True)

    swing_high, swing_low = detect_swings(high, low, window)

    swing_high_idx = swing_high[swing_high].index.tolist()
    swing_low_idx = swing_low[swing_low].index.tolist()

    if len(swing_high_idx) == 0 or len(swing_low_idx) == 0:
        return "NONE"

    last_swing_high = high.iloc[swing_high_idx[-1]]
    last_swing_low = low.iloc[swing_low_idx[-1]]
    current_close = close.iloc[-1]

    # Noise filter: require the break to exceed a fraction of ATR14
    min_break_size = 0.0
    if atr_series is not None and len(atr_series.dropna()) > 0:
        min_break_size = float(atr_series.dropna().iloc[-1]) * BOS_NOISE_ATR_MULT

    def exceeds(level, price, direction):
        if direction == "above":
            return (price - level) > min_break_size
        else:
            return (level - price) > min_break_size

    if ema_trend == "BULLISH":

        if exceeds(last_swing_high, current_close, "above"):
            return "BOS_BULL"

        if exceeds(last_swing_low, current_close, "below"):
            return "CHoCH_BEAR"

    elif ema_trend == "BEARISH":

        if exceeds(last_swing_low, current_close, "below"):
            return "BOS_BEAR"

        if exceeds(last_swing_high, current_close, "above"):
            return "CHoCH_BULL"

    return "NONE"


def get_period_levels(coin_df):
    """
    Returns Previous Day/Week/Month High & Low based on the data
    available (assumes daily OHLC rows).
    """
    levels = {}

    d = coin_df.copy()
    d = d.set_index("date")

    # ---- Previous Day High/Low ----
    if len(d) >= 2:
        levels["PDH"] = round(float(d["high"].iloc[-2]), 8)
        levels["PDL"] = round(float(d["low"].iloc[-2]), 8)
    else:
        levels["PDH"] = NSD
        levels["PDL"] = NSD

    # ---- Previous Week High/Low ----
    weekly = d.resample("W").agg({"high": "max", "low": "min"})
    if len(weekly) >= 2:
        levels["PWH"] = round(float(weekly["high"].iloc[-2]), 8)
        levels["PWL"] = round(float(weekly["low"].iloc[-2]), 8)
    else:
        levels["PWH"] = NSD
        levels["PWL"] = NSD

    # ---- Previous Month High/Low ----
    # FIX: use try/except for pandas version compatibility ("ME" vs "M")
    try:
        monthly = d.resample("ME").agg({"high": "max", "low": "min"})
    except ValueError:
        monthly = d.resample("M").agg({"high": "max", "low": "min"})

    if len(monthly) >= 2:
        levels["PMH"] = round(float(monthly["high"].iloc[-2]), 8)
        levels["PML"] = round(float(monthly["low"].iloc[-2]), 8)
    else:
        levels["PMH"] = NSD
        levels["PML"] = NSD

    return levels


# =========================
# SHEET1 SYMBOL ORDER
# =========================
# Capture the original coin order from Sheet1 (the fetch-list sheet)
# so the DB output can be written back in that SAME sequence, instead
# of alphabetical. Falls back gracefully if Sheet1 isn't reachable for
# any reason (output still works, just alphabetical in that edge case).

try:
    sheet1_symbols_raw = spreadsheet.worksheet("Sheet1").col_values(1)
    sheet1_order = [
        str(x).strip().upper()
        for x in sheet1_symbols_raw
        if str(x).strip()
    ]
except Exception as e:
    print(f"Could not read Sheet1 order, falling back to alphabetical: {e}")
    sheet1_order = []

# =========================
# RANGE MODE: pre-validate dates
# =========================

if ANALYSIS_MODE == "RANGE":

    from_date_ts = pd.to_datetime(FROM_DATE)
    to_date_ts = pd.to_datetime(TO_DATE)

    if from_date_ts > to_date_ts:
        raise Exception("FROM_DATE cannot be after TO_DATE")

    print(f"RANGE MODE: analyzing as of {TO_DATE} (history available before this date will be used for indicators)")

# =========================
# PROCESS
# =========================

results = []

coins = sorted(
    df["coin"].dropna().unique()
)

print(
    f"Coins Found: {len(coins)}"
)

# FIX #1: timezone-aware "today" for live-row detection
today_norm = pd.Timestamp.now(tz=DATA_TIMEZONE).tz_localize(None).normalize()

for coin in coins:

    raw_coin_df = df[
        df["coin"] == coin
    ].copy()

    raw_coin_df = raw_coin_df.sort_values(
        "date"
    )

    # ---------------------------------
    # RANGE MODE: truncate to TO_DATE so nothing after it is used,
    # and treat TO_DATE's bar as "latest" for all calculations below.
    # ---------------------------------

    if ANALYSIS_MODE == "RANGE":

        raw_coin_df = raw_coin_df[raw_coin_df["date"] <= to_date_ts]

        if len(raw_coin_df) == 0:
            print(f"  [{coin}] No data available on/before {TO_DATE}, skipping.")
            continue

    latest = raw_coin_df.iloc[-1]

    # ---------------------------------
    # LIVE / INCOMPLETE CANDLE HANDLING (FIX #1: timezone-aware)
    # In RANGE mode, the "today" check is irrelevant (we're looking at
    # a historical date), so we always treat the last row as a closed
    # candle in that case.
    # ---------------------------------

    latest_date_norm = latest["date"].normalize()

    if ANALYSIS_MODE == "RANGE":
        is_live_row = False
    else:
        is_live_row = latest_date_norm == today_norm

    live_close = float(latest["close"])

    if is_live_row and len(raw_coin_df) > 1:
        coin_df = raw_coin_df.iloc[:-1].copy()
    else:
        coin_df = raw_coin_df.copy()

    # Previous close, for % change
    # NOTE: coin_df may or may not already exclude the latest live row.
    # We always want the close ONE bar before `latest`, so we look at
    # raw_coin_df (the unfiltered series) two rows back from the end.
    prev_close = None
    if len(raw_coin_df) >= 2:
        prev_close = float(raw_coin_df["close"].iloc[-2])

    pct_change = None
    if prev_close and prev_close != 0:
        pct_change = round(((live_close - prev_close) / prev_close) * 100, 4)

    row = {

        "coin":
            latest["coin"],

        "date":
            latest["date"].strftime(
                "%Y-%m-%d"
            ),

        "close":
            round(
                live_close,
                8
            ),

        "Pct_Change":
            pct_change if pct_change is not None else NSD,

        "Data_Status":
            "LIVE" if is_live_row else "CLOSED"

    }

    confidence_flags = []  # FIX #6: track quality issues per coin

    # =====================
    # ATR 7
    # =====================

    atr7_series = None

    if ENABLE_ATR7:

        if len(coin_df) >= ATR7_PERIOD + 5:

            atr7_series = ta.atr(
                coin_df["high"],
                coin_df["low"],
                coin_df["close"],
                length=ATR7_PERIOD
            )

            row["ATR7"] = round(
                float(atr7_series.iloc[-1]),
                8
            )

        else:

            row["ATR7"] = NSD

    # =====================
    # ATR 14
    # =====================

    atr14_series = None

    if ENABLE_ATR14 or ENABLE_ATR14_Z or ENABLE_ATR14_HALFLIFE or ENABLE_BOS_CHOCH:

        if len(coin_df) >= ATR14_PERIOD + 5:

            atr14_series = ta.atr(
                coin_df["high"],
                coin_df["low"],
                coin_df["close"],
                length=ATR14_PERIOD
            )

            if ENABLE_ATR14:
                row["ATR14"] = round(
                    float(atr14_series.iloc[-1]),
                    8
                )

        else:

            if ENABLE_ATR14:
                row["ATR14"] = NSD

    # =====================
    # ATR7 vs ATR14 CROSSOVER (volatility momentum signal)
    # =====================

    if ENABLE_ATR7 and (ENABLE_ATR14 or ENABLE_ATR14_Z or ENABLE_ATR14_HALFLIFE):

        a7 = row.get("ATR7", NSD)
        a14 = row.get("ATR14", NSD)

        if a7 == NSD or a14 == NSD:
            row["ATR_Crossover"] = NSD
        elif a7 > a14:
            row["ATR_Crossover"] = "EXPANDING"
        elif a7 < a14:
            row["ATR_Crossover"] = "CONTRACTING"
        else:
            row["ATR_Crossover"] = "FLAT"

    # =====================
    # ADX14 + REGIME LABEL (now surfaced in output)
    # =====================

    adx_series = None
    regime_series = None

    if ENABLE_ADX or ENABLE_ATR14_Z:

        if len(coin_df) >= ADX_PERIOD * 2:

            adx_df = ta.adx(
                coin_df["high"],
                coin_df["low"],
                coin_df["close"],
                length=ADX_PERIOD
            )

            adx_col = f"ADX_{ADX_PERIOD}"
            adx_series = adx_df[adx_col]

            regime_series = adx_series.apply(
                lambda x: "trending" if pd.notna(x) and x >= REGIME_ADX_THRESHOLD else "ranging"
            )

            if ENABLE_ADX:
                row["ADX14"] = round(
                    float(adx_series.iloc[-1]),
                    4
                )
                row["Regime"] = regime_series.iloc[-1]  # NEW: now in output

        else:

            if ENABLE_ADX:
                row["ADX14"] = NSD
                row["Regime"] = NSD

    # =====================
    # EMA20 / EMA50 / EMA200
    # =====================

    if ENABLE_EMA20:

        if len(coin_df) >= EMA20_PERIOD:

            ema20 = ta.ema(coin_df["close"], length=EMA20_PERIOD)
            row["EMA20"] = round(float(ema20.iloc[-1]), 8)

        else:
            row["EMA20"] = NSD

    if ENABLE_EMA50:

        if len(coin_df) >= EMA50_PERIOD:

            ema50 = ta.ema(coin_df["close"], length=EMA50_PERIOD)
            row["EMA50"] = round(float(ema50.iloc[-1]), 8)

        else:
            row["EMA50"] = NSD

    if ENABLE_EMA200:

        if len(coin_df) >= EMA200_PERIOD:

            ema200 = ta.ema(coin_df["close"], length=EMA200_PERIOD)
            row["EMA200"] = round(float(ema200.iloc[-1]), 8)

        else:
            row["EMA200"] = NSD

    # =====================
    # EMA TREND LABEL (combined signal)
    # =====================

    e20 = row.get("EMA20", NSD)
    e50 = row.get("EMA50", NSD)
    e200 = row.get("EMA200", NSD)

    if e20 == NSD or e50 == NSD:
        row["EMA_Trend"] = NSD
        confidence_flags.append("EMA_insufficient_data")

    elif e200 == NSD:
        # EMA200 not available yet -> use only EMA20 vs EMA50
        if e20 > e50:
            row["EMA_Trend"] = "BULLISH"
        elif e20 < e50:
            row["EMA_Trend"] = "BEARISH"
        else:
            row["EMA_Trend"] = "NEUTRAL"
        confidence_flags.append("EMA200_missing")

    else:
        if e20 > e50 > e200:
            row["EMA_Trend"] = "BULLISH"
        elif e20 < e50 < e200:
            row["EMA_Trend"] = "BEARISH"
        else:
            row["EMA_Trend"] = "NEUTRAL"

    # =====================
    # BOS / CHoCH (now noise-filtered using ATR14, thresholds loosened)
    # =====================

    if ENABLE_BOS_CHOCH:

        structure_signal = detect_bos_choch(
            coin_df, SWING_LOOKBACK, atr14_series,
            ema_trend=row.get("EMA_Trend")
        )
        row["BOS_CHoCH"] = structure_signal

    # =====================
    # PDH/PDL, PWH/PWL, PMH/PML
    # =====================

    if ENABLE_PERIOD_LEVELS:

        levels = get_period_levels(coin_df)
        row.update(levels)

    # =====================
    # RSI
    # =====================

    if ENABLE_RSI:

        if len(coin_df) >= RSI_PERIOD + 5:

            rsi = ta.rsi(
                coin_df["close"],
                length=RSI_PERIOD
            )

            row["RSI14"] = round(
                float(rsi.iloc[-1]),
                4
            )

        else:

            row["RSI14"] = NSD

    # =====================
    # ATR14 Z SCORE (Regime-Conditional Robust) — now with method flag
    # =====================
    # FIX (confidence gap): previously only "fallback" was flagged.
    # "insufficient_data" cases (not enough history at all) were
    # silently invisible to the Confidence score. Both are now flagged.

    if ENABLE_ATR14_Z:

        if atr14_series is not None and regime_series is not None and len(coin_df) >= Z_LOOKBACK:

            z, method = regime_conditional_robust_zscore(
                atr14_series, regime_series, Z_LOOKBACK
            )

            row["ATR14_Z"] = round(z, 4) if z is not None else NSD
            row["ATR14_Z_Method"] = method

            if method in ("fallback", "insufficient_data"):
                confidence_flags.append(f"ATR14_Z_{method}")

        else:

            row["ATR14_Z"] = NSD
            row["ATR14_Z_Method"] = "insufficient_data"
            confidence_flags.append("ATR14_Z_insufficient_data")

    # =====================
    # ATR14 HALF-LIFE — now with reason flag
    # =====================
    # FIX (confidence gap): non-"ok" reasons (insufficient_data,
    # oscillating_or_no_reversion, non_stationary, undefined) now all
    # contribute to the Confidence score, not just silently shown in
    # the Reason column.

    if ENABLE_ATR14_HALFLIFE:

        if atr14_series is not None and len(coin_df) >= Z_LOOKBACK:

            hl, reason = mean_reversion_half_life(atr14_series, Z_LOOKBACK)
            row["ATR14_HalfLife"] = hl if hl is not None else NSD
            row["ATR14_HalfLife_Reason"] = reason

            if reason != "ok":
                confidence_flags.append(f"ATR14_HalfLife_{reason}")

        else:

            row["ATR14_HalfLife"] = NSD
            row["ATR14_HalfLife_Reason"] = "insufficient_data"
            confidence_flags.append("ATR14_HalfLife_insufficient_data")

    # =====================
    # OBV + OBV Z-SCORE (Regime-Conditional Robust)
    # =====================
    # FIX (confidence gap): insufficient_data method now flagged too.

    if ENABLE_OBV:

        if len(coin_df) >= 5:

            obv_series = ta.obv(
                coin_df["close"],
                coin_df["volume"]
            )

            row["OBV"] = round(float(obv_series.iloc[-1]), 4)

            if regime_series is not None and len(coin_df) >= Z_LOOKBACK:

                z, method = regime_conditional_robust_zscore(
                    obv_series, regime_series, Z_LOOKBACK
                )
                row["OBV_Z"] = round(z, 4) if z is not None else NSD
                if method in ("fallback", "insufficient_data"):
                    confidence_flags.append(f"OBV_Z_{method}")

            else:
                row["OBV_Z"] = NSD
                confidence_flags.append("OBV_Z_insufficient_data")

            if len(coin_df) >= Z_LOOKBACK:
                p = percentile_rank(obv_series, Z_LOOKBACK)
                row["OBV_Percentile"] = p if p is not None else NSD
            else:
                row["OBV_Percentile"] = NSD

        else:

            row["OBV"] = NSD
            row["OBV_Z"] = NSD
            row["OBV_Percentile"] = NSD
            confidence_flags.append("OBV_insufficient_data")

    # =====================
    # RAW VOLUME (reference column)
    # =====================

    row["Volume"] = round(float(coin_df["volume"].iloc[-1]), 4)

    # =====================
    # VOLUME Z-SCORE (Regime-Conditional Robust)
    # =====================
    # FIX (confidence gap): insufficient_data method now flagged too.

    if ENABLE_VOLUME_Z:

        if regime_series is not None and len(coin_df) >= Z_LOOKBACK:

            z, method = regime_conditional_robust_zscore(
                coin_df["volume"], regime_series, Z_LOOKBACK
            )
            row["Volume_Z"] = round(z, 4) if z is not None else NSD
            if method in ("fallback", "insufficient_data"):
                confidence_flags.append(f"Volume_Z_{method}")

        else:
            row["Volume_Z"] = NSD
            confidence_flags.append("Volume_Z_insufficient_data")

    # =====================
    # ADX Z-SCORE (Regime-Conditional Robust)
    # =====================
    # FIX (confidence gap): insufficient_data method now flagged too.

    if ENABLE_ADX:

        if adx_series is not None and regime_series is not None and len(coin_df) >= Z_LOOKBACK:

            z, method = regime_conditional_robust_zscore(
                adx_series, regime_series, Z_LOOKBACK
            )
            row["ADX14_Z"] = round(z, 4) if z is not None else NSD
            if method in ("fallback", "insufficient_data"):
                confidence_flags.append(f"ADX14_Z_{method}")

        else:
            row["ADX14_Z"] = NSD
            confidence_flags.append("ADX14_Z_insufficient_data")

    # =====================
    # BOLLINGER BANDS
    # =====================

    if ENABLE_BB:

        if len(coin_df) >= BB_PERIOD:

            bb = ta.bbands(
                coin_df["close"],
                length=BB_PERIOD,
                std=BB_STD
            )

            upper_col = [c for c in bb.columns if c.startswith("BBU")][0]
            mid_col = [c for c in bb.columns if c.startswith("BBM")][0]
            lower_col = [c for c in bb.columns if c.startswith("BBL")][0]

            row["BB_UPPER"] = round(float(bb[upper_col].iloc[-1]), 8)
            row["BB_MIDDLE"] = round(float(bb[mid_col].iloc[-1]), 8)
            row["BB_LOWER"] = round(float(bb[lower_col].iloc[-1]), 8)

            if row["BB_MIDDLE"] != 0:
                bb_width = (row["BB_UPPER"] - row["BB_LOWER"]) / row["BB_MIDDLE"]
                row["BB_WIDTH"] = round(float(bb_width), 6)
            else:
                row["BB_WIDTH"] = NSD

        else:

            row["BB_UPPER"] = NSD
            row["BB_MIDDLE"] = NSD
            row["BB_LOWER"] = NSD
            row["BB_WIDTH"] = NSD

    # =====================
    # DONCHIAN CHANNEL (20)
    # =====================

    if ENABLE_DONCHIAN:

        if len(coin_df) >= DONCHIAN_PERIOD:

            dc_high = coin_df["high"].rolling(DONCHIAN_PERIOD).max()
            dc_low = coin_df["low"].rolling(DONCHIAN_PERIOD).min()

            row["DC20_HIGH"] = round(float(dc_high.iloc[-1]), 8)
            row["DC20_LOW"] = round(float(dc_low.iloc[-1]), 8)

        else:

            row["DC20_HIGH"] = NSD
            row["DC20_LOW"] = NSD

    # =====================
    # PERCENTILE CROSS-CHECK (ATR14, OBV, Volume, ADX)
    # =====================

    if ENABLE_PERCENTILE:

        if atr14_series is not None and len(coin_df) >= Z_LOOKBACK:
            p = percentile_rank(atr14_series, Z_LOOKBACK)
            row["ATR14_Percentile"] = p if p is not None else NSD
        else:
            row["ATR14_Percentile"] = NSD

        if len(coin_df) >= Z_LOOKBACK:
            p = percentile_rank(coin_df["volume"], Z_LOOKBACK)
            row["Volume_Percentile"] = p if p is not None else NSD
        else:
            row["Volume_Percentile"] = NSD

    # =====================
    # FIX #6: DATA CONFIDENCE SCORE
    # =====================
    # Simple transparency flag: tells you at a glance whether this row's
    # statistical fields (Z-scores, half-life) are fully reliable or
    # leaned on a fallback / had insufficient history.
    # FIX (this update): insufficient_data cases across ATR14_Z, OBV_Z,
    # Volume_Z, ADX14_Z, and ATR14_HalfLife are now ALL captured above,
    # so this score no longer misses rows that look "clean" on the
    # surface but are actually missing statistical history.

    if ENABLE_CONFIDENCE_SCORE:

        had_validation_issue = coin in [v["coin"] for v in validation_log]

        if had_validation_issue:
            confidence_flags.append("raw_data_issue")

        if len(confidence_flags) == 0:
            row["Confidence"] = "HIGH"
        elif len(confidence_flags) <= 2:
            row["Confidence"] = "MEDIUM"
        else:
            row["Confidence"] = "LOW"

        row["Confidence_Notes"] = ";".join(confidence_flags) if confidence_flags else ""

    results.append(row)

# =========================
# BUILD OUTPUT DATAFRAME
# =========================

out_df = pd.DataFrame(results)

# =========================
# CROSS-SECTIONAL RANK
# (Ranks each coin's Z-score against ALL other coins on this run)
# =========================

if ENABLE_CROSS_SECTIONAL_RANK:

    for col, rank_col in [
        ("ATR14_Z", "ATR14_Z_CrossRank"),
        ("OBV_Z", "OBV_Z_CrossRank"),
        ("Volume_Z", "Volume_Z_CrossRank"),
        ("ADX14_Z", "ADX14_Z_CrossRank"),
    ]:

        if col in out_df.columns:

            numeric_mask = pd.to_numeric(out_df[col], errors="coerce").notna()

            out_df[rank_col] = pd.Series(
                [NSD] * len(out_df),
                index=out_df.index,
                dtype="object"
            )

            ranks = (
                pd.to_numeric(out_df.loc[numeric_mask, col])
                .rank(pct=True) * 100
            ).round(2)

            for idx, val in ranks.items():
                out_df.at[idx, rank_col] = float(val)

# =========================
# FIX: DECISION-PRIORITY COLUMN ORDERING (left = highest impact)
# =========================

priority_order = [
    "coin", "date", "close", "Pct_Change", "Data_Status", "Confidence",
    "EMA_Trend", "Regime", "ADX14", "BOS_CHoCH",
    "PDH", "PDL", "PWH", "PWL", "PMH", "PML",
    "ATR14", "ATR7", "ATR_Crossover", "RSI14", "OBV", "Volume",
    "EMA20", "EMA50", "EMA200",
    "BB_UPPER", "BB_MIDDLE", "BB_LOWER", "BB_WIDTH",
    "DC20_HIGH", "DC20_LOW",
    "ATR14_Z", "ATR14_Z_Method", "OBV_Z", "Volume_Z", "ADX14_Z",
    "ATR14_HalfLife", "ATR14_HalfLife_Reason",
    "ATR14_Percentile", "Volume_Percentile", "OBV_Percentile",
    "ATR14_Z_CrossRank", "OBV_Z_CrossRank", "Volume_Z_CrossRank", "ADX14_Z_CrossRank",
    "Confidence_Notes",
]

existing_cols = [c for c in priority_order if c in out_df.columns]
remaining_cols = [c for c in out_df.columns if c not in existing_cols]
out_df = out_df[existing_cols + remaining_cols]

# =========================
# SORT OUTPUT IN SHEET1'S ORIGINAL SYMBOL ORDER
# =========================
# Instead of alphabetical, match the row order of Sheet1 (your
# fetch-list). Coins not found in sheet1_order (shouldn't normally
# happen) are appended at the end so nothing silently disappears.

if sheet1_order:

    order_map = {sym: idx for idx, sym in enumerate(sheet1_order)}
    out_df["_sort_key"] = out_df["coin"].map(order_map)

    # Anything not in Sheet1 order goes to the end, in alphabetical order
    max_key = (out_df["_sort_key"].max() if out_df["_sort_key"].notna().any() else -1)
    out_df["_sort_key"] = out_df["_sort_key"].fillna(max_key + 1)

    out_df = out_df.sort_values(["_sort_key", "coin"]).drop(columns=["_sort_key"])

else:
    out_df = out_df.sort_values("coin")

# =========================
# WRITE TO DB
# =========================

db_sheet.clear()

set_with_dataframe(
    db_sheet,
    out_df,
    include_index=False,
    include_column_header=True
)

# =========================
# SUMMARY
# =========================

print("\n========================")
print("DONE")
print("Analysis Mode:", ANALYSIS_MODE)
if ANALYSIS_MODE == "RANGE":
    print("As-of Date (TO_DATE):", TO_DATE, "| Reference FROM_DATE:", FROM_DATE)
print("Coins Processed:", len(out_df))
print("Output Sheet:", OUTPUT_SHEET)
print("Columns Generated:", list(out_df.columns))
if ENABLE_DATA_VALIDATION and validation_log:
    print("Data Validation Issues Found:", len(validation_log), "coin(s) - see warnings above")
if ENABLE_CONFIDENCE_SCORE:
    conf_counts = out_df["Confidence"].value_counts().to_dict()
    print("Confidence Breakdown:", conf_counts)
if ENABLE_BOS_CHOCH:
    bos_counts = out_df["BOS_CHoCH"].value_counts().to_dict()
    print("BOS_CHoCH Breakdown:", bos_counts)
print("========================")
