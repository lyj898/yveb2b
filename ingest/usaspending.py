"""Federal book vendors from USAspending — a systematic way to find Track B companies.

Every federal contract is public at api.usaspending.gov, with no key and no login. Filtering
awards to the book product-service codes (7610 books, 7630 periodicals, 7640 maps, 7670
microfilm) and to NAICS 424920 (book wholesalers) gives the companies that actually deliver
books to government buyers — distributors, jobbers and subscription agents at working scale.
That is the same population that buys overstock, and unlike a trade directory it is evidence:
each name comes with what they sold and for how much.

Two filters keep the list honest:

* **Relevance** — those PSCs also carry defence technical publications, so an award has to
  read like a book buy (and not like an avionics manual) to count.
* **Publishers are skipped** — McGraw Hill and Elsevier show up here, but they are our
  suppliers, not counterparties for surplus stock. They are logged, not stored.

Run:  python -m ingest.usaspending [--years 3] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common  # noqa: E402

SOURCE = "usaspending"
API_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
PAGE_SIZE = 100
MAX_PAGES = 5
DEFAULT_YEARS = 3

log = logging.getLogger("textbook-leads.usaspending")

PSC_CODES = ["7610", "7630", "7640", "7670"]
NAICS_CODES = ["424920"]
AWARD_TYPES = ["A", "B", "C", "D"]          # definitive contracts and IDV variants

# An award has to look like a book buy. Checked against description + recipient name.
RELEVANT = re.compile(
    r"\b(book|textbook|publication|subscription|journal|periodical|serial|librar|"
    r"instructional material|course material|curriculum|educational material|"
    r"e-?book|reference material|monograph)", re.I)
# …and not like a defence documentation contract, which shares the same PSCs.
IRRELEVANT = re.compile(
    r"(interactive electronic technical|ietm|technical manual|depot|avionic|aircraft|"
    r"weapon|missile|schematic|sustainment|logistics support|engineering services|"
    r"configuration management|maintenance manual)", re.I)
# Publishers are our suppliers, not surplus counterparties.
PUBLISHER = re.compile(
    r"(mcgraw|elsevier|wolters|wiley|pearson|cengage|springer|sage publi|taylor & francis|"
    r"oxford university press|cambridge university press|houghton mifflin|scholastic|"
    r"publishing|publishers|press, inc|west publishing)", re.I)

# Plenty of these awards are database access, not physical stock. We keep them — a serials
# agent still moves print — but flag them so the print-first view can filter them out.
DIGITAL = re.compile(r"(online|electronic|e-?resource|database|web|digital|access to|"
                     r"platform|streaming)", re.I)

FIELDS = ["Award ID", "Recipient Name", "Awarding Agency", "Awarding Sub Agency",
          "Award Amount", "Description", "Place of Performance State Code", "Start Date",
          "End Date", "recipient_id"]

# Individual awards kept per vendor, largest first — this is the evidence behind the total.
AWARDS_KEPT = 10
AWARD_URL = "https://www.usaspending.gov/award/{internal_id}"


def query(body: dict) -> list[dict]:
    import requests

    results: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        response = requests.post(API_URL, json=body | {"page": page}, timeout=120,
                                 headers={"User-Agent": common.USER_AGENT,
                                          "Content-Type": "application/json"})
        if response.status_code >= 400:
            log.warning("USAspending %s: %s", response.status_code, response.text[:200])
            break
        payload = response.json()
        batch = payload.get("results") or []
        results.extend(batch)
        if not payload.get("page_metadata", {}).get("hasNext"):
            break
        time.sleep(1.0)          # public API, no key — stay gentle
    return results


def build_queries(years: int) -> list[dict]:
    start_year = int(common.today()[:4]) - years
    window = [{"start_date": f"{start_year}-01-01", "end_date": common.today()}]
    base = {"fields": FIELDS, "limit": PAGE_SIZE, "sort": "Award Amount", "order": "desc",
            "subawards": False}
    queries = [base | {"filters": {"award_type_codes": AWARD_TYPES, "psc_codes": [psc],
                                   "time_period": window}} for psc in PSC_CODES]
    queries += [base | {"filters": {"award_type_codes": AWARD_TYPES, "naics_codes": NAICS_CODES,
                                    "time_period": window}}]
    return queries


def is_relevant(award: dict) -> bool:
    haystack = f"{award.get('Description') or ''} {award.get('Recipient Name') or ''}"
    return bool(RELEVANT.search(haystack)) and not IRRELEVANT.search(haystack)


def aggregate(awards: list[dict]) -> dict[str, dict]:
    """One entry per recipient: totals, states, agencies and the largest award seen."""
    vendors: dict[str, dict] = defaultdict(
        lambda: {"name": None, "total": 0.0, "awards": 0, "states": set(),
                 "agencies": set(), "top_description": None, "top_amount": 0.0,
                 "detail": []})
    for award in awards:
        name = (award.get("Recipient Name") or "").strip()
        if not name:
            continue
        key = common.normalize_name(name) or name.lower()
        vendor = vendors[key]
        vendor["name"] = vendor["name"] or name.title()
        amount = float(award.get("Award Amount") or 0)
        vendor["total"] += amount
        vendor["awards"] += 1
        if award.get("Place of Performance State Code"):
            vendor["states"].add(award["Place of Performance State Code"])
        if award.get("Awarding Agency"):
            vendor["agencies"].add(award["Awarding Agency"])
        if amount >= vendor["top_amount"]:
            vendor["top_amount"] = amount
            vendor["top_description"] = (award.get("Description") or "").strip()[:200]
        vendor["detail"].append({
            "award_id": award.get("Award ID"),
            "amount": amount,
            "start": (award.get("Start Date") or "")[:10] or None,
            "end": (award.get("End Date") or "")[:10] or None,
            "agency": award.get("Awarding Agency"),
            "sub_agency": award.get("Awarding Sub Agency"),
            "state": award.get("Place of Performance State Code"),
            "description": (award.get("Description") or "").strip()[:240],
            "url": (AWARD_URL.format(internal_id=award["generated_internal_id"])
                    if award.get("generated_internal_id") else None),
        })

    for vendor in vendors.values():
        vendor["detail"].sort(key=lambda a: -a["amount"])
        del vendor["detail"][AWARDS_KEPT:]
    return vendors


UPSERT_WITH_NAME = """
INSERT INTO organizations
    (name, name_normalized, org_type, track, segment, state, source, status, programs_flags, notes)
