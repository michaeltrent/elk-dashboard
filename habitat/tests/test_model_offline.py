"""Offline end-to-end test of the model on a synthetic 9 x 9 km region.

Builds a fake DEM (hills), LANDFIRE bands (timber, aspen, meadow, sage),
roads, water, a 2020 burn, public land, two units, and CPW-style validation
polygons; then runs features -> score -> evaluate -> train and checks the
results make sense. No network needed.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, box

import config

tmp = Path(tempfile.mkdtemp())
config.OUT_DIR = tmp / "habitat"
config.VALIDATION_DIR = tmp / "val"
config.REGIONS = {"test": [31, 32]}
CRS = config.WORK_CRS
X0, Y0, N = 700000.0, 4400000.0, 900        # 900 x 10 m = 9 km
rdir = config.OUT_DIR / "test"
(rdir / "landfire").mkdir(parents=True)
config.VALIDATION_DIR.mkdir(parents=True)
(config.OUT_DIR / "_cache").mkdir(parents=True)

# --- units: west half = 31, east half = 32
units = gpd.GeoDataFrame({"GMUID": [31, 32]},
                         geometry=[box(X0, Y0 - 9000, X0 + 4500, Y0), box(X0 + 4500, Y0 - 9000, X0 + 9000, Y0)], crs=CRS)
units.to_file(config.OUT_DIR / "gmu_boundaries.gpkg", layer="gmu")

# --- DEM: east-west ridge through the middle (south face in the south half)
yy, xx = np.mgrid[0:N, 0:N].astype("float32")
z = 2600 + 250 * np.exp(-((yy - 450) / 180) ** 2) + 40 * np.sin(xx / 60)
z[:20, :20] = -9999
with rasterio.open(rdir / "dem_10m.tif", "w", driver="GTiff", width=N, height=N, count=1, dtype="float32",
                   crs=CRS, transform=from_origin(X0, Y0, 10, 10), nodata=-9999) as f:
    f.write(z, 1)

# --- LANDFIRE (30 m, 3 bands: EVT, EVC, CC)
M = N // 3
evt = np.full((M, M), 7140, "int32")                   # meadow
evt[:, : M // 2] = 7050                                # lodgepole west
evt[M // 2:, M // 2 - 20: M // 2 + 20] = 7011          # aspen strip south
evt[: M // 4, M // 2:] = 7125                          # sage NE
evc = np.where(np.isin(evt, [7050, 7011]), 160, 330).astype("int32")
evc[-10:, -10:] = 11                                    # a pond (non-habitat)
cc = np.where(np.isin(evt, [7050, 7011]), 65, 0).astype("int32")
with rasterio.open(rdir / "landfire" / "lf.tif", "w", driver="GTiff", width=M, height=M, count=3, dtype="int32",
                   crs=CRS, transform=from_origin(X0, Y0, 30, 30), nodata=-9999) as f:
    for i, a in enumerate((evt, evc, cc), 1):
        f.write(a, i)
pd.DataFrame({"VALUE": [7140, 7050, 7011, 7125],
              "EVT_NAME": ["Northern Rocky Mountain Subalpine-Upper Montane Grassland",
                           "Rocky Mountain Lodgepole Pine Forest",
                           "Rocky Mountain Aspen Forest and Woodland",
                           "Inter-Mountain Basins Big Sagebrush Steppe"],
              "EVT_PHYS": ["Grassland", "Conifer", "Hardwood", "Shrubland"],
              "EVT_LF": ["Herb", "Tree", "Tree", "Shrub"]}).to_csv(
    config.OUT_DIR / "_cache" / config.LANDFIRE_EVT_CSV.rsplit("/", 1)[-1], index=False)

# --- vectors
gp = rdir / "features.gpkg"
road_y = Y0 - 1500                                       # open road across the north
gpd.GeoDataFrame({"source": ["BLM", "TIGER", "BLM"], "motorized": [True, True, False],
                  "open_hunt": ["open", "unknown", "closed"]},
                 geometry=[LineString([(X0, road_y), (X0 + 9000, road_y)]),
                           LineString([(X0 + 7000, Y0 - 3000), (X0 + 7000, Y0 - 9000)]),
                           LineString([(X0 + 2000, Y0 - 9000), (X0 + 2000, Y0 - 5000)])],
                 crs=CRS).to_file(gp, layer="roads_trails")
gpd.GeoDataFrame({"permanence": ["perennial", "intermittent"]},
                 geometry=[LineString([(X0, Y0 - 8000), (X0 + 9000, Y0 - 7800)]),
                           LineString([(X0 + 4500, Y0 - 4600), (X0 + 4500, Y0 - 7800)])],
                 crs=CRS).to_file(gp, layer="streams_nhd")
gpd.GeoDataFrame({"fcode": [39004]}, geometry=[box(X0 + 8700, Y0 - 9000, X0 + 9000, Y0 - 8700)],
                 crs=CRS).to_file(gp, layer="waterbodies_nhd")
gpd.GeoDataFrame({"source": ["NHD"]}, geometry=[Point(X0 + 3000, Y0 - 6000)], crs=CRS).to_file(gp, layer="springs_nhd")
gpd.GeoDataFrame({"kind": ["spring"]}, geometry=[Point(X0 + 6000, Y0 - 6500)], crs=CRS).to_file(gp, layer="water_rights_cdss")
gpd.GeoDataFrame({"MngNm_Desc": ["BLM"]}, geometry=[box(X0, Y0 - 9000, X0 + 6000, Y0)], crs=CRS).to_file(gp, layer="public_land")
gpd.GeoDataFrame({"fire_name": ["TEST FIRE", "OLD FIRE"], "year": [2020, 1990]},
                 geometry=[box(X0 + 5000, Y0 - 7000, X0 + 6500, Y0 - 5500), box(X0 + 5200, Y0 - 6800, X0 + 6000, Y0 - 6000)],
                 crs=CRS).to_file(gp, layer="fire_perimeters")

# --- CPW-style validation: winter concentration on the south face, forest/meadow edge,
# far from the open road; summer concentration up by the road on the north side.
vp = config.VALIDATION_DIR / "test_cpw_elk.gpkg"
gpd.GeoDataFrame(geometry=[box(X0 + 3500, Y0 - 7200, X0 + 5500, Y0 - 5200)], crs=CRS).to_file(vp, layer="elk_winter_concentration")
gpd.GeoDataFrame(geometry=[box(X0 + 6000, Y0 - 2000, X0 + 8500, Y0 - 1000)], crs=CRS).to_file(vp, layer="elk_summer_concentration")

# ------------------------------------------------------------------ run
import model_evaluate, model_features, model_score, model_train  # noqa: E402
from model_features import load  # noqa: E402

model_features.main()
veg = load("test", "veg_class")
import model_config as mc  # noqa: E402
classes = {mc.VEG_CLASSES[i] for i in np.unique(veg) if i != 255}
assert {"conifer", "aspen", "grass", "sage", "nonhab"} <= classes, classes
north = load("test", "northness")
assert np.nanmean(north[: M // 2 - 30]) > 0.5 and np.nanmean(north[M // 2 + 30:]) < -0.5, "aspect sign"
yrs = load("test", "years_since_fire")
assert np.nanmin(yrs) == 6 and np.isfinite(yrs).any(), "latest fire should win (2026-2020=6)"
d = load("test", "dist_road_open")
r_road = int((Y0 - road_y) / 30)
assert d[r_road, 100] < 31 and d[r_road + 50, 100] > 1400
print("features OK:", sorted(classes))

model_score.main(("late", "early"))
with rasterio.open(rdir / "model" / "suitability_late.tif") as f:
    late = f.read(1)
assert np.nanmax(late) <= 100 and np.nanmin(late) >= 0
assert late[-5, -5] == 0, "pond should be non-habitat"
with rasterio.open(rdir / "model" / "suitability_late_habitat.tif") as f:
    hab = f.read(1)
with rasterio.open(rdir / "model" / "components_late.tif") as f:
    names = list(f.descriptions)
    elev_c = f.read(names.index("elevation") + 1)
assert "elevation" in names and "security" in names
# habitat map ignores roads: the road band should not be a dip in the habitat map
assert np.nanmean(hab[r_road - 3: r_road + 3]) > np.nanmean(late[r_road - 3: r_road + 3])
# late season: ridge top (highest) scores lower on elevation than the lower flank
assert np.nanmean(elev_c[M // 2 - 3: M // 2 + 3]) < np.nanmean(elev_c[M // 2 + 60: M // 2 + 90])
near_road, far = np.nanmean(late[r_road - 3: r_road + 3]), np.nanmean(late[r_road + 60: r_road + 90])
assert near_road < far, (near_road, far)
assert (rdir / "model" / "suitability_late.png").exists()
print(f"score OK: near open road {near_road:.0f} vs 2 km away {far:.0f}")

model_evaluate.main(("late",))
ev = pd.read_csv(config.OUT_DIR / "model_evaluation_late.csv")
assert set(ev["map"]) == {"late", "late_habitat"}, set(ev["map"])
assert (rdir / "model" / "suitability_late_habitat.png").exists()
hunt = ev[(ev.unit == "all") & (ev["map"] == "late")]
wc = hunt[hunt.layer == "winter_concentration"].iloc[0]
sc = hunt[hunt.layer == "summer_concentration"].iloc[0]
assert wc.auc > 0.6 and wc.auc > sc.auc, (wc.auc, sc.auc)
print(f"evaluate OK: winter conc AUC {wc.auc:.2f} (lift {wc.lift_top10:.1f}) vs summer conc AUC {sc.auc:.2f}")

# --- training: fake observations clustered in the south-face aspen/meadow edge, both units
rng = np.random.default_rng(0)
pts = [(X0 + rng.uniform(3600, 5400), Y0 - rng.uniform(5300, 7100)) for _ in range(50)]
pts += [(X0 + rng.uniform(4600, 5400), Y0 - rng.uniform(5300, 7100)) for _ in range(30)]
pts += [(X0 + rng.uniform(4000, 4500), Y0 - rng.uniform(5300, 7100)) for _ in range(30)]
g = gpd.GeoSeries([Point(p) for p in pts], crs=CRS).to_crs(4326)
pd.DataFrame({"lat": g.y, "lon": g.x, "date": "2025-11-01"}).to_csv(config.OUT_DIR / "observations.csv", index=False)
model_train.main(["--from", "10-20", "--to", "11-20"])
with rasterio.open(rdir / "model" / "suitability_trained.tif") as f:
    tr = f.read(1)
assert np.nanmax(tr) <= 100
model_evaluate.main(("trained",))
ev = pd.read_csv(config.OUT_DIR / "model_evaluation_trained.csv")
wct = ev[(ev.unit == "all") & (ev.layer == "winter_concentration") & (ev["map"] == "trained")].iloc[0]
assert wct.auc > 0.8, wct.auc
print(f"train OK: trained map vs winter conc AUC {wct.auc:.2f}")
# --- dashboard layer export
import json  # noqa: E402
import export_layers  # noqa: E402
export_layers.OUT = tmp / "site" / "habitat_layers"
export_layers.main([])
meta = json.loads((export_layers.OUT / "meta.json").read_text())
ids = {l["id"] for l in meta["layers"]}
assert {"late", "late_habitat", "vegetation", "canopy", "forage", "cover", "slope", "aspect",
        "burn", "security", "roads", "water", "springs", "fires"} <= ids, ids
assert all((export_layers.OUT / "vectors" / f"{v}.geojson").exists() for v in ("roads", "water", "springs", "fires"))
print(f"export OK: {len(ids)} dashboard layers")
print("ALL MODEL TESTS PASSED")
