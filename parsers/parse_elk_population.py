#!/usr/bin/env python3
"""
CPW Elk Population Estimate PDF Parser
Parses Colorado Parks & Wildlife Post-Hunt Elk Population Estimate PDFs (2003-2024).

Handles four format eras:
  1. 2003-2012:  DAU | GMUs | Estimate
  2. 2013-2018:  DAU | GMUs | Estimate  (same structure, minor DAU changes)
  3. 2019-2023:  DAU | GMUs | Estimate | Bull/Cow Ratio
  4. 2024+:      DAU | Herd Name | GMUs | Estimate | Bull/Cow Ratio

Also extracts the DAU-to-GMU mapping for each year (critical for dissolving
GMU boundaries into DAU boundaries on the map).

Usage:
    python parse_elk_population.py pop_estimates/*.pdf -o output
    python parse_elk_population.py pop_estimates/2024*.pdf -o output --debug
"""

import re
import csv
import json
import sys
import logging
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    print("ERROR: pdfplumber is required. Install with: pip install pdfplumber")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

DEBUG_DUMP = False


# ---------------------------------------------------------------------------
# Line-level parsing
# ---------------------------------------------------------------------------

# Main data line: DAU  <middle>  Estimate  [Ratio]
# DAU is 1-2 digit number (possibly zero-padded in 2024+)
# Middle contains optional herd name + GMU list (comma-separated numbers)
# Estimate is a number with possible commas
# Ratio is an optional small integer at the end
POP_LINE_RE = re.compile(
    r'^(\d{1,2})\s+'                      # DAU number
    r'(.+?)\s+'                           # Middle section (herd name + GMUs)
    r'([\d,]+)\s*'                        # Population estimate
    r'(\d+)?\s*$'                         # Optional bull/cow ratio
)

# Total line
TOTAL_LINE_RE = re.compile(
    r'^Total\s+Statewide\s+Estimate\s+([\d,]+)', re.IGNORECASE
)

# Special case: DAU with 0 population and no GMUs (e.g., E-99 in 2024)
ZERO_POP_RE = re.compile(
    r'^(\d{1,2})\s+(.+?)\s+0\s*$'
)


def parse_middle(middle: str):
    """Split the middle section into (herd_name, gmu_list).
    
    The GMU list is a trailing sequence of comma-separated numbers.
    The herd name (if present) is everything before that.
    """
    # Find trailing comma-separated number sequence
    gmu_match = re.search(r'((?:\d+,\s*)*\d+)\s*$', middle)
    if gmu_match:
        gmu_str = gmu_match.group(1)
        herd_name = middle[:gmu_match.start()].strip()
        gmus = [int(x.strip()) for x in gmu_str.split(',')]
        return herd_name or None, gmus
    return None, []


def parse_int(s: str) -> int:
    """Parse an integer, handling commas."""
    s = s.strip().replace(",", "")
    if not s or s == '-':
        return 0
    return int(s)


# ---------------------------------------------------------------------------
# PDF parsing
# ---------------------------------------------------------------------------

