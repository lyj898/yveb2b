"""SAM.gov Get Opportunities poller — the signals engine.

Pulls active federal solicitations for books / educational materials and upserts them into
``signals``. Federal buyers relevant to us: VA and DoD hospitals and nursing programs,
Bureau of Prisons education, federal academies, Indian Health Service, agency libraries.

API:  https://api.sam.gov/opportunities/v2/search   (public, requires a free api_key)
Key:  environment variable SAM_GOV_API_KEY (GitHub Actions secret in CI). Never hardcoded.

Run:  python -m ingest.samgov_poller [--days 14] [--limit 200] [--dry-run]

The API caps postedFrom/postedTo to a one-year window and 1000 records per page; we page
through with offset and stop when a page comes back short.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common  # noqa: E402

SOURCE = "sam_gov"
API_URL = "https://api.sam.gov/opportunities/v2/search"
PAGE_SIZE = 1000
MAX_PAGES = 20

# Non-federal public API keys are capped at 10 requests PER DAY (federal keys get 1,000),
# and a paged query spends one request per page. The budget is enforced here rather than
# discovered as a 429 halfway through a run, and whatever it cuts is logged — a silent
# truncation would look exactly like "no solicitations today".
REQUEST_BUDGET = 10

# --- Filter config — edit these freely; they are the whole targeting surface -------------
#
# NAICS: 511130 book publishers, 424920 book/periodical merchant wholesalers,
#        459210 book retailers, 541519 (some library services buys land here).
NAICS_CODES = ["511130", "424920", "459210"]

# PSC (Product Service Codes) for books and publications:
#   7610 books and pamphlets            7630 newspapers and periodicals
#   7640 maps/atlases/charts            7650 drawings and specifications
#   7660 sheet and book music           7670 microfilm/processed
#   R708 support: publication services  76   parent group
PSC_CODES = ["7610", "7630", "7640", "7670", "76"]

# Free-text fallback: SAM.gov classifies inconsistently, so a title sweep catches notices
# that carry no useful NAICS/PSC. Each term is a separate API call, and nine of them do not
# fit a 10-request budget — so titles are opt-in (--with-titles), for a weekly deeper run.
TITLE_KEYWORDS = [
    "textbook", "textbooks", "library books", "book supply", "course materials",
    "educational materials", "nursing textbooks", "medical books", "periodicals",
]

# Notice types worth acting on. 'p' presolicitation, 'o' solicitation, 'k' combined synopsis,
# 'r' sources sought. Awards and justifications are excluded — the buy is already gone.
NOTICE_TYPES = ["p", "o", "k", "r"]

DEFAULT_LOOKBACK_DAYS = 30

log = logging.getLogger("textbook-leads.samgov")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def api_key() -> str:
    key = os.environ.get("SAM_GOV_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "SAM_GOV_API_KEY is not set.\n"
            "  local:  $env:SAM_GOV_API_KEY = '<key>'   (PowerShell)\n"
            "  CI:     repository secret SAM_GOV_API_KEY\n"
            "Get a key at https://sam.gov -> Account Details -> Public API key."
        )
    return key


class Budget:
    """Counts API requests so a run cannot exceed the daily cap."""

    def __init__(self, total: int = REQUEST_BUDGET) -> None:
        self.total = total
        self.spent = 0

    def take(self) -> bool:
        if self.spent >= self.total:
            return False
        self.spent += 1
        return True

    @property
    def left(self) -> int:
        return max(0, self.total - self.spent)


def search(params: dict, *, key: str, budget: Budget) -> list[dict]:
    """Page through /opportunities/v2/search and return every notice matched."""
    import requests

    results: list[dict] = []
    for page in range(MAX_PAGES):
        if not budget.take():
            log.warning("request budget exhausted mid-query — results may be incomplete")
            break
        query = params | {
            "api_key": key,
            "limit": PAGE_SIZE,
            "offset": page * PAGE_SIZE,
        }
        response = requests.get(
            API_URL, params=query, timeout=90,
            headers={"User-Agent": common.USER_AGENT, "Accept": "application/json"},
        )
        if response.status_code == 429:
            log.warning("rate limited by SAM.gov — stopping this query early")
            break
        if response.status_code >= 400:
            log.error("SAM.gov %s: %s", response.status_code, response.text[:300])
            response.raise_for_status()

        payload = response.json()
        batch = payload.get("opportunitiesData") or []
        results.extend(batch)
        log.info("  page %d: %d notices (total so far %d)", page + 1, len(batch), len(results))
        if len(batch) < PAGE_SIZE:
            break
        time.sleep(1.0)  # be gentle even on a public API
    return results


def build_queries(days: int, *, with_titles: bool = False) -> list[dict]:
    """One query per filter dimension. SAM.gov ANDs its filter params, so NAICS, PSC and
    title sweeps have to be issued separately or they'd cancel each other out."""
    today = dt.date.today()
    window = {
        "postedFrom": (today - dt.timedelta(days=days)).strftime("%m/%d/%Y"),
        "postedTo": today.strftime("%m/%d/%Y"),
        "ptype": ",".join(NOTICE_TYPES),
    }
    queries = [window | {"ncode": code} for code in NAICS_CODES]
    queries += [window | {"ccode": code} for code in PSC_CODES]
    if with_titles:
        queries += [window | {"title": term} for term in TITLE_KEYWORDS]
    return queries


