"""
Rio Grande Water Surveillance - Step 3: river / surface water sampling data
---------------------------------------------------------------------------
Source: Water Quality Portal (waterqualitydata.us), run by USGS + EPA.

What this does:
  1. Asks the Water Quality Portal for sampling results since 2021 in the 13
     Texas Rio Grande counties for three measurements:
       - E. coli (bacteria - a direct health risk for people in contact with water)
       - Nitrate (fertilizer / wastewater runoff)
       - Total dissolved solids (salinity)
  2. Queries BOTH the legacy service (EPA, state, IBWC data) and the newer
     WQX3.0 beta service (where new USGS data has gone since March 2024),
     then merges and de-duplicates. If one service fails, it keeps going.
  3. Detects column names instead of assuming them.
  4. Places each sampling site in a county by its coordinates (point-in-polygon).
  5. Saves:
       data/processed/wq_sites.csv    (one row per sampling site)
       data/processed/wq_results.csv  (one row per sample result)

Run from the project folder:  python water_quality.py
"""

import io
import re
import sys
import json
import zipfile
import urllib.request
from pathlib import Path

try:
    import pandas as pd
    import requests
except ImportError:
    sys.exit("Missing packages. Activate your venv and run: pip install -r requirements.txt")

BASE = Path(__file__).resolve().parent
RAW = BASE / "data" / "raw"
PROC = BASE / "data" / "processed"
GEO_CACHE = RAW / "us_counties.geojson"
GEO_URL = "https://raw.githubusercontent.com/plotly/datasets/master/geojson-counties-fips.json"

COUNTIES = {
    "El Paso": "48141", "Hudspeth": "48229", "Presidio": "48377", "Brewster": "48043",
    "Terrell": "48443", "Val Verde": "48465", "Kinney": "48271", "Maverick": "48323",
    "Webb": "48479", "Zapata": "48505", "Starr": "48427", "Hidalgo": "48215",
    "Cameron": "48061",
}

# Exact WQP characteristic names (case-sensitive) -> label used in the app
CHARACTERISTICS = {
    "Escherichia coli": "E. coli",
    "Nitrate": "Nitrate",
    "Inorganic nitrogen (nitrate and nitrite)": "Nitrate",
    "Total dissolved solids": "Total dissolved solids",
}
START_DATE = "01-01-2021"  # MM-DD-YYYY, as the portal expects

LEGACY_STATION = "https://www.waterqualitydata.us/data/Station/search"
LEGACY_RESULT = "https://www.waterqualitydata.us/data/Result/search"
WQX3_RESULT = "https://www.waterqualitydata.us/wqx3/Result/search"


def step(msg):
    print(f"\n=== {msg} ===")


# ----------------------------------------------------------------------------
# Download helpers
# ----------------------------------------------------------------------------
def base_params():
    p = [("countycode", f"US:48:{fips[2:]}") for fips in COUNTIES.values()]
    p += [("characteristicName", c) for c in CHARACTERISTICS]
    p += [("startDateLo", START_DATE), ("mimeType", "csv"), ("zip", "yes")]
    return p


REFRESH = "--refresh" in sys.argv


def fetch(url, params, label):
    cache = RAW / ("wqp_" + re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") + ".csv")
    if cache.exists() and not REFRESH:
        df = pd.read_csv(cache, dtype=str, low_memory=False)
        print(f"  {label}: {len(df):,} rows (from saved download - add --refresh to re-download)")
        return df
    df = _download(url, params, label)
    if df is not None:
        RAW.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache, index=False)
    return df


def _download(url, params, label):
    print(f"Requesting {label} ... (can take 1-3 minutes)")
    try:
        r = requests.get(url, params=params, timeout=600, headers={"User-Agent": "Mozilla/5.0"})
    except Exception as e:
        print(f"  ! {label} failed: {e}")
        return None
    if r.status_code != 200:
        print(f"  ! {label} returned HTTP {r.status_code}: {r.text[:200]}")
        return None
    content = r.content
    if content[:2] == b"PK":  # zipped response
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            if not names:
                print(f"  ! {label}: empty ZIP")
                return None
            content = z.read(names[0])
    if not content.strip():
        print(f"  ! {label}: no rows returned")
        return None
    df = pd.read_csv(io.BytesIO(content), dtype=str, low_memory=False)
    print(f"  {label}: {len(df):,} rows")
    return df