def parse_elk_population_pdf(pdf_path: str) -> dict:
    """Parse a CPW elk population estimate PDF.
    
    Returns:
        {
            'year': int,
            'species': 'elk',
            'source_file': str,
            'statewide_total': int,
            'records': [...],
            'dau_gmu_mapping': {...}
        }
    """
    log.info(f"Parsing: {pdf_path}")
    
    with pdfplumber.open(pdf_path) as pdf:
        all_text = ""
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                all_text += text + "\n"
    
    if not all_text:
        raise ValueError(f"No text extracted from {pdf_path}")
    
    lines = all_text.split('\n')
    
    # Extract year from title line
    year = None
    has_ratio = False
    has_herd_name = False
    
    for line in lines:
        m = re.search(r'(20\d{2})\s+POST\s+HUNT', line, re.IGNORECASE)
        if m:
            year = int(m.group(1))
        if 'Bull/Cow' in line or 'ratio' in line.lower():
            has_ratio = True
        if 'Herd Name' in line or 'Herd name' in line:
            has_herd_name = True
    
    if year is None:
        m = re.search(r'(20\d{2})', str(pdf_path))
        if m:
            year = int(m.group(1))
        else:
            raise ValueError(f"Could not determine year from {pdf_path}")
    
    log.info(f"  Year: {year}, has_ratio: {has_ratio}, has_herd_name: {has_herd_name}")
    
    records = []
    statewide_total = 0
    
    # Skip lines that are headers/footers
    skip_patterns = [
        'ELK', 'POST HUNT', 'Post Hunt', 'Bull/Cow',
        'DAU*', 'DAU *', 'GAME MANAGEMENT', 'Game Management',
        '* DAU', '** DAU', '*** DAU',
        'post hunt', 'Colorado Parks', 'Terrestrial',
        'UNITES', 'UNITS INVOLVED',
    ]
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Check for total line
        tm = TOTAL_LINE_RE.match(line)
        if tm:
            statewide_total = parse_int(tm.group(1))
            # Also parse the optional statewide ratio
            ratio_m = re.search(r'([\d,]+)\s+(\d+)\s*$', line)
            continue
        
        # Skip headers/footers
        if any(skip in line for skip in skip_patterns):
            continue
        
        # Skip footnote lines
        if line.startswith('*'):
            continue
        
        # Must start with a 1-2 digit number (the DAU)
        dau_match = re.match(r'^(\d{1,2})\s+(.+)$', line)
        if not dau_match:
            if DEBUG_DUMP:
                log.debug(f"  Unparsed: {repr(line)}")
            continue
        
        dau_num = int(dau_match.group(1))
        rest = dau_match.group(2).strip()
        
        # Strip footnote markers (*** etc.) from the text
        rest = re.sub(r'\*+', '', rest).strip()
        
        # Parse from the right: split into tokens
        # The rightmost number(s) are population and optional ratio
        # Everything else is the middle (herd name + GMUs)
        tokens = rest.split()
        
        if not tokens:
            continue
        
        # Determine how many trailing numeric columns to expect
        # Population is always present; ratio is present if has_ratio
        if has_ratio:
            # Last token = ratio, second-to-last = population
            # But some rows might not have a ratio (e.g., E-99 with 0 pop)
            # Try: last two tokens are numbers → pop + ratio
            if len(tokens) >= 2 and re.match(r'^[\d,]+$', tokens[-1]) and re.match(r'^[\d,]+$', tokens[-2]):
                ratio = int(tokens[-1])
                pop_est = parse_int(tokens[-2])
                middle = ' '.join(tokens[:-2])
            elif re.match(r'^[\d,]+$', tokens[-1]):
                # Only one trailing number (e.g., "0" for E-99)
                pop_est = parse_int(tokens[-1])
                ratio = None
                middle = ' '.join(tokens[:-1])
            else:
                if DEBUG_DUMP:
                    log.debug(f"  Cannot parse DAU {dau_num}: {repr(rest)}")
                continue
        else:
            # No ratio column — last token is the population estimate
            if re.match(r'^[\d,]+$', tokens[-1]):
                pop_est = parse_int(tokens[-1])
                ratio = None
                middle = ' '.join(tokens[:-1])
            else:
                if DEBUG_DUMP:
                    log.debug(f"  Cannot parse DAU {dau_num}: {repr(rest)}")
                continue
        
        # Parse middle into herd name + GMU list
        herd_name, gmus = parse_middle(middle)
        
        # If no GMUs found but middle has content, it's a text-only entry (no GMUs)
        if not gmus and middle:
            herd_name = middle.strip()
        
        dau_id = f"E-{dau_num}"
        
        records.append({
            'year': year,
            'species': 'elk',
            'dau': dau_id,
            'dau_number': dau_num,
            'herd_name': herd_name,
            'gmus': gmus,
            'population_estimate': pop_est,
            'bull_cow_ratio': ratio,
        })
    
    # Build DAU → GMU mapping
    dau_gmu_mapping = {}
    for rec in records:
        if rec['gmus']:
            dau_gmu_mapping[rec['dau']] = rec['gmus']
    
    # Validation
    record_total = sum(r['population_estimate'] for r in records)
    if statewide_total and record_total != statewide_total:
        log.warning(f"  Sum mismatch: records sum to {record_total:,} but statewide total is {statewide_total:,}")
    
    log.info(f"  DAUs: {len(records)}, Statewide total: {statewide_total:,}, Sum check: {record_total:,}")
    
    result = {
        'year': year,
        'species': 'elk',
        'source_file': str(pdf_path),
        'statewide_total': statewide_total,
        'records': records,
        'dau_gmu_mapping': dau_gmu_mapping,
    }
    
    return result