# ---------------------------------------------------------------------------
# Normalize
# ---------------------------------------------------------------------------

def _agency_name(notice: dict) -> str | None:
    office = notice.get("fullParentPathName") or ""
    parts = [p.strip() for p in office.split(".") if p.strip()]
    return parts[-1] if parts else (notice.get("department") or None)


def _deadline(notice: dict) -> str | None:
    """responseDeadLine looks like '2026-09-15T17:00:00-04:00'; we keep the date."""
    raw = notice.get("responseDeadLine") or notice.get("archiveDate")
    return raw[:10] if isinstance(raw, str) and len(raw) >= 10 else None


def _state(notice: dict) -> str | None:
    place = notice.get("placeOfPerformance") or {}
    code = (place.get("state") or {}).get("code") if isinstance(place.get("state"), dict) else None
    return common.normalize_state(code or (notice.get("officeAddress") or {}).get("state"))


def _amount(notice: dict) -> float | None:
    award = notice.get("award") or {}
    try:
        return float(award["amount"]) if award.get("amount") else None
    except (TypeError, ValueError):
        return None


def match_or_create_org(conn, notice: dict) -> int | None:
    """Match the buying agency to an existing org by name; otherwise create a gov_agency org.

    Federal notices often name a VA medical center or a service academy that is already in the
    DB from IPEDS — matching there is what turns a signal into a scored lead.
    """
    agency = _agency_name(notice)
    if not agency:
        return None
    normalized = common.normalize_name(agency)
    if not normalized:
        return None

    hit = conn.execute(
        "SELECT id FROM organizations WHERE name_normalized = ? ORDER BY lead_score IS NULL, id LIMIT 1",
        (normalized,)).fetchone()
    if hit:
        return hit[0]

    state = _state(notice)
    cur = conn.execute(
        """INSERT INTO organizations
               (name, name_normalized, org_type, track, segment, state, source, notes)
           VALUES (?, ?, 'gov_agency', 'A', ?, ?, 'sam_gov', ?)""",
        (agency, normalized, notice.get("department") or "Federal buyer", state,
         json.dumps({"sam_department": notice.get("department"),
                     "sam_office": notice.get("officeAddress", {}).get("city")})))
    return cur.lastrowid


UPSERT_SIGNAL = """
INSERT INTO signals
    (org_id, org_name_raw, signal_type, title, description, url, reference_number, deadline,
     posted_date, amount_estimate, naics_code, psc_code, state, source, source_key, status)
VALUES (:org_id, :org_name_raw, :signal_type, :title, :description, :url, :reference_number,
        :deadline, :posted_date, :amount_estimate, :naics_code, :psc_code, :state, 'sam_gov',
        :source_key, :status)
ON CONFLICT (source, source_key) WHERE source_key IS NOT NULL DO UPDATE SET
    org_id          = COALESCE(excluded.org_id, signals.org_id),
    title           = excluded.title,
    description     = excluded.description,
    url             = excluded.url,
    deadline        = excluded.deadline,
    amount_estimate = excluded.amount_estimate,
    state           = excluded.state,
    status          = excluded.status,
    date_updated    = datetime('now')
"""


def normalize_notice(conn, notice: dict, counters: dict) -> None:
    notice_id = notice.get("noticeId")
    common.stage_record(
        conn, source=SOURCE, record_type="signal", payload=notice,
        source_key=notice_id, source_url=notice.get("uiLink"),
    )

    deadline = _deadline(notice)
    expired = bool(deadline and deadline < dt.date.today().isoformat())
    notice_type = (notice.get("type") or "").lower()
    signal_type = "rfp" if "sources sought" in notice_type or "presolicitation" in notice_type \
        else "open_tender"

    existed = conn.execute(
        "SELECT 1 FROM signals WHERE source = ? AND source_key = ?", (SOURCE, notice_id)).fetchone()

    conn.execute(UPSERT_SIGNAL, {
        "org_id": match_or_create_org(conn, notice),
        "org_name_raw": _agency_name(notice),
        "signal_type": signal_type,
        "title": (notice.get("title") or "Untitled solicitation").strip(),
        "description": (notice.get("description") or "")[:2000] or None,
        "url": notice.get("uiLink"),
        "reference_number": notice.get("solicitationNumber") or notice_id,
        "deadline": deadline,
        "posted_date": (notice.get("postedDate") or "")[:10] or None,
        "amount_estimate": _amount(notice),
        "naics_code": notice.get("naicsCode"),
        "psc_code": notice.get("classificationCode"),
        "state": _state(notice),
        "source_key": notice_id,
        "status": "expired" if expired else "open",
    })
    counters["updated" if existed else "new"] += 1


