import sys, json, random, tempfile
from pathlib import Path
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np, geopandas as gpd, pandas as pd
from shapely.geometry import box, Point, LineString, mapping
import config, common

tmp = Path(tempfile.mkdtemp())
config.OUT_DIR = tmp / "habitat"; config.VALIDATION_DIR = tmp / "val"

# 1. study area from slim boundaries
u = common.load_units()
assert sorted(u.GMUID) == [21,22,28,30,31,32,37,371], u.GMUID.tolist()
# regression: boundaries saved by write_layer (lower-cased columns) must reload
config.OUT_DIR.mkdir(parents=True, exist_ok=True)
common.write_layer(u.drop(columns="region"), config.OUT_DIR / "gmu_boundaries.gpkg", "gmu")
u2 = common.load_units()
assert sorted(u2.GMUID) == sorted(u.GMUID) and "region" in u2
print("saved-boundary reload OK")
for r in config.REGIONS:
    poly, bb = common.region_aoi(r); print(r, [round(v,3) for v in bb], round(poly.area/2.59e6), "sq mi buffered")

# 2. fake ArcGIS server: 23 random points, maxRecordCount 5 -> must split & dedupe
random.seed(1)
PTS = [dict(OBJECTID=i, FCODE=45800 if i%2 else 48800, x=-108.6+random.random()*0.5, y=39.2+random.random()*0.4) for i in range(23)]
PTS.append(dict(OBJECTID=99, FCODE=45800, x=-108.35, y=39.4))  # likely on split line
class R:
    def __init__(s, d): s.d=d
    def json(s): return s.d
import requests
CALLS = {"429": 0, "504": 0}
class Resp504:
    status_code = 504
def fake_get(url, params=None, **k):
    if params.get("f")=="json" and not url.endswith("/query"):
        return R({"maxRecordCount":5,"objectIdField":"OBJECTID"})
    w,s_,e,n = map(float, params["geometry"].split(","))
    if CALLS["429"] == 0:                       # first query: quota error
        CALLS["429"] += 1
        return R({"error": {"code": 429, "message": "quota"}})
    if (e-w) > 0.3:                             # big boxes: gateway timeout
        CALLS["504"] += 1
        err = requests.HTTPError("504"); err.response = Resp504(); raise err
    sel=[p for p in PTS if w<=p["x"]<=e and s_<=p["y"]<=n and ("45800" not in params["where"] or p["FCODE"]==45800)]
    if params.get("returnCountOnly"): return R({"count":len(sel)})
    assert len(sel)<=5, "server limit exceeded"
    return R({"type":"FeatureCollection","features":[{"type":"Feature","geometry":mapping(Point(p["x"],p["y"])),"properties":{k:v for k,v in p.items() if k not in "xy"}} for p in sel]})
common.get = fake_get; common.layer_info.cache_clear()
common.time.sleep = lambda s: None  # skip the 61 s rate-limit wait in tests
g = common.arcgis_query("http://fake/0", (-108.6,39.2,-108.1,39.6))
assert len(g)==24 and g.OBJECTID.is_unique, len(g)
assert CALLS["429"]==1 and CALLS["504"]>0, CALLS
g3 = common.arcgis_query("http://fake/2", (-108.6,39.2,-108.1,39.6), tile_deg=0.2)
assert len(g3)==24 and g3.OBJECTID.is_unique
print("rate-limit wait, 504 split, and pre-tiling OK;", CALLS)
g2 = common.arcgis_query("http://fake/1", (-108.6,39.2,-108.1,39.6), where="FCODE = 45800")
assert len(g2)==sum(p["FCODE"]==45800 for p in PTS)
print("arcgis split/dedupe OK:", len(g), "and springs", len(g2))

# 3. MVUM date logic (hunt window Oct 24 - Nov 15)
import fetch_roads as fr
cases = {"01/01-12/31":True, "06/15-10/15":False, "06/15-11/30":True, "12/01-04/30":False,
         "05/01-06/30,10/20-11/05":True, "":None, None:None, "11/16-10/23":False}
for k,v in cases.items(): assert fr.open_during_hunt(k) is v, (k, fr.open_during_hunt(k))
print("date logic OK")

# 4. BLM + MVUM status mapping
L = LineString([(-108.3,39.3),(-108.29,39.31)])
blm = gpd.GeoDataFrame({"PLAN_MODE_TRNSPRT":["Motorized","Motorized","Motorized","Non-Motorized"],
    "PLAN_OHV_ROUTE_DSGNTN":["Open","Closed","Limited","Open"],"PLAN_SEASON_RSTRCT_CODE":["NO","NO","YES","NO"],
    "PLAN_ALLOW_MODE_TRNSPRT":["ALL_MOTO_VEH"]*3+["HIK_ONLY"]}, geometry=[L]*4, crs=4326)
fr.arcgis_query = lambda *a, **k: blm
b = fr.blm_routes(None); assert list(b.open_hunt)==[True,False,None,False], list(b.open_hunt)
mv = gpd.GeoDataFrame({"NAME":["a","b","c","d"],"PASSENGERVEHICLE":["open",None,"open",None],
    "PASSENGERVEHICLE_DATESOPEN":["01/01-12/31",None,"06/01-10/15",None],
    "ATV":[None,"open",None,None],"ATV_DATESOPEN":[None,"weird",None,None]}, geometry=[L]*4, crs=4326)
fr.arcgis_query = lambda *a, **k: mv
m = fr.mvum("x", None, "ROAD"); assert list(m.open_hunt)==[True,None,False,False] and list(m.motorized)==[True,True,True,False], m[["open_hunt","motorized"]]
print("road status OK")