# ----------------------------------------------------------------------------
# Column detection
# ----------------------------------------------------------------------------
def norm(s):
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def detect(cols, prefer, contains=None):
    normed = {c: norm(c) for c in cols}
    for p in prefer:
        for c, n in normed.items():
            if n == norm(p):
                return c
    if contains:
        for c, n in normed.items():
            if norm(contains) in n:
                return c
    return None


SITE_FIELDS = {
    "site_id": (["MonitoringLocationIdentifier", "Location_Identifier"], "LOCATIONIDENTIFIER"),
    "site_name": (["MonitoringLocationName", "Location_Name"], "LOCATIONNAME"),
    "lat": (["LatitudeMeasure", "Location_Latitude"], "LATITUDE"),
    "lon": (["LongitudeMeasure", "Location_Longitude"], "LONGITUDE"),
    "org": (["OrganizationFormalName", "Org_FormalName"], "FORMALNAME"),
}
RESULT_FIELDS = {
    "site_id": (["MonitoringLocationIdentifier", "Location_Identifier"], "LOCATIONIDENTIFIER"),
    "characteristic": (["CharacteristicName", "Result_Characteristic"], "CHARACTERISTIC"),
    "value": (["ResultMeasureValue", "Result_Measure"], "MEASUREVALUE"),
    "unit": (["ResultMeasure/MeasureUnitCode", "Result_MeasureUnit", "ResultMeasureUnit",
              "Result_MeasureUnitCode", "ResultMeasureMeasureUnitCode"], None),
    "date": (["ActivityStartDate", "Activity_StartDate"], "STARTDATE"),
    "org": (["OrganizationFormalName", "Org_FormalName"], "FORMALNAME"),
    "lat": (["LatitudeMeasure", "Location_Latitude"], None),
    "lon": (["LongitudeMeasure", "Location_Longitude"], None),
    "site_name": (["MonitoringLocationName", "Location_Name"], None),
}


def find_unit_column(cols):
    """Result unit column: mentions RESULT + UNIT, but not detection/quantitation limits."""
    for c in cols:
        n = norm(c)
        if "RESULT" in n and "UNIT" in n and not any(x in n for x in ("DETECTION", "QUANT", "LIMIT", "DEPTH")):
            return c
    return None


def standardize(df, fields, label):
    out = pd.DataFrame(index=df.index)
    for key, (prefer, contains) in fields.items():
        col = detect(df.columns, prefer, contains)
        if key == "unit" and col is None:
            col = find_unit_column(df.columns)
        if key == "unit":
            print(f"  {label}: unit column = {col}")
        out[key] = df[col] if col else None
        if col is None and key in ("site_id", "characteristic", "value", "lat", "lon"):
            print(f"  ({label}: no column found for '{key}')")
    return out


