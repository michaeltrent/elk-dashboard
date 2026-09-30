"""Score late- (or early-) season elk suitability, 0-100 -- two maps.

Each component maps a feature to 0-1 using the curves in model_config.py;
components are combined as a weighted geometric mean, so one very poor
factor pulls the score down even if the rest look good. Non-habitat (water,
developed, barren) scores 0.

  suitability_<season>_habitat.tif  all components except road security:
                                    where elk want to be (compared with CPW)
  suitability_<season>.tif          habitat + security: where elk will be
                                    under hunting pressure (scout from this)
  components_<season>.tif           one band per component, to see why a
                                    spot scored the way it did
plus quick-look PNGs of both maps.
"""
import numpy as np
import rasterio

import config
import model_config as mc
from common import log, region_dir
from model_features import feat_dir, focal_mean, load, veg_values


def logistic(d, mid, scale):
    return 1 / (1 + np.exp(-(d - mid) / scale))


def components(region, season):
    s = mc.SEASONS[season]
    f = lambda n: load(region, n)
    out = {}

    out["security"] = (logistic(f("dist_road_open"), mc.ROAD_OPEN_MID_M, mc.ROAD_OPEN_SCALE_M)
                       * (0.5 + 0.5 * logistic(f("dist_road_unknown"), mc.ROAD_UNKNOWN_MID_M,
                                               mc.ROAD_UNKNOWN_SCALE_M)))

    # Season-specific forage/cover values, recomputed from vegetation class.
    forage, cover = veg_values(f("veg_class"), f("canopy"),
                               mc.FORAGE_BY_SEASON[season], mc.COVER_BY_SEASON[season])
    fo = np.clip(focal_mean(forage, mc.FORAGE_RADIUS_M) / mc.FORAGE_ENOUGH, 0, 1)
    co = np.clip(focal_mean(cover, mc.COVER_RADIUS_M) / mc.COVER_ENOUGH, 0, 1)
    out["interspersion"] = np.sqrt(fo * co)
    del forage, cover

    # Elevation relative to this region's units (0 = lowest, 1 = highest).
    elev, units = f("elev"), f("gmu")
    inside = elev[(units > 0) & np.isfinite(elev)]
    qs = np.linspace(0, 1, 101)
    pct = np.interp(elev, np.quantile(inside, qs), qs)
    key = f"late_{mc.LATE_SNOW}" if season == "late" else season
    pts = mc.ELEVATION_CURVES[key]
    out["elevation"] = np.interp(pct, [p for p, _ in pts], [v for _, v in pts]).astype("float32")
    out["elevation"][~np.isfinite(elev)] = np.nan

    sl = f("slope")
    out["slope"] = np.clip(1 - (sl - mc.SLOPE_GOOD) / (mc.SLOPE_BAD - mc.SLOPE_GOOD)
                           * (1 - mc.SLOPE_FLOOR), mc.SLOPE_FLOOR, 1)

    # +1 favors south/west (late), -1 north/east (early); range 0.4-1.0
    toward = -(f("northness") * 0.8 + f("eastness") * 0.2) * s["aspect_pref"]
    out["aspect"] = 0.7 + 0.3 * np.clip(toward, -1, 1)

    wr = 0.5 + 0.5 * np.exp(-f("dist_water_reliable") / mc.WATER_RELIABLE_M)
    wa = 0.5 + 0.3 * np.exp(-f("dist_water_any") / mc.WATER_ANY_M)
    out["water"] = np.maximum(wr, wa)

    yrs = f("years_since_fire")
    burn = np.full(yrs.shape, mc.BURN_NONE, "float32")
    lo = -np.inf
    for hi, val in mc.BURN_CURVE:
        burn[(yrs > lo) & (yrs <= hi)] = val
        lo = hi
    out["burn"] = burn

    # Mild preference for benches/sidehills over exposed ridgetops and draw bottoms.
    out["terrain"] = 1 - 0.3 * np.clip(np.abs(f("tpi300")) / 40, 0, 1)
    return out


