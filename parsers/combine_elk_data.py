#!/usr/bin/env python3
"""
CPW Elk Data Combiner
Merges elk harvest, DAU summary, and population estimate data into a single
JSON file optimized for the web frontend.

Usage:
    python combine_elk_data.py --harvest-dir output/harvest --population-dir output/population -o output/combined

    Or point at directories containing the per-year JSON files from the parsers:
    python combine_elk_data.py --harvest-dir harvest_output --population-dir pop_output

Inputs:
    - elk_harvest_YYYY.json files (from parse_elk_harvest.py)
    - elk_population_YYYY.json files (from parse_elk_population.py)

Output:
    - elk_combined.json  (single file for the web app)
    - elk_combined_slim.json  (harvest_rec_days + all/all only, for lightweight map view)
"""

import json
import os
import sys
import logging
from pathlib import Path
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def load_json_files(directory: str, pattern: str) -> list:
    """Load all JSON files matching a glob pattern from a directory."""
    d = Path(directory)
    if not d.exists():
        log.warning(f"Directory not found: {directory}")
        return []
    
    files = sorted(d.glob(pattern))
    results = []
    for f in files:
        with open(f) as fh:
            results.append(json.load(fh))
        log.info(f"  Loaded: {f.name}")
    return results


def build_dau_definitions(population_data: list) -> dict:
    """Build a unified DAU definition table from population estimates.
    
    Uses the most recent year's data for herd names and GMU assignments,
    and tracks any historical changes in GMU membership.
    """
    # Collect all DAU info across years
    dau_info = {}  # dau_id -> { years data }
    
    for pdata in population_data:
        year = pdata['year']
        for rec in pdata['records']:
            dau = rec['dau']
            if dau not in dau_info:
                dau_info[dau] = {
                    'dau': dau,
                    'dau_number': rec['dau_number'],
                    'herd_name': None,
                    'gmus': [],
                    'gmu_history': {},
                    'years_present': [],
                }
            
            entry = dau_info[dau]
            entry['years_present'].append(year)
            
            # Always take the latest herd name
            if rec.get('herd_name'):
                entry['herd_name'] = rec['herd_name']
            
            # Track GMU membership by year
            if rec['gmus']:
                entry['gmu_history'][year] = sorted(rec['gmus'])
    
    # For each DAU, set current GMUs from most recent year
    # and flag if GMU membership has changed over time
    for dau, info in dau_info.items():
        if info['gmu_history']:
            latest_year = max(info['gmu_history'].keys())
            info['gmus'] = info['gmu_history'][latest_year]
            
            # Check if GMUs have been stable
            unique_configs = set(tuple(g) for g in info['gmu_history'].values())
            if len(unique_configs) <= 1:
                # Stable — no need to keep history
                del info['gmu_history']
            else:
                # Changed — keep history for reference
                pass
        
        info['years_present'] = sorted(info['years_present'])
    
    return dau_info


def build_gmu_to_dau_lookup(dau_definitions: dict) -> dict:
    """Build a reverse lookup: GMU → DAU for the current (latest) mapping."""
    lookup = {}
    for dau, info in dau_definitions.items():
        for gmu in info.get('gmus', []):
            lookup[gmu] = dau
    return lookup


