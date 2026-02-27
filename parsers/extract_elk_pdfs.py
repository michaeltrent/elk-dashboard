#!/usr/bin/env python3
"""
Extract elk harvest and population estimate PDFs from CPW ZIP archives.

Usage:
    python extract_elk_pdfs.py <zip_folder> [output_folder]

Example:
    python extract_elk_pdfs.py harvest_reports elk_pdfs
"""

import os
import re
import sys
import zipfile
from pathlib import Path


def is_elk_harvest(name: str) -> bool:
    """Check if filename looks like an elk harvest report."""
    n = name.lower()
    return "elk" in n and ("harvest" in n or "hunting" in n) and n.endswith(".pdf")


def is_elk_population(name: str) -> bool:
    """Check if filename looks like an elk population estimate."""
    n = name.lower()
    return "elk" in n and ("population" in n or "estimate" in n or "status" in n or "herd" in n) and n.endswith(".pdf")


def extract_elk_pdfs(zip_folder: str, output_folder: str = "elk_pdfs"):
    zip_folder = Path(zip_folder)
    output = Path(output_folder)
    harvest_dir = output / "harvest_reports"
    population_dir = output / "population_estimates"
    other_elk_dir = output / "other_elk"

    harvest_dir.mkdir(parents=True, exist_ok=True)
    population_dir.mkdir(parents=True, exist_ok=True)
    other_elk_dir.mkdir(parents=True, exist_ok=True)

    zips = sorted(zip_folder.glob("*.zip"))
    print(f"Found {len(zips)} ZIP files in {zip_folder}\n")

    stats = {"harvest": 0, "population": 0, "other_elk": 0, "skipped": 0}

    for zf_path in zips:
        print(f"--- {zf_path.name} ---")
        try:
            with zipfile.ZipFile(zf_path, "r") as zf:
                for member in zf.namelist():
                    basename = os.path.basename(member)
                    if not basename.lower().endswith(".pdf"):
                        continue

                    if is_elk_harvest(basename):
                        dest = harvest_dir / basename
                        category = "harvest"
                    elif is_elk_population(basename):
                        dest = population_dir / basename
                        category = "population"
                    elif "elk" in basename.lower():
                        dest = other_elk_dir / basename
                        category = "other_elk"
                    else:
                        stats["skipped"] += 1
                        continue

                    # Extract the file
                    data = zf.read(member)
                    dest.write_bytes(data)
                    print(f"  [{category:>10}] {basename}")
                    stats[category] += 1

        except zipfile.BadZipFile:
            print(f"  ERROR: Not a valid ZIP file, skipping")

    print(f"\n{'='*50}")
    print(f"Harvest reports:      {stats['harvest']:>3}  → {harvest_dir}")
    print(f"Population estimates: {stats['population']:>3}  → {population_dir}")
    print(f"Other elk PDFs:       {stats['other_elk']:>3}  → {other_elk_dir}")
    print(f"Non-elk PDFs skipped: {stats['skipped']:>3}")
    print(f"{'='*50}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python extract_elk_pdfs.py <zip_folder> [output_folder]")
        print("       python extract_elk_pdfs.py harvest_reports elk_pdfs")
        sys.exit(1)

    zip_folder = sys.argv[1]
    output_folder = sys.argv[2] if len(sys.argv) > 2 else "elk_pdfs"
    extract_elk_pdfs(zip_folder, output_folder)