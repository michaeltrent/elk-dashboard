"""Model settings: vegetation classes, suitability curves, and season weights.

Everything tunable lives here. Change a weight or curve, rerun
`python run_model.py score evaluate`, and compare the evaluation table.
"""

# Year used for "years since fire".
SEASON_YEAR = 2026

# --- Vegetation classes (from LANDFIRE EVT names) ---------------------------------
# Order matters: the first matching rule wins (e.g., pinyon before "pine").
VEG_CLASSES = ["nonhab", "grass", "ag", "aspen", "browse", "sage",
               "riparian", "pj", "conifer", "other"]

VEG_RULES = [
    ("nonhab",   ["developed", "open water", "snow", "ice field", "barren", "quarr",
                  "mines", "sparse", "rock outcrop"]),
    ("ag",       ["agricultur", "cultivated", "pasture", "hay", "orchard", "vineyard"]),
    ("aspen",    ["aspen"]),
    ("riparian", ["riparian", "wetland", "floodplain", "marsh", "fen"]),
    ("pj",       ["pinyon", "juniper"]),
    ("sage",     ["sagebrush"]),
    ("browse",   ["gambel oak", "mountain mahogany", "montane shrub", "mountain shrub",
                  "deciduous shrub", "serviceberry", "chokecherry", "mixed montane shrubland"]),
    ("grass",    ["grassland", "meadow", "prairie", "herbaceous", "tundra"]),
    ("conifer",  ["spruce", " fir", "douglas-fir", "lodgepole", "ponderosa", "limber pine",
                  "bristlecone", "conifer", "pine"]),
]

# General forage/cover value of each class (0-1). Used for the saved
# features (forage_nbhd, cover_nbhd) that training and diagnostics read.
FORAGE = {"nonhab": 0, "grass": 1.0, "ag": 0.9, "aspen": 0.8, "browse": 0.8, "sage": 0.6,
          "riparian": 0.7, "pj": 0.3, "conifer": 0.1, "other": 0.3}
COVER = {"nonhab": 0, "grass": 0, "ag": 0, "aspen": 0.5, "browse": 0.5, "sage": 0.2,
         "riparian": 0.5, "pj": 0.8, "conifer": 1.0, "other": 0.3}

# Season-specific values used by the score. Late fall: diets shift to browse,
# shrubs and cured grass; aspen is leaf-off (less cover); pinyon-juniper is
# both thermal cover and browse. Early fall: green meadows, aspen, riparian.
FORAGE_BY_SEASON = {
    "late": {"nonhab": 0, "grass": 0.9, "ag": 0.9, "aspen": 0.6, "browse": 1.0, "sage": 0.8,
             "riparian": 0.7, "pj": 0.5, "conifer": 0.1, "other": 0.3},
    "early": {"nonhab": 0, "grass": 1.0, "ag": 0.8, "aspen": 0.9, "browse": 0.7, "sage": 0.4,
              "riparian": 0.9, "pj": 0.2, "conifer": 0.2, "other": 0.3},
}
COVER_BY_SEASON = {
    "late": {"nonhab": 0, "grass": 0, "ag": 0, "aspen": 0.4, "browse": 0.5, "sage": 0.25,
             "riparian": 0.4, "pj": 0.9, "conifer": 1.0, "other": 0.3},
    "early": {"nonhab": 0, "grass": 0, "ag": 0, "aspen": 0.7, "browse": 0.5, "sage": 0.1,
              "riparian": 0.6, "pj": 0.7, "conifer": 1.0, "other": 0.3},
}
TREE_CLASSES = {"aspen", "pj", "conifer", "riparian"}   # cover scaled by canopy %

# Neighborhoods (meters) for "forage nearby" and "cover nearby".
FORAGE_RADIUS_M = 500
COVER_RADIUS_M = 250

# --- Suitability curves ----------------------------------------------------------
# Security: logistic rise with distance from roads open to vehicles during the
# hunt. 'mid' is where security reaches 50%.
ROAD_OPEN_MID_M, ROAD_OPEN_SCALE_M = 800, 300
ROAD_UNKNOWN_MID_M, ROAD_UNKNOWN_SCALE_M = 400, 200   # gated/unknown roads count less

