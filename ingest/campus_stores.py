"""Campus stores — the Track A buyer that actually purchases physical textbooks.

Libraries buy monographs and serials. The party that buys course textbooks in bulk, every
term, is the campus store. And the qualification that matters is who runs it:

    independent   the institution runs its own store -> a local buying decision, a real lead
    managed       Follett, Barnes & Noble College (bkstr.com), eCampus, Akademos and friends
                  buy centrally -> the store manager cannot say yes, so it is not a lead

Both are recorded, because knowing a campus is locked to Follett saves a rep the call.

Per institution: fetch the home page, find the link to its store, and classify it by where
that link points. If it stays on the institution's own site, fetch that page too and take any
published address or phone. Two to three requests per host, robots checked, 4s apart.

Runs across many hosts at once — the delay in common.polite_get is per host, so parallelism
across different institutions is polite. Resumable: every institution examined is stamped, so
a later run picks up where this one stopped.

Run:  python -m ingest.campus_stores [--limit 250] [--workers 8] [--recheck]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common  # noqa: E402

SOURCE = "campus_stores"
log = logging.getLogger("textbook-leads.campus-stores")

# Third-party operators. If the store link leaves for one of these, the buying decision does
# too. bkstr.com is Barnes & Noble College; efollett/follett is Follett.
MANAGED_HOSTS = {
    "bkstr.com": "Barnes & Noble College",
    "bncollege.com": "Barnes & Noble College",
    "barnesandnoble.com": "Barnes & Noble College",
    "efollett.com": "Follett",
    "follett.com": "Follett",
    "fdcbookstore.com": "Follett",
    "ecampus.com": "eCampus",
    "akademos.com": "Akademos",
    "textbookx.com": "Akademos / TextbookX",
    "slingshotedu.com": "Slingshot",
    "mbsdirect.net": "MBS Direct",
    "mbsdirect.com": "MBS Direct",
    "verbasoftware.com": "Verba / VitalSource",
    "amazon.com": "Amazon",
    "texasbook.com": "Texas Book Company",
}

STORE_LINK = re.compile(r"(bookstore|book store|campus store|campus shop|university store|"
                        r"college store|textbook)", re.I)
# Merchandise-only stores sell hoodies, not course materials.
MERCH_ONLY = re.compile(r"(spirit|apparel|merch|gift shop|clothing)", re.I)
STORE_PATHS = ["/bookstore", "/campus-store"]

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(?:\+1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
IGNORE_EMAIL = re.compile(r"(no-?reply|privacy|webmaster|postmaster|careers|jobs|"
                          r"wixpress|sentry|example\.com|^sample@|^test@|^user@|^email@|"
                          r"^name@|^first\.last@)", re.I)
STORE_MAILBOX = re.compile(r"(bookstore|book|store|textbook|course)", re.I)


def classify_link(url: str, own_domain: str) -> tuple[str, str | None]:
    """('managed', operator) | ('own_site', None) | ('external', host)"""
    host = (urllib.parse.urlparse(url).netloc or "").lower().removeprefix("www.")
    for managed_host, operator in MANAGED_HOSTS.items():
        if host == managed_host or host.endswith("." + managed_host):
            return "managed", operator
    if not host or host == own_domain or host.endswith("." + own_domain):
        return "own_site", None
    return "external", host


def find_store_link(soup: BeautifulSoup, base_url: str, own_domain: str) -> str | None:
    best = None
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        haystack = f"{text} {anchor['href']}"
        if not STORE_LINK.search(haystack) or MERCH_ONLY.search(text):
            continue
        url = urllib.parse.urljoin(base_url, anchor["href"])
        if not url.lower().startswith("http"):
            continue
        kind, _ = classify_link(url, own_domain)
        if kind == "managed":
            return url                    # unambiguous: a third party runs the store
        best = best or url
    return best


def safe_get(url: str):
    """polite_get, but a connection-level failure returns None instead of raising.

    Used for absolute store-subdomain URLs, where a www.-fallback rarely applies (the
    subdomain is usually already correctly scoped) — the goal here is just to stop one
    dead server from taking down the whole examine() call and leaving the institution
    permanently un-stamped and endlessly retried.
    """
    import requests

    try:
        return common.polite_get(url, timeout=25, allow_redirects=True)
    except requests.exceptions.RequestException:
        return None


def fetch_with_www_fallback(path: str, domain: str) -> tuple:
    """GET https://{domain}{path}. Returns (response_or_None, note_if_failed).

    Retries on www.{domain} when the bare host's TLS certificate doesn't cover it — a
    real, common misconfiguration: many institutions' certificates list www.example.edu
    and every department subdomain, but not the bare apex, while IPEDS' WEBADDR and our
    own normalize_domain both strip 'www.' before we ever make a request. Confirmed on
    Cal State Fullerton and El Paso Community College, both very real, very live sites
    that were being recorded as unreachable purely because of this.
    """
    import requests

    if not common.robots_allows(f"https://{domain}{path}"):
        return None, "robots-disallowed"
    try:
        return common.polite_get(f"https://{domain}{path}", timeout=25,
                                 allow_redirects=True), None
    except requests.exceptions.SSLError:
        pass
    except requests.exceptions.RequestException as exc:
        return None, f"unreachable: {type(exc).__name__}"
    try:
        response = common.polite_get(f"https://www.{domain}{path}", timeout=25,
                                     allow_redirects=True)
        return response, None
    except requests.exceptions.RequestException as exc:
        return None, f"unreachable (bare host TLS failed, www. also failed: {type(exc).__name__})"


def examine(domain: str) -> dict:
    """Everything we can learn about one institution's store, in 2-3 requests."""
    result: dict = {"operator": None, "kind": "unknown", "store_url": None,
                    "email": None, "phone": None, "note": None}
    response, note = fetch_with_www_fallback("/", domain)
    if response is None:
        result["note"] = note
        return result
    if response.status_code >= 400:
        result["note"] = f"home page HTTP {response.status_code}"
        return result

    soup = BeautifulSoup(response.text, "html.parser")
    link = find_store_link(soup, response.url, domain)
    if not link:
        for path in STORE_PATHS:          # a couple of conventional guesses, then give up
            probe, _ = fetch_with_www_fallback(path, domain)
            if probe is not None and probe.status_code < 400:
                link, soup = probe.url, BeautifulSoup(probe.text, "html.parser")
                break
    if not link:
        result["note"] = "no store link found"
        return result

    result["store_url"] = link
    kind, operator = classify_link(link, domain)
    result["kind"], result["operator"] = kind, operator
    if kind == "managed":
        return result                     # central buying: no local contact worth storing

    page = safe_get(link)
    if page is None or page.status_code >= 400:
        result["note"] = "store page unavailable"
        return result

    store_soup = BeautifulSoup(page.text, "html.parser")

    # Re-check on the store page itself: many colleges link a local page that then hands off.
    for anchor in store_soup.find_all("a", href=True):
        handoff, operator = classify_link(
            urllib.parse.urljoin(page.url, anchor["href"]), domain)
        if handoff == "managed" and STORE_LINK.search(
                f"{anchor.get_text(' ', strip=True)} {anchor['href']}"):
            result.update(kind="managed", operator=operator)
            return result

    # The address usually lives one hop deeper, on the store's own contact page.
    pages = [store_soup]
    for anchor in store_soup.find_all("a", href=True):
        label = f"{anchor.get_text(' ', strip=True)} {anchor['href']}".lower()
        if "contact" in label or "about" in label or "staff" in label:
            deeper = safe_get(urllib.parse.urljoin(page.url, anchor["href"]))
            if deeper is not None and deeper.status_code < 400:
                pages.append(BeautifulSoup(deeper.text, "html.parser"))
            break

    text = " ".join(p.get_text(" ", strip=True) for p in pages)
    root = domain.split(".")[-2] if domain.count(".") >= 1 else domain
    for candidate in EMAIL_RE.findall(text):
        email = candidate.lower().rstrip(".")
        if IGNORE_EMAIL.search(email):
            continue
        local, _, host = email.partition("@")
        if root not in host:
            continue
        # Only a store-shaped mailbox is worth keeping. A page's first random address is
        # a bursar or a webmaster; storing it would put a wrong number in front of a rep,
        # which is worse than a blank.
        if STORE_MAILBOX.search(local):
            result["email"] = email
            break

    phone = PHONE_RE.search(text)
    result["phone"] = phone.group(0).strip() if phone else None
    if result["kind"] != "external":
        result["kind"] = "independent"
    return result


