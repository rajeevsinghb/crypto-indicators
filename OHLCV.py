# =========================
# INSTALL LIBRARIES (run this once in terminal, not needed inside script)
# pip install ccxt pandas gspread gspread-dataframe google-auth
# =========================

import os
import time
import random
import ccxt
import pandas as pd
import gspread

from google.oauth2.service_account import Credentials
from gspread_dataframe import set_with_dataframe
from gspread.exceptions import APIError

# =========================
# GOOGLE AUTH (Service Account - works on GitHub/local/server)
# =========================
#
# Colab me "auth.authenticate_user()" sirf Colab notebook ke andar kaam
# karta hai (browser popup ke through). GitHub / local / server pe ye
# available nahi hota, isliye Service Account JSON key use karte hain.
#
# SETUP STEPS (one-time):
# 1. Google Cloud Console -> IAM & Admin -> Service Accounts -> Create.
# 2. Enable "Google Sheets API" and "Google Drive API" for the project.
# 3. Create a JSON key for that service account, download it.
# 4. Open your Google Sheet -> Share -> add the service account's
#    email (looks like xxxx@xxxx.iam.gserviceaccount.com) as Editor.
# 5. Save the JSON key file as credentials.json next to this script
#    (DO NOT commit this file to GitHub - add it to .gitignore),
#    OR set it as an environment variable (recommended for GitHub Actions).

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_gspread_client():

```
import json
import os

creds_json = os.environ["GOOGLE_CREDENTIALS"]

creds_dict = json.loads(creds_json)

creds = Credentials.from_service_account_info(
    creds_dict,
    scopes=SCOPES
)

return gspread.authorize(creds)
```


gc = get_gspread_client()

# =========================
# SETTINGS
# =========================

SPREADSHEET_ID = "13DYY42q_PDLxinGkqT1rigsG2t8zzOioFBcL-suOQFU"

# Change anytime
QUOTE_CURRENCY = "USDT"     # USDT / USDC / USD
LOOKBACK_DAYS = 365          # 30 / 90 / 365 etc.

# Exchange Priority
EXCHANGE_PRIORITY = [
    "bybit",
    "okx",
    "kucoin",
    "mexc",
    "gate"
]

# =========================
# RATE-LIMIT SAFE WRAPPER
# (handles Sheets API 429 / 503 errors with exponential backoff)
# =========================

def safe_call(func, *args, max_retries=8, base_wait=5, **kwargs):
    """
    Calls func(*args, **kwargs). On 429 (quota) or 503 (transient)
    errors from Google Sheets API, retries with exponential backoff
    + jitter. Raises after max_retries if still failing.
    """

    for attempt in range(1, max_retries + 1):

        try:
            return func(*args, **kwargs)

        except APIError as e:

            err_str = str(e)

            is_rate_limit = "429" in err_str
            is_transient = "503" in err_str or "500" in err_str

            if is_rate_limit or is_transient:

                wait = (base_wait * (2 ** (attempt - 1))) + random.uniform(0, 3)
                wait = min(wait, 120)  # cap wait at 2 minutes

                print(
                    f"  [RateLimit] Attempt {attempt}/{max_retries} failed "
                    f"({'429 quota' if is_rate_limit else 'transient error'}). "
                    f"Waiting {wait:.1f}s before retry..."
                )

                time.sleep(wait)

            else:
                # Non-retryable API error (bad request, permissions, etc.)
                raise

    raise Exception(
        f"safe_call: failed after {max_retries} retries for {func.__name__}"
    )


def safe_sleep(seconds):
    """Small pacing delay to avoid hammering the API in tight loops."""
    time.sleep(seconds)


# =========================
# CHUNKED SHEET WRITER
# Writes a big dataframe in row-chunks instead of one giant call,
# and paces each chunk so we never burst past quota -
# works no matter how many coins / how many rows.
# =========================

