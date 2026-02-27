#!/usr/bin/env python3
#!/usr/bin/env python3
"""
CPW Elk Harvest PDF Parser
Parses Colorado Parks & Wildlife Statewide Elk Harvest Estimate PDFs (2003-2024).

Handles two table formats:
  1. Harvest/Recreation Days: Unit | Bulls | Cows | Calves | Harvest | Hunters | Success | Rec.Days
  2. Percent Success:         Unit | Antlered Harvest | Antlered Hunters | Antlered %Success |
                                     Antlerless Harvest | Antlerless Hunters | Antlerless %Success

Also handles:
  3. DAU Summary: DAU-level aggregate with confidence intervals
  4. Bosque del Oso: By season instead of by unit

Tested against 2019 format (pre-2020) and designed for 2020+ format adaptation.
"""

import re
import csv
import json
import sys
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

import pdfplumber

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Global flag for debug diagnostic dumps
DEBUG_DUMP = False


def normalize_spaced_ordinals(text: str) -> str:
    """Fix PDF extraction artifacts like '1 s t' → '1st', '2 n d' → '2nd', etc."""
    text = re.sub(r'\b1\s+s\s+t\b', '1st', text)
    text = re.sub(r'\b2\s+n\s+d\b', '2nd', text)
    text = re.sub(r'\b3\s+r\s+d\b', '3rd', text)
    text = re.sub(r'\b4\s+t\s+h\b', '4th', text)
    return text


# ---------------------------------------------------------------------------
# Section classification
# ---------------------------------------------------------------------------

@dataclass
class SectionMeta:
    """Metadata extracted from a section title."""
    table_type: str          # 'harvest_rec_days' | 'percent_success' | 'dau_summary' | 'bosque'
    method: Optional[str]    # 'all' | 'rifle' | 'archery' | 'muzzleloader' | 'rfw' | 'plo_early' | 'plo_late' | 'late' | 'early_hc' | 'damage' | 'bosque'
    season: Optional[str]    # None | 'all' | '1st' | '2nd' | '3rd' | '4th' | '2nd_split' | '3rd_split' | '4th_split'
    license_type: Optional[str]  # None | 'antlered' | 'antlerless' | 'either_sex' | 'plo'
    raw_title: str = ""


