"""10 m elevation (USGS 3DEP 1/3 arc-second) for each region.

Memory-bounded: the output grid (UTM 13N, 10 m, covering the buffered region)
is created on disk first, then filled one 1024x1024 block at a time. Each
3DEP tile is a cloud-optimized GeoTIFF read through a warped virtual view, so
only the bytes under each block are downloaded and at most one block (~4 MB)
per tile is in memory. Peak use stays well under 1 GB regardless of region
size.

Output: <region>/dem_10m.tif. Slope, aspect, and terrain position get derived
from this in the model step.
"""
import math

import numpy as np
import rasterio
import rasterio.warp
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window, from_bounds

import config
from common import log, region_aoi, region_dir

GDAL_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                GDAL_HTTP_MULTIRANGE="YES", GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
                GDAL_HTTP_MAX_RETRY=4, GDAL_HTTP_RETRY_DELAY=2,
                VSI_CACHE="TRUE", GDAL_CACHEMAX=256)
BLOCK = 1024
NODATA = -9999.0


def tiles_for(bbox):
    """3DEP tiles are named by their NW corner: n40w108 covers 39-40N, 107-108W."""
    w, s, e, n = bbox
    for lat in range(math.floor(s) + 1, math.ceil(n) + 1):
        for lon in range(math.floor(-e) + 1, math.ceil(-w) + 1):
            yield config.DEP_TILE_URL.format(lat=lat, lon=lon)


def target_grid(poly):
    res = config.DEM_RES_M
    minx, miny, maxx, maxy = poly.bounds
    minx, miny = math.floor(minx / res) * res, math.floor(miny / res) * res
    maxx, maxy = math.ceil(maxx / res) * res, math.ceil(maxy / res) * res
    width, height = int((maxx - minx) / res), int((maxy - miny) / res)
    return from_origin(minx, maxy, res, res), width, height


def blocks(width, height):
    for r in range(0, height, BLOCK):
        for c in range(0, width, BLOCK):
            yield Window(c, r, min(BLOCK, width - c), min(BLOCK, height - r))


def main():
    for region in config.REGIONS:
        log(f"Terrain -- {region}")
        poly, bbox = region_aoi(region)
        out = region_dir(region) / "dem_10m.tif"
        transform, width, height = target_grid(poly)
        log(f"  grid {width}x{height} @ {config.DEM_RES_M} m")
        profile = dict(driver="GTiff", width=width, height=height, count=1, dtype="float32",
                       crs=config.WORK_CRS, transform=transform, nodata=NODATA,
                       compress="deflate", predictor=3, tiled=True,
                       blockxsize=256, blockysize=256, BIGTIFF="IF_SAFER")
        filled = 0
        with rasterio.Env(**GDAL_ENV), rasterio.open(out, "w+", **profile) as dst:
            for url in tiles_for(bbox):
                name = url.rsplit("/", 1)[-1]
                try:
                    src = rasterio.open(f"/vsicurl/{url}")
                except rasterio.errors.RasterioIOError:
                    log(f"  missing tile {name} (ok if outside the data)")
                    continue
                with src, WarpedVRT(src, crs=config.WORK_CRS, transform=transform,
                                    width=width, height=height, nodata=NODATA,
                                    src_nodata=src.nodata, resampling=Resampling.bilinear) as vrt:
                    # Only visit blocks this tile actually covers.
                    tb = rasterio.warp.transform_bounds(src.crs, config.WORK_CRS, *src.bounds)
                    cover = from_bounds(*tb, transform=transform).round_offsets().round_lengths()
                    cover = cover.intersection(Window(0, 0, width, height))
                    n = 0
                    for win in blocks(width, height):
                        try:
                            win.intersection(cover)
                        except rasterio.errors.WindowError:
                            continue
                        new = vrt.read(1, window=win)
                        ok = new != NODATA
                        if not ok.any():
                            continue
                        cur = dst.read(1, window=win)
                        take = ok & (cur == NODATA)
                        if take.any():
                            cur[take] = new[take]
                            dst.write(cur, 1, window=win)
                            filled += int(take.sum())
                        n += 1
                    log(f"  {name}: filled {n} blocks")
        pct = 100 * filled / (width * height)
        log(f"  wrote {out.name}: {width}x{height}, {pct:.0f}% of grid filled")


if __name__ == "__main__":
    main()
