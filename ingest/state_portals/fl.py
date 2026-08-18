"""Florida — MyFloridaMarketPlace Vendor Bid System.

The vendor portal is an Angular app over a public JSON search:

    POST https://vendor.myfloridamarketplace.com/mfmp/pub/search/bids       (no key, no login)
    POST https://vendor.myfloridamarketplace.com/mfmp/pub/search/bids/count

``title`` is a substring match and an empty title returns nothing, so we issue one query per
keyword and de-duplicate on ``advertisementId``. Everything is staged; only advertisements
that are still live (not closed/withdrawn/cancelled, deadline in the future) become signals.

Run:  python -m ingest.state_portals.fl [--all] [--keyword textbook]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import common  # noqa: E402

SOURCE = "state_portal_fl"
BASE = "https://vendor.myfloridamarketplace.com"
SEARCH_URL = f"{BASE}/mfmp/pub/search/bids"
COUNT_URL = f"{SEARCH_URL}/count"
PORTAL_URL = f"{BASE}/search/bids"
DETAIL_URL = f"{BASE}/search/bids/detail/{{ad_id}}"
PAGE_SIZE = 100
MAX_PAGES = 20

log = logging.getLogger("textbook-leads.fl")

# --- Search terms — edit freely -------------------------------------------
KEYWORDS = [
    "textbook", "book", "library", "instructional material", "course material",
    "curriculum", "periodical", "subscription", "e-book", "publication",
    "learning resource", "nursing education", "medical reference",
]
# Titles that match a keyword but are not a materials buy.
NEGATIVE = [
    # K-12 — not our market
    "k-12", "k12", "k-5", "k-8", "elementary", "middle school", "high school", "pre-k",
    "bookkeeping", "booking", "bookmobile", "chromebook", "notebook computer",
    "library automation", "space utilization", "library building", "roof",
    "flooring", "renovation", "construction",
]
# MFMP advertisement statuses that mean the opportunity is gone.
DEAD_STATUSES = {"CLOSED", "WITHDRAWN", "CANCELLED", "CANCELED", "COMPLETE", "COMPLETED"}

TYPE_TO_SIGNAL = {
    "Request for Proposals": "rfp",
    "Request for Information": "rfp",
    "Invitation to Bid": "open_tender",
    "Invitation to Negotiate": "open_tender",
    "Agency Decision": "other",
    "Single Source": "other",
}


def session():
    import requests

    http = requests.Session()
    http.headers.update({
        "User-Agent": common.USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": PORTAL_URL,
    })
    common.polite_get(PORTAL_URL, session=http, timeout=45)
    return http


def query(term: str, page: int) -> dict:
    return {
        "pageSize": PAGE_SIZE, "page": page, "title": term,
        "type": [], "status": [], "agency": [], "commodityCodes": [],
        "adNumber": "", "agencyAdvertisementNumber": "", "publishedDate": "",
        "openDate": "", "endDate": "", "intendsToParticipate": "", "assignee": "",
    }


def search(http, term: str) -> list[dict]:
    found: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        response = http.post(SEARCH_URL, json=query(term, page), timeout=90)
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list) or not batch:
            break
        found.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return found


def is_live(ad: dict) -> bool:
    if (ad.get("status") or "").upper() in DEAD_STATUSES:
        return False
    close = (ad.get("closeDate") or "")[:10]
    return bool(close) and close >= common.today()


def is_relevant(ad: dict) -> bool:
    title = (ad.get("title") or "").lower()
    return not any(bad in title for bad in NEGATIVE)


def match_or_create_org(conn, ad: dict) -> int | None:
    agency = (ad.get("agency") or (ad.get("organization") or {}).get("name") or "").strip()
    if not agency:
        return None
    normalized = common.normalize_name(agency)
    if not normalized:
        return None
    hit = conn.execute(
        "SELECT id FROM organizations WHERE name_normalized = ? AND (state = 'FL' OR state IS NULL)"
        " ORDER BY size_metric DESC LIMIT 1", (normalized,)).fetchone()
    if hit:
        return hit[0]
    cur = conn.execute(
        """INSERT INTO organizations (name, name_normalized, org_type, track, segment,
                                      state, source, status)
           VALUES (?, ?, 'gov_agency', 'A', 'Florida state agency', 'FL', ?, 'new')""",
        (agency, normalized, SOURCE))
    return cur.lastrowid


UPSERT_SIGNAL = """
INSERT INTO signals
    (org_id, org_name_raw, signal_type, title, url, reference_number, deadline, posted_date,
     state, source, source_key, status)
