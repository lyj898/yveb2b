"""IPEDS import — the organizations backbone.

Loads every active US postsecondary institution from the NCES IPEDS Data Center into
``organizations``, with enrollment as ``size_metric`` and program flags derived from
completions (CIP) data.

Three public files, all direct ZIP downloads from nces.ed.gov/ipeds/datacenter/data/:

  HD<year>.zip      Institutional characteristics / directory — name, address, website, sector
  EFFY<year>.zip    12-month unduplicated headcount enrollment -> size_metric
  C<year>_A.zip     Completions by CIP code -> programs_flags (nursing, allied health, ...)

Run:  python -m ingest.ipeds_import [--year 2024] [--refresh]

Downloads are cached under data/ so re-runs are offline and free.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import sys
import zipfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common  # noqa: E402

SOURCE = "ipeds"
BASE_URL = "https://nces.ed.gov/ipeds/datacenter/data"
DEFAULT_YEAR = 2024
CACHE_DIR = common.REPO_ROOT / "data" / "ipeds"

log = logging.getLogger("textbook-leads.ipeds")

# --- IPEDS code mappings ---------------------------------------------------
# SECTOR: 1 Public 4yr, 2 PrivNP 4yr, 3 PrivFP 4yr, 4 Public 2yr, 5 PrivNP 2yr,
#         6 PrivFP 2yr, 7 Public <2yr, 8 PrivNP <2yr, 9 PrivFP <2yr, 0/99 admin/unknown
SECTOR_TO_ORG_TYPE = {
    1: "university",
    2: "university",
    3: "career_college",   # for-profit 4-year — buys like a career college, not a university
    4: "community_college",
    5: "community_college",
    6: "career_college",
    7: "career_college",
    8: "career_college",
    9: "career_college",
}
SECTOR_LABEL = {
    1: "Public 4-year", 2: "Private nonprofit 4-year", 3: "Private for-profit 4-year",
    4: "Public 2-year", 5: "Private nonprofit 2-year", 6: "Private for-profit 2-year",
    7: "Public <2-year", 8: "Private nonprofit <2-year", 9: "Private for-profit <2-year",
    0: "Administrative unit", 99: "Sector unknown",
}
CONTROL_LABEL = {1: "public", 2: "private_nonprofit", 3: "private_for_profit"}

# CIP family -> program flag. Checked longest-prefix-first, so 51.38 beats bare 51.
CIP_PREFIX_FLAGS: list[tuple[str, str]] = [
    ("51.38", "nursing"),          # registered nursing / advanced practice
    ("51.39", "nursing"),          # practical nursing / nursing assistants
    ("51.16", "nursing"),          # legacy nursing family, still used by some reporters
    ("51.12", "medical"),          # medicine (MD/DO)
    ("51.20", "pharmacy"),
    ("51.04", "dentistry"),
    ("51.24", "veterinary"),
    ("51.", "allied_health"),      # everything else in the health professions family
    ("60.", "medical"),            # medical residency/internship programs
    ("22.", "law"),
    ("52.", "business"),
    ("14.", "engineering"),
    ("15.", "engineering"),        # engineering technologies
    ("13.", "education"),
    ("42.", "psychology"),
]
ALL_FLAGS = sorted({flag for _, flag in CIP_PREFIX_FLAGS})

# HD has ~75 columns, most of them URLs we never use. Staging all of them adds ~8 MB to a DB
# the dashboard has to download client-side, so we stage this subset instead: everything we
# normalize from, plus the fields a future enrichment pass would plausibly want.
HD_STAGE_COLUMNS = [
    "UNITID", "INSTNM", "IALIAS", "ADDR", "CITY", "STABBR", "ZIP", "FIPS", "COUNTYNM", "CBSA",
    "GENTELE", "EIN", "OPEID", "WEBADDR", "ADMINURL", "SECTOR", "ICLEVEL", "CONTROL", "HLOFFER",
    "DEGGRANT", "HBCU", "HOSPITAL", "MEDICAL", "TRIBAL", "LOCALE", "OPENPUBL", "CYACTIVE",
    "PSEFLAG", "INSTCAT", "C21BASIC", "F1SYSNAM", "F1SYSCOD", "CHFNM", "CHFTITLE",
]

# Segments drive scoring; nursing/allied health is the LWW priority.
PRIORITY_HEALTH_FLAGS = ("nursing", "allied_health", "medical", "pharmacy")


# ---------------------------------------------------------------------------
# Download / load
# ---------------------------------------------------------------------------

def fetch_zip_csv(filename: str, *, refresh: bool = False) -> pd.DataFrame:
    """Download <filename>.zip from the IPEDS data center and return its CSV as a DataFrame."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / f"{filename}.zip"
    if refresh or not cached.exists():
        url = f"{BASE_URL}/{filename}.zip"
        log.info("downloading %s", url)
        response = common.polite_get(url, timeout=180)
        if response is None:
            raise RuntimeError(f"robots.txt disallows {url}")
        response.raise_for_status()
        cached.write_bytes(response.content)
        log.info("cached %s (%.1f MB)", cached.name, len(response.content) / 1e6)
    else:
        log.info("using cached %s", cached.name)

    with zipfile.ZipFile(io.BytesIO(cached.read_bytes())) as archive:
        # Prefer the revised file (…_rv.csv) when NCES has published one.
        names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        name = next((n for n in names if "_rv" in n.lower()), names[0])
        with archive.open(name) as handle:
            # IPEDS ships latin-1 with a UTF-8 BOM on the header; low_memory off for mixed types.
            return pd.read_csv(handle, encoding="latin-1", low_memory=False)