def combine(comps, weights, names):
    acc = np.zeros_like(comps["security"])
    for name in names:
        acc += weights[name] * np.log(np.clip(np.nan_to_num(comps[name], nan=0.5), 0.01, 1))
    return np.exp(acc / sum(weights[n] for n in names)) * 100


def score(region, season):
    comps = components(region, season)
    w = mc.SEASONS[season]["weights"]
    habitat_names = [n for n in comps if n != "security" and w.get(n, 0) > 0]
    maps = {f"{season}_habitat": combine(comps, w, habitat_names),
            season: combine(comps, w, habitat_names + ["security"])}

    veg = load(region, "veg_class")
    nonhab = veg == mc.VEG_CLASSES.index("nonhab")
    nodata = ~np.isfinite(load(region, "elev"))
    with rasterio.open(feat_dir(region) / "elev.tif") as ref:
        prof = ref.profile
    prof.update(dtype="float32", nodata=np.nan)
    mdir = region_dir(region) / "model"
    for name, suit in maps.items():
        suit[nonhab] = 0
        suit[nodata] = np.nan
        with rasterio.open(mdir / f"suitability_{name}.tif", "w", **prof) as dst:
            dst.write(suit.astype("float32"), 1)
        label = "habitat (no road security)" if name.endswith("_habitat") else "hunt (habitat + security)"
        quicklook(region, name, suit, prof, label)
        v = suit[np.isfinite(suit)]
        log(f"  {name}: median {np.median(v):.0f}, top-10% threshold {np.percentile(v, 90):.0f}")
    cprof = dict(prof, count=len(comps))
    with rasterio.open(mdir / f"components_{season}.tif", "w", **cprof) as dst:
        for i, (name, arr) in enumerate(comps.items(), 1):
            dst.write(arr.astype("float32"), i)
            dst.set_band_description(i, name)


def quicklook(region, season, suit, prof, label=""):
    """PNG map: suitability with unit boundaries and CPW winter concentration
    outlines (outlines are for eyeballing only)."""
    import geopandas as gpd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = prof["transform"]
    ext = (t.c, t.c + t.a * prof["width"], t.f + t.e * prof["height"], t.f)
    fig, ax = plt.subplots(figsize=(10, 10 * prof["height"] / prof["width"]))
    step = max(1, prof["width"] // 2000)
    im = ax.imshow(suit[::step, ::step], extent=ext, cmap="YlOrRd", vmin=0, vmax=100)
    gmu = gpd.read_file(config.OUT_DIR / "gmu_boundaries.gpkg").to_crs(config.WORK_CRS)
    gcol = next(c for c in gmu.columns if c.lower() == "gmuid")
    units = [u for units in config.REGIONS.values() for u in units]
    gmu = gmu[gmu[gcol].astype(int).isin(units)]
    gmu.boundary.plot(ax=ax, color="black", linewidth=1)
    for _, r in gmu.iterrows():
        p = r.geometry.representative_point()
        ax.annotate(str(int(r[gcol])), (p.x, p.y), fontsize=12, weight="bold", ha="center")
    val = config.VALIDATION_DIR / f"{region}_cpw_elk.gpkg"
    try:
        wc = gpd.read_file(val, layer="elk_winter_concentration").to_crs(config.WORK_CRS)
        wc.boundary.plot(ax=ax, color="blue", linewidth=0.6)
    except Exception:
        pass
    ax.set_xlim(ext[0], ext[1]); ax.set_ylim(ext[2], ext[3])
    ax.set_title(f"{region} -- {season.split('_')[0]} season, {label}\n(blue = CPW winter concentration)")
    ax.set_xticks([]); ax.set_yticks([])
    fig.colorbar(im, ax=ax, fraction=0.03, label="suitability")
    out = region_dir(region) / "model" / f"suitability_{season}.png"
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    log(f"  quick-look map -> {out}")


def main(seasons=("late",)):
    for region in config.REGIONS:
        log(f"Score -- {region}")
        if mc.LATE_SNOW != "normal":
            log(f"  late-season snow scenario: {mc.LATE_SNOW}")
        for season in seasons:
            score(region, season)


if __name__ == "__main__":
    main()