VALUES (:org_id, :org_name_raw, :signal_type, :title, :url, :reference_number, :deadline,
        :posted_date, 'FL', :source, :source_key, 'open')
ON CONFLICT (source, source_key) WHERE source_key IS NOT NULL DO UPDATE SET
    org_id       = COALESCE(excluded.org_id, signals.org_id),
    title        = excluded.title,
    deadline     = excluded.deadline,
    status       = 'open',
    date_updated = datetime('now')
"""


def run(*, keep_all: bool = False, keywords: list[str] | None = None) -> None:
    if not common.robots_allows(SEARCH_URL):
        raise SystemExit("robots.txt disallows the MFMP search endpoint — record it in SOURCES.md")

    conn = common.connect()
    http = session()
    seen_ids: set[int] = set()

    with common.ingest_run(conn, SOURCE) as counters:
        for term in (keywords or KEYWORDS):
            try:
                ads = search(http, term)
            except Exception as exc:  # noqa: BLE001 — a failed term must not kill the run
                counters["errors"] += 1
                log.warning("term %r failed: %s: %s", term, type(exc).__name__, exc)
                continue
            log.info("%-22r %d advertisements", term, len(ads))

            for ad in ads:
                ad_id = ad.get("advertisementId")
                if ad_id in seen_ids:
                    continue          # the same ad matches several keywords
                seen_ids.add(ad_id)
                counters["seen"] += 1
                try:
                    common.stage_record(
                        conn, source=SOURCE, record_type="signal", payload=ad,
                        source_key=str(ad_id), source_url=PORTAL_URL)

                    if not keep_all and not (is_live(ad) and is_relevant(ad)):
                        continue

                    existed = conn.execute(
                        "SELECT 1 FROM signals WHERE source = ? AND source_key = ?",
                        (SOURCE, str(ad_id))).fetchone()
                    conn.execute(UPSERT_SIGNAL, {
                        "org_id": match_or_create_org(conn, ad),
                        "org_name_raw": ad.get("agency"),
                        "signal_type": TYPE_TO_SIGNAL.get(ad.get("type"), "open_tender"),
                        "title": (ad.get("title") or "Untitled advertisement").strip(),
                        "url": DETAIL_URL.format(ad_id=ad_id),
                        "reference_number": ad.get("agencyAdNumber") or ad.get("uniqueName"),
                        "deadline": (ad.get("closeDate") or "")[:10] or None,
                        "posted_date": (ad.get("publishDate") or "")[:10] or None,
                        "source": SOURCE,
                        "source_key": str(ad_id),
                    })
                    counters["updated" if existed else "new"] += 1
                except Exception as exc:  # noqa: BLE001
                    counters["errors"] += 1
                    log.warning("ad %s failed: %s: %s", ad_id, type(exc).__name__, exc)
            conn.commit()

        conn.execute(
            "UPDATE signals SET status = 'expired', date_updated = datetime('now')"
            " WHERE source = ? AND status = 'open' AND deadline < date('now')", (SOURCE,))
        conn.commit()

    report(conn)


def report(conn) -> None:
    staged, kept = conn.execute(
        "SELECT (SELECT COUNT(*) FROM source_records WHERE source = ?),"
        "       (SELECT COUNT(*) FROM signals WHERE source = ?)", (SOURCE, SOURCE)).fetchone()
    print(f"\nFlorida: {staged} advertisements staged, {kept} kept as signals\n")
    for row in conn.execute(
        "SELECT deadline, org_name_raw, title, reference_number FROM signals"
        " WHERE source = ? ORDER BY deadline DESC LIMIT 15", (SOURCE,)
    ):
        print(f"  {row['deadline']}  {(row['org_name_raw'] or '?')[:30]:30} {row['title'][:58]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll the Florida Vendor Bid System")
    parser.add_argument("--all", dest="keep_all", action="store_true",
                        help="keep every advertisement, including closed ones")
    parser.add_argument("--keyword", action="append", dest="keywords",
                        help="search this term instead of the default list (repeatable)")
    args = parser.parse_args()
    common.setup_logging()
    run(keep_all=args.keep_all, keywords=args.keywords)


if __name__ == "__main__":
    main()