def classify_section(title: str) -> Optional[SectionMeta]:
    """Classify a section title into structured metadata."""
    t = title.strip()

    # DAU summary
    if "By DAU" in t or "by DAU" in t or "DAU with" in t or "DAU w " in t:
        return SectionMeta("dau_summary", "all", "all", None, t)

    # Bosque del Oso
    if "Bosque del Oso" in t:
        return SectionMeta("bosque", "bosque", "all", None, t)

    # Damage / AFA / Auction
    if "Damage" in t or "AFA" in t or "Auction" in t:
        return SectionMeta("harvest_rec_days", "damage", "all", None, t)

    # Determine table type
    if "Percent Success" in t:
        tbl = "percent_success"
    elif "Recreation Days" in t:
        tbl = "harvest_rec_days"
    else:
        return None

    # Determine method & season & license type
    method = None
    season = None
    license_type = None

    # Ranching for Wildlife
    if "Ranching for Wildlife" in t:
        method = "rfw"
        season = "all"
    # Early High Country
    elif "Early High Country" in t:
        method = "early_hc"
        season = "all"
    # PLO
    elif "PLO Seasons Only" in t or "Private Land Only" in t:
        if "Early PLO" in t:
            method = "plo_early"
        elif "Late PLO" in t:
            method = "plo_late"
        elif "Private Land Only" in t:
            method = "plo"
        else:
            method = "plo"
        season = "all"
    # Late Seasons
    elif "Late Seasons" in t and "PLO" not in t.split("Late Seasons")[0]:
        method = "late"
        season = "all"
        if "Includes PLOs" in t:
            method = "late_incl_plo"
    # Limited license seasons
    elif "Limited" in t:
        # Determine license type
        if "Antlered" in t and "Antlerless" not in t:
            license_type = "antlered"
        elif "Antlerless" in t:
            license_type = "antlerless"
        elif "Either-sex" in t or "Either Sex" in t:
            license_type = "either_sex"
        
        # Determine season
        if "Split" in t:
            m = re.search(r'(\d)(?:st|nd|rd|th)\s+Split', t)
            if m:
                season = f"{m.group(1)}{'st' if m.group(1)=='1' else 'nd' if m.group(1)=='2' else 'rd' if m.group(1)=='3' else 'th'}_split"
                # Normalize
                ordinals = {'1': '1st_split', '2': '2nd_split', '3': '3rd_split', '4': '4th_split'}
                season = ordinals.get(m.group(1), season)
        else:
            m = re.search(r'(\d)(?:st|nd|rd|th)\s+Season', t)
            if m:
                ordinals = {'1': '1st', '2': '2nd', '3': '3rd', '4': '4th'}
                season = ordinals.get(m.group(1), m.group(1))
        
        method = "rifle"  # Limited seasons are rifle-based
    # Archery
    elif "Archery" in t:
        method = "archery"
        season = "all"
    # Muzzleloader
    elif "Muzzleloader" in t or "Muzzle" in t:
        method = "muzzleloader"
        season = "all"
    # Rifle by season
    elif "Rifle" in t:
        method = "rifle"
        if "First" in t or "1st" in t:
            season = "1st"
        elif "Second" in t or "2nd" in t:
            season = "2nd"
        elif "Third" in t or "3rd" in t:
            season = "3rd"
        elif "Fourth" in t or "4th" in t:
            season = "4th"
        else:
            season = "all"
    # All Manners of Take
    elif "All Manners" in t:
        method = "all"
        season = "all"
    else:
        method = "unknown"
        season = "all"

    return SectionMeta(tbl, method, season, license_type, t)


# ---------------------------------------------------------------------------
# Number parsing
# ---------------------------------------------------------------------------

def parse_int(s: str) -> int:
    """Parse an integer, handling commas."""
    s = s.strip().replace(",", "")
    if not s or s == '-':
        return 0
    return int(s)


def parse_float(s: str) -> float:
    """Parse a float, handling commas."""
    s = s.strip().replace(",", "")
    if not s or s == '-':
        return 0.0
    return float(s)


# ---------------------------------------------------------------------------
# Line-level parsing
# ---------------------------------------------------------------------------

# Regex for harvest/rec-days data lines:
# Unit  Bulls  Cows  Calves  TotalHarvest  TotalHunters  PctSuccess  TotalRecDays
# Numbers may have commas (e.g., 1,132). Unit can be numeric (e.g., 201, 441, 851).
HARVEST_LINE_RE = re.compile(
    r'^(\d+)\s+'                           # Unit (GMU number)
    r'([\d,]+)\s+'                         # Bulls
    r'([\d,]+)\s+'                         # Cows
    r'([\d,]+)\s+'                         # Calves
    r'([\d,]+)\s+'                         # Total Harvest
    r'([\d,]+)\s+'                         # Total Hunters
    r'(\d+)\s+'                            # Percent Success (integer)
    r'([\d,]+)\s*$'                        # Total Rec Days
)

# Regex for the Total line at the bottom of harvest tables
HARVEST_TOTAL_RE = re.compile(
    r'^Total\s+'
    r'([\d,]+)\s+'
    r'([\d,]+)\s+'
    r'([\d,]+)\s+'
    r'([\d,]+)\s+'
    r'([\d,]+)\s+'
    r'(\d+)\s+'
    r'([\d,]+)\s*$'
)