def expire_stale(conn) -> int:
    """Anything whose deadline has passed is no longer a lead."""
    cur = conn.execute(
        "UPDATE signals SET status = 'expired', date_updated = datetime('now')"
        " WHERE status = 'open' AND deadline IS NOT NULL AND deadline < date('now')")
    return cur.rowcount


# ---------------------------------------------------------------------------

def run(*, days: int = DEFAULT_LOOKBACK_DAYS, dry_run: bool = False,
        limit: int | None = None, with_titles: bool = False) -> None:
    queries = build_queries(days, with_titles=with_titles)
    if dry_run:
        print(f"{len(queries)} queries would be issued over a {days}-day window "
              f"(budget {REQUEST_BUDGET} requests/day):\n")
        for query in queries:
            focus = {k: v for k, v in query.items() if k in ("ncode", "ccode", "title")}
            print(f"  {focus}  posted {query['postedFrom']} -> {query['postedTo']}")
        print("\nSet SAM_GOV_API_KEY and re-run without --dry-run to pull live notices.")
        return

    key = api_key()
    conn = common.connect()
    budget = Budget()
    seen_ids: set[str] = set()

    if len(queries) > REQUEST_BUDGET:
        log.warning("%d queries against a %d-request budget — the last %d will be skipped",
                    len(queries), REQUEST_BUDGET, len(queries) - REQUEST_BUDGET)

    with common.ingest_run(conn, SOURCE) as counters:
        for query in queries:
            focus = {k: v for k, v in query.items() if k in ("ncode", "ccode", "title")}
            if budget.left == 0:
                log.warning("skipping %s — daily request budget spent", focus)
                counters["errors"] += 1
                continue
            log.info("query %s (budget left %d)", focus, budget.left)
            try:
                notices = search(query, key=key, budget=budget)
            except Exception as exc:  # noqa: BLE001 — one bad query must not kill the run
                counters["errors"] += 1
                log.warning("query %s failed: %s: %s", focus, type(exc).__name__, exc)
                continue

            for notice in notices:
                notice_id = notice.get("noticeId")
                if not notice_id or notice_id in seen_ids:
                    continue          # the same notice matches several of our filters
                seen_ids.add(notice_id)
                counters["seen"] += 1
                try:
                    normalize_notice(conn, notice, counters)
                except Exception as exc:  # noqa: BLE001
                    counters["errors"] += 1
                    log.warning("notice %s failed: %s: %s", notice_id, type(exc).__name__, exc)
                if limit and counters["seen"] >= limit:
                    break
            conn.commit()
            if limit and counters["seen"] >= limit:
                break

        expired = expire_stale(conn)
        conn.commit()
        log.info("marked %d past-deadline signals expired", expired)

    report(conn)


def report(conn) -> None:
    total, open_future = conn.execute(
        "SELECT COUNT(*), SUM(status='open' AND deadline >= date('now')) FROM signals"
        " WHERE source = 'sam_gov'").fetchone()
    print(f"\nSAM.gov signals: {total} total, {open_future or 0} open with a future deadline\n")
    print("Next 15 by deadline:")
    for row in conn.execute(
        "SELECT deadline, days_to_deadline, reference_number, org_name_raw, title"
        "  FROM v_open_signals WHERE source='sam_gov' AND deadline IS NOT NULL"
        " ORDER BY deadline LIMIT 15"
    ):
        print(f"  {row['deadline']}  (+{row['days_to_deadline']:>3}d)  "
              f"{(row['org_name_raw'] or '?')[:34]:34}  {row['title'][:60]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll SAM.gov for book/education solicitations")
    parser.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                        help="lookback window on postedDate (default 30)")
    parser.add_argument("--limit", type=int, help="stop after N notices (smoke test)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the queries that would be issued; no API key needed")
    parser.add_argument("--with-titles", action="store_true",
                        help="also sweep TITLE_KEYWORDS — exceeds a 10-request/day key")
    args = parser.parse_args()
    common.setup_logging()
    run(days=args.days, dry_run=args.dry_run, limit=args.limit, with_titles=args.with_titles)


if __name__ == "__main__":
    main()
