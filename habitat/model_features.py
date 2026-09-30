"""Build model covariates for each region on a 30 m grid.

The 30 m grid is aligned to the 10 m DEM (each model cell = exactly 3x3 DEM
cells), so terrain is computed from the full 10 m data and then averaged:
slope and aspect keep their 10 m detail instead of being derived from a
coarsened DEM. 30 m is the vegetation data's native resolution.

Output: data/habitat/<region>/model/features/<name>.tif
"""
import warnings

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import rasterio
from rasterio.features import rasterize
from rasterio.transform import Affine
from rasterio.warp import Resampling, reproject
from rasterio.windows import Window
from scipy import ndimage as ndi

import config
import model_config as mc
from common import SESSION, log, region_dir

warnings.filterwarnings("ignore", message="Mean of empty slice")
warnings.filterwarnings("ignore", category=RuntimeWarning, message="invalid value")
RES = 30.0


# ------------------------------------------------------------------ grid + io

def grid(region):
    with rasterio.open(region_dir(region) / "dem_10m.tif") as d:
        t = d.transform
        return dict(driver="GTiff", width=d.width // 3, height=d.height // 3, count=1,
                    dtype="float32", crs=d.crs, nodata=np.nan,
                    transform=Affine(t.a * 3, 0, t.c, 0, t.e * 3, t.f),
                    compress="deflate", tiled=True, blockxsize=256, blockysize=256)


def feat_dir(region):
    d = region_dir(region) / "model" / "features"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save(region, name, arr, prof):
    p = dict(prof)
    if arr.dtype == np.uint8:
        p.update(dtype="uint8", nodata=255)
    elif arr.dtype == np.uint16:
        p.update(dtype="uint16", nodata=0)
    else:
        arr = arr.astype("float32")
        p.update(dtype="float32", nodata=np.nan, predictor=3)
    with rasterio.open(feat_dir(region) / f"{name}.tif", "w", **p) as f:
        f.write(arr, 1)


def load(region, name):
    with rasterio.open(feat_dir(region) / f"{name}.tif") as f:
        a = f.read(1)
    return a.astype("float32") if a.dtype == np.float32 else a


def focal_mean(a, radius_m):
    """NaN-aware square moving average with the given radius."""
    size = max(3, int(round(2 * radius_m / RES)) | 1)
    valid = np.isfinite(a)
    s = ndi.uniform_filter(np.where(valid, a, 0).astype("float32"), size, mode="nearest")
    n = ndi.uniform_filter(valid.astype("float32"), size, mode="nearest")
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0.05, s / n, np.nan).astype("float32")


# ------------------------------------------------------------------ terrain

def terrain(region, prof):
    """Slope/northness/eastness at 10 m, averaged to 30 m; elevation; TPI."""
    W, H = prof["width"], prof["height"]
    elev = np.full((H, W), np.nan, "float32")
    slope, north, east = elev.copy(), elev.copy(), elev.copy()
    block = 512  # model rows per chunk (~1,500 DEM rows)
    with rasterio.open(region_dir(region) / "dem_10m.tif") as d:
        nod = d.nodata
        for r0 in range(0, H, block):
            r1 = min(H, r0 + block)
            a, b = max(0, r0 * 3 - 1), min(d.height, r1 * 3 + 1)   # 1-row halo
            z = d.read(1, window=Window(0, a, d.width, b - a)).astype("float32")
            if nod is not None:
                z[z == nod] = np.nan
            gy, gx = np.gradient(z, 10.0)            # gy: +south, gx: +east
            sl = slice(r0 * 3 - a, r0 * 3 - a + (r1 - r0) * 3)
            z, gy, gx = z[sl, :W * 3], gy[sl, :W * 3], gx[sl, :W * 3]
            mag = np.hypot(gx, gy)
            with np.errstate(invalid="ignore", divide="ignore"):
                n_ = np.where(mag > 1e-3, gy / mag, 0.0)   # + = faces north
                e_ = np.where(mag > 1e-3, -gx / mag, 0.0)  # + = faces east
            n_[np.isnan(mag)] = np.nan
            e_[np.isnan(mag)] = np.nan
            s_ = np.degrees(np.arctan(mag))

            def agg(x):
                return np.nanmean(x.reshape(r1 - r0, 3, W, 3), axis=(1, 3))

            elev[r0:r1], slope[r0:r1] = agg(z), agg(s_)
            north[r0:r1], east[r0:r1] = agg(n_), agg(e_)
    save(region, "elev", elev, prof)
    save(region, "slope", slope, prof)
    save(region, "northness", north, prof)
    save(region, "eastness", east, prof)
    save(region, "tpi300", elev - focal_mean(elev, 300), prof)     # + = ridge/knob, - = draw
    save(region, "tpi1000", elev - focal_mean(elev, 1000), prof)
    log(f"  terrain: {W}x{H} cells @ 30 m (from 10 m DEM)")


