# Colorado Elk Harvest & Population Dashboard

Interactive web dashboard visualizing Colorado Parks & Wildlife elk harvest statistics and population estimates across all Game Management Units (GMUs) from 2006–2024.

**Live site:** [yourdomain.com](https://yourdomain.com)

![Dashboard Screenshot](docs/screenshot.png)

## Features

- **Choropleth map** of all 185 Colorado GMUs colored by harvest metric
- **Season/method filtering** — All Manners, Archery, Muzzleloader, Rifle (1st–4th)
- **Year slider** with 1-year, 3-year, and 5-year averaging modes
- **GMU detail panel** — click any unit to see harvest breakdown and trends
- **DAU population chart** — population estimates over time for the selected herd unit
- **Stacked harvest trends** — bulls/cows/calves by year per GMU
- **Basemap toggle** — Dark, Light, or Satellite imagery
- **GMU labels** on map at all zoom levels

## Project Structure

```
cpw_mapping_project/
│
├── README.md
├── .gitignore
│
├── parsers/                          # Data pipeline scripts
│   ├── parse_elk_harvest.py          # Harvest report PDF parser
│   ├── parse_elk_population.py       # Population estimate PDF parser
│   ├── combine_elk_data.py           # Merges all data into dashboard JSON
│   └── extract_elk_pdfs.py           # Utility: extract PDFs from CPW ZIPs
│
├── dashboard/                        # Web app (what gets deployed)
│   ├── index.html                    # Single-file app (Leaflet + Chart.js)
│   └── data/
│       ├── elk_dashboard.json        # Combined harvest + population data
│       └── gmu_boundaries_slim.geojson  # Simplified GMU boundaries (283 KB)
│
├── harvest_reports/                  # Source PDFs (not tracked in git)
│   ├── 2006StatewideElkHarvest.pdf
│   ├── ...
│   └── 2024StatewideElkHarvest.pdf
│
├── population_estimates/             # Source PDFs (not tracked in git)
│   ├── 2005ElkPopulationEstimate.pdf
│   ├── ...
│   └── 2024 Post-Hunt Elk Population Estimates w Ratios.pdf
│
├── mapping_files/                    # GIS source files (not tracked in git)
│   └── Game_Management_Units__GMUs___CPW.geojson
│
└── output/                           # Parser output (not tracked in git)
    ├── harvest/
    ├── population/
    └── combined/
```

## Data Pipeline

The pipeline converts CPW PDF reports into a single optimized JSON file for the web app.

### Prerequisites

```bash
pip install pdfplumber
```

### Step 1 — Parse harvest reports

```powershell
python parsers/parse_elk_harvest.py (Get-ChildItem harvest_reports\*.pdf).FullName -o output/harvest
```

Produces `elk_harvest_YYYY.json` for each year. Handles three format eras:
- 2006–2015: No TOC, no DAU summary
- 2016–2020: TOC with dots, DAU summary
- 2021–2024: Reformatted TOC, zero-padded DAU IDs, commas in numbers

### Step 2 — Parse population estimates

```powershell
python parsers/parse_elk_population.py (Get-ChildItem population_estimates\*.pdf).FullName -o output/population
```

Produces `elk_population_YYYY.json` for each year. Handles four format eras:
- 2003–2012: DAU + GMUs + estimate only
- 2013–2018: Same structure
- 2019–2023: Added bull/cow ratio column
- 2024+: Added herd name column

### Step 3 — Combine into dashboard JSON

```powershell
python parsers/combine_elk_data.py --harvest-dir output/harvest --population-dir output/population -o output/combined
```

Produces three files:
| File | Purpose | Size (19yr) |
|------|---------|-------------|
| `elk_combined.json` | Full dataset, all methods/seasons/license types | ~24 MB |
| `elk_combined_slim.json` | All-manners only, lightweight | ~600 KB |
| `elk_dashboard.json` | Method/season breakdown, compact field names | ~2 MB |

### Step 4 — Update dashboard

```powershell
copy output\combined\elk_dashboard.json dashboard\data\
```

### Full rebuild (all steps)

```powershell
python parsers/parse_elk_harvest.py (Get-ChildItem harvest_reports\*.pdf).FullName -o output/harvest
python parsers/parse_elk_population.py (Get-ChildItem population_estimates\*.pdf).FullName -o output/population
python parsers/combine_elk_data.py --harvest-dir output/harvest --population-dir output/population -o output/combined
copy output\combined\elk_dashboard.json dashboard\data\
```

## Local Development

```powershell
cd dashboard
python -m http.server 8000
# Open http://localhost:8000
```

## Deployment (GitHub Pages)

1. Push this repo to GitHub
2. Go to **Settings → Pages → Source**: Deploy from branch `main`, folder `/dashboard`
3. Under **Custom domain**, enter your domain
4. At your registrar, add a CNAME record: `yourdomain.com → yourusername.github.io`

The site is entirely static — no server, no build step, no dependencies at runtime.

## Data Sources

- **Harvest reports**: [CPW Harvest Statistics](https://cpw.state.co.us/thingstodo/Pages/Statistics.aspx)
- **Population estimates**: CPW Terrestrial Section post-hunt estimates
- **GMU boundaries**: [CPW GIS Data](https://cpw.state.co.us/learn/Pages/Maps.aspx)

## Technical Notes

- GMU boundaries simplified from 25.7 MB → 283 KB using Douglas-Peucker (tolerance 0.003°, ~330m)
- Dashboard JSON uses single-letter field names (`y`=year, `g`=gmu, `h`=harvest, etc.) to reduce file size
- Choropleth uses quantile breaks (7 classes) recalculated per filter change
- Population estimates are at the DAU level; the GMU→DAU lookup (`ELKDAU` field in GeoJSON) connects them
- DAU definitions occasionally change between years (DAUs added/removed); GMU assignments have been stable