# ----------------------------------------------------------------------------
# County assignment (pure-Python point-in-polygon, no GIS install needed)
# ----------------------------------------------------------------------------
def load_county_shapes():
    if not GEO_CACHE.exists():
        GEO_CACHE.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(GEO_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            GEO_CACHE.write_bytes(r.read())
    geo = json.loads(GEO_CACHE.read_text(encoding="utf-8"))
    by_fips = {v: k for k, v in COUNTIES.items()}
    shapes = []
    for ft in geo["features"]:
        fid = str(ft.get("id"))
        if fid in by_fips:
            g = ft["geometry"]
            polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
            shapes.append((by_fips[fid], [poly[0] for poly in polys]))
    return shapes


def inside(lon, lat, ring):
    hit = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            hit = not hit
        j = i
    return hit


def county_for(lon, lat, shapes):
    for name, rings in shapes:
        if any(inside(lon, lat, r) for r in rings):
            return name
    return None


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    PROC.mkdir(parents=True, exist_ok=True)
    params = base_params()

    step("1. Download from the Water Quality Portal")
    st_legacy = fetch(LEGACY_STATION, params, "legacy sites")
    res_legacy = fetch(LEGACY_RESULT, params, "legacy results")
    res_wqx3 = fetch(WQX3_RESULT, params + [("dataProfile", "basicPhysChem")], "WQX3 results (incl. newer USGS)")

    if res_legacy is None and res_wqx3 is None:
        sys.exit("\nNo results from either service. Check Wi-Fi and try again in a few minutes "
                 "(the portal is sometimes slow on weekends).")

    step("2. Standardize columns")
    results = []
    if res_legacy is not None:
        results.append(standardize(res_legacy, RESULT_FIELDS, "legacy results").assign(service="legacy"))
    if res_wqx3 is not None:
        results.append(standardize(res_wqx3, RESULT_FIELDS, "WQX3 results").assign(service="WQX3"))
    res = pd.concat(results, ignore_index=True)

    step("2b. Diagnostics (raw rows per measurement and service)")
    diag = res.assign(has_value=pd.to_numeric(res["value"], errors="coerce").notna())
    for (svc, ch), g in diag.groupby(["service", "characteristic"]):
        print(f"  {svc:<7} {str(ch)[:42]:<42} rows={len(g):>5}  numeric values={int(g['has_value'].sum()):>5}")
    ec_units = diag.loc[diag["characteristic"] == "Escherichia coli", "unit"].fillna("").replace("", "(blank)").value_counts().head(6)
    if len(ec_units):
        print("  E. coli units seen: " + ", ".join(f"{u} ({n})" for u, n in ec_units.items()))
    else:
        print("  No E. coli rows were returned by either service.")

    # Site coordinates: legacy station file, plus any coordinates carried on result rows
    site_parts = []
    if st_legacy is not None:
        site_parts.append(standardize(st_legacy, SITE_FIELDS, "legacy sites"))
    site_parts.append(res[["site_id", "site_name", "lat", "lon", "org"]])
    sites = pd.concat(site_parts, ignore_index=True)
    sites["lat"] = pd.to_numeric(sites["lat"], errors="coerce")
    sites["lon"] = pd.to_numeric(sites["lon"], errors="coerce")
    sites = sites.dropna(subset=["site_id", "lat", "lon"]).drop_duplicates("site_id")

    step("3. Clean results")
    res["label"] = res["characteristic"].map(CHARACTERISTICS)
    res = res.dropna(subset=["label"])
    res["value"] = pd.to_numeric(res["value"], errors="coerce")
    res = res.dropna(subset=["site_id", "value"])
    res["date"] = pd.to_datetime(res["date"], errors="coerce")
    res["unit"] = res["unit"].fillna("").astype(str).str.strip()
    # E. coli: keep per-100 mL counts (CFU or MPN); blank units are kept (WQP E. coli is reported per 100 mL)
    is_ecoli = res["label"] == "E. coli"
    u = res["unit"].str.lower()
    ok_unit = u.str.contains("100|cfu|mpn", regex=True, na=False) | (u == "") | (u == "nan")
    dropped_units = int((is_ecoli & ~ok_unit).sum())
    res = res[~is_ecoli | ok_unit]
    if dropped_units:
        print(f"E. coli rows dropped for non-comparable units: {dropped_units}")
    before = len(res)
    res = res.drop_duplicates(["site_id", "label", "date", "value"])
    print(f"Results kept: {len(res):,} (removed {before - len(res):,} duplicates across services)")

    step("4. Assign each site to a county")
    shapes = load_county_shapes()
    sites["county"] = [county_for(lo, la, shapes) for lo, la in zip(sites["lon"], sites["lat"])]
    outside = sites["county"].isna().sum()
    sites = sites.dropna(subset=["county"])
    sites = sites[sites["site_id"].isin(res["site_id"])]
    res = res[res["site_id"].isin(sites["site_id"])]
    print(f"Sites inside the 13 counties: {len(sites):,} (dropped {outside} with coordinates outside)")

    sites[["site_id", "site_name", "org", "lat", "lon", "county"]].to_csv(PROC / "wq_sites.csv", index=False)
    res[["site_id", "label", "characteristic", "value", "unit", "date", "org"]].to_csv(
        PROC / "wq_results.csv", index=False)

    step("RESULT")
    print(f"Sampling sites: {len(sites):,}   Sample results: {len(res):,}")
    if res["date"].notna().any():
        print(f"Date range: {res['date'].min():%Y-%m-%d} to {res['date'].max():%Y-%m-%d}")
    print("\nResults per measurement:")
    for lab, n in res["label"].value_counts().items():
        print(f"  {lab:<24} {n:>6,}")
    print("\nSites per county:")
    counts = sites["county"].value_counts()
    for c in COUNTIES:
        print(f"  {c:<10} {counts.get(c, 0):>5}")
    print(f"\nSaved: {PROC / 'wq_sites.csv'}")
    print(f"Saved: {PROC / 'wq_results.csv'}")
    print("\nStep 3 data complete.")


if __name__ == "__main__":
    main()
