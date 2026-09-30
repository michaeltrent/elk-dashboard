"""Water: NHD springs/seeps, streams, waterbodies, plus Colorado DWR (CDSS)
water-right structures such as decreed springs and stock ponds.

NHD springs mostly came from old USGS topo maps and some have dried up; CDSS
springs have a water right attached, so they're more likely to still flow.
Both are kept and tagged by source so the model can weight them differently.
"""
import warnings
import zipfile
from functools import lru_cache

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely

import config
from common import SESSION, clip_to, get, log, region_aoi, region_dir, write_layer

warnings.filterwarnings("ignore", message="Measured \\(M\\) geometry types")

# NHD flowline FCODEs -> flow permanence
FLOW = {46006: "perennial", 46003: "intermittent", 46007: "ephemeral",
        55800: "artificial_path", 33600: "canal_ditch", 33400: "connector"}


# ---------------------------------------------------------------- NHD files

def _download(url, dest):
    """Stream a file to disk with progress; skip if already cached."""
    if dest.exists() and dest.stat().st_size > 0:
        log(f"  cached {dest.name}")
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    with SESSION.get(url, stream=True, timeout=config.HTTP_TIMEOUT) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done, next_mark = 0, 0.25
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total and done / total >= next_mark:
                    log(f"    {dest.name}: {100 * done // total}%")
                    next_mark += 0.25
    tmp.replace(dest)
    log(f"  downloaded {dest.name} ({done / 1e6:.0f} MB)")
    return dest


def _gpkg_path(zip_path):
    """GDAL path to the GeoPackage inside the zip (read without extracting)."""
    with zipfile.ZipFile(zip_path) as z:
        inner = next(n for n in z.namelist() if n.lower().endswith(".gpkg"))
    return f"/vsizip/{zip_path.resolve().as_posix()}/{inner}"


def _read_layer(gpkg, name, bbox):
    """Read one NHD layer within bbox; layer/column names matched ignoring case."""
    layers = {n.lower(): n for n in pyogrio.list_layers(gpkg)[:, 0]}
    if name.lower() not in layers:
        raise LookupError(f"no layer '{name}' in {gpkg} (has: {sorted(layers.values())})")
    gdf = gpd.read_file(gpkg, layer=layers[name.lower()], bbox=bbox)
    gdf = gdf.rename(columns={c: c.lower() for c in gdf.columns if c != "geometry"})
    if len(gdf):
        gdf["geometry"] = shapely.force_2d(gdf.geometry.values)  # NHD lines carry Z/M
    return gdf


def nhd(region, bbox, poly):
    """Springs, streams, and waterbodies for a region from the HU4 GeoPackages."""
    cache = config.OUT_DIR / "_cache"
    cache.mkdir(parents=True, exist_ok=True)
    out = {"NHDPoint": [], "NHDFlowline": [], "NHDWaterbody": []}
    for hu4 in config.NHD_HU4[region]:
        url = config.NHD_HU4_URL.format(hu4=hu4)
        gpkg = _gpkg_path(_download(url, cache / url.rsplit("/", 1)[-1]))
        for layer in out:
            g = _read_layer(gpkg, layer, bbox)
            log(f"    HU {hu4} {layer}: {len(g):,} in area")
            if len(g):
                out[layer].append(g)

    def merged(key, id_col="permanent_identifier"):
        parts = out[key]
        if not parts:
            return None
        g = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
        if id_col in g:   # features on a watershed boundary appear in both files
            g = g.drop_duplicates(subset=id_col)
        return clip_to(g, poly)

    springs = merged("NHDPoint")
    if springs is not None and len(springs):
        springs = springs[springs["fcode"] == 45800].copy()
        springs["source"] = "NHD"

    streams = merged("NHDFlowline")
    if streams is not None and len(streams):
        streams["permanence"] = streams["fcode"].map(FLOW).fillna("other")

    return springs, streams, merged("NHDWaterbody")


@lru_cache(maxsize=None)
def _county(county):
    rows = get(config.CDSS_STRUCTURES, {"county": county, "format": "json",
                                        "pageSize": 50000}).json().get("ResultList", [])
    log(f"  CDSS {county}: {len(rows):,} structures")
    return pd.DataFrame(rows)


def cdss(bbox, poly):
    frames = [_county(c) for c in config.CDSS_COUNTIES]  # fetched once, reused per region
    df = pd.concat(frames, ignore_index=True)
    if df.empty:
        return None
    df = df.dropna(subset=["latdecdeg", "longdecdeg"])
    w, s, e, n = bbox
    df = df[df.longdecdeg.between(w, e) & df.latdecdeg.between(s, n)]

    stype = df.get("structureType", pd.Series("", index=df.index)).fillna("").str.upper()
    src = df.get("waterSource", pd.Series("", index=df.index)).fillna("").str.upper()
    name = df.get("structureName", pd.Series("", index=df.index)).fillna("").str.upper()
    is_spring = stype.str.contains("SPRING") | src.str.contains("SPRING") | name.str.contains(r"\bSPRING")
    is_pond = stype.str.contains("RESERVOIR|POND") | name.str.contains(r"\bPOND|STOCK|TANK")
    df = df[is_spring | is_pond].copy()
    df["kind"] = ["spring" if s_ else "pond" for s_ in is_spring[df.index]]
    keep = [c for c in ["wdid", "structureName", "structureType", "waterSource",
                        "ciuCode", "kind", "county"] if c in df.columns]
    gdf = gpd.GeoDataFrame(df[keep], geometry=gpd.points_from_xy(df.longdecdeg, df.latdecdeg),
                           crs="EPSG:4326")
    gdf["source"] = "CDSS"
    return clip_to(gdf, poly)


def main():
    for region in config.REGIONS:
        log(f"Water -- {region}")
        poly, bbox = region_aoi(region)
        path = region_dir(region) / "features.gpkg"
        try:
            springs, streams, waterbodies = nhd(region, bbox, poly)
            write_layer(springs, path, "springs_nhd")
            write_layer(streams, path, "streams_nhd")
            write_layer(waterbodies, path, "waterbodies_nhd")
        except Exception as e:
            log(f"  NHD: FAILED ({e})")
        try:
            write_layer(cdss(bbox, poly), path, "water_rights_cdss")
        except Exception as e:
            log(f"  CDSS: FAILED ({e})")


if __name__ == "__main__":
    main()
