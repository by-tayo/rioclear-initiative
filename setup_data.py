"""
Rio Grande Water Surveillance - Step 1: drinking water compliance data
-----------------------------------------------------------------------
What this does:
  1. Downloads EPA ECHO's "Drinking Water System Search Dataset" (~35 MB ZIP)
     from https://echo.epa.gov/tools/data-downloads (skips if already downloaded).
  2. Unzips it into data/raw/.
  3. Discovers which CSV holds the water-system records and which columns hold
     the state and the counties served (column names are detected, not assumed).
  4. Keeps only Texas systems in the counties along the Rio Grande.
  5. Saves data/processed/tx_border_water_systems.csv and a column list you can
     open to see every available field.

Run from the project folder:  python setup_data.py
"""

import sys
import zipfile
import urllib.request
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    sys.exit("pandas is not installed. Activate your venv and run: pip install -r requirements.txt")

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
URL = "https://echo.epa.gov/files/echodownloads/SDWA_system_search_download.zip"

BASE = Path(__file__).resolve().parent
RAW_DIR = BASE / "data" / "raw"
PROC_DIR = BASE / "data" / "processed"
ZIP_PATH = RAW_DIR / "SDWA_system_search_download.zip"
EXTRACT_DIR = RAW_DIR / "sdwa_system_search"
OUT_CSV = PROC_DIR / "tx_border_water_systems.csv"
COLS_TXT = PROC_DIR / "available_columns.txt"

# Texas counties along the Rio Grande, upstream (El Paso) to the Gulf (Cameron)
BORDER_COUNTIES = [
    "El Paso", "Hudspeth", "Presidio", "Brewster", "Terrell", "Val Verde",
    "Kinney", "Maverick", "Webb", "Zapata", "Starr", "Hidalgo", "Cameron",
]

TEXAS_VALUES = {"TX", "TEXAS", "48"}  # 48 = Texas FIPS code, just in case


def step(msg):
    print(f"\n=== {msg} ===")


# ----------------------------------------------------------------------------
# 1. Download
# ----------------------------------------------------------------------------
def download():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if ZIP_PATH.exists() and ZIP_PATH.stat().st_size > 1_000_000:
        print(f"Already downloaded: {ZIP_PATH.name} ({ZIP_PATH.stat().st_size/1e6:.1f} MB). Skipping.")
        return
    print(f"Downloading from {URL}")
    print("(about 35 MB - may take a minute on event Wi-Fi)")

    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(ZIP_PATH, "wb") as out:
            total = int(resp.headers.get("Content-Length", 0))
            done = 0
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done * 100 // total}%", end="", flush=True)
        print()
    except Exception as e:
        if ZIP_PATH.exists():
            ZIP_PATH.unlink()
        sys.exit(
            f"\nDownload failed: {e}\n"
            f"Fallback: open {URL} in your browser, then move the file to:\n  {ZIP_PATH}\n"
            "and run this script again."
        )

    if not zipfile.is_zipfile(ZIP_PATH):
        ZIP_PATH.unlink()
        sys.exit("The downloaded file is not a valid ZIP (the site may have returned an error page). "
                 "Use the browser fallback described above.")
    print(f"Saved {ZIP_PATH.name} ({ZIP_PATH.stat().st_size/1e6:.1f} MB)")


# ----------------------------------------------------------------------------
# 2. Unzip
# ----------------------------------------------------------------------------
def unzip():
    if EXTRACT_DIR.exists() and any(EXTRACT_DIR.rglob("*.csv")):
        print("Already extracted. Skipping.")
        return
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ZIP_PATH) as z:
        z.extractall(EXTRACT_DIR)
    print(f"Extracted to {EXTRACT_DIR}")


# ----------------------------------------------------------------------------
# 3. Discover the right CSV and columns
# ----------------------------------------------------------------------------
def read_header(path):
    for enc in ("utf-8", "latin-1"):
        try:
            return list(pd.read_csv(path, nrows=0, encoding=enc).columns), enc
        except UnicodeDecodeError:
            continue
    return [], "latin-1"


def pick_column(columns, must_contain, prefer=()):
    """Return the best-matching column name (case-insensitive), or None."""
    upper = {c: c.upper().replace(" ", "").replace("_", "") for c in columns}
    for p in prefer:  # exact preferred names first
        p_norm = p.upper().replace(" ", "").replace("_", "")
        for orig, norm in upper.items():
            if norm == p_norm:
                return orig
    for orig, norm in upper.items():
        if must_contain in norm:
            return orig
    return None


