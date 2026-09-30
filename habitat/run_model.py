"""Run the habitat model.

    python run_model.py                      # features -> score -> evaluate (late season)
    python run_model.py score evaluate       # after changing weights in model_config.py
    python run_model.py --season early       # archery / 1st rifle version
    python run_model.py train                # once data/habitat/observations.csv exists
    python run_model.py train evaluate --season trained

'features' is the slow step (a few minutes per region); only rerun it when
the fetched data changes. 'score' + 'evaluate' take well under a minute.
"""
import argparse

import model_evaluate
import model_features
import model_score
import model_train
from common import log

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("steps", nargs="*",
                    help="any of: features score evaluate train (default: features score evaluate)")
    ap.add_argument("--season", default="late", choices=["late", "early", "trained", "both"])
    a = ap.parse_args()
    valid = ["features", "score", "evaluate", "train"]
    a.steps = a.steps or ["features", "score", "evaluate"]
    bad = [x for x in a.steps if x not in valid]
    if bad:
        ap.error(f"unknown step(s) {bad}; choose from {valid}")
    seasons = ("late", "early") if a.season == "both" else (a.season,)
    for step in a.steps:
        log(f"===== {step} =====")
        if step == "features":
            model_features.main()
        elif step == "score":
            model_score.main([s for s in seasons if s != "trained"])
        elif step == "evaluate":
            model_evaluate.main(seasons)
        elif step == "train":
            model_train.main([])