# ---------------------------------------------------------------------------
# Export functions
# ---------------------------------------------------------------------------

def export_to_csv(data: dict, output_dir: str = "."):
    """Export parsed data to CSV."""
    import os
    output_dir = Path(output_dir)
    os.makedirs(str(output_dir), exist_ok=True)
    
    year = data['year']
    
    csv_path = output_dir / f"elk_population_{year}.csv"
    if data['records']:
        fieldnames = ['year', 'species', 'dau', 'dau_number', 'herd_name',
                      'gmus', 'population_estimate', 'bull_cow_ratio']
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for rec in data['records']:
                row = dict(rec)
                row['gmus'] = '; '.join(str(g) for g in rec['gmus'])
                writer.writerow(row)
        log.info(f"  Wrote: {csv_path} ({len(data['records'])} rows)")
    
    # Also export DAU-GMU mapping as a separate CSV
    mapping_path = output_dir / f"dau_gmu_mapping_{year}.csv"
    with open(mapping_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['year', 'dau', 'gmu'])
        for dau, gmus in sorted(data['dau_gmu_mapping'].items(),
                                 key=lambda x: int(x[0].split('-')[1])):
            for gmu in gmus:
                writer.writerow([year, dau, gmu])
    log.info(f"  Wrote: {mapping_path}")
    
    return csv_path, mapping_path


def export_to_json(data: dict, output_path: str):
    """Export parsed data to JSON."""
    with open(output_path, 'w') as f:
        json.dump(data, f, indent=2)
    log.info(f"  Wrote: {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Parse CPW Elk Population Estimate PDFs into CSV/JSON.",
        epilog="Example: python parse_elk_population.py pop_estimates/*.pdf -o output"
    )
    parser.add_argument("pdf_files", nargs="+", help="One or more PDF files to parse")
    parser.add_argument("-o", "--output-dir", default="output", help="Output directory (default: output)")
    parser.add_argument("--debug", action="store_true", help="Show unparsed lines")
    
    args, remaining = parser.parse_known_args()
    if remaining:
        args.output_dir = remaining[-1]
    if args.pdf_files and not args.pdf_files[-1].lower().endswith('.pdf'):
        args.output_dir = args.pdf_files.pop()
    if not args.pdf_files:
        parser.error("No PDF files specified")
    
    if args.debug:
        DEBUG_DUMP = True
        log.setLevel(logging.DEBUG)
    
    all_data = []
    for pdf_path in args.pdf_files:
        if not pdf_path.lower().endswith('.pdf'):
            continue
        try:
            data = parse_elk_population_pdf(pdf_path)
            export_to_csv(data, args.output_dir)
            export_to_json(data, str(Path(args.output_dir) / f"elk_population_{data['year']}.json"))
            all_data.append(data)
        except Exception as e:
            log.error(f"Failed to parse {pdf_path}: {e}")
            import traceback
            traceback.print_exc()
    
    # Combined output
    if all_data:
        combined_records = []
        combined_mappings = {}
        for d in all_data:
            combined_records.extend(d['records'])
            for dau, gmus in d['dau_gmu_mapping'].items():
                key = f"{d['year']}_{dau}"
                combined_mappings[key] = {
                    'year': d['year'],
                    'dau': dau,
                    'gmus': gmus,
                }
        
        combined = {
            'species': 'elk',
            'years': sorted(set(d['year'] for d in all_data)),
            'population_records': combined_records,
            'dau_gmu_mappings': combined_mappings,
            'statewide_totals': {
                d['year']: d['statewide_total'] for d in all_data
            },
        }
        combined_path = Path(args.output_dir) / "elk_population_combined.json"
        export_to_json(combined, str(combined_path))
    
    print(f"\nDone. Processed {len(all_data)} file(s).")