def _clean_columns(frame: pd.DataFrame) -> pd.DataFrame:
    # IPEDS CSVs are latin-1 but carry a UTF-8 BOM, which decodes to 'ï»¿' on the first
    # column name. Strip both forms so 'UNITID' is always addressable.
    frame.columns = [
        re.sub(r"^(?:﻿|ï»¿)+", "", str(c).strip()).upper() for c in frame.columns
    ]
    return frame


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------

def build_program_flags(completions: pd.DataFrame) -> dict[int, dict[str, int]]:
    """UNITID -> {flag: 1} from CIP codes with at least one award conferred."""
    completions = _clean_columns(completions)
    frame = completions[["UNITID", "CIPCODE", "CTOTALT"]].copy()
    frame["CIPCODE"] = frame["CIPCODE"].astype(str).str.strip()
    frame["CTOTALT"] = pd.to_numeric(frame["CTOTALT"], errors="coerce").fillna(0)
    # CIPCODE 99 rows are institution grand totals, not programs.
    frame = frame[(frame["CTOTALT"] > 0) & (~frame["CIPCODE"].str.startswith("99"))]

    def flag_for(cip: str) -> str | None:
        for prefix, flag in CIP_PREFIX_FLAGS:
            if cip.startswith(prefix):
                return flag
        return None

    frame["flag"] = frame["CIPCODE"].map(flag_for)
    frame = frame.dropna(subset=["flag"])

    flags: dict[int, dict[str, int]] = {}
    for unitid, flag in zip(frame["UNITID"], frame["flag"]):
        flags.setdefault(int(unitid), {})[flag] = 1
    return flags


def build_enrollment(effy: pd.DataFrame) -> dict[int, float]:
    """UNITID -> 12-month unduplicated headcount (all students, all levels)."""
    effy = _clean_columns(effy)
    total = effy[effy["EFFYALEV"] == 1]
    return {
        int(u): float(v)
        for u, v in zip(total["UNITID"], pd.to_numeric(total["EFYTOTLT"], errors="coerce"))
        if pd.notna(v)
    }


def derive_segment(row: pd.Series, flags: dict[str, int]) -> str:
    health = [f for f in PRIORITY_HEALTH_FLAGS if flags.get(f)]
    sector = SECTOR_LABEL.get(int(row.get("SECTOR", 99) or 99), "Sector unknown")
    if health:
        return f"{sector} - health programs ({', '.join(health)})"
    return sector


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

INSERT_SQL = """
INSERT INTO organizations
    (name, name_normalized, org_type, track, segment, website_domain, state, city, address,
     size_metric, size_metric_type, programs_flags, source, notes)
VALUES (:name, :name_normalized, :org_type, 'A', :segment, :website_domain, :state, :city,
        :address, :size_metric, 'enrollment', :programs_flags, 'ipeds', :notes)
"""

UPDATE_SQL = """
UPDATE organizations SET
    name           = :name,
    name_normalized= :name_normalized,
    org_type       = :org_type,
    segment        = :segment,
    website_domain = :website_domain,
    state          = :state,
    city           = :city,
    address        = :address,
    size_metric    = COALESCE(:size_metric, size_metric),
    programs_flags = :programs_flags,
    notes          = :notes,
    date_updated   = datetime('now')
WHERE id = :id
"""