# Regex for percent-success data lines:
# Unit  AntleredHarvest  AntleredHunters  Antlered%  AntlerlessHarvest  AntlerlessHunters  Antlerless%
SUCCESS_LINE_RE = re.compile(
    r'^(\d+)\s+'                           # Unit
    r'([\d,]+)\s+'                         # Antlered Harvest
    r'([\d,]+)\s+'                         # Antlered Hunters
    r'(\d+)\s+'                            # Antlered % Success
    r'([\d,]+)\s+'                         # Antlerless Harvest
    r'([\d,]+)\s+'                         # Antlerless Hunters
    r'(\d+)\s*$'                           # Antlerless % Success
)

SUCCESS_TOTAL_RE = re.compile(
    r'^Total\s+'
    r'([\d,]+)\s+'
    r'([\d,]+)\s+'
    r'(\d+)\s+'
    r'([\d,]+)\s+'
    r'([\d,]+)\s+'
    r'(\d+)\s*$'
)

# DAU summary lines:
# Old (2019-2020): E-1  386  5  376  397  222  9  204  241  57.5  1792  59  1681  1911  0.959  0.552
# New (2021+):     E-01 373  0  373  373  214  7  201  228  57%  1,652 33  1,589 1,718 0.97  0.64
DAU_LINE_RE = re.compile(
    r'^(E-\d+)\s+'                         # DAU ID (E-1 or E-01)
    r'([\d,]+)\s+([\d,]+)\s+'              # Hunters estimate, SE
    r'([\d,]+)\s+([\d,]+)\s+'              # Hunters LCL, UCL
    r'([\d,]+)\s+([\d,]+)\s+'              # Harvest estimate, SE
    r'([\d,]+)\s+([\d,]+)\s+'              # Harvest LCL, UCL
    r'([\d.]+)%?\s+'                       # Percent Success (may have %)
    r'([\d,]+)\s+([\d,]+)\s+'              # Rec Days estimate, SE
    r'([\d,]+)\s+([\d,]+)\s+'              # Rec Days LCL, UCL
    r'([\d.]+)\s+([\d.]+)\s*$'             # Sample Rate, Response Rate
)

# Bosque del Oso lines: Season  Bulls  Cows  Calves  Harvest  Hunters  Success  RecDays
# Old (2019-2020): by-season rows like "Archery  10  5  0  15  ..."
# New (2024+): regular unit row "851  4  11  0  15  75  20  245"
BOSQUE_LINE_RE = re.compile(
    r'^(Archery|Muzzleloader|1st Rifle|2nd Rifle|3rd Rifle|4th Rifle|Late Rifle|Total)\s+'
    r'([\d,]+)\s+'
    r'([\d,]+)\s+'
    r'([\d,]+)\s+'
    r'([\d,]+)\s+'
    r'([\d,]+)\s+'
    r'(\d+)\s+'
    r'([\d,]+)\s*$'
)

# Section title pattern (on data pages, not TOC)
SECTION_TITLE_RE = re.compile(
    r'^(20\d{2})\s+Elk\s+'
    r'(?:Harvest|Hunters)'
    r'.*?'
    r'(?:Recreation Days|Percent Success|Confidence)'
    r'.*$'
)


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------

def extract_full_text(pdf_path: str) -> tuple[str, int]:
    """Extract all text from PDF, return (full_text, year).
    
    Skips the TOC pages (pages with '...' lines indicating page references).
    """
    lines = []
    year = None
    
    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            text = page.extract_text()
            if text is None:
                continue
            
            page_lines = text.split('\n')
            
            # Detect TOC pages:
            # Old format: lines with "...." (page number refs)
            # New format (2021+): "Contents" header with section titles ending in page numbers
            is_toc = False
            dot_count = sum(1 for l in page_lines if '....' in l)
            if dot_count > 5:
                is_toc = True
            
            # Also detect new-style TOC: page has "Contents" as a heading
            if not is_toc:
                # Check first few lines for "Contents" 
                first_lines = ' '.join(page_lines[:5])
                if "Contents" in first_lines:
                    is_toc = True
            
            # Skip TOC and methodology pages
            if is_toc:
                # But still extract the year from TOC
                if year is None:
                    m = re.search(r'(20\d{2})\s+(?:Colorado\s+)?Elk', text)
                    if m:
                        year = int(m.group(1))
                continue
            
            # Skip methodology text (page 3 typically)
            if "Methodology" in text and "stratified random" in text:
                if year is None:
                    m = re.search(r'(20\d{2})\s+(?:Colorado\s+)?Elk', text)
                    if m:
                        year = int(m.group(1))
                continue
            
            for line in page_lines:
                line = line.strip()
                if not line:
                    continue
                # Skip page numbers that appear alone
                if re.match(r'^\d{1,3}$', line):
                    continue
                lines.append(line)
    
    # Extract year from first section title if not found
    if year is None:
        for line in lines:
            m = re.match(r'(20\d{2})\s+Elk', line)
            if m:
                year = int(m.group(1))
                break
    
    return '\n'.join(lines), year


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------

