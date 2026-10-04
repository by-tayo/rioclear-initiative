"""
Rio Grande Water Surveillance - Step 2: interactive dashboard
-------------------------------------------------------------
Layout:  sidebar (filters)  |  map (left)  +  key findings (right)  |  system table (bottom)

Data:
  - data/processed/tx_border_water_systems.csv   (made by setup_data.py, EPA ECHO SDWA)
  - County shapes: plotly's public US-counties GeoJSON (downloaded once, cached in data/raw/)

Run:  streamlit run app.py
"""

import json
import re
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st
import folium
import branca.colormap as cm
from streamlit_folium import st_folium

# ----------------------------------------------------------------------------
# Paths and constants
# ----------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent
DATA_CSV = BASE / "data" / "processed" / "tx_border_water_systems.csv"
GEO_CACHE = BASE / "data" / "raw" / "us_counties.geojson"
GEO_URL = "https://raw.githubusercontent.com/plotly/datasets/master/geojson-counties-fips.json"
WQ_SITES = BASE / "data" / "processed" / "wq_sites.csv"
WQ_RESULTS = BASE / "data" / "processed" / "wq_results.csv"

# E. coli reference values (CFU or MPN per 100 mL) used for SCREENING only:
#  126 = EPA / Texas geometric-mean criterion for primary contact recreation in freshwater
#  399 = Texas single-sample criterion for primary contact recreation
ECOLI_GEOMEAN_REF = 126
ECOLI_SINGLE_REF = 399

# Texas counties along the Rio Grande (upstream -> Gulf) with their 5-digit FIPS codes
COUNTIES = {
    "El Paso": "48141", "Hudspeth": "48229", "Presidio": "48377", "Brewster": "48043",
    "Terrell": "48443", "Val Verde": "48465", "Kinney": "48271", "Maverick": "48323",
    "Webb": "48479", "Zapata": "48505", "Starr": "48427", "Hidalgo": "48215",
    "Cameron": "48061",
}

# Palette: geographic blue / green / yellow
BLUE, GREEN, YELLOW = "#1f5f99", "#3a9d5d", "#e8c547"
SCALE = ["#fdf6c3", YELLOW, "#8cc37a", GREEN, "#2b8cbe", BLUE]

# One color per county, alternating blue / green / yellow so neighbors never match
COUNTY_COLORS = {
    "El Paso": "#1f5f99", "Hudspeth": "#3a9d5d", "Presidio": "#e8c547",
    "Brewster": "#5aa9e6", "Terrell": "#7fbf3f", "Val Verde": "#f2a93b",
    "Kinney": "#2b8cbe", "Maverick": "#1b7a4b", "Webb": "#d4b106",
    "Zapata": "#0f4c81", "Starr": "#9bd18b", "Hidalgo": "#f6d55c",
    "Cameron": "#4f9da6",
}

st.set_page_config(page_title="Rio Grande Water Surveillance", page_icon="💧", layout="wide")

