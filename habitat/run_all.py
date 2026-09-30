"""Run every fetcher in order. Each step logs and continues on failure, so a
single slow or down service doesn't cost you the rest of the run.

    python run_all.py              # everything
    python run_all.py roads water  # just some steps
"""
import sys
import traceback

import fetch_boundaries, fetch_fire, fetch_landfire, fetch_public_land, fetch_roads
import fetch_terrain, fetch_validation, fetch_water
from common import log

STEPS = {
    "boundaries": fetch_boundaries.main,   # first: everything else clips to these
    "roads": fetch_roads.main,
    "water": fetch_water.main,
    "public_land": fetch_public_land.main,
    "terrain": fetch_terrain.main,
    "landfire": fetch_landfire.main,
    "fire": fetch_fire.main,
    "validation": fetch_validation.main,   # kept separate from model inputs
}

if __name__ == "__main__":
    wanted = sys.argv[1:] or list(STEPS)
    failed = []
    for name in wanted:
        log(f"===== {name} =====")
        try:
            STEPS[name]()
        except SystemExit as e:
            log(f"{name} skipped: {e}")
            failed.append(name)
        except Exception:
            traceback.print_exc()
            failed.append(name)
    log("done" + (f" -- failed/skipped: {', '.join(failed)}" if failed else ""))
