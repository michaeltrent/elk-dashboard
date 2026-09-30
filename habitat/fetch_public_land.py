"""Public land (PAD-US) for each region, plus the per-unit public-land %
the dashboard has been missing.

Output
  <region>/features.gpkg : layer 'public_land'
  data/habitat/unit_public_land.csv : GMUID, sq mi, public sq mi, % public,
                                      % open-access, breakdown by manager
"""
import geopandas as gpd
import pandas as pd

import config
from common import arcgis_query, clip_to, load_units, log, region_aoi, region_dir, write_layer

KEEP = ["Mang_Type", "MngTp_Desc", "Mang_Name", "MngNm_Desc", "Pub_Access",
        "Des_Tp", "DesTp_Desc", "Unit_Nm", "GAP_Sts"]


def main():
    units = load_units()
    rows = []
    for region in config.REGIONS:
        log(f"Public land -- {region}")
        poly, bbox = region_aoi(region)
        gdf = arcgis_query(config.PADUS, bbox, tile_deg=0.25, simplify=0.0001)
        gdf = clip_to(gdf, poly)
        if gdf is None or gdf.empty:
            log("  no PAD-US features")
            continue
        gdf = gdf[[c for c in KEEP if c in gdf.columns] + ["geometry"]]
        if "Pub_Access" in gdf:
            gdf = gdf[gdf["Pub_Access"].isin(["OA", "RA"])]   # drop closed-access parcels
        write_layer(gdf, region_dir(region) / "features.gpkg", "public_land")

        mgr_col = "MngNm_Desc" if "MngNm_Desc" in gdf else ("Mang_Name" if "Mang_Name" in gdf else None)
        # PAD-US overlaps itself (e.g., wilderness inside a national forest),
        # so dissolve before measuring area.
        for _, u in units[units.region == region].iterrows():
            inside = gpd.clip(gdf, u.geometry)
            total = u.geometry.area
            pub = inside.union_all().area if len(inside) else 0
            oa = inside[inside["Pub_Access"] == "OA"] if "Pub_Access" in inside else inside
            oa_area = oa.union_all().area if len(oa) else 0
            row = {"GMUID": u.GMUID, "region": region,
                   "sq_mi": round(total / 2.59e6, 1),
                   "public_sq_mi": round(pub / 2.59e6, 1),
                   "pct_public": round(100 * pub / total, 1),
                   "pct_open_access": round(100 * oa_area / total, 1)}
            if mgr_col and len(inside):
                by = inside.dissolve(by=mgr_col).area
                for mgr, a in by.items():
                    row[f"pct_{str(mgr).strip()}"] = round(100 * a / total, 1)
            rows.append(row)
            log(f"  unit {u.GMUID}: {row['pct_public']}% public")
    if rows:
        out = config.OUT_DIR / "unit_public_land.csv"
        pd.DataFrame(rows).sort_values("GMUID").to_csv(out, index=False)
        log(f"wrote {out}")


if __name__ == "__main__":
    main()