def split_into_sections(text: str, year: int) -> list[tuple[SectionMeta, list[str]]]:
    """Split full text into sections based on title lines.
    
    Returns list of (SectionMeta, [data_lines]).
    """
    lines = text.split('\n')
    sections = []
    current_meta = None
    current_lines = []
    
    year_str = str(year)
    
    for line in lines:
        # Normalize spaced ordinals (e.g., "1 s t" → "1st") from PDF extraction
        normalized = normalize_spaced_ordinals(line)
        
        # Check if this is a section title
        if normalized.startswith(year_str) and ("Elk" in normalized) and (
            "Recreation Days" in normalized or "Percent Success" in normalized or 
            "Confidence" in normalized
        ):
            # Save previous section
            if current_meta is not None:
                sections.append((current_meta, current_lines))
            
            meta = classify_section(normalized)
            if meta:
                current_meta = meta
                current_lines = []
            else:
                current_meta = None
                current_lines = []
            continue
        
        # Skip header/subheader lines (handle both old and new format variants)
        if normalized.startswith("Total Total Percent Total"):
            continue
        if normalized.startswith("Unit Bulls Cows Calves"):
            continue
        if normalized.startswith("Antlered Antlered"):
            continue
        if normalized.startswith("Unit Harvest Hunters"):
            continue
        if normalized.startswith("Total Hunters") or normalized.startswith("Percent"):
            continue
        if normalized.startswith("DAU estimate SE") or normalized.startswith("DAU estimate"):
            continue
        if normalized.startswith("Total Days") or normalized.startswith("Total Recreation"):
            continue
        if re.match(r'^(Season|Total Total|Unit Bulls|Unit Harvest|DAU estimate|Percent$)', normalized):
            continue
        # Also skip the sub-header for harvest tables
        if normalized in ("Success", "Rate", "Intervals"):
            continue
        # Skip asterisk notes that appear in some years
        if normalized.startswith("*"):
            continue
        # Skip the DAU map title (appears as a data line in some years)
        if "Data Analysis Unit" in normalized and "Map" in normalized:
            continue
        
        if current_meta is not None:
            current_lines.append(line)
    
    # Save last section
    if current_meta is not None:
        sections.append((current_meta, current_lines))
    
    return sections


# ---------------------------------------------------------------------------
# Record parsing
# ---------------------------------------------------------------------------

@dataclass
class HarvestRecord:
    year: int
    species: str
    gmu: int
    section_method: str        # From section title classification
    section_season: Optional[str]
    section_license_type: Optional[str]
    bulls: int = 0
    cows: int = 0
    calves: int = 0
    total_harvest: int = 0
    total_hunters: int = 0
    pct_success: int = 0
    total_rec_days: int = 0
    # For percent-success tables
    antlered_harvest: Optional[int] = None
    antlered_hunters: Optional[int] = None
    antlered_pct_success: Optional[int] = None
    antlerless_harvest: Optional[int] = None
    antlerless_hunters: Optional[int] = None
    antlerless_pct_success: Optional[int] = None
    table_type: str = ""
    section_title: str = ""


