"""Roads and trails, tagged by whether motor vehicles can use them during the
hunt window (config.HUNT_WINDOW).

Sources
  BLM  - Colorado Ground Transportation Linear Features, "Routes by Allowed
         Mode of Transportation" (designation + seasonal restriction flag)
  USFS - Motor Vehicle Use Map roads and trails (per-vehicle-class open dates)
  Census TIGER - county roads, highways, and energy/private roads that
         neither agency layer covers (important in the Piceance gas field)

Output columns (all sources): source, name, motorized (bool),
open_hunt (True / False / None = unknown), detail
"""
import re

import geopandas as gpd
import pandas as pd

import config
from common import arcgis_query, clip_to, find_layer, log, region_aoi, region_dir, write_layer

# ---------------------------------------------------------------- date logic

def _doy(m, d):
    return (pd.Timestamp(2026, m, d) - pd.Timestamp(2026, 1, 1)).days


HUNT_START, HUNT_END = (_doy(*config.HUNT_WINDOW[0]), _doy(*config.HUNT_WINDOW[1]))
_RANGE = re.compile(r"(\d{1,2})/(\d{1,2})\s*-\s*(\d{1,2})/(\d{1,2})")


def open_during_hunt(dates_open):
    """MVUM DATESOPEN strings look like '06/15-11/30' or '01/01-12/31', and
    can hold several comma-separated ranges. True if any range overlaps the
    hunt window; None if the string can't be parsed."""
    if dates_open is None or (isinstance(dates_open, float) and pd.isna(dates_open)):
        return None
    ranges = _RANGE.findall(str(dates_open))
    if not ranges:
        return None
    for m1, d1, m2, d2 in ranges:
        a, b = _doy(int(m1), int(d1)), _doy(int(m2), int(d2))
        days = set(range(a, b + 1)) if a <= b else set(range(a, 366)) | set(range(0, b + 1))
        if days & set(range(HUNT_START, HUNT_END + 1)):
            return True
    return False


# ---------------------------------------------------------------- BLM

def blm_routes(bbox):
    gdf = arcgis_query(config.BLM_GTLF_ROUTES, bbox)
    if gdf.empty:
        return gdf
    mode = gdf.get("PLAN_MODE_TRNSPRT", pd.Series(index=gdf.index, dtype=object))
    dsg = gdf.get("PLAN_OHV_ROUTE_DSGNTN", pd.Series(index=gdf.index, dtype=object))
    seas = gdf.get("PLAN_SEASON_RSTRCT_CODE", pd.Series(index=gdf.index, dtype=object))

    motorized = mode.eq("Motorized")

    def status(i):
        if not motorized[i] or dsg[i] == "Closed":
            return False
        if dsg[i] == "Open" and seas[i] != "YES":
            return True
        return None  # Limited, seasonal, or unknown -> needs field knowledge

    out = gpd.GeoDataFrame({
        "source": "BLM",
        "name": gdf.get("ROUTE_PRMRY_NM", gdf.get("PLAN_ROUTE_NAME", None)),
        "motorized": motorized,
        "open_hunt": [status(i) for i in gdf.index],
        "detail": gdf.get("PLAN_ALLOW_MODE_TRNSPRT"),
    }, geometry=gdf.geometry, crs=gdf.crs)
    return out


# ---------------------------------------------------------------- USFS MVUM

VEHICLE_FIELDS = [
    "PASSENGERVEHICLE", "HIGHCLEARANCEVEHICLE", "TRUCK", "FOURWD_GT50INCHES",
    "TWOWD_GT50INCHES", "OTHER_OHV_GT50INCHES", "ATV", "MOTORCYCLE",
    "OTHERWHEELED_OHV", "OTHER_OHV_LT50INCHES",
]


def _dates_field(v):
    return {"FOURWD_GT50INCHES": "FOURWD_GT50_DATESOPEN",
            "TWOWD_GT50INCHES": "TWOWD_GT50_DATESOPEN",
            "OTHER_OHV_GT50INCHES": "OTHER_OHV_GT50_DATESOPEN",
            "OTHER_OHV_LT50INCHES": "OTHER_OHV_LT50_DATESOPEN"}.get(v, f"{v}_DATESOPEN")


def mvum(layer_url, bbox, kind):
    gdf = arcgis_query(layer_url, bbox)
    if gdf.empty:
        return gdf

    def status(row):
        classes = [v for v in VEHICLE_FIELDS if v in row and isinstance(row[v], str) and row[v].strip()]
        if not classes:
            return False, False, ""
        results = [open_during_hunt(row.get(_dates_field(v))) for v in classes]
        if any(r is True for r in results):
            return True, True, ",".join(classes)
        if all(r is False for r in results):
            return True, False, ",".join(classes)
        return True, None, ",".join(classes)

    s = gdf.apply(status, axis=1, result_type="expand")
    return gpd.GeoDataFrame({
        "source": f"USFS_MVUM_{kind}",
        "name": gdf.get("NAME"),
        "motorized": s[0].astype(bool),
        "open_hunt": s[1],
        "detail": s[2],
    }, geometry=gdf.geometry, crs=gdf.crs)


# ---------------------------------------------------------------- Census TIGER

def tiger_roads(bbox):
    frames = []
    for name in ("Primary Roads", "Secondary Roads", "Local Roads"):
        try:
            url = find_layer(config.TIGERWEB_TRANSPORT, name)
        except LookupError as e:
            log(f"  TIGER: {e}")
            continue
        g = arcgis_query(url, bbox)
        if not g.empty:
            g["detail"] = name
            frames.append(g)
    if not frames:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    gdf = pd.concat(frames, ignore_index=True)
    return gpd.GeoDataFrame({
        "source": "TIGER",
        "name": gdf.get("NAME"),
        "motorized": True,
        # County/state roads are open; many TIGER 'local' roads in energy
        # fields are gated -- leave those for field review.
        "open_hunt": [True if d != "Local Roads" else None for d in gdf["detail"]],
        "detail": gdf["detail"],
    }, geometry=gdf.geometry, crs="EPSG:4326")


def main():
    for region in config.REGIONS:
        log(f"Roads -- {region}")
        poly, bbox = region_aoi(region)
        path = region_dir(region) / "features.gpkg"
        parts = []
        for label, fn in [("BLM GTLF", lambda: blm_routes(bbox)),
                          ("USFS MVUM roads", lambda: mvum(config.USFS_MVUM_ROADS, bbox, "ROAD")),
                          ("USFS MVUM trails", lambda: mvum(config.USFS_MVUM_TRAILS, bbox, "TRAIL")),
                          ("Census TIGER", lambda: tiger_roads(bbox))]:
            try:
                g = clip_to(fn(), poly)
                log(f"  {label}: {0 if g is None else len(g):,}")
                if g is not None and not g.empty:
                    parts.append(g)
            except Exception as e:  # keep going; one dead service shouldn't sink the run
                log(f"  {label}: FAILED ({e})")
        if parts:
            roads = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=config.WORK_CRS)
            roads["open_hunt"] = roads["open_hunt"].map({True: "open", False: "closed"}).fillna("unknown")
            write_layer(roads, path, "roads_trails")


if __name__ == "__main__":
    main()
