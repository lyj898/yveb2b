"""Georgia Procurement Registry (GPR) — open solicitations for GA state, county, city,
K-12 and university-system buyers.

The registry is a DataTables front end over a public JSON endpoint:

    POST https://ssl.doas.state.ga.us/gpr/eventSearch      (no key, no login)

Its ``eventIdTitle`` filter matches the title only and misses anything phrased differently,
so we pull every open event once (a few hundred rows) and apply the relevance filter here,
where it is one editable constant rather than a dozen server round-trips.

Run:  python -m ingest.state_portals.ga [--all] [--limit N]
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import common  # noqa: E402

SOURCE = "state_portal_ga"
BASE = "https://ssl.doas.state.ga.us/gpr"
SEARCH_URL = f"{BASE}/eventSearch"
INDEX_URL = f"{BASE}/index"
DETAIL_URL = f"{BASE}/eventDetails?eSourceNumber={{key}}&sourceSystemType={{source}}"
PAGE_SIZE = 200

log = logging.getLogger("textbook-leads.ga")

# --- Relevance filter — edit freely ---------------------------------------
# Matched case-insensitively against the solicitation title.
KEYWORDS = [
    "textbook", "text book", "book", "library material", "library resource",
    "instructional material", "course material", "curriculum", "learning resource",
    "periodical", "serial", "subscription", "e-book", "ebook", "audiobook",
    "publication", "print material", "media center", "reference material",
    "nursing education", "medical reference", "database subscription",
]
# Titles that would otherwise trip the "book" / "print" style keywords.
NEGATIVE = [
    "bookkeeping", "book keeping", "booking", "printer", "printing services",
    "book bindery equipment", "bookmobile chassis", "notebook computer", "chromebook",
]
# Buyer types that could plausibly buy course materials at all.
RELEVANT_GOV_TYPES = {"state", "K-12", "county", "city", "other"}

REQUEST_TYPE_TO_SIGNAL = {
    "Request for Proposal": "rfp",
    "Request for Information": "rfp",
    "Request for Quote": "open_tender",
    "Notice": "other",
    "Sole Source": "other",
}


def session():
    """A session that has visited the index page — the endpoint expects the same cookies."""
    import requests

    http = requests.Session()
    http.headers.update({
        "User-Agent": common.USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": INDEX_URL,
    })
    common.polite_get(INDEX_URL, session=http, timeout=45)
    return http


def payload(start: int, length: int) -> dict:
    data = {
        "draw": 1, "start": start, "length": length,
        "search[value]": "", "search[regex]": "false",
        "order[0][column]": 5, "order[0][dir]": "asc",
        "responseType": "ALL", "eventStatus": "OPEN", "eventIdTitle": "",
        "govType": "ALL", "govEntity": "", "catType": "ALL", "eventProcessType": "ALL",
        "dateRangeType": "", "rangeStartDate": "", "rangeEndDate": "",
        "isReset": "false", "persisted": "false", "refreshSearchData": "false",
    }
    for column in range(8):
        data[f"columns[{column}][data]"] = str(column)
        data[f"columns[{column}][searchable]"] = "true"
        data[f"columns[{column}][orderable]"] = "true"
        data[f"columns[{column}][search][value]"] = ""
    return data


def fetch_open_events(http, limit: int | None = None) -> list[dict]:
    events: list[dict] = []
    start = 0
    while True:
        response = http.post(SEARCH_URL, data=payload(start, PAGE_SIZE), timeout=90)
        response.raise_for_status()
        body = response.json()
        batch = body.get("data") or []
        events.extend(batch)
        total = body.get("recordsFiltered", len(events))
        log.info("  fetched %d/%s open events", len(events), total)
        start += PAGE_SIZE
        if not batch or len(events) >= total or (limit and len(events) >= limit):
            break
    return events[:limit] if limit else events


def is_relevant(event: dict) -> bool:
    title = (event.get("title") or "").lower()
    if any(bad in title for bad in NEGATIVE):
        return False
    if event.get("governmentType") not in RELEVANT_GOV_TYPES:
        return False
    return any(re.search(rf"\b{re.escape(word)}", title) for word in KEYWORDS)


def match_or_create_org(conn, event: dict) -> int | None:
    agency = (event.get("agencyName") or "").strip()
    if not agency:
        return None
    normalized = common.normalize_name(agency)
    if not normalized:
        return None

    hit = conn.execute(
        "SELECT id FROM organizations WHERE name_normalized = ? AND (state = 'GA' OR state IS NULL)"
        " ORDER BY size_metric DESC LIMIT 1", (normalized,)).fetchone()
    if hit:
        return hit[0]

    # School boards and county governments are buyers we do not have from IPEDS.
    org_type = "gov_agency"
    if event.get("governmentType") == "K-12" or "board of education" in agency.lower():
        org_type = "gov_agency"
    cur = conn.execute(
        """INSERT INTO organizations (name, name_normalized, org_type, track, segment,
                                      state, source, status)
           VALUES (?, ?, ?, 'A', ?, 'GA', ?, 'new')""",
        (agency, normalized, org_type,
         f"Georgia {event.get('governmentType') or 'public'} buyer", SOURCE))
    return cur.lastrowid


UPSERT_SIGNAL = """
INSERT INTO signals
    (org_id, org_name_raw, signal_type, title, url, reference_number, deadline, posted_date,
     state, source, source_key, status)