st.markdown(
    f"""
    <style>
      .block-container {{ padding-top: 1.5rem; }}
      h1 {{ color: {BLUE}; }}
      .finding {{ border-left: 5px solid {GREEN}; background: rgba(58,157,93,0.08);
                  padding: 0.6rem 0.9rem; margin-bottom: 0.6rem; border-radius: 4px; }}
      .finding.alert {{ border-left-color: {YELLOW}; background: rgba(232,197,71,0.15); }}
    </style>
    """,
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------------------
# Column detection (ECHO column names are discovered, not assumed)
# ----------------------------------------------------------------------------
def norm(s):
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def detect(cols, prefer, contains):
    normed = {c: norm(c) for c in cols}
    for p in prefer:
        for c, n in normed.items():
            if n == norm(p):
                return c
    for c, n in normed.items():
        if norm(contains) in n:
            return c
    return None


def as_flag(series):
    """Turn a Y/N or numeric column into True/False."""
    nums = pd.to_numeric(series, errors="coerce")
    if nums.notna().mean() > 0.5:
        return nums.fillna(0) > 0
    return series.fillna("").astype(str).str.strip().str.upper().isin({"Y", "YES", "TRUE", "T", "1"})


@st.cache_data
def load_systems():
    if not DATA_CSV.exists():
        return None, {}
    df = pd.read_csv(DATA_CSV, dtype=str)
    cols = list(df.columns)
    found = {
        "id": detect(cols, ["PWSId", "PWS_ID", "PWSID"], "PWSID"),
        "name": detect(cols, ["PWSName", "PWS_NAME"], "NAME"),
        "cities": detect(cols, ["CitiesServed", "CITIES_SERVED"], "CITIES"),
        "population": detect(cols, ["PopulationServedCount", "POPULATION_SERVED_COUNT"], "POPULATION"),
        "type": detect(cols, ["PWSTypeDesc", "PWS_TYPE_DESC", "PWSTypeCode"], "TYPE"),
        "source": detect(cols, ["PrimarySourceDesc", "PRIMARY_SOURCE_DESC"], "SOURCE"),
        "health": detect(cols, ["HealthFlag", "HEALTH_FLAG"], "HEALTH"),
        "serious": detect(cols, ["SeriousViolator", "SERIOUS_VIOLATOR"], "SERIOUS"),
        "qtrs_vio": detect(cols, ["QtrsWithVio", "QTRS_WITH_VIO"], "QTRSWITHVIO"),
    }
    if found["population"]:
        df["_pop"] = pd.to_numeric(df[found["population"]], errors="coerce").fillna(0)
    else:
        df["_pop"] = 0
    df["_health"] = as_flag(df[found["health"]]) if found["health"] else False
    df["_serious"] = as_flag(df[found["serious"]]) if found["serious"] else False
    df["_qtrs"] = pd.to_numeric(df[found["qtrs_vio"]], errors="coerce").fillna(0) if found["qtrs_vio"] else 0
    return df, found


@st.cache_data
def load_geo():
    if not GEO_CACHE.exists():
        GEO_CACHE.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(GEO_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            GEO_CACHE.write_bytes(r.read())
    with open(GEO_CACHE, encoding="utf-8") as f:
        geo = json.load(f)
    wanted = set(COUNTIES.values())
    geo["features"] = [ft for ft in geo["features"] if str(ft.get("id")) in wanted]
    return geo


@st.cache_data
def load_water_quality():
    """Per-site, per-measurement summary. Returns None if Step 3 data isn't there yet."""
    if not (WQ_SITES.exists() and WQ_RESULTS.exists()):
        return None
    sites = pd.read_csv(WQ_SITES, dtype={"site_id": str})
    res = pd.read_csv(WQ_RESULTS, dtype={"site_id": str}, parse_dates=["date"])
    res = res[res["value"] >= 0]
    rows = []
    for (sid, label), g in res.groupby(["site_id", "label"]):
        v = g["value"]
        row = {"site_id": sid, "label": label, "n": len(v), "median": v.median(), "max": v.max(),
               "latest": g["date"].max(), "unit": g["unit"].mode().iat[0] if g["unit"].notna().any() else ""}
        if label == "E. coli":
            pos = v.clip(lower=1)  # geometric mean needs positive values
            row["geomean"] = float(np.exp(np.log(pos).mean()))
            row["pct_over_single"] = round(100 * (v > ECOLI_SINGLE_REF).mean(), 1)
        rows.append(row)
    summ = pd.DataFrame(rows)
    if summ.empty:
        return None
    return summ.merge(sites, on="site_id", how="inner")


def county_stats(df):
    rows = []
    for county in COUNTIES:
        sub = df[df["border_county"] == county]
        n = len(sub)
        health = int(sub["_health"].sum())
        rows.append({
            "county": county,
            "systems": n,
            "population": int(sub["_pop"].sum()),
            "health_systems": health,
            "pct_health": round(100 * health / n, 1) if n else 0.0,
            "serious": int(sub["_serious"].sum()),
            "pop_health": int(sub.loc[sub["_health"], "_pop"].sum()),
            "qtrs": int(sub["_qtrs"].sum()),
        })
    return pd.DataFrame(rows).set_index("county")


# ----------------------------------------------------------------------------
# Load
# ----------------------------------------------------------------------------
df, found = load_systems()
if df is None:
    st.error("Data file not found. Run `python setup_data.py` first, from this same folder.")
    st.stop()

try:
    geo = load_geo()
except Exception as e:
    st.error(f"Could not download county shapes: {e}\nCheck Wi-Fi, then refresh the page.")
    st.stop()

stats = county_stats(df)
wq = load_water_quality()

# ----------------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------------
st.sidebar.header("Filters")
selected = st.sidebar.multiselect("Counties", list(COUNTIES), default=list(COUNTIES))

metrics = {"Number of water systems": "systems"}
if found["health"]:
    metrics["% of systems with health-based violation flag"] = "pct_health"
    metrics["People served by flagged systems"] = "pop_health"
if found["serious"]:
    metrics["Serious violators"] = "serious"
if found["qtrs_vio"]:
    metrics["Quarters with violations (total)"] = "qtrs"
if found["population"]:
    metrics["Total population served"] = "population"

map_mode = st.sidebar.radio("Map coloring", ["Distinct color per county", "Heat scale by a metric"])
if map_mode == "Heat scale by a metric":
    metric_label = st.sidebar.radio("Color the map by", list(metrics), index=min(1, len(metrics) - 1))
else:
    metric_label = "Number of water systems"
metric = metrics[metric_label]

st.sidebar.markdown("---")
st.sidebar.subheader("Surface water quality")
if wq is None:
    st.sidebar.info("Run `python water_quality.py` to add surface-water sampling sites.")
    show_wq, wq_label = False, None
else:
    show_wq = st.sidebar.checkbox("Show sampling sites", value=True)
    labels = [l for l in ["E. coli", "Nitrate", "Total dissolved solids"] if l in set(wq["label"])]
    wq_label = st.sidebar.selectbox("Measurement", labels)
st.sidebar.markdown("---")

map_height = st.sidebar.slider("Map height (px)", 450, 1000, 650, step=50,
                               help="Make the map taller for full-screen screenshots.")

with st.sidebar.expander("Detected data columns"):
    for k, v in found.items():
        st.write(f"**{k}** → {v if v else '❌ not found'}")

st.sidebar.caption("Source: EPA ECHO Drinking Water System Search dataset (SDWIS, refreshed quarterly).")

# ----------------------------------------------------------------------------
# Header
# ----------------------------------------------------------------------------
st.title("💧 Rio Grande Water Surveillance")
st.caption("Drinking water compliance across the 13 Texas counties along the Rio Grande - El Paso to Cameron.")

view = stats.loc[selected] if selected else stats.iloc[0:0]

k1, k2, k3, k4 = st.columns(4)
k1.metric("Water systems", f"{int(view['systems'].sum()):,}")
k2.metric("Population served", f"{int(view['population'].sum()):,}")
k3.metric("Systems with health-based flag", f"{int(view['health_systems'].sum()):,}" if found["health"] else "n/a")
k4.metric("Serious violators", f"{int(view['serious'].sum()):,}" if found["serious"] else "n/a")

# ----------------------------------------------------------------------------
# Map + findings
# ----------------------------------------------------------------------------
left, right = st.columns([2, 1])

with left:
    values = view[metric] if not view.empty else pd.Series([0])
    vmin, vmax = float(values.min()), float(values.max())
    if vmin == vmax:
        vmax = vmin + 1
    colormap = cm.LinearColormap(SCALE, vmin=vmin, vmax=vmax, caption=metric_label)

    fips_to_county = {v: k for k, v in COUNTIES.items()}
    for ft in geo["features"]:
        name = fips_to_county[str(ft["id"])]
        s = stats.loc[name]
        ft["properties"].update({
            "county": name,
            "value": float(s[metric]),
            "selected": name in selected,
            "t_systems": f"{int(s['systems']):,}",
            "t_pop": f"{int(s['population']):,}",
            "t_serious": f"{int(s['serious'])}" if found["serious"] else "n/a",
        })

    def style(ft):
        p = ft["properties"]
        if not p["selected"]:
            return {"fillColor": "#d9d9d9", "color": "#999999", "weight": 1, "fillOpacity": 0.4}
        if map_mode == "Distinct color per county":
            fill = COUNTY_COLORS[p["county"]]
        else:
            fill = colormap(p["value"])
        return {"fillColor": fill, "color": "#ffffff", "weight": 2, "fillOpacity": 0.8}

    m = folium.Map(
        location=[28.6, -102.0], zoom_start=6, tiles="OpenStreetMap",
        zoom_snap=0.25,    # allow in-between zoom levels (6.25, 6.5, ...)
        zoom_delta=0.25,   # each +/- click moves a quarter step instead of a full level
        wheel_px_per_zoom_level=120,
    )
    folium.GeoJson(
        geo,
        name="Counties",
        style_function=style,
        highlight_function=lambda ft: {"weight": 4, "color": YELLOW},
        tooltip=folium.GeoJsonTooltip(
            fields=["county", "t_systems", "t_pop", "t_serious"],
            aliases=["County", "Water systems", "Population served", "Serious violators"],
            sticky=True,
        ),
    ).add_to(m)
    if map_mode == "Heat scale by a metric":
        colormap.add_to(m)

    # River / surface-water sampling sites
    if show_wq and wq_label:
        sub = wq[(wq["label"] == wq_label) & (wq["county"].isin(selected))].copy()
        # Custom pane above the county-name labels (Leaflet marker pane is z-index 600)
        folium.map.CustomPane("sites", z_index=650).add_to(m)
        layer = folium.FeatureGroup(name=f"Sampling sites: {wq_label}")
        if wq_label == "E. coli":
            def site_color(r):
                if r["geomean"] > ECOLI_SINGLE_REF:
                    return "#d95f02", f"above {ECOLI_SINGLE_REF}"
                if r["geomean"] > ECOLI_GEOMEAN_REF:
                    return YELLOW, f"{ECOLI_GEOMEAN_REF}-{ECOLI_SINGLE_REF}"
                return GREEN, f"at or below {ECOLI_GEOMEAN_REF}"
        else:
            q1, q2 = sub["median"].quantile([1 / 3, 2 / 3]) if len(sub) else (0, 0)
            def site_color(r):
                if r["median"] > q2:
                    return "#d95f02", "highest third of sites"
                if r["median"] > q1:
                    return YELLOW, "middle third of sites"
                return GREEN, "lowest third of sites"

        for _, r in sub.iterrows():
            color, band = site_color(r)
            unit = r["unit"] if isinstance(r["unit"], str) else ""
            lines = [
                f"<b>{r['site_name']}</b>",
                f"{r['county']} County &middot; {r['org']}",
                f"<hr style='margin:4px 0'>{wq_label}: <b>{int(r['n'])}</b> samples, latest {r['latest']:%Y-%m-%d}",
            ]
            if wq_label == "E. coli":
                lines.append(f"Geometric mean: <b>{r['geomean']:,.0f}</b> {unit} ({band})")
                lines.append(f"Samples above {ECOLI_SINGLE_REF}: <b>{r['pct_over_single']}%</b>")
            else:
                lines.append(f"Median: <b>{r['median']:,.2f}</b> {unit} ({band})")
                lines.append(f"Max: {r['max']:,.2f} {unit}")
            folium.CircleMarker(
                location=[r["lat"], r["lon"]], radius=7, weight=1.5, color="#333333", pane="sites",
                fill=True, fill_color=color, fill_opacity=0.9,
                tooltip=f"{r['site_name']} ({band})",
                popup=folium.Popup("<br>".join(lines), max_width=320),
            ).add_to(layer)
        layer.add_to(m)

        legend_rows = (
            [("#d95f02", f"Geo. mean above {ECOLI_SINGLE_REF}"), (YELLOW, f"{ECOLI_GEOMEAN_REF}-{ECOLI_SINGLE_REF}"),
             (GREEN, f"At or below {ECOLI_GEOMEAN_REF}")] if wq_label == "E. coli" else
            [("#d95f02", "Highest third"), (YELLOW, "Middle third"), (GREEN, "Lowest third")]
        )
        legend_html = "".join(
            f"<div><span style='display:inline-block;width:11px;height:11px;border-radius:50%;"
            f"background:{c};border:1px solid #333;margin-right:6px'></span>{t}</div>" for c, t in legend_rows)
        unit_note = "CFU or MPN per 100 mL, since 2021" if wq_label == "E. coli" else "site medians, since 2021"
        m.get_root().html.add_child(folium.Element(
            "<div style='position:absolute;bottom:28px;left:12px;z-index:9999;background:white;"
            "padding:8px 10px;border-radius:6px;font:12px sans-serif;box-shadow:0 1px 4px rgba(0,0,0,.3)'>"
            f"<b>{wq_label}</b> <span style='color:#666'>({unit_note})</span>{legend_html}</div>"))

    # Auto-zoom so every selected county fits on screen
    pts = []
    for ft in geo["features"]:
        if ft["properties"]["county"] not in selected:
            continue
        g = ft["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        pts += [pt for poly in polys for pt in poly[0]]
    if pts:
        lons = [p[0] for p in pts]; lats = [p[1] for p in pts]
        m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]], padding=(20, 20))

    # County name labels at the true center (area centroid) of each county shape
    def ring_centroid(ring):
        """Area-weighted centroid of one polygon ring (shoelace formula)."""
        a = cx = cy = 0.0
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            cross = x1 * y2 - x2 * y1
            a += cross
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
        if a == 0:  # degenerate shape: fall back to simple average
            return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring), 0.0
        a /= 2
        return cx / (6 * a), cy / (6 * a), abs(a)

    def center(ft):
        geom = ft["geometry"]
        polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        # Use the largest piece (ignores small islands), outer ring only
        best = max((ring_centroid([tuple(pt[:2]) for pt in poly[0]]) for poly in polys), key=lambda c: c[2])
        lon, lat, _ = best
        return lat, lon

    for ft in geo["features"]:
        name = ft["properties"]["county"]
        if name not in selected:
            continue
        folium.Marker(
            location=center(ft),
            icon=folium.DivIcon(
                icon_size=(0, 0),       # zero-size anchor box sits exactly on the center point
                icon_anchor=(0, 0),
                html=(
                    "<div style='font: 600 12px sans-serif; color:#1a1a1a; white-space:nowrap;"
                    "text-align:center; transform:translate(-50%,-50%); display:inline-block;"
                    "text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 3px #fff;'>"
                    f"{name}</div>"
                ),
            ),
        ).add_to(m)

    out = st_folium(m, height=map_height, use_container_width=True, returned_objects=["last_active_drawing"])
    clicked = None
    if out and out.get("last_active_drawing"):
        clicked = out["last_active_drawing"].get("properties", {}).get("county")

