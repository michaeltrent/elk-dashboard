"""Export model outputs and inputs as dashboard map layers.

Raster layers -> XYZ PNG tiles (Web Mercator, paletted PNGs to keep size down)
    Model:       hunt map, habitat map  (percentile classes within each unit group)
    Background:  vegetation type, tree canopy, late-season forage and cover,
                 slope, aspect, burn age, road security
Feature layers -> simplified GeoJSON, clipped to the units, loaded on demand
    roads (by hunt-season status), streams + ponds, springs & stock ponds,
    fire perimeters

Everything is described in docs/data/habitat_layers/meta.json; the dashboard
builds its layer controls from that file, so a layer that wasn't exported
simply doesn't appear.

    python export_layers.py                  # late season, everything
    python export_layers.py --only model     # just re-export the model maps
    python export_layers.py --season early

Re-run after re-scoring, then commit docs/data/habitat_layers/ and deploy.
"""
import argparse
import json
import math
import shutil
import time

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import rasterio
from PIL import Image
from rasterio.features import rasterize
from rasterio.warp import Resampling, reproject, transform_bounds

import config
import model_config as mc
from common import log, region_dir
from model_features import focal_mean, load, veg_values

OUT = config.ROOT.parent / "docs" / "data" / "habitat_layers"
TILE = 256
ORIGIN = 20037508.342789244
MIN_ZOOM = 8
MODEL_MAX_ZOOM = 12        # ~30 m/px at this latitude = the model's resolution
INPUT_MAX_ZOOM = 12        # lower to 11 to halve background-layer size
SIZE_WARN_MB = 80


# ------------------------------------------------------------------ helpers

def hexa(h, a=255):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)


def bins(arr, edges, inside):
    """Class 1..len(edges)-1 by edges; 0 = transparent (outside units / NaN)."""
    cls = np.zeros(arr.shape, "uint8")
    ok = inside & np.isfinite(arr)
    cls[ok] = np.clip(np.digitize(arr[ok], edges[1:-1]) + 1, 1, len(edges) - 1)
    return cls


def region_grid(region):
    with rasterio.open(region_dir(region) / "model" / "features" / "elev.tif") as f:
        return f.transform, f.crs, (f.height, f.width)


# ------------------------------------------------------------------ raster layers
# Each builder returns a uint8 class array (0 = transparent) for one region.

PCT_CLASSES = [("Top 50%", 50, hexa("#ffeda0", 110)), ("Top 30%", 70, hexa("#feb24c", 145)),
               ("Top 20%", 80, hexa("#fd8d3c", 175)), ("Top 10%", 90, hexa("#f03b20", 205)),
               ("Top 5%", 95, hexa("#a50026", 235))]


def percentile_classes(region, name):
    with rasterio.open(region_dir(region) / "model" / f"suitability_{name}.tif") as f:
        suit = f.read(1)
    inside = (load(region, "gmu") > 0) & np.isfinite(suit)
    qs = np.linspace(0, 100, 201)
    pct = np.interp(suit, np.quantile(suit[inside], qs / 100), qs)
    cls = np.zeros(suit.shape, "uint8")
    for i, (_, pmin, _) in enumerate(PCT_CLASSES, 1):
        cls[inside & (pct >= pmin)] = i
    return cls


VEG_COLORS = {"grass": "#d8c26a", "ag": "#f3e7a1", "aspen": "#9bd67f", "browse": "#b98555",
              "sage": "#a9b99b", "riparian": "#4fb3bf", "pj": "#6f8f55", "conifer": "#1f5e3a",
              "other": "#8a8a8a", "nonhab": "#4a4a4a"}
VEG_LABELS = {"grass": "Grass / meadow", "ag": "Agriculture", "aspen": "Aspen",
              "browse": "Oak / mtn shrub", "sage": "Sagebrush", "riparian": "Riparian",
              "pj": "Pinyon-juniper", "conifer": "Conifer", "other": "Other",
              "nonhab": "Water / developed / barren"}