VALUES (:name, :name_normalized, 'wholesaler', 'B', :segment, :state, :source, 'new', '{}', :notes)
"""


def run(*, years: int = DEFAULT_YEARS, dry_run: bool = False) -> None:
    conn = common.connect()
    raw: list[dict] = []

    with common.ingest_run(conn, SOURCE) as counters:
        for body in build_queries(years):
            focus = body["filters"].get("psc_codes") or body["filters"].get("naics_codes")
            try:
                batch = query(body)
            except Exception as exc:  # noqa: BLE001
                counters["errors"] += 1
                log.warning("query %s failed: %s: %s", focus, type(exc).__name__, exc)
                continue
            log.info("%-12s %d awards", focus[0], len(batch))
            raw.extend(batch)

        # One award can match several of our queries (a textbook contract carries both
        # PSC 7610 and NAICS 424920). Without this, its value is counted once per query and
        # every vendor total is inflated.
        unique: dict[str, dict] = {}
        for award in raw:
            key = str(award.get("generated_internal_id") or award.get("Award ID") or id(award))
            unique.setdefault(key, award)
        duplicates = len(raw) - len(unique)
        if duplicates:
            log.info("dropped %d awards matched by more than one query", duplicates)
        raw = list(unique.values())

        for award in raw:
            common.stage_record(
                conn, source=SOURCE, record_type="organization", payload=award,
                source_key=str(award.get("generated_internal_id") or award.get("Award ID")),
                source_url=API_URL)
        conn.commit()

        relevant = [a for a in raw if is_relevant(a)]
        log.info("%d awards staged, %d read like book buys", len(raw), len(relevant))

        vendors = aggregate(relevant)
        publishers = {k: v for k, v in vendors.items() if PUBLISHER.search(v["name"])}
        for vendor in publishers.values():
            log.info("skipping publisher (supplier, not counterparty): %s", vendor["name"])
        vendors = {k: v for k, v in vendors.items() if k not in publishers}
        log.info("%d vendors after removing %d publishers", len(vendors), len(publishers))

        for key, vendor in sorted(vendors.items(), key=lambda kv: -kv[1]["total"]):
            counters["seen"] += 1
            if dry_run:
                continue
            try:
                state = sorted(vendor["states"])[0] if len(vendor["states"]) == 1 else None
                notes = json.dumps({
                    "likely_digital": bool(DIGITAL.search(vendor["top_description"] or "")),
                    "federal_awards": vendor["awards"],
                    "federal_total_usd": round(vendor["total"], 2),
                    "agencies": sorted(vendor["agencies"])[:5],
                    "largest_award": vendor["top_description"],
                    "states": sorted(vendor["states"])[:5],
                    "awards_detail": vendor["detail"],
                })
                common.stage_record(conn, source=SOURCE, record_type="organization",
                                    payload={"name": vendor["name"], **json.loads(notes)},
                                    source_key=key, source_url=API_URL)

                existing = conn.execute(
                    "SELECT id, source FROM organizations WHERE name_normalized = ? LIMIT 1",
                    (key,)).fetchone()
                segment = (f"Federal book vendor — ${vendor['total']:,.0f} across "
                           f"{vendor['awards']} award(s)")
                if existing:
                    # Already known (often from the Track B seed list) — enrich, do not duplicate.
                    conn.execute(
                        "UPDATE organizations SET segment = ?, notes = ?,"
                        "       date_updated = datetime('now') WHERE id = ?",
                        (segment, notes, existing["id"]))
                    counters["updated"] += 1
                else:
                    conn.execute(UPSERT_WITH_NAME, {
                        "name": vendor["name"], "name_normalized": key,
                        "segment": segment, "state": state, "source": SOURCE, "notes": notes})
                    counters["new"] += 1
            except Exception as exc:  # noqa: BLE001
                counters["errors"] += 1
                log.warning("%s failed: %s: %s", vendor["name"], type(exc).__name__, exc)
        conn.commit()

    report(conn, dry_run=dry_run, vendors=vendors if dry_run else None)


def report(conn, *, dry_run: bool = False, vendors: dict | None = None) -> None:
    if dry_run and vendors is not None:
        print(f"\n{len(vendors)} vendors would be stored:\n")
        for vendor in sorted(vendors.values(), key=lambda v: -v["total"])[:25]:
            print(f"  {vendor['name'][:38]:38} ${vendor['total']:>12,.0f}  "
                  f"{vendor['awards']:>3} awards  {(vendor['top_description'] or '')[:40]}")
        return

    total = conn.execute(
        "SELECT COUNT(*) FROM organizations WHERE source = ?", (SOURCE,)).fetchone()[0]
    print(f"\nFederal book vendors stored: {total}\n")
    for row in conn.execute(
        "SELECT name, state, segment FROM organizations WHERE source = ?"
        " ORDER BY CAST(json_extract(notes, '$.federal_total_usd') AS REAL) DESC LIMIT 20",
        (SOURCE,)
    ):
        print(f"  {row['name'][:40]:40} {row['state'] or '--'}  {row['segment']}")
    print(f"\nTrack B total: "
          f"{conn.execute(chr(83) + 'ELECT COUNT(*) FROM organizations WHERE track IN (' + chr(39) + 'B' + chr(39) + ', ' + chr(39) + 'both' + chr(39) + ')').fetchone()[0]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Find federal book vendors via USAspending")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS)
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = parser.parse_args()
    common.setup_logging()
    run(years=args.years, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