with right:
    st.subheader("Key findings")
    if view.empty:
        st.info("Select at least one county.")
    else:
        top_sys = view["systems"].idxmax()
        st.markdown(
            f"<div class='finding'><b>{top_sys}</b> has the most public water systems "
            f"in this selection ({int(view.loc[top_sys, 'systems'])}).</div>",
            unsafe_allow_html=True,
        )
        if found["health"] and view["health_systems"].sum() > 0:
            worst = view["pct_health"].idxmax()
            st.markdown(
                f"<div class='finding alert'><b>{worst}</b> has the highest share of systems carrying a "
                f"health-based violation flag: <b>{view.loc[worst, 'pct_health']}%</b> "
                f"({int(view.loc[worst, 'health_systems'])} of {int(view.loc[worst, 'systems'])}).</div>",
                unsafe_allow_html=True,
            )
            st.markdown(
                f"<div class='finding alert'>About <b>{int(view['pop_health'].sum()):,}</b> people are served "
                f"by systems with a health-based violation flag.</div>",
                unsafe_allow_html=True,
            )
        if found["serious"] and view["serious"].sum() > 0:
            st.markdown(
                f"<div class='finding alert'><b>{int(view['serious'].sum())}</b> systems are listed as "
                f"serious violators.</div>",
                unsafe_allow_html=True,
            )
        if wq is not None:
            ec = wq[(wq["label"] == "E. coli") & (wq["county"].isin(selected))]
            if len(ec):
                over = int((ec["geomean"] > ECOLI_GEOMEAN_REF).sum())
                st.markdown(
                    f"<div class='finding alert'><b>{over} of {len(ec)}</b> surface-water sampling sites have an E. coli "
                    f"geometric mean above <b>{ECOLI_GEOMEAN_REF}</b> per 100 mL since 2021, the level used "
                    f"for safe swimming and wading.</div>",
                    unsafe_allow_html=True,
                )
                worst_c = ec.groupby("county")["geomean"].median().idxmax()
                st.markdown(
                    f"<div class='finding'>Highest typical E. coli levels: <b>{worst_c}</b> County "
                    f"(median of site geometric means).</div>",
                    unsafe_allow_html=True,
                )
        st.caption("Drinking-water flags: EPA ECHO. Surface-water data (Rio Grande, tributaries and drains): "
                   "Water Quality Portal. Surface-water comparisons are a screening view, not an official assessment.")