@dataclass
class DAURecord:
    year: int
    species: str
    dau: str
    total_hunters: int = 0
    hunters_se: int = 0
    hunters_lcl: int = 0
    hunters_ucl: int = 0
    total_harvest: int = 0
    harvest_se: int = 0
    harvest_lcl: int = 0
    harvest_ucl: int = 0
    pct_success: float = 0.0
    total_rec_days: int = 0
    rec_days_se: int = 0
    rec_days_lcl: int = 0
    rec_days_ucl: int = 0
    sample_rate: float = 0.0
    response_rate: float = 0.0


def parse_section(meta: SectionMeta, lines: list[str], year: int, species: str = "elk") -> list:
    """Parse data lines from a section into records."""
    records = []
    
    if meta.table_type == "dau_summary":
        return parse_dau_section(lines, year, species)
    
    if meta.table_type == "bosque":
        return parse_bosque_section(lines, year, species, meta)
    
    if meta.table_type == "harvest_rec_days":
        for line in lines:
            m = HARVEST_LINE_RE.match(line)
            if m:
                rec = HarvestRecord(
                    year=year,
                    species=species,
                    gmu=parse_int(m.group(1)),
                    section_method=meta.method,
                    section_season=meta.season,
                    section_license_type=meta.license_type,
                    bulls=parse_int(m.group(2)),
                    cows=parse_int(m.group(3)),
                    calves=parse_int(m.group(4)),
                    total_harvest=parse_int(m.group(5)),
                    total_hunters=parse_int(m.group(6)),
                    pct_success=parse_int(m.group(7)),
                    total_rec_days=parse_int(m.group(8)),
                    table_type=meta.table_type,
                    section_title=meta.raw_title,
                )
                records.append(rec)
            elif HARVEST_TOTAL_RE.match(line):
                continue  # Skip total rows
            elif line.strip() and not line.startswith("Seasons"):
                # Log unparsed non-empty lines for debugging
                log.debug(f"  Unparsed (harvest): '{line}'")
    
    elif meta.table_type == "percent_success":
        for line in lines:
            m = SUCCESS_LINE_RE.match(line)
            if m:
                rec = HarvestRecord(
                    year=year,
                    species=species,
                    gmu=parse_int(m.group(1)),
                    section_method=meta.method,
                    section_season=meta.season,
                    section_license_type=meta.license_type,
                    antlered_harvest=parse_int(m.group(2)),
                    antlered_hunters=parse_int(m.group(3)),
                    antlered_pct_success=parse_int(m.group(4)),
                    antlerless_harvest=parse_int(m.group(5)),
                    antlerless_hunters=parse_int(m.group(6)),
                    antlerless_pct_success=parse_int(m.group(7)),
                    table_type=meta.table_type,
                    section_title=meta.raw_title,
                )
                records.append(rec)
            elif SUCCESS_TOTAL_RE.match(line):
                continue
            elif line.strip() and not line.startswith("Seasons"):
                log.debug(f"  Unparsed (success): '{line}'")
    
    return records


def parse_dau_section(lines: list[str], year: int, species: str) -> list[DAURecord]:
    """Parse DAU summary lines."""
    records = []
    for line in lines:
        m = DAU_LINE_RE.match(line)
        if m:
            # Normalize DAU ID: E-01 → E-1, E-06 → E-6
            raw_dau = m.group(1)
            dau_parts = raw_dau.split('-')
            dau_id = f"{dau_parts[0]}-{int(dau_parts[1])}" if len(dau_parts) == 2 else raw_dau
            
            records.append(DAURecord(
                year=year,
                species=species,
                dau=dau_id,
                total_hunters=parse_int(m.group(2)),
                hunters_se=parse_int(m.group(3)),
                hunters_lcl=parse_int(m.group(4)),
                hunters_ucl=parse_int(m.group(5)),
                total_harvest=parse_int(m.group(6)),
                harvest_se=parse_int(m.group(7)),
                harvest_lcl=parse_int(m.group(8)),
                harvest_ucl=parse_int(m.group(9)),
                pct_success=parse_float(m.group(10)),
                total_rec_days=parse_int(m.group(11)),
                rec_days_se=parse_int(m.group(12)),
                rec_days_lcl=parse_int(m.group(13)),
                rec_days_ucl=parse_int(m.group(14)),
                sample_rate=parse_float(m.group(15)),
                response_rate=parse_float(m.group(16)),
            ))
        else:
            if line.strip() and not line.startswith("Intervals"):
                log.debug(f"  Unparsed (DAU): '{line}'")
    return records