def run(year: int = DEFAULT_YEAR, *, refresh: bool = False, limit: int | None = None) -> None:
    conn = common.connect()
    directory = _clean_columns(fetch_zip_csv(f"HD{year}", refresh=refresh))
    enrollment = build_enrollment(fetch_zip_csv(f"EFFY{year}", refresh=refresh))
    program_flags = build_program_flags(fetch_zip_csv(f"C{year}_A", refresh=refresh))

    # Active, primarily-postsecondary, degree/certificate-granting institutions only.
    active = directory[(directory["CYACTIVE"] == 1) & (directory["PSEFLAG"] == 1)]
    if limit:
        active = active.head(limit)
    log.info("directory rows: %d total, %d active postsecondary", len(directory), len(active))

    with common.ingest_run(conn, SOURCE) as counters:
        for _, row in active.iterrows():
            counters["seen"] += 1
            unitid = int(row["UNITID"])
            try:
                flags = {flag: 0 for flag in ALL_FLAGS} | program_flags.get(unitid, {})
                sector = int(row.get("SECTOR", 99) or 99)
                name = str(row["INSTNM"]).strip()
                domain = common.normalize_domain(row.get("WEBADDR"))
                payload = {
                    k: (None if pd.isna(row[k]) else row[k])
                    for k in HD_STAGE_COLUMNS if k in row.index
                }

                common.stage_record(
                    conn, source=SOURCE, record_type="organization", payload=payload,
                    source_key=str(unitid), source_version=str(year),
                    source_url=f"{BASE_URL}/HD{year}.zip",
                )

                params = {
                    "name": name,
                    "name_normalized": common.normalize_name(name),
                    "org_type": SECTOR_TO_ORG_TYPE.get(sector, "other"),
                    "segment": derive_segment(row, flags),
                    "website_domain": domain,
                    "state": common.normalize_state(row.get("STABBR")),
                    "city": (str(row["CITY"]).strip() if pd.notna(row.get("CITY")) else None),
                    "address": (str(row["ADDR"]).strip() if pd.notna(row.get("ADDR")) else None),
                    "size_metric": enrollment.get(unitid),
                    "programs_flags": json.dumps(flags),
                    "notes": json.dumps({
                        "ipeds_unitid": unitid,
                        "sector": SECTOR_LABEL.get(sector, "unknown"),
                        "control": CONTROL_LABEL.get(int(row.get("CONTROL", 0) or 0), "unknown"),
                        "has_hospital": int(row.get("HOSPITAL", 0) or 0) == 1,
                        "grants_medical_degree": int(row.get("MEDICAL", 0) or 0) == 1,
                    }),
                }

                # Identity is UNITID, not domain: branch campuses of one system share a
                # website but are separate buyers (own bookstore, own library budget).
                existing = conn.execute(
                    "SELECT id FROM organizations "
                    " WHERE json_extract(notes, '$.ipeds_unitid') = ?", (unitid,)).fetchone()

                if domain:
                    holder = conn.execute(
                        "SELECT json_extract(notes, '$.ipeds_unitid') FROM organizations"
                        "  WHERE website_domain = ?", (domain,)).fetchone()
                    if holder is not None and (existing is None or holder[0] != unitid):
                        # Domain already belongs to a sibling campus — keep this row distinct
                        # and let enrich/dedupe.py decide later whether they should merge.
                        params["website_domain"] = None
                        params["notes"] = json.dumps(
                            json.loads(params["notes"]) | {"shared_domain": domain})

                if existing:
                    conn.execute(UPDATE_SQL, params | {"id": existing[0]})
                    counters["updated"] += 1
                else:
                    conn.execute(INSERT_SQL, params)
                    counters["new"] += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not kill the run
                counters["errors"] += 1
                log.warning("UNITID %s failed: %s: %s", unitid, type(exc).__name__, exc)

            if counters["seen"] % 500 == 0:
                conn.commit()
                log.info("... %d rows processed", counters["seen"])
        conn.commit()

    report(conn)


def report(conn) -> None:
    total = conn.execute("SELECT COUNT(*) FROM organizations WHERE source='ipeds'").fetchone()[0]
    print(f"\norganizations from IPEDS: {total}\n")
    print("By org_type:")
    for row in conn.execute(
        "SELECT org_type, COUNT(*) n, CAST(AVG(size_metric) AS INT) avg_size"
        "  FROM organizations WHERE source='ipeds' GROUP BY org_type ORDER BY n DESC"
    ):
        print(f"  {row['org_type']:20} {row['n']:6}  avg enrollment {row['avg_size']}")
    print("\nHealth-program institutions (LWW priority):")
    for flag in ("nursing", "allied_health", "medical", "pharmacy"):
        n = conn.execute(
            f"SELECT COUNT(*) FROM organizations "
            f"WHERE source='ipeds' AND json_extract(programs_flags, '$.{flag}') = 1").fetchone()[0]
        print(f"  {flag:15} {n}")
    print("\nTop 15 states:")
    for row in conn.execute(
        "SELECT state, COUNT(*) n FROM organizations WHERE source='ipeds'"
        " GROUP BY state ORDER BY n DESC LIMIT 15"
    ):
        print(f"  {row['state']}  {row['n']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import IPEDS institutions into leads.db")
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR)
    parser.add_argument("--refresh", action="store_true", help="re-download even if cached")
    parser.add_argument("--limit", type=int, help="process only the first N rows (smoke test)")
    args = parser.parse_args()
    common.setup_logging()
    run(args.year, refresh=args.refresh, limit=args.limit)


if __name__ == "__main__":
    main()