# ----------------------------------------------------------------------------
# Where to act first: county priority ranking
# ----------------------------------------------------------------------------
st.markdown("---")
st.subheader("Where to act first")
st.caption(
    "Each county gets a 0-100 score: the average of up to three signals, each scaled from the lowest county (0) "
    "to the highest (1). Drinking water: share of systems with a health-based violation flag, and serious "
    "violators per 10 systems. Surface water: share of E. coli sites with a geometric mean above "
    f"{ECOLI_GEOMEAN_REF} per 100 mL. Counties without E. coli monitoring are scored on drinking water only."
)

pr = stats.loc[selected].copy() if selected else stats.iloc[0:0].copy()
if not pr.empty:
    pr["serious_per10"] = (10 * pr["serious"] / pr["systems"].where(pr["systems"] > 0)).fillna(0)
    if wq is not None:
        ec_all = wq[wq["label"] == "E. coli"]
        g = ec_all.groupby("county")
        pr["ecoli_sites"] = g.size().reindex(pr.index).fillna(0).astype(int)
        pr["ecoli_over"] = g.apply(lambda x: int((x["geomean"] > ECOLI_GEOMEAN_REF).sum())).reindex(pr.index).fillna(0).astype(int)
        pr["ecoli_worst"] = g["geomean"].max().reindex(pr.index)
        pr["pct_ecoli_over"] = (100 * pr["ecoli_over"] / pr["ecoli_sites"].where(pr["ecoli_sites"] > 0))
    else:
        pr["ecoli_sites"], pr["ecoli_over"], pr["ecoli_worst"], pr["pct_ecoli_over"] = 0, 0, np.nan, np.nan

    def scaled(col):
        v = pr[col].astype(float)
        lo, hi = v.min(skipna=True), v.max(skipna=True)
        if pd.isna(lo) or hi == lo:
            return v * 0 if not pd.isna(lo) else v
        return (v - lo) / (hi - lo)

    comps = pd.DataFrame({
        "Health-flagged systems": scaled("pct_health") if found["health"] else np.nan,
        "Serious violators": scaled("serious_per10") if found["serious"] else np.nan,
        "E. coli above 126": scaled("pct_ecoli_over"),
    }, index=pr.index)
    pr["score"] = (100 * comps.mean(axis=1, skipna=True)).round(0).fillna(0)
    pr["main_driver"] = comps.idxmax(axis=1, skipna=True).where(comps.max(axis=1) > 0, "No standout signal")
    pr = pr.sort_values("score", ascending=False)

    # Top 3 cards
    cards = st.columns(3)
    for col, (county, r) in zip(cards, pr.head(3).iterrows()):
        actions = []
        if found["health"] and r["health_systems"] > 0:
            actions.append(f"Follow up on the <b>{int(r['health_systems'])}</b> drinking-water system(s) with "
                           f"health-based violation flags, serving about <b>{int(r['pop_health']):,}</b> people.")
        if found["serious"] and r["serious"] > 0:
            actions.append(f"Prioritize compliance support for <b>{int(r['serious'])}</b> serious violator(s).")
        if r["ecoli_sites"] > 0 and r["ecoli_over"] > 0:
            actions.append(f"Trace bacteria sources at <b>{int(r['ecoli_over'])} of {int(r['ecoli_sites'])}</b> "
                           f"surface-water sites above {ECOLI_GEOMEAN_REF} (worst geometric mean "
                           f"<b>{r['ecoli_worst']:,.0f}</b>).")
        if r["ecoli_sites"] == 0:
            actions.append("No E. coli monitoring found here since 2021: a monitoring gap worth closing.")
        if not actions:
            actions.append("No drinking-water or E. coli warning signals in this data.")
        col.markdown(
            f"<div class='finding alert'><div style='font-size:1.15rem'><b>{county}</b> &middot; "
            f"score <b>{int(r['score'])}</b></div>"
            f"<div style='color:#888;font-size:0.85rem;margin-bottom:6px'>Main driver: {r['main_driver']}</div>"
            + "".join(f"<div style='margin-top:4px'>&bull; {a}</div>" for a in actions) + "</div>",
            unsafe_allow_html=True,
        )

    # Full ranking table
    table_pr = pd.DataFrame({
        "County": pr.index,
        "Priority score": pr["score"].astype(int).values,
        "Main driver": pr["main_driver"].values,
        "Systems w/ health flag": [f"{int(h)} of {int(n)}" for h, n in zip(pr["health_systems"], pr["systems"])],
        "Serious violators": pr["serious"].astype(int).values,
        "People served by flagged systems": pr["pop_health"].astype(int).values,
        "E. coli sites above 126": [f"{int(o)} of {int(n)}" if n else "no monitoring"
                                    for o, n in zip(pr["ecoli_over"], pr["ecoli_sites"])],
    })
    st.dataframe(
        table_pr, hide_index=True, width="stretch",
        column_config={
            "Priority score": st.column_config.ProgressColumn("Priority score", min_value=0, max_value=100, format="%d"),
            "People served by flagged systems": st.column_config.NumberColumn(format="%d"),
        },
    )

# ----------------------------------------------------------------------------
# System table
# ----------------------------------------------------------------------------
st.subheader("Water systems")
options = ["All selected counties"] + selected
default_idx = options.index(clicked) if clicked in options else 0
choice = st.selectbox("Show systems in", options, index=default_idx,
                      help="Tip: click a county on the map to jump here.")

table = df[df["border_county"].isin(selected)] if choice == options[0] else df[df["border_county"] == choice]
show_cols = [c for c in [found["name"], found["id"], "border_county", found["cities"], found["population"],
                         found["type"], found["source"], found["health"], found["serious"], found["qtrs_vio"]] if c]
table = table.sort_values(["_health", "_qtrs", "_pop"], ascending=False)
st.dataframe(table[show_cols], width="stretch", hide_index=True)
st.caption(f"{len(table):,} systems shown, sorted by health-based flag, then violation quarters, then population.")