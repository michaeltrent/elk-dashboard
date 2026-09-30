"""Score the suitability map against CPW's elk activity layers.

For each CPW layer (winter concentration, severe winter range, ...):
  AUC      - chance a random cell inside the CPW polygon scores higher than a
             random cell outside it. 0.5 = no better than chance; 0.7+ = good.
  lift@10  - how over-represented the CPW polygon is in the model's top 10%
             of cells. 1.0 = no better than chance; 2.0 = twice as common.
Computed for the whole region and per unit.

Late-season (2nd/3rd rifle) elk are moving toward winter range but aren't all
on it yet, so expect winter concentration / severe winter range to score
well, and summer concentration / production areas to score lower. That
contrast is part of the check.

Also scores each factor on its own (model components plus a few raw
features the model doesn't use yet, e.g. elevation) against the same layers,
so you can see which factors pull toward CPW's areas and which pull away:
AUC > 0.5 = higher values sit inside the CPW polygons; < 0.5 = outside.

Output: data/habitat/model_evaluation_<season>.csv
        data/habitat/model_factors_<season>.csv
"""
import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from rasterio.features import rasterize
from scipy.stats import rankdata

import config
import model_config as mc
from common import log, region_dir
from model_features import feat_dir, load

rng = np.random.default_rng(42)


def auc(score, inside):
    n1, n0 = inside.sum(), (~inside).sum()
    if n1 == 0 or n0 == 0:
        return np.nan
    r = rankdata(score)
    return (r[inside].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def metrics(score, inside, sample=300_000):
    if len(score) > sample:
        idx = rng.choice(len(score), sample, replace=False)
        score, inside = score[idx], inside[idx]
    base = inside.mean()
    top = score >= np.quantile(score, 0.9)
    return {"auc": auc(score, inside),
            "lift_top10": (inside[top].mean() / base) if base > 0 else np.nan,
            "pct_area": 100 * base}


def evaluate(region, season):
    """season here is a map name: 'late', 'late_habitat', 'trained', ..."""
    import rasterio
    with rasterio.open(region_dir(region) / "model" / f"suitability_{season}.tif") as f:
        suit, prof = f.read(1), f.profile
    units = load(region, "gmu")
    val_path = config.VALIDATION_DIR / f"{region}_cpw_elk.gpkg"
    have = set(pyogrio.list_layers(val_path)[:, 0]) if val_path.exists() else set()
    rows = []
    for layer in mc.VALIDATION_LAYERS:
        if layer not in have:
            continue
        g = gpd.read_file(val_path, layer=layer).to_crs(config.WORK_CRS)
        g = g[g.geometry.notna() & ~g.geometry.is_empty]
        if g.empty:
            continue
        inside = rasterize(((geom, 1) for geom in g.geometry), out_shape=suit.shape,
                           transform=prof["transform"], fill=0, dtype="uint8") > 0
        for unit in [0] + [u for u in config.REGIONS[region]]:
            m = np.isfinite(suit) & ((units > 0) if unit == 0 else (units == unit))
            if m.sum() < 100:
                continue
            r = metrics(suit[m], inside[m])
            rows.append({"region": region, "map": season, "unit": "all" if unit == 0 else unit,
                         "layer": layer.replace("elk_", ""), **r})
    return rows


RAW_FEATURES = ["elev", "slope", "northness", "tpi1000", "canopy", "forage_nbhd", "cover_nbhd",
                "dist_road_open", "dist_water_reliable", "public"]


def factor_eval(region, season, sample=300_000):
    """AUC of each component and raw feature vs each CPW layer, region-wide."""
    import rasterio
    mdir = region_dir(region) / "model"
    factors = {}
    comp_path = mdir / f"components_{season}.tif"
    if comp_path.exists():
        with rasterio.open(comp_path) as f:
            for i, name in enumerate(f.descriptions, 1):
                factors[f"component: {name}"] = f.read(i)
    for n in RAW_FEATURES:
        if (feat_dir(region) / f"{n}.tif").exists():
            factors[f"raw: {n}"] = load(region, n).astype("float32")
    transform = None
    hunt_label = "== trained map ==" if season == "trained" else "== hunt map =="
    for label, name in (("== habitat map ==", f"{season}_habitat"), (hunt_label, season)):
        p = mdir / f"suitability_{name}.tif"
        if p.exists():
            with rasterio.open(p) as f:
                factors[label] = f.read(1)
                transform = f.transform
    units = load(region, "gmu")
    m = units > 0
    idx = np.flatnonzero(m.ravel())
    if len(idx) > sample:
        idx = rng.choice(idx, sample, replace=False)

    val_path = config.VALIDATION_DIR / f"{region}_cpw_elk.gpkg"
    have = set(pyogrio.list_layers(val_path)[:, 0]) if val_path.exists() else set()
    rows = []
    for layer in mc.VALIDATION_LAYERS:
        if layer not in have:
            continue
        g = gpd.read_file(val_path, layer=layer).to_crs(config.WORK_CRS)
        g = g[g.geometry.notna() & ~g.geometry.is_empty]
        if g.empty:
            continue
        inside = (rasterize(((geom, 1) for geom in g.geometry), out_shape=units.shape,
                            transform=transform, fill=0, dtype="uint8") > 0).ravel()[idx]
        for fname, arr in factors.items():
            v = arr.ravel()[idx]
            ok = np.isfinite(v)
            rows.append({"region": region, "factor": fname, "layer": layer.replace("elk_", ""),
                         "auc": auc(v[ok], inside[ok])})
    return rows


def main(seasons=("late",)):
    for season in seasons:
        rows = []
        for region in config.REGIONS:
            for name in (f"{season}_habitat", season):
                if (region_dir(region) / "model" / f"suitability_{name}.tif").exists():
                    rows += evaluate(region, name)
        if not rows:
            log(f"no {season} suitability maps to evaluate")
            continue
        df = pd.DataFrame(rows)
        out = config.OUT_DIR / f"model_evaluation_{season}.csv"
        df.round(3).to_csv(out, index=False)
        log(f"Evaluation ({season}) -> {out}")
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            for metric in ("auc", "lift_top10"):
                t = df[df.unit == "all"].pivot_table(index="layer", columns=["region", "map"],
                                                    values=metric).round(2)
                print(f"\n{metric.upper()} by map")
                print(t.to_string())

        frows = []
        for region in config.REGIONS:
            mdir = region_dir(region) / "model"
            if any((mdir / f"suitability_{n}.tif").exists() for n in (season, f"{season}_habitat")):
                frows += factor_eval(region, season)
        if frows:
            fd = pd.DataFrame(frows)
            fout = config.OUT_DIR / f"model_factors_{season}.csv"
            fd.round(3).to_csv(fout, index=False)
            with pd.option_context("display.width", 200, "display.max_columns", 20):
                for region in fd.region.unique():
                    t = fd[fd.region == region].pivot_table(index="factor", columns="layer",
                                                            values="auc", sort=False).round(2)
                    print(f"\nPer-factor AUC -- {region} ({season})  "
                          f"[>0.5 = points toward CPW area, <0.5 = away]")
                    print(t.to_string())
            log(f"Factor breakdown -> {fout}")


if __name__ == "__main__":
    main()