VALUES (:org_id, :org_name_raw, :signal_type, :title, :url, :reference_number, :deadline,
        :posted_date, 'GA', :source, :source_key, :status)
ON CONFLICT (source, source_key) WHERE source_key IS NOT NULL DO UPDATE SET
    org_id       = COALESCE(excluded.org_id, signals.org_id),
    title        = excluded.title,
    url          = excluded.url,
    deadline     = excluded.deadline,
    status       = excluded.status,
    date_updated = datetime('now')
"""


def run(*, keep_all: bool = False, limit: int | None = None) -> None:
    if not common.robots_allows(SEARCH_URL):
        raise SystemExit("robots.txt disallows the GPR search endpoint — record it in SOURCES.md")

    conn = common.connect()
    http = session()

    with common.ingest_run(conn, SOURCE) as counters:
        events = fetch_open_events(http, limit)
        log.info("%d open events statewide", len(events))

        for event in events:
            counters["seen"] += 1
            key = event.get("esourceNumberKey") or event.get("esourceNumber")
            try:
                common.stage_record(
                    conn, source=SOURCE, record_type="signal", payload=event,
                    source_key=key, source_url=INDEX_URL)

                if not keep_all and not is_relevant(event):
                    continue

                existed = conn.execute(
                    "SELECT 1 FROM signals WHERE source = ? AND source_key = ?",
                    (SOURCE, key)).fetchone()
                deadline = (event.get("closingDate") or "")[:10] or None
                conn.execute(UPSERT_SIGNAL, {
                    "org_id": match_or_create_org(conn, event),
                    "org_name_raw": event.get("agencyName"),
                    "signal_type": REQUEST_TYPE_TO_SIGNAL.get(
                        event.get("bidProcessType"), "open_tender"),
                    "title": (event.get("title") or "Untitled solicitation").strip(),
                    "url": DETAIL_URL.format(key=key, source=event.get("sourceId")),
                    "reference_number": event.get("esourceNumber"),
                    "deadline": deadline,
                    "posted_date": (event.get("postingDate") or "")[:10] or None,
                    "source": SOURCE,
                    "source_key": key,
                    "status": "open",
                })
                counters["updated" if existed else "new"] += 1
            except Exception as exc:  # noqa: BLE001 — one bad row never kills a run
                counters["errors"] += 1
                log.warning("event %s failed: %s: %s", key, type(exc).__name__, exc)

        conn.execute(
            "UPDATE signals SET status = 'expired', date_updated = datetime('now')"
            " WHERE source = ? AND status = 'open' AND deadline < date('now')", (SOURCE,))
        conn.commit()

    report(conn)


def report(conn) -> None:
    staged, kept = conn.execute(
        "SELECT (SELECT COUNT(*) FROM source_records WHERE source = ?),"
        "       (SELECT COUNT(*) FROM signals WHERE source = ?)", (SOURCE, SOURCE)).fetchone()
    print(f"\nGeorgia: {staged} open events staged, {kept} kept as signals\n")
    for row in conn.execute(
        "SELECT deadline, org_name_raw, title, reference_number FROM signals"
        " WHERE source = ? AND status = 'open' ORDER BY deadline LIMIT 20", (SOURCE,)
    ):
        print(f"  {row['deadline']}  {(row['org_name_raw'] or '?')[:34]:34} {row['title'][:60]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll the Georgia Procurement Registry")
    parser.add_argument("--all", dest="keep_all", action="store_true",
                        help="keep every open event, not just book/library-relevant ones")
    parser.add_argument("--limit", type=int, help="stop after N events (smoke test)")
    args = parser.parse_args()
    common.setup_logging()
    run(keep_all=args.keep_all, limit=args.limit)


if __name__ == "__main__":
    main()
