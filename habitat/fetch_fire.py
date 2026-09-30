"""Fire perimeters (MTBS, 1984 on) for each region.

Large fires reshape elk habitat for decades: forage (grass, aspen suckers)
peaks a few years after a burn while hot-burned timber loses its cover. The
model uses years-since-fire from these perimeters.
"""
import config
from common import arcgis_query, clip_to, log, region_aoi, region_dir, write_layer


def main():
    for region in config.REGIONS:
        log(f"Fire perimeters -- {region}")
        poly, bbox = region_aoi(region)
        gdf = clip_to(arcgis_query(config.MTBS_BOUNDARIES, bbox), poly)
        if gdf is not None and len(gdf):
            cols = {c.lower(): c for c in gdf.columns}
            for _, r in gdf.sort_values(cols.get("year", gdf.columns[0])).iterrows():
                log(f"  {r.get(cols.get('fire_name'), '?')} ({r.get(cols.get('year'), '?')}), "
                    f"{r.get(cols.get('acres'), 0):,.0f} ac")
        write_layer(gdf, region_dir(region) / "features.gpkg", "fire_perimeters")


if __name__ == "__main__":
    main()