def parse_bosque_section(lines: list[str], year: int, species: str, meta: SectionMeta) -> list[HarvestRecord]:
    """Parse Bosque del Oso section.
    
    Old format (2019-2020): by-season rows (Archery, Muzzleloader, 1st Rifle, etc.)
    New format (2024+): regular unit row (GMU 851) - same as harvest table format.
    """
    records = []
    for line in lines:
        # Try old by-season format first
        m = BOSQUE_LINE_RE.match(line)
        if m:
            season_name = m.group(1).strip()
            if season_name == "Total":
                continue  # Skip total/summary rows
            season_map = {
                'Archery': ('archery', 'all'),
                'Muzzleloader': ('muzzleloader', 'all'),
                '1st Rifle': ('rifle', '1st'),
                '2nd Rifle': ('rifle', '2nd'),
                '3rd Rifle': ('rifle', '3rd'),
                '4th Rifle': ('rifle', '4th'),
                'Late Rifle': ('rifle', 'late'),
                'Total': ('all', 'all'),
            }
            method, season = season_map.get(season_name, ('unknown', 'unknown'))
            
            rec = HarvestRecord(
                year=year,
                species=species,
                gmu=851,
                section_method=f"bosque_{method}",
                section_season=season,
                section_license_type=None,
                bulls=parse_int(m.group(2)),
                cows=parse_int(m.group(3)),
                calves=parse_int(m.group(4)),
                total_harvest=parse_int(m.group(5)),
                total_hunters=parse_int(m.group(6)),
                pct_success=parse_int(m.group(7)),
                total_rec_days=parse_int(m.group(8)),
                table_type="bosque",
                section_title=meta.raw_title,
            )
            records.append(rec)
            continue
        
        # Try new format (regular harvest line for GMU 851)
        m2 = HARVEST_LINE_RE.match(line)
        if m2:
            rec = HarvestRecord(
                year=year,
                species=species,
                gmu=parse_int(m2.group(1)),
                section_method="bosque_all",
                section_season="all",
                section_license_type=None,
                bulls=parse_int(m2.group(2)),
                cows=parse_int(m2.group(3)),
                calves=parse_int(m2.group(4)),
                total_harvest=parse_int(m2.group(5)),
                total_hunters=parse_int(m2.group(6)),
                pct_success=parse_int(m2.group(7)),
                total_rec_days=parse_int(m2.group(8)),
                table_type="bosque",
                section_title=meta.raw_title,
            )
            records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Main parsing function
# ---------------------------------------------------------------------------