def write_dataframe_safely(
    worksheet,
    df,
    chunk_rows=5000,
    pause_between_chunks=2,
):
    """
    Clears the worksheet, then writes the dataframe in chunks.
    Each chunk write + the initial clear are wrapped in safe_call,
    so transient 429s are retried automatically. Pausing between
    chunks keeps us comfortably under the per-minute write quota
    regardless of total data size.
    """

    # 1. Clear the sheet (retried on 429)
    safe_call(worksheet.clear)
    safe_sleep(2)

    if len(df) == 0:
        print("  [Write] Empty dataframe, nothing to write (sheet cleared).")
        return

    total_rows = len(df)
    num_chunks = (total_rows // chunk_rows) + (1 if total_rows % chunk_rows else 0)

    print(f"  [Write] Writing {total_rows} rows in {num_chunks} chunk(s) of up to {chunk_rows} rows...")

    for i in range(num_chunks):

        start = i * chunk_rows
        end = min(start + chunk_rows, total_rows)

        chunk_df = df.iloc[start:end]

        # First chunk includes header + starts at row 1.
        # Subsequent chunks are appended below, without re-writing header.
        row_offset = 1 if i == 0 else (start + 2)  # +2 accounts for header row

        safe_call(
            set_with_dataframe,
            worksheet,
            chunk_df,
            row=row_offset,
            include_index=False,
            include_column_header=(i == 0),
        )

        print(f"    Chunk {i + 1}/{num_chunks} written (rows {start}-{end - 1}).")

        if i < num_chunks - 1:
            safe_sleep(pause_between_chunks)

    print("  [Write] Done.")


# =========================
# OPEN GOOGLE SHEETS
# =========================

spreadsheet = safe_call(gc.open_by_key, SPREADSHEET_ID)

sheet1 = safe_call(spreadsheet.worksheet, "Sheet1")
sheet2 = safe_call(spreadsheet.worksheet, "Sheet2")

# =========================
# READ SYMBOLS
# =========================

symbols = safe_call(sheet1.col_values, 1)

symbols = [
    str(x).strip().upper()
    for x in symbols
    if str(x).strip()
]

print(f"Symbols Found: {len(symbols)}")

# =========================
# LOAD EXCHANGES ONCE
# =========================

exchanges = {}

for ex_name in EXCHANGE_PRIORITY:

    try:

        exchange_class = getattr(
            ccxt,
            ex_name
        )

        exchange = exchange_class({
            "enableRateLimit": True
        })

        exchange.load_markets()

        exchanges[ex_name] = exchange

        print(f"Loaded: {ex_name}")

    except Exception as e:

        print(
            f"Failed: {ex_name} -> {e}"
        )

print(
    f"\nActive Exchanges: {len(exchanges)}"
)

# =========================
# FIND USDT SYMBOL ONLY
# =========================

def find_symbol(coin):

    target_symbol = (
        f"{coin}/{QUOTE_CURRENCY}"
    )

    for ex_name in EXCHANGE_PRIORITY:

        if ex_name not in exchanges:
            continue

        exchange = exchanges[ex_name]

        if target_symbol in exchange.markets:

            return (
                ex_name,
                exchange,
                target_symbol
            )

    return None, None, None

# =========================
# FETCH DATA
# (ccxt calls already rate-limited via enableRateLimit=True,
#  no change needed here - this part was never the bottleneck)
# =========================

rows = []

for idx, coin in enumerate(
    symbols,
    start=1
):

    print(
        f"[{idx}/{len(symbols)}] {coin}",
        end=" "
    )

    ex_name, exchange, market_symbol = (
        find_symbol(coin)
    )

    if exchange is None:

        print(
            f"NOT FOUND ({coin}/{QUOTE_CURRENCY})"
        )

        rows.append([
            coin,
            "NOT_FOUND",
            "",
            "",
            "",
            "",
            "",
            ""
        ])

        continue

    try:

        candles = exchange.fetch_ohlcv(
            market_symbol,
            timeframe="1d",
            limit=LOOKBACK_DAYS
        )

        print(
            f"{ex_name} | "
            f"{market_symbol} | "
            f"{len(candles)} candles"
        )

        for candle in candles:

            rows.append([

                coin,

                ex_name,

                pd.to_datetime(
                    candle[0],
                    unit="ms"
                ).strftime("%Y-%m-%d"),

                candle[1],  # open
                candle[2],  # high
                candle[3],  # low
                candle[4],  # close
                candle[5],  # volume

            ])

    except Exception as e:

        print(
            f"ERROR -> {e}"
        )

        rows.append([
            coin,
            "ERROR",
            "",
            "",
            "",
            "",
            "",
            ""
        ])

# =========================
# DATAFRAME
# =========================

df = pd.DataFrame(

    rows,

    columns=[
        "coin",
        "exchange",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]

)

# =========================
# SORT
# =========================

if len(df) > 0:

    df = df.sort_values(
        ["coin", "date"]
    )

# =========================
# WRITE SHEET2 (rate-limit safe, chunked, works for any data size)
# =========================

write_dataframe_safely(
    sheet2,
    df,
    chunk_rows=5000,          # increase/decrease if you want; auto-adapts via num_chunks
    pause_between_chunks=2,   # seconds between chunk writes
)

# =========================
# SUMMARY
# =========================

print("\n====================================")
print("DONE")
print("Quote Currency :", QUOTE_CURRENCY)
print("Lookback Days  :", LOOKBACK_DAYS)
print("Rows Written   :", len(df))
print("Output Sheet   : Sheet2")
print("====================================")