def discover():
    csvs = sorted(EXTRACT_DIR.rglob("*.csv"))
    if not csvs:
        # some ECHO zips use .txt for comma-delimited files
        csvs = sorted(EXTRACT_DIR.rglob("*.txt"))
    if not csvs:
        sys.exit(f"No CSV/TXT files found in {EXTRACT_DIR}. Check the ZIP contents.")

    print("Files found in the ZIP:")
    for f in csvs:
        print(f"  - {f.name} ({f.stat().st_size/1e6:.1f} MB)")

    best = None
    for f in sorted(csvs, key=lambda p: p.stat().st_size, reverse=True):
        cols, enc = read_header(f)
        state_col = pick_column(cols, "STATE", prefer=("StateCode", "PrimacyAgency", "State", "FacState"))
        county_col = pick_column(cols, "COUNT", prefer=("CountiesServed", "CountyServed", "Counties", "County"))
        if state_col and county_col:
            best = (f, enc, cols, state_col, county_col)
            break

    if not best:
        PROC_DIR.mkdir(parents=True, exist_ok=True)
        with open(COLS_TXT, "w", encoding="utf-8") as out:
            for f in csvs:
                cols, _ = read_header(f)
                out.write(f"{f.name}\n  " + "\n  ".join(cols) + "\n\n")
        sys.exit("Could not auto-detect state/county columns. "
                 f"All column names were written to {COLS_TXT}. Tell Claude which columns hold state and county.")

    f, enc, cols, state_col, county_col = best
    print(f"\nUsing file:     {f.name}")
    print(f"State column:   {state_col}")
    print(f"County column:  {county_col}")
    return f, enc, cols, state_col, county_col


# ----------------------------------------------------------------------------
# 4. Filter to Texas Rio Grande counties
# ----------------------------------------------------------------------------
def filter_and_save(f, enc, cols, state_col, county_col):
    df = pd.read_csv(f, dtype=str, encoding=enc, low_memory=False)
    print(f"Total rows nationwide: {len(df):,}")

    state_vals = df[state_col].fillna("").str.strip().str.upper()
    tx = df[state_vals.isin(TEXAS_VALUES) | state_vals.str.startswith("TX")]
    print(f"Texas rows:            {len(tx):,}")
    if tx.empty:
        sample = df[state_col].dropna().unique()[:15]
        sys.exit(f"No Texas rows matched. Sample values in {state_col}: {list(sample)}")

    county_text = tx[county_col].fillna("").str.upper()
    pattern = "|".join(rf"\b{c.upper()}\b" for c in BORDER_COUNTIES)
    border = tx[county_text.str.contains(pattern, regex=True)].copy()

    # Tag each system with the first border county it serves (handy for map filters)
    def first_county(text):
        t = str(text).upper()
        for c in BORDER_COUNTIES:
            if c.upper() in t:
                return c
        return None

    border["border_county"] = border[county_col].apply(first_county)

    PROC_DIR.mkdir(parents=True, exist_ok=True)
    border.to_csv(OUT_CSV, index=False)
    with open(COLS_TXT, "w", encoding="utf-8") as out:
        out.write(f"Source file: {f.name}\n\n" + "\n".join(cols) + "\n")

    step("RESULT")
    print(f"Rio Grande border-county water systems: {len(border):,}")
    print("\nSystems per county:")
    counts = border["border_county"].value_counts()
    for c in BORDER_COUNTIES:
        print(f"  {c:<10} {counts.get(c, 0):>5}")
    missing = [c for c in BORDER_COUNTIES if counts.get(c, 0) == 0]
    if missing:
        print(f"\nNote: no systems matched for {missing} - may be spelling in the data; we'll check.")
    print(f"\nSaved: {OUT_CSV}")
    print(f"Column list: {COLS_TXT}")


if __name__ == "__main__":
    step("1. Download")
    download()
    step("2. Unzip")
    unzip()
    step("3. Discover columns")
    found = discover()
    step("4. Filter to Texas Rio Grande counties")
    filter_and_save(*found)
    print("\nStep 1 complete.")
