# Elk habitat data pipeline

Fetches every public input for the elk habitat prediction layer, clipped to
the units being evaluated. The CPW elk activity layers are fetched **for
validation only** and written to a separate folder, so they can't leak into
the model.

## Units

| Region | Units | Herd |
|---|---|---|
| `yellow_creek` | 21, 22, 30, 31, 32 | E-10 (west of Rifle/Meeker) |
| `grand_summit` | 28, 37, 371 | E-13 (Grand/Summit) |

Each region is fetched as one AOI with a 3 km buffer, so distance-to-road and
distance-to-water are correct near unit edges. Change `REGIONS` in
`config.py` to add units.

## Run

```bash
cd habitat
pip install -r requirements.txt
export LFPS_EMAIL=you@example.com      # LANDFIRE requires it
python run_all.py                      # or: python run_all.py roads water
```

Steps run in order and keep going if one service is down; rerun just the
failed step later.

## Sources and outputs

Everything lands in `data/habitat/<region>/` in NAD83 / UTM 13N (EPSG:26913).

| Step | Source | Output | Model use |
|---|---|---|---|
| boundaries | CPW GMU Boundary (Big Game) | `gmu_boundaries.gpkg` | clipping, per-unit stats |
| roads | BLM CO GTLF routes by allowed mode | `features.gpkg: roads_trails` | distance to **open** motorized routes |
| | USFS Motor Vehicle Use Map roads + trails | | seasonal open dates per vehicle class |
| | Census TIGER primary/secondary/local roads | | county roads, highways, energy roads |
| water | USGS NHD High Resolution watershed files (HU4 GeoPackages, cached in `_cache/`): springs/seeps (FCODE 45800) | `springs_nhd` | water distance (low weight -- old topo data) |
| | NHD flowlines, tagged perennial/intermittent/ephemeral | `streams_nhd` | water distance by permanence |
| | NHD waterbodies | `waterbodies_nhd` | |
| | Colorado DWR / CDSS water-right structures | `water_rights_cdss` | decreed springs + stock ponds (higher weight) |
| public_land | PAD-US public access | `public_land` + `unit_public_land.csv` | access mask; dashboard % public feature |
| terrain | USGS 3DEP 1/3 arc-sec (10 m), windowed COG reads | `dem_10m.tif` | slope, aspect, terrain position, ruggedness |
| landfire | LANDFIRE EVT, EVC, canopy cover (30 m) | `landfire/` | forage classes, cover, edge |
| validation | CPW Species Activity Mapping elk layers | `data/habitat_validation/<region>_cpw_elk.gpkg` | **scoring only** |

### Roads: `open_hunt` column

Every road/trail gets `open_hunt` = `open`, `closed`, or `unknown` for the
hunt window in `config.HUNT_WINDOW` (default Oct 24 - Nov 15):

- **BLM**: open if motorized, designated Open, and no seasonal restriction;
  Limited or seasonal routes are `unknown`.
- **USFS MVUM**: open if any vehicle class's `*_DATESOPEN` range overlaps
  the window.
- **TIGER**: highways and county roads are open. **Local roads are
  `unknown`**, because many in the gas fields are gated. The field crew
  should mark these.

`unknown` is where local knowledge beats any dataset; plan to override these
by hand.

## Known caveats

- **NHD springs** are largely digitized from decades-old topo maps; some are
  dry. Weight CDSS springs (with a water right) and perennial streams higher.
- **LANDFIRE layer names** change with each release. If the job fails on a
  layer name, update `LANDFIRE_LAYERS` from <https://lfps.usgs.gov/products>.
- **Memory**: the DEM is built block by block on disk (~20 MB working
  memory), so region size doesn't matter. The output file for
  `yellow_creek` is ~12k x 13k cells.
- **Slow/overloaded servers**: heavy layers are requested in small tiles;
  a timeout splits the tile smaller, and rate-limit replies (PAD-US allows
  60 large requests/min) pause 61 s and retry. Expect the water and
  public-land steps to take a while.
- **PAD-US** overlaps itself (e.g., wilderness inside a national forest);
  public-land % is computed on the dissolved union, so it isn't double-counted.

## Tests

`python tests/test_offline.py` runs the query splitting, road-status rules,
public-land math, and DEM stitching against a mock server and synthetic tiles
-- no network needed.

## Model

```bash
python run_all.py fire          # fire perimeters (MTBS), needed by the model
python run_model.py             # features -> score -> evaluate, late season
python run_model.py --season early
python run_model.py score evaluate   # after editing model_config.py (fast)
```

**features** (slow, rerun only when data changes) builds a 30 m grid aligned
to the 10 m DEM: slope/aspect from 10 m then averaged, elevation, terrain
position (TPI 300 m / 1 km), LANDFIRE vegetation classes, forage within
500 m and cover within 250 m, distance to open and unknown-status roads,
distance to reliable and any water, public land, years since fire, unit ids.

**score** combines eight components (security, forage/cover interspersion,
elevation, slope, aspect, water, burn age, terrain) as a weighted geometric
mean, 0-100, and makes two maps per season in `data/habitat/<region>/model/`:

- `suitability_<season>_habitat.tif` -- everything except road security:
  where elk want to be. This is the map compared with CPW's layers, which
  describe elk without hunters.
- `suitability_<season>.tif` -- habitat + security: where elk will be under
  hunting pressure. Scout from this one.

Plus `components_<season>.tif` (one band per component -- open it to see
*why* a spot scored as it did) and quick-look PNGs of both maps. Forage and
cover values are season-specific (late fall favors browse, sage, pinyon-
juniper). Elevation is scored relative to each region's own range; set
`LATE_SNOW` in `model_config.py` to `storm` or `dry` to match the fall.

**evaluate** compares the map to CPW layers it never saw: AUC (0.5 = chance)
and lift in the top 10% of cells, per region and per unit, written to
`data/habitat/model_evaluation_<season>.csv`. For late season, winter
concentration/severe winter range should beat summer concentration.

**train** (once you have field data): put elk locations in
`data/habitat/observations.csv` (`lat,lon,date,kind,count,observer,notes`;
only lat/lon required). It fits a resource selection function against
random background points, validates on held-out units, prints what the elk
actually selected for, and writes `suitability_trained.tif`:

```bash
python run_model.py train
python run_model.py evaluate --season trained
```

At least 30 observations inside the units are required; more units = a
more honest held-out check.

`python tests/test_model_offline.py` runs the whole model on a synthetic
region (no network).

## Dashboard layers

```bash
python export_layers.py                 # after `run_model.py score`
python export_layers.py --only model    # quick: just re-export the two model maps
```

Writes everything the dashboard needs to `docs/data/habitat_layers/`:

- **Model** (tiles): hunt map and habitat map, as percentile classes within
  each unit group (top 5 / 10 / 20 / 30 / 50%).
- **Background layers** (tiles, one at a time): vegetation type, tree canopy,
  late-season forage and cover, slope, aspect, years since fire, road security.
- **Features** (GeoJSON, loaded when switched on, clickable): roads & trails by
  hunt-season status, streams & ponds, springs & stock ponds, fire perimeters.

The dashboard's **Elk Habitat Model** panel is built from `meta.json`, so only
exported layers appear and nothing shows until the first export. Tiles go to
zoom 12 (~30 m, the model's resolution). The export prints the total size;
if it's too large for your host, set `INPUT_MAX_ZOOM = 11` in
`export_layers.py`. Re-run after re-scoring, then commit
`docs/data/habitat_layers/` and deploy. `habitat/tools/patch_dashboard.py`
adds the panel to `docs/index.html` (already applied; safe to re-run).