def raster_layers(season):
    """Registry: id -> spec. 'build' returns a class array for a region."""
    def inside(region):
        return load(region, "gmu") > 0

    def veg(region):
        v = load(region, "veg_class")
        out = np.where((v != 255) & inside(region), v + 1, 0).astype("uint8")
        return out

    def canopy(region):
        return bins(load(region, "canopy").astype("float32"), [-1, 10, 25, 40, 60, 101],
                    inside(region))

    def forage_cover(which):
        def build(region):
            f, c = veg_values(load(region, "veg_class"), load(region, "canopy"),
                              mc.FORAGE_BY_SEASON[season], mc.COVER_BY_SEASON[season])
            arr = (focal_mean(f, mc.FORAGE_RADIUS_M) if which == "forage"
                   else focal_mean(c, mc.COVER_RADIUS_M))
            return bins(arr, [-1, 0.2, 0.4, 0.6, 0.8, 2], inside(region))
        return build

    def slope(region):
        return bins(load(region, "slope"), [-1, 10, 20, 30, 40, 91], inside(region))

    def aspect(region):
        n, e, s = load(region, "northness"), load(region, "eastness"), load(region, "slope")
        az = (np.degrees(np.arctan2(e, n)) + 360) % 360
        cls = (((az + 22.5) // 45) % 8 + 1).astype("uint8")
        cls[~(inside(region) & np.isfinite(az) & (s >= 5))] = 0     # flats left clear
        return cls

    def burn(region):
        y = load(region, "years_since_fire")
        return bins(y, [-1, 2, 7, 15, 30, 100], inside(region))

    def security(region):
        with rasterio.open(region_dir(region) / "model" / f"components_{season}.tif") as f:
            i = list(f.descriptions).index("security") + 1
            sec = f.read(i)
        return bins(sec, [-1, 0.2, 0.4, 0.6, 0.8, 2], inside(region))

    greens = ["#edf8e9", "#bae4b3", "#74c476", "#31a354", "#006d2c"]
    ramp5 = lambda cols, labels, a=200: [(l, hexa(c, a)) for l, c in zip(labels, cols)]
    return {
        season: dict(group="model", label="Hunt map (habitat + road security)",
                     build=lambda r: percentile_classes(r, season), max_zoom=MODEL_MAX_ZOOM,
                     legend=[(l, c) for l, _, c in PCT_CLASSES]),
        f"{season}_habitat": dict(group="model", label="Habitat map (no road security)",
                                  build=lambda r: percentile_classes(r, f"{season}_habitat"),
                                  max_zoom=MODEL_MAX_ZOOM, legend=[(l, c) for l, _, c in PCT_CLASSES]),
        "vegetation": dict(group="input", label="Vegetation type (LANDFIRE)", build=veg,
                           legend=[(VEG_LABELS[c], hexa(VEG_COLORS[c], 200)) for c in mc.VEG_CLASSES]),
        "canopy": dict(group="input", label="Tree canopy cover", build=canopy,
                       legend=ramp5(greens, ["<10%", "10-25%", "25-40%", "40-60%", "60%+"])),
        "forage": dict(group="input", label=f"Forage within {mc.FORAGE_RADIUS_M} m ({season})",
                       build=forage_cover("forage"),
                       legend=ramp5(["#fff7bc", "#fee391", "#fec44f", "#ec7014", "#8c2d04"],
                                    ["Very low", "Low", "Moderate", "High", "Very high"])),
        "cover": dict(group="input", label=f"Cover within {mc.COVER_RADIUS_M} m ({season})",
                      build=forage_cover("cover"),
                      legend=ramp5(["#f7fcfd", "#ccece6", "#66c2a4", "#238b45", "#00441b"],
                                   ["Very low", "Low", "Moderate", "High", "Very high"])),
        "slope": dict(group="input", label="Slope", build=slope,
                      legend=ramp5(["#ffffcc", "#c2e699", "#78c679", "#e6550d", "#a63603"],
                                   ["0-10°", "10-20°", "20-30°", "30-40°", "40°+"])),
        "aspect": dict(group="input", label="Aspect (slopes over 5°)", build=aspect,
                       legend=[(d, hexa(c, 190)) for d, c in zip(
                           ["N", "NE", "E", "SE", "S", "SW", "W", "NW"],
                           ["#3b6fb6", "#5aa0c8", "#9bd3c0", "#f2e394", "#f39c4a", "#e0603a",
                            "#b0527a", "#6a58a8"])]),
        "burn": dict(group="input", label="Years since fire (MTBS)", build=burn,
                     legend=ramp5(["#7f0000", "#d7301f", "#fc8d59", "#fdcc8a", "#fef0d9"],
                                  ["0-2 yr", "3-7 yr", "8-15 yr", "16-30 yr", "30+ yr"], 190)),
        "security": dict(group="input", label="Road security (distance from open roads)",
                         build=security,
                         legend=ramp5(["#d73027", "#fc8d59", "#fee08b", "#91cf60", "#1a9850"],
                                      ["Very low", "Low", "Moderate", "High", "Very high"], 180)),
    }


def tile_range(b, z):
    size = 2 * ORIGIN / 2 ** z
    minx, miny, maxx, maxy = b
    return (size, range(int((minx + ORIGIN) // size), int((maxx + ORIGIN) // size) + 1),
            range(int((ORIGIN - maxy) // size), int((ORIGIN - miny) // size) + 1))


def palette_png(arr, colors, path):
    img = Image.fromarray(arr, "P")
    pal = [0, 0, 0] + [c for rgba in colors for c in rgba[:3]]
    img.putpalette(pal + [0] * (768 - len(pal)))
    img.save(path, optimize=True, transparency=bytes([0] + [rgba[3] for rgba in colors]))


def export_raster(lid, spec, bounds_out):
    tiles = {}
    max_zoom = spec.get("max_zoom", INPUT_MAX_ZOOM)
    for region in config.REGIONS:
        try:
            cls = spec["build"](region)
        except (FileNotFoundError, rasterio.errors.RasterioIOError, ValueError) as e:
            log(f"  {lid}/{region}: skipped ({e})")
            continue
        transform, crs, (h, w) = region_grid(region)
        l, t = transform.c, transform.f
        r, b = l + transform.a * w, t + transform.e * h
        b3857 = transform_bounds(crs, "EPSG:3857", l, b, r, t)
        bounds_out.append(transform_bounds(crs, "EPSG:4326", l, b, r, t))
        for z in range(MIN_ZOOM, max_zoom + 1):
            size, xs, ys = tile_range(b3857, z)
            rs = Resampling.nearest if z == max_zoom else Resampling.mode
            for x in xs:
                for y in ys:
                    dst = np.zeros((TILE, TILE), "uint8")
                    tt = rasterio.transform.from_origin(-ORIGIN + x * size, ORIGIN - y * size,
                                                        size / TILE, size / TILE)
                    reproject(cls, dst, src_transform=transform, src_crs=crs, src_nodata=0,
                              dst_transform=tt, dst_crs="EPSG:3857", dst_nodata=0, resampling=rs)
                    if dst.any():
                        k = (z, x, y)
                        tiles[k] = np.where(tiles[k] > 0, tiles[k], dst) if k in tiles else dst
    if not tiles:
        return None
    d0 = OUT / "tiles" / lid
    if d0.exists():
        shutil.rmtree(d0)
    colors = [c for _, c in spec["legend"]]
    nbytes = 0
    for (z, x, y), arr in tiles.items():
        d = d0 / str(z) / str(x)
        d.mkdir(parents=True, exist_ok=True)
        palette_png(arr, colors, d / f"{y}.png")
        nbytes += (d / f"{y}.png").stat().st_size
    log(f"  {lid}: {len(tiles):,} tiles, {nbytes / 1e6:.1f} MB")
    return {"id": lid, "type": "raster", "group": spec["group"], "label": spec["label"],
            "url": f"data/habitat_layers/tiles/{lid}/{{z}}/{{x}}/{{y}}.png",
            "minZoom": MIN_ZOOM, "maxNativeZoom": max_zoom,
            "legend": [{"label": l, "rgba": list(c)} for l, c in spec["legend"]], "bytes": nbytes}


# ------------------------------------------------------------------ vector layers

def units_union():
    gmu = gpd.read_file(config.OUT_DIR / "gmu_boundaries.gpkg").to_crs(config.WORK_CRS)
    gcol = next(c for c in gmu.columns if c.lower() == "gmuid")
    wanted = [u for us in config.REGIONS.values() for u in us]
    return gmu[gmu[gcol].astype(int).isin(wanted)].union_all()


def read(region, layer):
    p = region_dir(region) / "features.gpkg"
    if not p.exists() or layer not in set(pyogrio.list_layers(p)[:, 0]):
        return None
    return gpd.read_file(p, layer=layer).to_crs(config.WORK_CRS)


def gather(layer):
    parts = [g for g in (read(r, layer) for r in config.REGIONS) if g is not None and len(g)]
    return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=config.WORK_CRS) if parts else None


def s(v):
    return "" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v).strip()


def vector_layers():
    """id -> (label, builder returning GeoDataFrame with columns c (category),
    n (name), d (detail), styles {category: style}, legend order)."""
    def roads():
        g = gather("roads_trails")
        if g is None:
            return None
        mot = g["motorized"].astype(str).str.lower().isin(["true", "1"])
        g["c"] = np.where(~mot, "nonmotor", g["open_hunt"].fillna("unknown"))
        g["n"] = g.get("name", pd.Series("", index=g.index)).map(s)
        g["d"] = [f"{src} · {det}".strip(" ·") for src, det in
                  zip(g.get("source", "").map(s), g.get("detail", pd.Series("", index=g.index)).map(s))]
        return g[["c", "n", "d", "geometry"]]

    def water():
        st, wb = gather("streams_nhd"), gather("waterbodies_nhd")
        parts = []
        if st is not None:
            st = st[st["permanence"].isin(["perennial", "intermittent", "artificial_path"])].copy()
            st["c"] = st["permanence"].replace({"artificial_path": "perennial"})
            st["n"] = st.get("gnis_name", pd.Series("", index=st.index)).map(s)
            st["d"] = st["c"].str.capitalize() + " stream"
            parts.append(st[["c", "n", "d", "geometry"]])
        if wb is not None:
            wb = wb.copy()
            wb["c"] = "pond"
            wb["n"] = wb.get("gnis_name", pd.Series("", index=wb.index)).map(s)
            wb["d"] = "Lake / pond / reservoir"
            parts.append(wb[["c", "n", "d", "geometry"]])
        return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=config.WORK_CRS) if parts else None

    def springs():
        a, b = gather("springs_nhd"), gather("water_rights_cdss")
        parts = []
        if a is not None:
            a = a.copy()
            a["c"] = "nhd_spring"
            a["n"] = a.get("gnis_name", pd.Series("", index=a.index)).map(s)
            a["d"] = "USGS topo spring (may be dry)"
            parts.append(a[["c", "n", "d", "geometry"]])
        if b is not None:
            b = b.copy()
            b["c"] = np.where(b.get("kind", "spring") == "pond", "cdss_pond", "cdss_spring")
            b["n"] = b.get("structurename", pd.Series("", index=b.index)).map(s)
            b["d"] = ["Decreed " + ("stock pond" if c == "cdss_pond" else "spring") + (f" · WDID {w}" if s(w) else "")
                      for c, w in zip(b["c"], b.get("wdid", pd.Series("", index=b.index)))]
            parts.append(b[["c", "n", "d", "geometry"]])
        return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=config.WORK_CRS) if parts else None

    def fires():
        g = gather("fire_perimeters")
        if g is None:
            return None
        cols = {c.lower(): c for c in g.columns}
        g["c"] = "fire"
        g["n"] = g[cols["fire_name"]].map(s).str.title() if "fire_name" in cols else ""
        yr = g[cols["year"]] if "year" in cols else pd.Series("", index=g.index)
        ac = g[cols["acres"]] if "acres" in cols else pd.Series(np.nan, index=g.index)
        g["d"] = [f"{int(y)}" + (f" · {a:,.0f} acres" if pd.notna(a) else "") if pd.notna(y) else ""
                  for y, a in zip(yr, ac)]
        return g[["c", "n", "d", "geometry"]]

    return {
        "roads": ("Roads & trails (hunt-season status)", roads, 5, "line", {
            "open": {"label": "Open to vehicles", "color": "#e8453c", "weight": 2},
            "unknown": {"label": "Gated / seasonal / unknown", "color": "#f0a202", "weight": 1.6, "dash": "4 3"},
            "closed": {"label": "Closed to vehicles", "color": "#9aa0a6", "weight": 1.2, "dash": "2 3"},
            "nonmotor": {"label": "Non-motorized trail", "color": "#c9a0dc", "weight": 1.2, "dash": "1 3"}}),
        "water": ("Streams & ponds", water, 8, "line", {
            "perennial": {"label": "Perennial stream", "color": "#2f86d6", "weight": 1.8},
            "intermittent": {"label": "Intermittent stream", "color": "#7fb8e8", "weight": 1, "dash": "4 3"},
            "pond": {"label": "Lake / pond", "color": "#2f86d6", "weight": 1, "fill": 0.45}}),
        "springs": ("Springs & stock ponds", springs, 0, "point", {
            "cdss_spring": {"label": "Decreed spring (CDSS)", "color": "#00c2ff", "radius": 4},
            "cdss_pond": {"label": "Decreed stock pond (CDSS)", "color": "#1e5eff", "radius": 4},
            "nhd_spring": {"label": "Topo-map spring (may be dry)", "color": "#8fd3e8", "radius": 3}}),
        "fires": ("Fire perimeters (MTBS)", fires, 20, "poly", {
            "fire": {"label": "Burned area", "color": "#ff6a00", "weight": 1.5, "fill": 0.12}}),
    }


def export_vector(lid, label, build, tol_m, kind, styles, clip):
    g = build()
    if g is None or g.empty:
        log(f"  {lid}: no data (skipped)")
        return None
    g = g[g.geometry.notna() & ~g.geometry.is_empty]
    g = gpd.clip(g, clip)
    if tol_m:
        g["geometry"] = g.geometry.simplify(tol_m, preserve_topology=True)
    g = g[~g.geometry.is_empty].to_crs(4326)
    g = g[g["c"].isin(styles)]
    d = OUT / "vectors"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{lid}.geojson"
    if p.exists():
        p.unlink()
    g.to_file(p, driver="GeoJSON", COORDINATE_PRECISION=5, RFC7946="YES")
    nbytes = p.stat().st_size
    log(f"  {lid}: {len(g):,} features, {nbytes / 1e6:.1f} MB")
    counts = g["c"].value_counts().to_dict()
    return {"id": lid, "type": "vector", "kind": kind, "label": label,
            "url": f"data/habitat_layers/vectors/{lid}.geojson",
            "styles": {k: v for k, v in styles.items() if k in counts}, "bytes": nbytes}


# ------------------------------------------------------------------ main

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="late", choices=list(mc.SEASONS))
    ap.add_argument("--only", choices=["model", "inputs", "features"])
    a = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    meta_path = OUT / "meta.json"
    old = json.loads(meta_path.read_text()) if meta_path.exists() else {"layers": []}
    keep = {l["id"]: l for l in old.get("layers", [])}      # layers not re-exported this run
    bounds = [tuple(old["bounds"][0][::-1]) + tuple(old["bounds"][1][::-1])] if old.get("bounds") else []

    log("Raster layers")
    for lid, spec in raster_layers(a.season).items():
        if a.only == "features" or (a.only == "model" and spec["group"] != "model") \
                or (a.only == "inputs" and spec["group"] != "input"):
            continue
        m = export_raster(lid, spec, bounds)
        if m:
            keep[lid] = m
    if a.only in (None, "features"):
        log("Feature layers")
        clip = units_union().buffer(500)
        for lid, (label, build, tol, kind, styles) in vector_layers().items():
            m = export_vector(lid, label, build, tol, kind, styles, clip)
            if m:
                keep[lid] = m
    if not keep:
        raise SystemExit("Nothing exported -- run the pipeline and `python run_model.py` first.")

    order = [a.season, f"{a.season}_habitat", "vegetation", "canopy", "forage", "cover", "slope",
             "aspect", "burn", "security", "roads", "water", "springs", "fires"]
    layers = sorted(keep.values(), key=lambda l: order.index(l["id"]) if l["id"] in order else 99)
    bb = [b for b in bounds if b]
    meta = {
        "generated": time.strftime("%Y-%m-%d"),
        "season": a.season,
        "snow_scenario": mc.LATE_SNOW if a.season == "late" else None,
        "units": dict(config.REGIONS),
        "bounds": [[min(b[1] for b in bb), min(b[0] for b in bb)],
                   [max(b[3] for b in bb), max(b[2] for b in bb)]] if bb else None,
        "layers": layers,
    }
    meta_path.write_text(json.dumps(meta, indent=1))
    total = sum(l.get("bytes", 0) for l in layers) / 1e6
    log(f"wrote {meta_path} -- {len(layers)} layers, {total:.0f} MB total")
    if total > SIZE_WARN_MB:
        log(f"  NOTE: over {SIZE_WARN_MB} MB. Set INPUT_MAX_ZOOM = 11 in export_layers.py to "
            "roughly quarter the background-layer size.")


if __name__ == "__main__":
    main()
