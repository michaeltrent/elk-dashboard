"""Shared helpers: HTTP, ArcGIS REST queries, study-area geometry, output."""
from __future__ import annotations

import json
import time
from functools import lru_cache

import geopandas as gpd
import pandas as pd
import requests

import config

SESSION = requests.Session()
SESSION.headers["User-Agent"] = config.USER_AGENT


def get(url, params=None, retries=4, **kw):
    """GET with retry/backoff. Raises on final failure."""
    for i in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=config.HTTP_TIMEOUT, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if i == retries - 1:
                raise
            time.sleep(2 ** i * 2)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --- ArcGIS REST ---------------------------------------------------------------

@lru_cache(maxsize=None)
def layer_info(layer_url):
    return get(layer_url, {"f": "json"}).json()


def _esri_json_to_gdf(data, sr_wkid):
    """Convert Esri JSON features to a GeoDataFrame (fallback when a server
    won't return GeoJSON)."""
    from shapely.geometry import LineString, MultiLineString, Point, Polygon, MultiPolygon

    rows, geoms = [], []
    for f in data.get("features", []):
        g = f.get("geometry") or {}
        if "x" in g:
            geom = Point(g["x"], g["y"])
        elif "paths" in g:
            parts = [LineString(p) for p in g["paths"] if len(p) > 1]
            geom = parts[0] if len(parts) == 1 else MultiLineString(parts)
        elif "rings" in g:
            # Esri rings: outer rings clockwise, holes counter-clockwise.
            polys = []
            for ring in g["rings"]:
                if len(ring) < 4:
                    continue
                area = sum(ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
                           for i in range(len(ring) - 1))
                if area <= 0 or not polys:          # clockwise => outer ring
                    polys.append([ring, []])
                else:
                    polys[-1][1].append(ring)
            geom = MultiPolygon([Polygon(o, h) for o, h in polys]) if polys else None
        else:
            geom = None
        rows.append(f.get("attributes", {}))
        geoms.append(geom)
    return gpd.GeoDataFrame(rows, geometry=geoms, crs=f"EPSG:{sr_wkid}")


class ServerBusy(Exception):
    """Server timed out or errored on a request -- usually because the area
    asked for is too big. The caller splits the box and tries smaller pieces."""


def _query(layer_url, bbox, where, out_fields, count_only=False, simplify=None):
    w, s, e, n = bbox
    params = {
        "where": where,
        "geometry": f"{w},{s},{e},{n}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields,
        "outSR": 4326,
        "returnGeometry": "true",
        "f": "geojson",
    }
    if simplify:  # generalize big polygons server-side (degrees; 0.0001 ~ 10 m)
        params.update(maxAllowableOffset=simplify, geometryPrecision=6)
    if count_only:
        params.update(returnCountOnly="true", f="json")
    for attempt in range(6):
        try:
            data = get(f"{layer_url}/query", params, retries=2).json()
        except requests.HTTPError as ex:
            code = ex.response.status_code if ex.response is not None else 0
            if code == 429:
                time.sleep(61); continue
            if code >= 500:
                raise ServerBusy(str(ex)) from ex
            raise
        except (requests.Timeout, requests.ConnectionError) as ex:
            raise ServerBusy(str(ex)) from ex
        err = data.get("error") if isinstance(data, dict) else None
        if err:
            if err.get("code") == 429:          # ArcGIS Online quota: wait it out
                log("  rate limited by server -- waiting 61 s")
                time.sleep(61); continue
            if err.get("code", 0) >= 500:
                raise ServerBusy(str(err))
            raise RuntimeError(f"{layer_url}: {err}")
        return data
    raise RuntimeError(f"{layer_url}: still rate limited after retries")


def _split(bbox):
    w, s, e, n = bbox
    mx, my = (w + e) / 2, (s + n) / 2
    return [(w, s, mx, my), (mx, s, e, my), (w, my, mx, n), (mx, my, e, n)]


def _grid(bbox, step):
    w, s, e, n = bbox
    ys = [s + i * step for i in range(int((n - s) / step) + 1)] + [n]
    xs = [w + i * step for i in range(int((e - w) / step) + 1)] + [e]
    return [(x0, y0, x1, y1) for y0, y1 in zip(ys, ys[1:]) for x0, x1 in zip(xs, xs[1:])
            if x1 > x0 and y1 > y0]


def _concat(parts, id_field):
    parts = [p for p in parts if p is not None and len(p)]
    if not parts:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    gdf = pd.concat(parts, ignore_index=True)
    if id_field in gdf.columns:
        gdf = gdf.drop_duplicates(subset=id_field)
    return gpd.GeoDataFrame(gdf, geometry="geometry", crs="EPSG:4326")


