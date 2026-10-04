# RioClear Initiative

**Live dashboard:** https://rioclear-initiative.streamlit.app

RioClear Initiative maps drinking-water compliance and surface-water contamination across the 13 Texas counties along the Rio Grande, and ranks where action should start first.

## The problem

Data on drinking-water compliance and river water quality is split across separate federal and state databases. No single view shows where both problems overlap.

## What it does

- **County map** of the 13 border counties, from El Paso to Cameron
- **Sampling-site dots** for E. coli, nitrate, and total dissolved solids, colored by safety level
- **Key findings** computed live from the data
- **Where to act first:** a transparent 0-100 priority score for each county

## Key findings

- 23 drinking-water systems serving about 86,000 people carry health-based violation flags.
- 24 of 57 surface-water sites have typical E. coli levels above 126 per 100 mL, the EPA/Texas level for safe swimming and wading.
- The Rio Grande below Laredo had a typical E. coli level of 8,172, about 65 times that level.
- Top priority counties: **Starr, Webb, Presidio**.
- Hudspeth and Kinney counties have no E. coli monitoring in the data since 2021.

High river bacteria levels do not mean tap water is unsafe. This is a screening tool, not an official water-quality assessment.

## Data sources

| Source | What we used |
| --- | --- |
| [EPA ECHO](https://echo.epa.gov/tools/data-downloads) | Compliance records for 509 public drinking-water systems |
| [Water Quality Portal](https://www.waterqualitydata.us/) (USGS + EPA) | E. coli, nitrate, and TDS samples from 68 sites, 2021-2026 |
| U.S. counties GeoJSON (Plotly) | County outlines, matched by FIPS code |

## How it works

1. `setup_data.py` downloads EPA ECHO data and keeps the 509 systems in the 13 border counties.
2. `water_quality.py` queries both Water Quality Portal services (older + WQX3.0), fixes unit issues, removes 1,115 duplicate rows, and places each site in its county with a point-in-polygon test.
3. `app.py` is a Streamlit dashboard that reads the cleaned files and computes the map, findings, and priority score.

**Priority score:** the average of three signals, each scaled from the lowest county (0) to the highest (1): share of health-flagged systems, serious violators per 10 systems, and share of E. coli sites above 126.

## Run it locally

```
python -m venv .venv
source .venv/Scripts/activate   
pip install -r requirements.txt
python setup_data.py            
python water_quality.py         
streamlit run app.py
```

The cleaned data is already in `data/processed/`, so `streamlit run app.py` works right away.

## Built with

Python, pandas, Streamlit, Folium, Leaflet

## Team

- Tania I. Ortiz ([@by-tayo](https://github.com/by-tayo))
- Rebecca M. Parra ([@beck0to100](https://github.com/beck0to100))