def combine_data(harvest_dir: str, population_dir: str) -> dict:
    """Combine all harvest and population data into a single structure."""
    
    log.info("Loading harvest data...")
    harvest_data = [d for d in load_json_files(harvest_dir, "elk_harvest_*.json")
                    if 'year' in d]  # skip _combined.json
    
    log.info("Loading population data...")
    population_data = [d for d in load_json_files(population_dir, "elk_population_*.json")
                       if 'year' in d]  # skip _combined.json
    
    if not harvest_data and not population_data:
        log.error("No data files found!")
        sys.exit(1)
    
    # -----------------------------------------------------------------------
    # 1. Harvest records — strip verbose fields, keep everything else
    # -----------------------------------------------------------------------
    harvest_records = []
    harvest_years = set()
    
    for hdata in harvest_data:
        year = hdata['year']
        harvest_years.add(year)
        
        for rec in hdata['harvest_records']:
            # Drop section_title (verbose, redundant) and species (always elk)
            clean = {k: v for k, v in rec.items() 
                     if k not in ('section_title', 'species')}
            harvest_records.append(clean)
    
    log.info(f"  Harvest records: {len(harvest_records):,} across {len(harvest_years)} years")
    
    # -----------------------------------------------------------------------
    # 2. DAU harvest summaries (from harvest reports, 2016+)
    # -----------------------------------------------------------------------
    dau_harvest_records = []
    dau_harvest_years = set()
    
    for hdata in harvest_data:
        year = hdata['year']
        for rec in hdata.get('dau_records', []):
            clean = {k: v for k, v in rec.items() if k != 'species'}
            dau_harvest_records.append(clean)
            dau_harvest_years.add(year)
    
    log.info(f"  DAU harvest summaries: {len(dau_harvest_records):,} across {len(dau_harvest_years)} years")
    
    # -----------------------------------------------------------------------
    # 3. Population estimates
    # -----------------------------------------------------------------------
    population_records = []
    population_years = set()
    
    for pdata in population_data:
        year = pdata['year']
        population_years.add(year)
        
        for rec in pdata['records']:
            clean = {
                'year': rec['year'],
                'dau': rec['dau'],
                'dau_number': rec['dau_number'],
                'herd_name': rec['herd_name'],
                'population_estimate': rec['population_estimate'],
                'bull_cow_ratio': rec['bull_cow_ratio'],
            }
            population_records.append(clean)
    
    log.info(f"  Population records: {len(population_records):,} across {len(population_years)} years")
    
    # -----------------------------------------------------------------------
    # 4. DAU definitions (unified from population data)
    # -----------------------------------------------------------------------
    dau_definitions = build_dau_definitions(population_data)
    gmu_to_dau = build_gmu_to_dau_lookup(dau_definitions)
    
    log.info(f"  DAU definitions: {len(dau_definitions)} DAUs, {len(gmu_to_dau)} GMU mappings")
    
    # -----------------------------------------------------------------------
    # 5. Statewide population totals (for trend line)
    # -----------------------------------------------------------------------
    statewide_population = {}
    for pdata in population_data:
        statewide_population[pdata['year']] = pdata['statewide_total']
    
    # -----------------------------------------------------------------------
    # 6. Statewide harvest totals (from "all manners" method, all season)
    # -----------------------------------------------------------------------
    statewide_harvest = {}
    for hdata in harvest_data:
        year = hdata['year']
        all_manner = [r for r in hdata['harvest_records']
                      if r['section_method'] == 'all' 
                      and r['section_season'] == 'all'
                      and r['table_type'] == 'harvest_rec_days']
        if all_manner:
            statewide_harvest[year] = {
                'total_harvest': sum(r['total_harvest'] for r in all_manner),
                'total_hunters': sum(r['total_hunters'] for r in all_manner),
                'total_bulls': sum(r['bulls'] for r in all_manner),
                'total_cows': sum(r['cows'] for r in all_manner),
                'total_calves': sum(r['calves'] for r in all_manner),
                'total_rec_days': sum(r['total_rec_days'] for r in all_manner),
                'gmu_count': len(all_manner),
            }
    
    # -----------------------------------------------------------------------
    # Build combined output
    # -----------------------------------------------------------------------
    all_years = sorted(harvest_years | population_years)
    
    combined = {
        'metadata': {
            'species': 'elk',
            'state': 'Colorado',
            'source': 'Colorado Parks & Wildlife',
            'years': all_years,
            'harvest_years': sorted(harvest_years),
            'population_years': sorted(population_years),
            'generated': str(date.today()),
            'record_counts': {
                'harvest': len(harvest_records),
                'dau_harvest_summary': len(dau_harvest_records),
                'population': len(population_records),
                'dau_definitions': len(dau_definitions),
            },
        },
        'statewide_trends': {
            'population': statewide_population,
            'harvest': statewide_harvest,
        },
        'dau_definitions': dau_definitions,
        'gmu_to_dau': {str(k): v for k, v in gmu_to_dau.items()},
        'harvest': harvest_records,
        'dau_harvest_summary': dau_harvest_records,
        'population': population_records,
    }
    
    return combined


def build_slim_version(combined: dict) -> dict:
    """Build a lightweight version with only the "all manners / all seasons"
    harvest_rec_days data — ideal for the initial map view before the user
    drills into specific methods/seasons."""
    
    slim_harvest = [
        r for r in combined['harvest']
        if r['section_method'] == 'all'
        and r['section_season'] == 'all'
        and r['table_type'] == 'harvest_rec_days'
    ]
    
    # Also keep only the core numeric fields
    slim_fields = [
        'year', 'gmu', 'bulls', 'cows', 'calves',
        'total_harvest', 'total_hunters', 'pct_success', 'total_rec_days',
    ]
    slim_harvest = [{k: r[k] for k in slim_fields if k in r} for r in slim_harvest]
    
    slim = {
        'metadata': {
            **combined['metadata'],
            'record_counts': {
                'harvest': len(slim_harvest),
                'population': combined['metadata']['record_counts']['population'],
                'dau_definitions': combined['metadata']['record_counts']['dau_definitions'],
            },
            'note': 'Slim version: harvest limited to all-manners/all-seasons harvest_rec_days only',
        },
        'statewide_trends': combined['statewide_trends'],
        'dau_definitions': combined['dau_definitions'],
        'gmu_to_dau': combined['gmu_to_dau'],
        'harvest': slim_harvest,
        'population': combined['population'],
    }
    
    return slim


