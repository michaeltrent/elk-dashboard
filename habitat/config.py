"""Configuration for the elk habitat data pipeline.

Everything the fetchers need lives here: which units, how they group into
regions, where data comes from, and where it goes.
"""
from pathlib import Path

# --- Study area -------------------------------------------------------------
# Units are grouped into regions so we don't download the empty ground
# between two distant groups. Each region is fetched as one buffered AOI.
REGIONS = {
    "yellow_creek": [21, 22, 30, 31, 32],   # DAU E-10, west of Rifle/Meeker
    "grand_summit": [28, 37, 371],          # DAU E-13, Grand/Summit counties
}

# Buffer around the unit boundaries (meters). Elk and roads don't stop at a
# unit line; distance-to-road/water near an edge needs features just outside.
AOI_BUFFER_M = 3000

# Working projection: NAD83 / UTM zone 13N (meters). All outputs use this.
WORK_CRS = "EPSG:26913"

# Hunt window used to decide whether a road is open to motor vehicles
# (MM, DD) inclusive. Default spans 2nd + 3rd rifle 2026.
HUNT_WINDOW = ((10, 24), (11, 15))

# --- Output paths -----------------------------------------------------------
ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT.parent / "data" / "habitat"
# Validation layers are written to a separate folder on purpose, so they can
# never be picked up as model inputs by accident.
VALIDATION_DIR = ROOT.parent / "data" / "habitat_validation"

# Fallback boundary file (simplified) shipped with the dashboard.
SLIM_GMU_GEOJSON = ROOT.parent / "docs" / "data" / "gmu_boundaries_slim.geojson"

# --- Sources ----------------------------------------------------------------
CPW_ADMIN = "https://services5.arcgis.com/ttNGmDvKQA7oeDQ3/ArcGIS/rest/services/CPWAdminData/FeatureServer"
CPW_GMU_LAYER = f"{CPW_ADMIN}/6"            # CPW GMU Boundary (Big Game), field GMUID

BLM_GTLF_ROUTES = "https://gis.blm.gov/coarcgis/rest/services/transportation/BLM_CO_GTLF/FeatureServer/6"
USFS_MVUM = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_MVUM_01/MapServer"
USFS_MVUM_ROADS = f"{USFS_MVUM}/1"
USFS_MVUM_TRAILS = f"{USFS_MVUM}/2"
TIGERWEB_TRANSPORT = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer"

# NHD High Resolution, downloaded as one GeoPackage per 4-digit watershed
# (HU4) from USGS's file bucket. The live NHD map server times out on
# region-sized queries, so files are far more reliable. Zips are cached in
# data/habitat/_cache and reused on later runs.
NHD_HU4_URL = ("https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/NHD/HU4/GPKG/"
               "NHD_H_{hu4}_HU4_GPKG.zip")
NHD_HU4 = {
    "yellow_creek": ["1401", "1403", "1405"],  # Colorado headwaters, Colorado-Dolores, White-Yampa
    "grand_summit": ["1401"],
}

CDSS_STRUCTURES = "https://dwr.state.co.us/Rest/GET/api/v2/structures/"
CDSS_COUNTIES = ["RIO BLANCO", "GARFIELD", "MESA", "GRAND", "SUMMIT", "EAGLE"]

PADUS = "https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/PADUS_Public_Access/FeatureServer/0"

# USGS 3DEP 1/3 arc-second (~10 m) cloud-optimized GeoTIFF tiles.
DEP_TILE_URL = ("https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/"
                "n{lat:02d}w{lon:03d}/USGS_13_n{lat:02d}w{lon:03d}.tif")
DEM_RES_M = 10

# LANDFIRE Product Service. Layer codes change with each LANDFIRE release;
# check https://lfps.usgs.gov/products and update if a job fails.
LFPS_SUBMIT = "https://lfps.usgs.gov/api/job/submit"
LFPS_STATUS = "https://lfps.usgs.gov/api/job/status"
LANDFIRE_LAYERS = ["LF2023_EVT", "LF2023_EVC", "LF2023_CC"]
# LFPS requires an email to identify the requester. Set it in the
# environment rather than here:  export LFPS_EMAIL=you@example.com
LFPS_EMAIL_ENV = "LFPS_EMAIL"

# LANDFIRE EVT class table (names/physiognomy for each EVT code). Must match
# the LANDFIRE_LAYERS version above (LF2023 -> LF23_EVT_240.csv).
LANDFIRE_EVT_CSV = "https://landfire.gov/sites/default/files/CSV/LF2023/LF23_EVT_240.csv"

# Monitoring Trends in Burn Severity -- fire perimeters, all years (1984+)
MTBS_BOUNDARIES = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_MTBS_01/MapServer/63"

# CPW Species Activity Mapping -- VALIDATION ONLY, never a model input.
CPW_SPECIES = "https://services5.arcgis.com/ttNGmDvKQA7oeDQ3/arcgis/rest/services/CPWSpeciesData/FeatureServer"
CPW_ELK_VALIDATION_LAYERS = {
    33: "elk_migration_patterns",
    35: "elk_summer_concentration",
    36: "elk_summer_range",
    37: "elk_production_area",
    40: "elk_migration_corridors",
    41: "elk_severe_winter_range",
    42: "elk_winter_concentration",
    43: "elk_winter_range",
    44: "elk_overall_range",
}

HTTP_TIMEOUT = 120
USER_AGENT = "elk-dashboard-habitat-pipeline/0.1"