# ------------------------------------------------------------------ vegetation

def evt_table():
    cache = config.OUT_DIR / "_cache" / config.LANDFIRE_EVT_CSV.rsplit("/", 1)[-1]
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(SESSION.get(config.LANDFIRE_EVT_CSV, timeout=120).content)
    df = pd.read_csv(cache, encoding="latin-1")
    df.columns = [c.upper() for c in df.columns]
    return df


def classify(name, phys="", lf=""):
    n, p = f" {str(name).lower()} ", f"{phys} {lf}".lower()
    for cls, keys in mc.VEG_RULES:
        if any(k in n for k in keys):
            return cls
    if "herb" in p or "grass" in p:
        return "grass"
    if "conifer" in p:
        return "conifer"
    if "shrub" in p:
        return "browse"
    return "other"


def veg_values(veg, cc, forage_tbl, cover_tbl):
    """Per-cell forage and cover value from vegetation class (+ canopy %)."""
    forage = np.full(veg.shape, np.nan, "float32")
    cover = forage.copy()
    for i, cls in enumerate(mc.VEG_CLASSES):
        m = veg == i
        forage[m] = forage_tbl[cls]
        cover[m] = cover_tbl[cls]
        if cls in mc.TREE_CLASSES:   # denser canopy = better cover
            cover[m] *= 0.4 + 0.6 * np.minimum(cc[m], 70) / 70
    return forage, cover


def landfire_bands(region):
    d = region_dir(region) / "landfire"
    tif = next((p for p in d.glob("*.tif")), None)
    if tif is None:
        raise FileNotFoundError(f"no LANDFIRE .tif in {d} -- run fetch step 'landfire'")
    with rasterio.open(tif) as src:
        desc = [(x or "").upper() for x in src.descriptions]
    idx = {}
    for key in ("EVT", "EVC", "CC"):
        hit = [i for i, s_ in enumerate(desc, 1) if key in s_ and not (key == "CC" and "EVC" in s_)]
        idx[key] = hit[0] if hit else None
    order = [l.split("_")[-1] for l in config.LANDFIRE_LAYERS]     # fallback: request order
    for key in idx:
        if idx[key] is None and key in order:
            idx[key] = order.index(key) + 1
    return tif, idx


def read_to_grid(tif, band, prof, resampling=Resampling.nearest):
    out = np.full((prof["height"], prof["width"]), -9999, "int32")
    with rasterio.open(tif) as src:
        reproject(rasterio.band(src, band), out, dst_transform=prof["transform"],
                  dst_crs=prof["crs"], dst_nodata=-9999, resampling=resampling)
    return out


def vegetation(region, prof):
    tif, idx = landfire_bands(region)
    evt = read_to_grid(tif, idx["EVT"], prof)
    evc = read_to_grid(tif, idx["EVC"], prof) if idx.get("EVC") else None
    cc = read_to_grid(tif, idx["CC"], prof).astype("float32") if idx.get("CC") else None

    table = evt_table()
    phys = table.get("EVT_PHYS", pd.Series("", index=table.index))
    lf = table.get("EVT_LF", pd.Series("", index=table.index))
    lookup = {int(v): mc.VEG_CLASSES.index(classify(n, p, l))
              for v, n, p, l in zip(table["VALUE"], table["EVT_NAME"], phys, lf)}
    veg = np.full(evt.shape, 255, "uint8")
    for code in np.unique(evt):
        if code in lookup:
            veg[evt == code] = lookup[code]
    if evc is not None:   # EVC catches water/developed/ag the EVT may miss
        veg[(evc == 11) | (evc == 12) | ((evc >= 13) & (evc <= 25)) | (evc == 31)] = mc.VEG_CLASSES.index("nonhab")
        veg[((evc >= 61) & (evc <= 69)) | (evc == 82)] = mc.VEG_CLASSES.index("ag")

    if cc is not None:
        cc[(cc < 0) | (cc > 100)] = 0
    else:
        cc = np.zeros(evt.shape, "float32")

    forage, cover = veg_values(veg, cc, mc.FORAGE, mc.COVER)
    save(region, "veg_class", veg, prof)
    save(region, "canopy", cc, prof)
    save(region, "forage_nbhd", focal_mean(forage, mc.FORAGE_RADIUS_M), prof)
    save(region, "cover_nbhd", focal_mean(cover, mc.COVER_RADIUS_M), prof)
    counts = {mc.VEG_CLASSES[i]: round(100 * float((veg == i).mean()), 1)
              for i in range(len(mc.VEG_CLASSES)) if (veg == i).any()}
    log(f"  vegetation classes (% of grid): {counts}")