# Interspersion: forage and cover both need to be nearby. These are the
# neighborhood fractions at which each is considered "enough".
FORAGE_ENOUGH = 0.35
COVER_ENOUGH = 0.40

# Slope (degrees): full value up to SLOPE_GOOD, falling to SLOPE_FLOOR at SLOPE_BAD.
SLOPE_GOOD, SLOPE_BAD, SLOPE_FLOOR = 20, 40, 0.2

# Water: distance decay (meters) for reliable vs any water.
WATER_RELIABLE_M, WATER_ANY_M = 1500, 800

# Burn age (years since fire) -> value; unburned = BURN_NONE.
BURN_NONE = 0.8
BURN_CURVE = [(2, 0.6), (15, 1.0), (30, 0.9)]   # (up to N years, value); older -> BURN_NONE

# Elevation: scored by where a cell sits within its own region's elevation
# range (0 = lowest in the units, 1 = highest), so the same curve works in the
# Piceance and in Summit County. Each curve is (percentile, value) points.
# Late season depends on snow: a storm pushes elk low early; a dry fall
# leaves them higher. Pick the scenario that matches the forecast.
LATE_SNOW = "normal"          # "normal", "storm", or "dry"
ELEVATION_CURVES = {
    "late_normal": [(0.0, 0.8), (0.1, 1.0), (0.6, 1.0), (1.0, 0.45)],
    "late_storm":  [(0.0, 1.0), (0.45, 1.0), (0.75, 0.5), (1.0, 0.3)],
    "late_dry":    [(0.0, 0.6), (0.2, 1.0), (0.75, 1.0), (1.0, 0.6)],
    "early":       [(0.0, 0.5), (0.4, 0.8), (0.6, 1.0), (1.0, 1.0)],
}

# --- Seasons --------------------------------------------------------------------
# aspect_pref: +1 favors south/west faces (late season), -1 north/east (early).
# weights: relative influence of each component in the weighted geometric mean.
# Two maps are made per season:
#   habitat map -- every component EXCEPT security: where elk want to be.
#                  This is what gets compared with CPW's (hunter-free) layers.
#   hunt map    -- habitat plus security: where elk will be under hunting
#                  pressure. This is the one to scout from.
SEASONS = {
    "late": {   # 2nd/3rd rifle, late Oct - mid Nov
        "aspect_pref": +1,
        "weights": {"security": 3.0, "interspersion": 3.0, "elevation": 2.5, "slope": 1.0,
                    "aspect": 0.5, "water": 0.5, "burn": 1.0, "terrain": 0.25},
    },
    "early": {  # archery / 1st rifle, Sept - mid Oct
        "aspect_pref": -1,
        "weights": {"security": 2.5, "interspersion": 3.0, "elevation": 1.5, "slope": 1.0,
                    "aspect": 1.0, "water": 2.0, "burn": 1.0, "terrain": 0.5},
    },
}

# --- Validation ------------------------------------------------------------------
# CPW layers the score is checked against (never used as inputs).
VALIDATION_LAYERS = ["elk_winter_concentration", "elk_severe_winter_range", "elk_winter_range",
                     "elk_summer_concentration", "elk_production_area"]

# --- Training (from field observations) ------------------------------------------
OBSERVATIONS_CSV = "observations.csv"      # in data/habitat/
TRAIN_FEATURES = ["slope", "northness", "eastness", "elev", "tpi300", "tpi1000", "canopy",
                  "forage_nbhd", "cover_nbhd", "dist_road_open", "dist_road_unknown",
                  "dist_water_reliable", "dist_water_any", "years_since_fire", "public"]
LOG_FEATURES = {"dist_road_open", "dist_road_unknown", "dist_water_reliable", "dist_water_any"}
QUADRATIC = {"slope", "elev", "tpi300"}    # allow "not too high, not too low" responses
BACKGROUND_POINTS = 10000
MIN_OBSERVATIONS = 30
