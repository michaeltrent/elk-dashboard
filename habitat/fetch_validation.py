"""CPW Species Activity Mapping elk layers -- VALIDATION ONLY.

These are written to data/habitat_validation/, never data/habitat/, so the
model step can't read them as inputs. They're for scoring the prediction
afterward (e.g., what share of the top-scored cells fall inside winter
concentration areas vs. what share you'd expect by chance).
"""
import config
from common import arcgis_query, clip_to, log, region_aoi, write_layer


def main():
    config.VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    for region in config.REGIONS:
        log(f"CPW elk validation layers -- {region}")
        poly, bbox = region_aoi(region)
        path = config.VALIDATION_DIR / f"{region}_cpw_elk.gpkg"
        for lid, name in config.CPW_ELK_VALIDATION_LAYERS.items():
            try:
                g = clip_to(arcgis_query(f"{config.CPW_SPECIES}/{lid}", bbox), poly)
                write_layer(g, path, name)
            except Exception as e:
                log(f"  {name}: FAILED ({e})")


if __name__ == "__main__":
    main()