def parse_elk_harvest_pdf(pdf_path: str, species: str = "elk") -> dict:
    """Parse a CPW elk harvest PDF and return structured data.
    
    Returns:
        {
            'year': int,
            'species': str,
            'source_file': str,
            'harvest_records': [HarvestRecord, ...],  # GMU-level harvest data
            'dau_records': [DAURecord, ...],           # DAU-level summary
            'sections_parsed': int,
            'sections_detail': [{name, record_count}, ...]
        }
    """
    log.info(f"Parsing: {pdf_path}")
    
    full_text, year = extract_full_text(pdf_path)
    if year is None:
        raise ValueError(f"Could not determine year from {pdf_path}")
    log.info(f"  Year: {year}")
    
    sections = split_into_sections(full_text, year)
    log.info(f"  Sections found: {len(sections)}")
    
    all_harvest = []
    all_dau = []
    sections_detail = []
    
    for meta, lines in sections:
        records = parse_section(meta, lines, year, species)
        
        # Separate DAU records from harvest records
        dau_recs = [r for r in records if isinstance(r, DAURecord)]
        harvest_recs = [r for r in records if isinstance(r, HarvestRecord)]
        
        all_dau.extend(dau_recs)
        all_harvest.extend(harvest_recs)
        
        section_info = {
            'title': meta.raw_title[:80],
            'type': meta.table_type,
            'method': meta.method,
            'season': meta.season,
            'license_type': meta.license_type,
            'records': len(records),
        }
        sections_detail.append(section_info)
        
        if records:
            log.info(f"  [{meta.table_type}] {meta.method}/{meta.season}: {len(records)} records")
        else:
            # Only warn if there were actual data lines (not just a title-only page)
            # Filter out header/blank lines to check if there was real content
            real_lines = [l for l in lines if l.strip() 
                         and not l.startswith("Total Total") 
                         and not l.startswith("Unit Bulls")
                         and not l.startswith("Antlered Antlered")
                         and not l.startswith("Unit Harvest")
                         and not l.startswith("Total Hunters")
                         and not l.startswith("DAU estimate")
                         and l not in ("Success", "Rate", "Percent", "Intervals")]
            if real_lines:
                log.warning(f"  [{meta.table_type}] {meta.method}/{meta.season}: 0 records — {meta.raw_title[:60]}")
                if DEBUG_DUMP:
                    log.warning(f"    DEBUG: {len(real_lines)} unparsed lines. First 5:")
                    for dl in real_lines[:5]:
                        log.warning(f"    >>> {repr(dl)}")
    
    result = {
        'year': year,
        'species': species,
        'source_file': str(pdf_path),
        'harvest_records': [asdict(r) for r in all_harvest],
        'dau_records': [asdict(r) for r in all_dau],
        'sections_parsed': len(sections),
        'sections_detail': sections_detail,
    }
    
    log.info(f"  Total harvest records: {len(all_harvest)}")
    log.info(f"  Total DAU records: {len(all_dau)}")
    
    return result


# ---------------------------------------------------------------------------
# Export functions
# ---------------------------------------------------------------------------

def export_to_csv(data: dict, output_dir: str = "."):
    """Export parsed data to CSV files."""
    output_dir = Path(output_dir)
    # On Windows/OneDrive, exist_ok can fail if a FILE exists at the path.
    # Use os.makedirs directly which handles this more gracefully.
    import os
    os.makedirs(str(output_dir), exist_ok=True)
    
    year = data['year']
    species = data['species']
    
    # Harvest records CSV
    harvest_path = output_dir / f"{species}_harvest_{year}.csv"
    if data['harvest_records']:
        fieldnames = list(data['harvest_records'][0].keys())
        with open(harvest_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data['harvest_records'])
        log.info(f"  Wrote: {harvest_path} ({len(data['harvest_records'])} rows)")
    
    # DAU records CSV
    dau_path = output_dir / f"{species}_dau_summary_{year}.csv"
    if data['dau_records']:
        fieldnames = list(data['dau_records'][0].keys())
        with open(dau_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data['dau_records'])
        log.info(f"  Wrote: {dau_path} ({len(data['dau_records'])} rows)")
    
    return harvest_path, dau_path


def export_to_json(data: dict, output_path: str):
    """Export parsed data to JSON."""
    with open(output_path, 'w') as f:
        json.dump(data, f, indent=2)
    log.info(f"  Wrote: {output_path}")


# ---------------------------------------------------------------------------
# Convenience: Build the "flat" table your web app actually needs
# ---------------------------------------------------------------------------