def arcgis_query(layer_url, bbox, where="1=1", out_fields="*", id_field=None,
                 tile_deg=None, simplify=None, depth=0):
    """Fetch every feature intersecting bbox (WGS84 W,S,E,N).

    - tile_deg: pre-split big areas into a grid of this many degrees. Use it
      for heavy layers (stream lines, PAD-US polygons) that time out when
      asked about a whole region at once.
    - If a count exceeds the server's page limit, or the server times out,
      the box is split into quadrants and retried recursively. Features that
      span a split line are de-duplicated by object id.
    - Rate-limit responses (HTTP/JSON 429) pause 61 s and retry.
    """
    info = layer_info(layer_url)
    max_rec = min(int(info.get("maxRecordCount") or 1000), 2000)
    id_field = id_field or info.get("objectIdField") or "OBJECTID"

    if tile_deg and depth == 0:
        tiles = _grid(bbox, tile_deg)
        log(f"  querying {len(tiles)} tiles of {tile_deg} deg")
        parts = []
        for i, t in enumerate(tiles, 1):
            parts.append(arcgis_query(layer_url, t, where, out_fields, id_field,
                                      simplify=simplify, depth=1))
            if i % 10 == 0 or i == len(tiles):
                log(f"    {i}/{len(tiles)} tiles done")
        return _concat(parts, id_field)

    def split(reason="too many features"):
        # Timeouts get at most 3 levels of splitting, so an overloaded server
        # fails in minutes (and says so) instead of hanging.
        limit = 10 if reason == "too many features" else 3
        if depth >= limit:
            raise RuntimeError(f"{layer_url}: server kept failing even on small boxes")
        if reason != "too many features":
            log(f"    server busy (level {depth}); splitting box")
        return _concat([arcgis_query(layer_url, b, where, out_fields, id_field,
                                     simplify=simplify, depth=depth + 1)
                        for b in _split(bbox)], id_field)

    try:
        n = _query(layer_url, bbox, where, out_fields, count_only=True).get("count", 0)
    except ServerBusy:
        return split("busy")
    if n == 0:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    if n > max_rec:
        return split()
    try:
        data = _query(layer_url, bbox, where, out_fields, simplify=simplify)
    except ServerBusy:
        return split("busy")
    if data.get("type") == "FeatureCollection":
        return gpd.GeoDataFrame.from_features(data["features"], crs="EPSG:4326")
    return _esri_json_to_gdf(data, 4326)


def find_layer(service_url, name_contains):
    """Return the URL of the first layer whose name contains the text."""
    info = get(service_url, {"f": "json"}).json()
    for lyr in info.get("layers", []):
        if name_contains.lower() in lyr["name"].lower():
            return f"{service_url}/{lyr['id']}"
    raise LookupError(f"No layer containing '{name_contains}' at {service_url}")


# --- Study area ----------------------------------------------------------------

def load_units():
    """GMU polygons for every configured unit, in WORK_CRS.

    Uses the full-resolution CPW boundary once fetch_boundaries.py has run;
    otherwise falls back to the simplified file the dashboard ships with.
    """
    full = config.OUT_DIR / "gmu_boundaries.gpkg"
    if full.exists():
        gdf = gpd.read_file(full)
    else:
        # The simplified file has a few degenerate rings; repair on read.
        gdf = gpd.read_file(config.SLIM_GMU_GEOJSON, on_invalid="fix")
        gdf["geometry"] = gdf.geometry.make_valid()
    # write_layer() lower-cases columns, so the saved file has 'gmuid'.
    gdf = gdf.rename(columns={c: "GMUID" for c in gdf.columns if c.upper() == "GMUID"})
    wanted = {u for units in config.REGIONS.values() for u in units}
    gdf = gdf[gdf["GMUID"].astype(int).isin(wanted)].copy()
    gdf["GMUID"] = gdf["GMUID"].astype(int)
    gdf["region"] = gdf["GMUID"].map({u: r for r, us in config.REGIONS.items() for u in us})
    return gdf.to_crs(config.WORK_CRS)


def region_aoi(region):
    """(buffered polygon in WORK_CRS, WGS84 bbox W,S,E,N) for a region."""
    units = load_units()
    poly = units[units.region == region].union_all().buffer(config.AOI_BUFFER_M)
    bbox = gpd.GeoSeries([poly], crs=config.WORK_CRS).to_crs(4326).total_bounds
    return poly, tuple(float(v) for v in bbox)


def fix_polygons(gdf):
    """Repair invalid polygons (self-intersections, 'side location conflict').

    Agency polygons -- PAD-US, CPW ranges -- are often slightly invalid, and
    server-side simplification can make it worse. make_valid() fixes them but
    may return a mix of polygons and stray lines; buffer(0) keeps only the
    polygon area.
    """
    if gdf is None or gdf.empty:
        return gdf
    bad = ~gdf.geometry.is_valid
    if bad.any():
        polys = gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"]) & bad
        gdf = gdf.copy()
        gdf.loc[polys, "geometry"] = gdf.loc[polys, "geometry"].make_valid().buffer(0)
        other = bad & ~polys
        gdf.loc[other, "geometry"] = gdf.loc[other, "geometry"].make_valid()
        gdf = gdf[~gdf.geometry.is_empty]
    return gdf


def clip_to(gdf, poly):
    """Reproject to WORK_CRS, repair invalid geometry, clip to the region."""
    if gdf is None or gdf.empty:
        return gdf
    gdf = gdf.to_crs(config.WORK_CRS)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    return gpd.clip(fix_polygons(gdf), poly)


def region_dir(region):
    d = config.OUT_DIR / region
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_layer(gdf, path, layer):
    """Write a layer to a GeoPackage; lower-case columns for consistency."""
    if gdf is None or gdf.empty:
        log(f"  {layer}: 0 features (nothing written)")
        return
    gdf = gdf.rename(columns={c: c.lower() for c in gdf.columns if c != "geometry"})
    # GeoPackage can't store python objects/lists; stringify them.
    for c in gdf.columns:
        if c != "geometry" and gdf[c].dtype == object:
            gdf[c] = gdf[c].map(lambda v: json.dumps(v) if isinstance(v, (list, dict)) else v)
    gdf.to_file(path, layer=layer, driver="GPKG")
    log(f"  {layer}: {len(gdf):,} features -> {path.name}")