# 5. public-land %: fake PAD-US polygon = west half of unit 31 (BLM) + overlapping wilderness
import fetch_public_land as fp
u31 = u[u.GMUID==31].to_crs(4326).geometry.iloc[0]
w,s_,e,n = u31.bounds; mid=(w+e)/2
from shapely.geometry import Polygon
bowtie = Polygon([(mid-0.05, s_+0.1), (mid-0.02, s_+0.13), (mid-0.05, s_+0.13), (mid-0.02, s_+0.1)])  # self-intersecting, inside the BLM box
assert not bowtie.is_valid
pad = gpd.GeoDataFrame({"MngNm_Desc":["Bureau of Land Management","Bureau of Land Management","Private","Bureau of Land Management"],
    "Pub_Access":["OA","OA","XA","OA"]}, geometry=[box(w-1,s_-1,mid,n+1), box(w-1,s_-1,mid-0.1,n+1), box(mid,s_,e,n), bowtie], crs=4326)
fp.arcgis_query = lambda *a, **k: pad
fp.main()
df = pd.read_csv(config.OUT_DIR/"unit_public_land.csv"); print(df.to_string(index=False))
r31 = df[df.GMUID==31].iloc[0]
bx = gpd.GeoSeries([box(w-1,s_-1,mid,n+1)],crs=4326).to_crs(config.WORK_CRS).iloc[0]
u31m = u[u.GMUID==31].geometry.iloc[0]
exact = u31m.intersection(bx).area/u31m.area*100
assert abs(r31.pct_public-exact)<0.2, (r31.pct_public, exact)   # overlap not double-counted, XA excluded
print(f"public land pct OK (expected {exact:.1f})")
print("layers:", gpd.list_layers(config.OUT_DIR/"yellow_creek"/"features.gpkg").name.tolist())

# 5b. NHD from cached watershed GeoPackages (zip, Z geometries, mixed-case names, cross-HU duplicate)
import zipfile, fetch_water as fw
cache = config.OUT_DIR / "_cache"; cache.mkdir(parents=True, exist_ok=True)
from shapely.geometry import LineString as LS
def make_hu(hu, extra):
    gp = tmp / f"NHD_H_{hu}_HU4_GPKG.gpkg"
    pts = gpd.GeoDataFrame({"FCode":[45800,48800,45800], "Permanent_Identifier":[f"p{hu}a",f"p{hu}b",f"p{hu}c"]},
        geometry=[Point(-108.4,39.4,2000), Point(-108.41,39.41,2000), Point(-100,45,0)], crs=4269)
    fl = gpd.GeoDataFrame({"FCode":[46006,46003,46007], "Permanent_Identifier":["shared",f"f{hu}",f"g{hu}"]},
        geometry=[LS([(-108.3,39.3,1),(-108.2,39.35,1)]), LS([(-108.5,39.5,1),(-108.45,39.55,1)]), LS([(-100,45,0),(-100.1,45,0)])], crs=4269)
    wb = gpd.GeoDataFrame({"FCode":[39004], "Permanent_Identifier":[f"w{hu}"]}, geometry=[box(-108.36,39.36,-108.35,39.37)], crs=4269)
    pts.to_file(gp, layer="NHDPoint"); fl.to_file(gp, layer="NHDFlowline"); wb.to_file(gp, layer="NHDWaterbody")
    with zipfile.ZipFile(cache / f"NHD_H_{hu}_HU4_GPKG.zip", "w") as z: z.write(gp, gp.name)
for hu in config.NHD_HU4["yellow_creek"]: make_hu(hu, None)
poly, bb = common.region_aoi("yellow_creek")
sp, st, wbs = fw.nhd("yellow_creek", bb, poly)
assert len(sp) == 3 and set(sp.fcode) == {45800}, sp               # one in-area spring per HU, wells dropped
assert len(st) == 1 + 3 and st.permanent_identifier.is_unique      # 'shared' deduped across HUs
assert set(st.permanence) == {"perennial", "intermittent"} and not st.geometry.has_z.any()
assert len(wbs) == 3
print("NHD file reader OK:", len(sp), "springs,", len(st), "streams,", len(wbs), "waterbodies")

# 6. terrain: two synthetic local 'tiles', stitched + reprojected
import rasterio, fetch_terrain as ft
from rasterio.transform import from_origin
tdir = tmp/"tiles"; tdir.mkdir()
for lat,lon in [(40,109),(40,108),(41,109),(41,108),(40,110),(41,110)]:
    p = tdir/f"n{lat}w{lon}.tif"; N=360
    with rasterio.open(p,"w",driver="GTiff",width=N,height=N,count=1,dtype="float32",crs="EPSG:4269",
        transform=from_origin(-lon, lat, 1/N, 1/N), nodata=-999999) as d:
        d.write((np.arange(N*N,dtype="float32").reshape(N,N)%1000+2000),1)
config.DEP_TILE_URL = str(tdir)+"/n{lat:02d}w{lon:03d}.tif"
_open = rasterio.open
ft.rasterio.open = lambda p,*a,**k: _open(str(p).replace("/vsicurl/",""),*a,**k)
config.REGIONS = {"yellow_creek": config.REGIONS["yellow_creek"]}
print("tiles:", [Path(t).name for t in ft.tiles_for(common.region_aoi("yellow_creek")[1])])
import resource
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss//1024
ft.main()
print("peak memory MB before/after terrain:", before, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss//1024)
with _open(config.OUT_DIR/"yellow_creek"/"dem_10m.tif") as d:
    a=d.read(1,masked=True,out_shape=(d.height//20,d.width//20)); print("DEM", d.crs, d.res, d.shape, "range", a.min(), a.max(), "valid %.0f%%"%(100*a.count()/a.size))
    assert d.res==(10.0,10.0) and a.min()>=2000
print("ALL TESTS PASSED")