def store_row(conn, org_id: int, found: dict) -> bool:
    """Persist what we learned. Returns True when a usable contact was written."""
    notes = json.loads(conn.execute("SELECT notes FROM organizations WHERE id = ?",
                                    (org_id,)).fetchone()[0] or "{}")
    notes["store"] = {"checked_at": common.today(), **found}
    conn.execute("UPDATE organizations SET notes = ?, date_updated = datetime('now')"
                 " WHERE id = ?", (json.dumps(notes), org_id))

    if not found.get("email"):
        return False
    conn.execute(
        """INSERT INTO contacts (org_id, name, title, role_type, email, phone, is_generic,
                                 source, source_url)
           VALUES (?, NULL, ?, 'bookstore_buyer', ?, ?, 1, ?, ?)
           ON CONFLICT (org_id, email) WHERE email IS NOT NULL DO UPDATE SET
               phone = COALESCE(excluded.phone, contacts.phone),
               title = excluded.title, date_updated = datetime('now')""",
        (org_id, f"Campus store ({found.get('kind')})", found["email"], found.get("phone"),
         SOURCE, found.get("store_url")))
    return True


def run(*, limit: int = 250, workers: int = 8, recheck: bool = False) -> None:
    conn = common.connect()
    # Priority: health-programme campuses first (the LWW segment), then by enrollment.
    targets = conn.execute(
        """SELECT id, name, website_domain FROM organizations
            WHERE source = 'ipeds' AND website_domain IS NOT NULL
              AND (? OR json_extract(notes, '$.store.checked_at') IS NULL)
            ORDER BY (json_extract(programs_flags, '$.nursing') = 1
                      OR json_extract(programs_flags, '$.allied_health') = 1) DESC,
                     size_metric DESC
            LIMIT ?""", (1 if recheck else 0, limit)).fetchall()
    log.info("examining %d institutions with %d workers", len(targets), workers)

    tally = {"independent": 0, "managed": 0, "unknown": 0, "contacts": 0}
    with common.ingest_run(conn, SOURCE) as counters:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(examine, t["website_domain"]): t for t in targets}
            for future in as_completed(futures):
                target = futures[future]
                counters["seen"] += 1
                try:
                    found = future.result()
                except Exception as exc:  # noqa: BLE001 — one campus never kills the run
                    counters["errors"] += 1
                    log.warning("%s failed: %s: %s", target["name"], type(exc).__name__, exc)
                    continue

                common.stage_record(conn, source=SOURCE, record_type="organization",
                                    payload={"institution": target["name"],
                                             "domain": target["website_domain"], **found},
                                    source_key=target["website_domain"],
                                    source_url=found.get("store_url"))
                tally[found["kind"] if found["kind"] in tally else "unknown"] += 1
                if store_row(conn, target["id"], found):
                    tally["contacts"] += 1
                    counters["new"] += 1
                else:
                    counters["updated"] += 1

                if counters["seen"] % 25 == 0:
                    conn.commit()
                    log.info("... %d/%d  independent=%d managed=%d contacts=%d",
                             counters["seen"], len(targets), tally["independent"],
                             tally["managed"], tally["contacts"])
        conn.commit()

    log.info("done: %s", tally)
    report(conn)