def build_dashboard_version(combined: dict) -> dict:
    """Build a dashboard-optimized version with method/season breakdowns
    and compact field names for faster loading."""
    
    KEEP_COMBOS = {
        ('all', 'all'), ('archery', 'all'), ('muzzleloader', 'all'),
        ('rifle', 'all'), ('rifle', '1st'), ('rifle', '2nd'),
        ('rifle', '3rd'), ('rifle', '4th'),
    }
    
    dash_harvest = []
    for r in combined['harvest']:
        if r['table_type'] != 'harvest_rec_days':
            continue
        if r.get('section_license_type') is not None:
            continue
        if (r['section_method'], r['section_season']) not in KEEP_COMBOS:
            continue
        dash_harvest.append({
            'y': r['year'], 'g': r['gmu'],
            'm': r['section_method'], 's': r['section_season'],
            'b': r['bulls'], 'c': r['cows'], 'v': r['calves'],
            'h': r['total_harvest'], 'n': r['total_hunters'],
            'p': r['pct_success'], 'r': r['total_rec_days'],
        })
    
    return {
        'metadata': {
            **combined['metadata'],
            'record_counts': {
                'harvest': len(dash_harvest),
                'population': combined['metadata']['record_counts']['population'],
                'dau_definitions': combined['metadata']['record_counts']['dau_definitions'],
            },
            'note': 'Dashboard version: harvest by method/season, compact field names',
        },
        'statewide_trends': combined['statewide_trends'],
        'dau_definitions': combined['dau_definitions'],
        'gmu_to_dau': combined['gmu_to_dau'],
        'harvest': dash_harvest,
        'population': combined['population'],
    }


def export(data: dict, output_path: str):
    """Write JSON with size reporting."""
    with open(output_path, 'w') as f:
        json.dump(data, f, separators=(',', ':'))  # compact
    
    size_bytes = os.path.getsize(output_path)
    if size_bytes > 1_000_000:
        log.info(f"  Wrote: {output_path} ({size_bytes / 1_000_000:.1f} MB)")
    else:
        log.info(f"  Wrote: {output_path} ({size_bytes / 1_000:.0f} KB)")
    
    # Also write a pretty-printed version for debugging
    pretty_path = output_path.replace('.json', '_pretty.json')
    with open(pretty_path, 'w') as f:
        json.dump(data, f, indent=2)
    log.info(f"  Wrote: {pretty_path} (pretty-printed)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Combine elk harvest and population data into a single JSON.",
        epilog="Example: python combine_elk_data.py --harvest-dir output/harvest --population-dir output/population"
    )
    parser.add_argument("--harvest-dir", required=True,
                        help="Directory containing elk_harvest_YYYY.json files")
    parser.add_argument("--population-dir", required=True,
                        help="Directory containing elk_population_YYYY.json files")
    parser.add_argument("-o", "--output-dir", default="output",
                        help="Output directory (default: output)")
    
    args = parser.parse_args()
    
    os.makedirs(args.output_dir, exist_ok=True)
    
    combined = combine_data(args.harvest_dir, args.population_dir)
    
    # Full version
    full_path = os.path.join(args.output_dir, "elk_combined.json")
    export(combined, full_path)
    
    # Slim version
    slim = build_slim_version(combined)
    slim_path = os.path.join(args.output_dir, "elk_combined_slim.json")
    export(slim, slim_path)
    
    # Dashboard version (method/season breakdown, compact field names)
    dash = build_dashboard_version(combined)
    dash_path = os.path.join(args.output_dir, "elk_dashboard.json")
    export(dash, dash_path)
    
    # Summary
    meta = combined['metadata']
    print(f"\n{'='*60}")
    print(f"Combined Elk Data Summary")
    print(f"{'='*60}")
    if meta['years']:
        print(f"  Years:              {meta['years'][0]} – {meta['years'][-1]}")
    if meta['harvest_years']:
        print(f"  Harvest years:      {len(meta['harvest_years'])} ({meta['harvest_years'][0]}–{meta['harvest_years'][-1]})")
    if meta['population_years']:
        print(f"  Population years:   {len(meta['population_years'])} ({meta['population_years'][0]}–{meta['population_years'][-1]})")
    else:
        print(f"  Population years:   0 (no population data found)")
    print(f"  Harvest records:    {meta['record_counts']['harvest']:,}")
    print(f"  DAU harvest sums:   {meta['record_counts']['dau_harvest_summary']:,}")
    print(f"  Population records: {meta['record_counts']['population']:,}")
    print(f"  DAU definitions:    {meta['record_counts']['dau_definitions']}")
    
    st = combined['statewide_trends']
    if st['population']:
        latest_pop_yr = max(st['population'].keys(), key=int)
        print(f"  Latest pop ({latest_pop_yr}):  {st['population'][latest_pop_yr]:,}")
    if st['harvest']:
        latest_h_yr = max(st['harvest'].keys(), key=int)
        h = st['harvest'][latest_h_yr]
        print(f"  Latest harvest ({latest_h_yr}): {h['total_harvest']:,} ({h['total_hunters']:,} hunters)")
    
    print(f"{'='*60}")