def build_app_tables(data: dict) -> tuple[list[dict], list[dict]]:
    """Transform raw parsed records into the flat tables the web app needs.
    
    Returns:
        (harvest_flat, dau_flat)
        
    harvest_flat columns:
        year, species, gmu, method, season, license_type,
        bulls, cows, calves, total_harvest, total_hunters, pct_success, total_rec_days,
        antlered_harvest, antlered_hunters, antlered_pct_success,
        antlerless_harvest, antlerless_hunters, antlerless_pct_success
    
    dau_flat columns:
        year, species, dau, total_hunters, ..., pct_success, ..., total_rec_days, ...
    """
    harvest_flat = []
    for rec in data['harvest_records']:
        harvest_flat.append({
            'year': rec['year'],
            'species': rec['species'],
            'gmu': rec['gmu'],
            'method': rec['section_method'],
            'season': rec['section_season'],
            'license_type': rec.get('section_license_type'),
            'table_type': rec['table_type'],
            'bulls': rec.get('bulls', 0),
            'cows': rec.get('cows', 0),
            'calves': rec.get('calves', 0),
            'total_harvest': rec.get('total_harvest', 0),
            'total_hunters': rec.get('total_hunters', 0),
            'pct_success': rec.get('pct_success', 0),
            'total_rec_days': rec.get('total_rec_days', 0),
            'antlered_harvest': rec.get('antlered_harvest'),
            'antlered_hunters': rec.get('antlered_hunters'),
            'antlered_pct_success': rec.get('antlered_pct_success'),
            'antlerless_harvest': rec.get('antlerless_harvest'),
            'antlerless_hunters': rec.get('antlerless_hunters'),
            'antlerless_pct_success': rec.get('antlerless_pct_success'),
        })
    
    dau_flat = data['dau_records']  # Already flat
    
    return harvest_flat, dau_flat


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Parse CPW Elk Harvest PDFs into CSV/JSON.",
        epilog="Example: python parse_elk_harvest.py harvest_reports/*.pdf -o output"
    )
    parser.add_argument("pdf_files", nargs="+", help="One or more PDF files to parse")
    parser.add_argument("-o", "--output-dir", default="output", help="Output directory (default: output)")
    parser.add_argument("--debug", action="store_true", help="Dump raw lines from failed sections for diagnosis")
    
    # Also support legacy positional "output" as last arg (if it doesn't end in .pdf)
    # This handles:  python script.py file1.pdf file2.pdf output_dir
    args, remaining = parser.parse_known_args()
    
    # If any remaining args, treat as output dir
    if remaining:
        args.output_dir = remaining[-1]
    
    # Also handle case where last positional pdf_file is actually the output dir
    if args.pdf_files and not args.pdf_files[-1].lower().endswith('.pdf'):
        args.output_dir = args.pdf_files.pop()
    
    if not args.pdf_files:
        parser.error("No PDF files specified")
    
    if args.debug:
        DEBUG_DUMP = True
        log.setLevel(logging.DEBUG)
    
    output_dir = args.output_dir
    
    all_data = []
    for pdf_path in args.pdf_files:
        if not pdf_path.lower().endswith('.pdf'):
            continue
        
        try:
            data = parse_elk_harvest_pdf(pdf_path)
            export_to_csv(data, output_dir)
            export_to_json(data, str(Path(output_dir) / f"elk_harvest_{data['year']}.json"))
            all_data.append(data)
        except Exception as e:
            log.error(f"Failed to parse {pdf_path}: {e}")
            import traceback
            traceback.print_exc()
    
    # If multiple files, combine into one master file
    if len(all_data) > 1:
        combined_harvest = []
        combined_dau = []
        for d in all_data:
            combined_harvest.extend(d['harvest_records'])
            combined_dau.extend(d['dau_records'])
        
        combined = {
            'species': 'elk',
            'years': sorted(set(d['year'] for d in all_data)),
            'harvest_records': combined_harvest,
            'dau_records': combined_dau,
        }
        combined_path = Path(output_dir) / "elk_harvest_combined.json"
        export_to_json(combined, str(combined_path))
    
    print(f"\nDone. Processed {len(all_data)} file(s).")