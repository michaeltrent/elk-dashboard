"""Vegetation and canopy from the LANDFIRE Product Service (30 m).

  EVT - Existing Vegetation Type: forage classes (aspen, meadow, oakbrush,
        sagebrush, mixed conifer...)
  EVC - Existing Vegetation Cover
  CC  - Forest Canopy Cover: cover vs. openings, edge

LFPS runs as an async job: submit -> poll -> download zip. Requires an email:
    export LFPS_EMAIL=you@example.com
"""
import io
import os
import time
import zipfile

import config
from common import get, log, region_aoi, region_dir


def run_job(bbox, email):
    w, s, e, n = bbox
    params = {
        "Email": email,
        "Layer_List": ";".join(config.LANDFIRE_LAYERS),
        "Area_of_Interest": f"{w:.4f} {s:.4f} {e:.4f} {n:.4f}",
        "Output_Projection": config.WORK_CRS.split(":")[1],
    }
    job = get(config.LFPS_SUBMIT, params).json()
    job_id = job.get("jobId") or job.get("JobId")
    if not job_id:
        raise RuntimeError(f"LFPS submit failed: {job}")
    log(f"  LFPS job {job_id} submitted")
    while True:
        time.sleep(20)
        st = get(config.LFPS_STATUS, {"JobId": job_id}).json()
        status = st.get("status", "")
        if status == "Succeeded":
            url = st.get("outputFile") or (st.get("results") or {}).get("Output_File")
            if isinstance(url, dict):
                url = url.get("url") or url.get("value")
            if not url:
                raise RuntimeError(f"LFPS finished but no output URL in: {st}")
            return url
        if status in ("Failed", "Canceled", "Timed out"):
            raise RuntimeError(f"LFPS job {status}: {st.get('messages')}\n"
                               "If the error mentions a layer name, update LANDFIRE_LAYERS in "
                               "config.py from https://lfps.usgs.gov/products")
        log(f"  {status or 'waiting'} (queue {st.get('queuePosition', '?')})")


def main():
    email = os.environ.get(config.LFPS_EMAIL_ENV)
    if not email:
        raise SystemExit(f"Set {config.LFPS_EMAIL_ENV} to your email first (LFPS requires it).")
    for region in config.REGIONS:
        log(f"LANDFIRE -- {region}")
        _, bbox = region_aoi(region)
        url = run_job(bbox, email)
        z = zipfile.ZipFile(io.BytesIO(get(url).content))
        d = region_dir(region) / "landfire"
        d.mkdir(exist_ok=True)
        z.extractall(d)
        log(f"  extracted {len(z.namelist())} files -> {d}")
        # The .tif is multi-band in LANDFIRE_LAYERS order; the .csv/.xml files
        # hold the EVT class names the model step uses to build forage classes.


if __name__ == "__main__":
    main()