# ------------------------------------------------------------------ vectors

def read_layer(region, layer):
    path = region_dir(region) / "features.gpkg"
    if not path.exists() or layer not in set(pyogrio.list_layers(path)[:, 0]):
        log(f"  (no '{layer}' layer -- treated as absent)")
        return None
    return gpd.read_file(path, layer=layer).to_crs(config.WORK_CRS)


def burn_mask(gdf, prof, values=None):
    shape = (prof["height"], prof["width"])
    if gdf is None or gdf.empty:
        return np.zeros(shape, bool) if values is None else np.zeros(shape, "float32")
    geoms = [g for g in gdf.geometry if g is not None and not g.is_empty]
    if values is None:
        return rasterize(((g, 1) for g in geoms), out_shape=shape, transform=prof["transform"],
                         fill=0, all_touched=True, dtype="uint8") > 0
    vals = [v for g, v in zip(gdf.geometry, values) if g is not None and not g.is_empty]
    return rasterize(zip(geoms, vals), out_shape=shape, transform=prof["transform"],
                     fill=0, all_touched=False, dtype="float32")


def distance(mask):
    if not mask.any():
        return np.full(mask.shape, 1e5, "float32")   # "very far"
    return (ndi.distance_transform_edt(~mask) * RES).astype("float32")


def vectors(region, prof):
    roads = read_layer(region, "roads_trails")
    if roads is not None:
        mot = roads["motorized"].astype(str).str.lower().isin(["true", "1"])
        save(region, "dist_road_open", distance(burn_mask(roads[mot & (roads.open_hunt == "open")], prof)), prof)
        save(region, "dist_road_unknown", distance(burn_mask(roads[mot & (roads.open_hunt == "unknown")], prof)), prof)
    else:
        for n in ("dist_road_open", "dist_road_unknown"):
            save(region, n, np.full((prof["height"], prof["width"]), 1e5, "float32"), prof)

    streams = read_layer(region, "streams_nhd")
    wb = read_layer(region, "waterbodies_nhd")
    springs = read_layer(region, "springs_nhd")
    rights = read_layer(region, "water_rights_cdss")
    reliable = burn_mask(wb, prof) | burn_mask(rights, prof)
    anyw = reliable.copy()
    if streams is not None:
        reliable |= burn_mask(streams[streams.permanence == "perennial"], prof)
        anyw |= burn_mask(streams[streams.permanence.isin(["perennial", "intermittent"])], prof)
    anyw |= burn_mask(springs, prof)
    save(region, "dist_water_reliable", distance(reliable), prof)
    save(region, "dist_water_any", distance(anyw), prof)

    save(region, "public", burn_mask(read_layer(region, "public_land"), prof).astype("float32"), prof)

    fires = read_layer(region, "fire_perimeters")
    if fires is not None and len(fires):
        ycol = next(c for c in fires.columns if c.lower() == "year")
        fires = fires.sort_values(ycol)                      # latest fire wins
        yr = burn_mask(fires, prof, values=fires[ycol].astype(float).tolist())
        yrs = np.where(yr > 0, mc.SEASON_YEAR - yr, np.nan).astype("float32")
        log(f"  burned: {100 * np.isfinite(yrs).mean():.1f}% of grid")
    else:
        yrs = np.full((prof["height"], prof["width"]), np.nan, "float32")
    save(region, "years_since_fire", yrs, prof)

    gmu = gpd.read_file(config.OUT_DIR / "gmu_boundaries.gpkg").to_crs(config.WORK_CRS)
    gcol = next(c for c in gmu.columns if c.lower() == "gmuid")
    units = rasterize(zip(gmu.geometry, gmu[gcol].astype(int)), out_shape=(prof["height"], prof["width"]),
                      transform=prof["transform"], fill=0, dtype="uint16")
    save(region, "gmu", units, prof)
    log("  distances, public land, burn age, unit ids done")


def main():
    for region in config.REGIONS:
        log(f"Features -- {region}")
        prof = grid(region)
        terrain(region, prof)
        vegetation(region, prof)
        vectors(region, prof)


if __name__ == "__main__":
    main()
