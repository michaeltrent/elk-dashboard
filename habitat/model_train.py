"""Train the model from field observations (resource selection function).

Put elk locations in data/habitat/observations.csv:

    lat,lon,date,kind,count,observer,notes
    39.4123,-108.2211,2025-11-02,sighting,14,Mike,bedded on bench
    39.3871,-108.1502,2024-10-28,harvest,1,Dave,

Only lat/lon are required. One point per herd sighting is enough; kind can
be sighting / harvest / sign (fresh tracks, beds, rubs). Stick to the season
you're modeling (e.g., Oct 20 - Nov 20 for late) -- use --from/--to to filter
by date.

The script compares the habitat at those points with 10,000 random points in
the same units (logistic regression = a resource selection function), checks
it with cross-validation that holds out whole units (so it's graded on units
it didn't learn from), then writes a trained map:
    <region>/model/suitability_trained.tif  (0-100 percentile rank)
and prints which factors the elk actually selected for.
"""
import argparse
import json

import numpy as np
import pandas as pd
import rasterio

import config
import model_config as mc
from common import log, region_dir
from model_features import feat_dir, load

rng = np.random.default_rng(7)


def design(cols):
    """Transform raw feature columns into model inputs."""
    X, names = [], []
    for n, v in cols.items():
        v = v.astype("float64")
        if n in mc.LOG_FEATURES:
            v = np.log1p(np.clip(v, 0, None))
        if n == "years_since_fire":
            v = np.where(np.isfinite(v), np.minimum(v, 40), 60)   # never burned -> 60
        X.append(v); names.append(n)
        if n in mc.QUADRATIC:
            X.append(v ** 2); names.append(f"{n}^2")
    return np.column_stack(X), names


def region_points(region, obs):
    with rasterio.open(feat_dir(region) / "elev.tif") as f:
        prof = f.profile
    feats = {n: load(region, n) for n in mc.TRAIN_FEATURES}
    units = load(region, "gmu")
    H, W = units.shape
    pts = obs.to_crs(config.WORK_CRS)
    inv = ~prof["transform"]
    cols, rows = inv * (pts.geometry.x.values, pts.geometry.y.values)
    r, c = np.floor(rows).astype(int), np.floor(cols).astype(int)
    ok = (r >= 0) & (r < H) & (c >= 0) & (c < W)
    pres = pd.DataFrame({"r": r[ok], "c": c[ok]}).drop_duplicates()
    pres = pres[units[pres.r, pres.c] > 0]

    avail = np.flatnonzero((units > 0).ravel())
    nb = min(len(avail), mc.BACKGROUND_POINTS)
    bg = rng.choice(avail, nb, replace=False)
    br, bc = np.unravel_index(bg, units.shape)

    def sample(rr, cc):
        return {n: a[rr, cc] for n, a in feats.items()}

    P, B = sample(pres.r.values, pres.c.values), sample(br, bc)
    return (P, units[pres.r, pres.c], B, units[br, bc], feats, prof)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(config.OUT_DIR / mc.OBSERVATIONS_CSV))
    ap.add_argument("--from", dest="date_from")
    ap.add_argument("--to", dest="date_to")
    a = ap.parse_args(argv)

    import geopandas as gpd
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    df = pd.read_csv(a.csv)
    df.columns = [c.lower() for c in df.columns]
    if "date" in df and (a.date_from or a.date_to):
        d = pd.to_datetime(df["date"], errors="coerce")
        md = d.dt.strftime("%m-%d")
        if a.date_from:
            df = df[md >= a.date_from]
        if a.date_to:
            df = df[md <= a.date_to]
    obs = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.lon, df.lat), crs=4326)
    log(f"{len(obs)} observations loaded")

    Xs, ys, gs, per_region = [], [], [], {}
    for region in config.REGIONS:
        P, pu, B, bu, feats, prof = region_points(region, obs)
        n = len(pu)
        log(f"  {region}: {n} observation cells inside units")
        per_region[region] = (feats, prof)
        if n == 0:
            continue
        for part, units, y in ((P, pu, 1), (B, bu, 0)):
            X, names = design(part)
            Xs.append(X); ys.append(np.full(len(X), y)); gs.append(units)
    if not ys or sum(int(y.sum()) for y in ys) < mc.MIN_OBSERVATIONS:
        raise SystemExit(f"Need at least {mc.MIN_OBSERVATIONS} observations inside the units to train.")
    X, y, g = np.vstack(Xs), np.concatenate(ys), np.concatenate(gs)
    keep = np.all(np.isfinite(X), axis=1)
    X, y, g = X[keep], y[keep], g[keep]

    model = lambda: make_pipeline(StandardScaler(),
                                  LogisticRegression(C=1.0, class_weight="balanced", max_iter=5000))
    units_with_obs = np.unique(g[y == 1])
    if len(units_with_obs) >= 3:
        folds = GroupKFold(n_splits=min(5, len(units_with_obs)))
        aucs = []
        for tr, te in folds.split(X, y, g):
            if y[te].min() == y[te].max():
                continue
            m = model().fit(X[tr], y[tr])
            aucs.append(roc_auc_score(y[te], m.predict_proba(X[te])[:, 1]))
        log(f"Held-out-unit AUC: {np.mean(aucs):.2f} (per fold: {', '.join(f'{v:.2f}' for v in aucs)})")
    else:
        log("Fewer than 3 units have observations -- skipping held-out-unit validation.")

    m = model().fit(X, y)
    coef = dict(zip(names, m[-1].coef_[0].round(3)))
    log("Selection (standardized coefficients; + = elk chose more of it):")
    for k, v in sorted(coef.items(), key=lambda kv: -abs(kv[1])):
        print(f"    {k:22s} {v:+.2f}")
    (config.OUT_DIR / "model_trained_coefficients.json").write_text(json.dumps(coef, indent=2))

    for region, (feats, prof) in per_region.items():
        H, W = next(iter(feats.values())).shape
        out = np.full((H, W), np.nan, "float32")
        for r0 in range(0, H, 256):
            block = {n: a[r0:r0 + 256].ravel() for n, a in feats.items()}
            Xb, _ = design(block)
            ok = np.all(np.isfinite(Xb), axis=1)
            p = np.full(len(Xb), np.nan)
            if ok.any():
                p[ok] = m.predict_proba(Xb[ok])[:, 1]
            out[r0:r0 + 256] = p.reshape(-1, W)
        v = out[np.isfinite(out)]
        pct = np.full_like(out, np.nan)
        pct[np.isfinite(out)] = 100 * (np.searchsorted(np.sort(v), v) / len(v))
        veg = load(region, "veg_class")
        pct[veg == mc.VEG_CLASSES.index("nonhab")] = 0
        prof = dict(prof, dtype="float32", nodata=np.nan)
        with rasterio.open(region_dir(region) / "model" / "suitability_trained.tif", "w", **prof) as f:
            f.write(pct.astype("float32"), 1)
        log(f"  {region}: wrote suitability_trained.tif")


if __name__ == "__main__":
    main()