def report(conn) -> None:
    print("\nCampus stores by operator:")
    for row in conn.execute(
        "SELECT COALESCE(json_extract(notes, '$.store.operator'),"
        "                json_extract(notes, '$.store.kind')) AS who, COUNT(*) n"
        "  FROM organizations WHERE json_extract(notes, '$.store.checked_at') IS NOT NULL"
        " GROUP BY who ORDER BY n DESC"
    ):
        print(f"  {str(row['who'])[:34]:34} {row['n']}")

    print("\nIndependent stores with a contact (the actual leads):")
    for row in conn.execute(
        """SELECT o.name, o.state, CAST(o.size_metric AS INT) size, c.email, c.phone
             FROM organizations o JOIN contacts c ON c.org_id = o.id
            WHERE c.source = ? AND json_extract(o.notes, '$.store.kind') = 'independent'
            ORDER BY o.size_metric DESC LIMIT 15""", (SOURCE,)
    ):
        print(f"  {row['name'][:30]:30} {row['state']} {row['size']:>7}  "
              f"{row['email'][:32]:32} {row['phone'] or ''}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Find campus stores and who runs them")
    parser.add_argument("--limit", type=int, default=250)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--recheck", action="store_true",
                        help="re-examine already-checked campuses")
    args = parser.parse_args()
    common.setup_logging()
    run(limit=args.limit, workers=args.workers, recheck=args.recheck)


if __name__ == "__main__":
    main()
