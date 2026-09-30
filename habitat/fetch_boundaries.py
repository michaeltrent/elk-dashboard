"""Full-resolution GMU boundaries from CPW (replaces the simplified file for
clipping and per-unit stats)."""
import geopandas as gpd

import config
from common import arcgis_query, log, write_layer


def main():
    units = sorted({u for us in config.REGIONS.values() for u in us})
    where = f"GMUID IN ({','.join(map(str, units))})"
    log(f"GMU boundaries for units {units}")
    # Colorado-wide envelope; the WHERE clause does the filtering.
    gdf = arcgis_query(config.CPW_GMU_LAYER, (-109.1, 36.9, -102.0, 41.1), where=where)
    if gdf.empty:
        raise SystemExit("No GMU polygons returned -- check CPW_GMU_LAYER in config.py")
    gdf = gdf.to_crs(config.WORK_CRS)
    gdf = gdf.dissolve(by="GMUID", as_index=False)  # in case a unit has several parts
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_layer(gdf, config.OUT_DIR / "gmu_boundaries.gpkg", "gmu")
    missing = set(units) - set(gdf["GMUID"].astype(int))
    if missing:
        log(f"  WARNING: units not found: {sorted(missing)}")


if __name__ == "__main__":
    main()
