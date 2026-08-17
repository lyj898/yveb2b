"""Contacts for the Track B companies, from their own published contact pages.

Only touches organizations whose ``site_status`` is ``live`` — the ones that answered the
domain check in ``ingest.wholesalers``. Robots-blocked hosts are never fetched.

Per company: load the home page, follow at most three links that look like contact / about /
wholesale / sell-to-us pages on the same host, and collect published ``mailto:`` addresses and
phone numbers. Nothing is guessed — an address is stored only if the company published it as a
link. Role addresses (sales@, wholesale@, buyback@) are marked ``is_generic``.

Run:  python -m ingest.wholesaler_contacts [--limit 5] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common  # noqa: E402

SOURCE = "wholesaler_contacts"
MAX_PAGES_PER_SITE = 3

log = logging.getLogger("textbook-leads.wholesaler-contacts")

# Link text / hrefs worth following, best first.
CONTACT_HINTS = [
    "sell to us", "sell your books", "wholesale", "bulk", "buyback", "buy back",
    "contact us", "contact", "customer service", "about us", "sales", "partners",
]
# Addresses that are never a buying contact.
IGNORE_EMAIL = re.compile(
    r"(no-?reply|do-?not-?reply|privacy|legal|dmca|abuse|webmaster|postmaster|"
    r"careers|jobs|recruit|unsubscribe|example\.com|sentry\.io|wixpress|"
    # Staging hosts leak into published markup (Texas Book Company ships wpcomstaging links).
    r"wpcomstaging|\.staging\.|staging\.|localhost)", re.I)
ROLE_PREFIXES = ("info", "sales", "wholesale", "purchasing", "orders", "buyback", "buying",
                 "buyer", "support", "service", "contact", "hello", "help", "customerservice")
PHONE_RE = re.compile(r"(?:\+1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")


def role_for(email: str) -> tuple[str, int]:
    """(role_type, is_generic) for an address."""
    local = email.split("@", 1)[0].lower()
    generic = int(local.startswith(ROLE_PREFIXES))
    if any(word in local for word in ("purchas", "buyer", "buying", "buyback", "acquisition")):
        return "purchasing_manager", generic
    if any(word in local for word in ("sales", "wholesale", "orders", "trade")):
        return "owner", generic
    return "other", generic


def candidate_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Same-host links that look like a contact/wholesale page, ranked by hint order."""
    host = urllib.parse.urlparse(base_url).netloc.lower().removeprefix("www.")
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        url = urllib.parse.urljoin(base_url, anchor["href"])
        parts = urllib.parse.urlparse(url)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.netloc.lower().removeprefix("www.") != host:
            continue
        url = parts._replace(fragment="", query="").geturl()
        if url in seen:
            continue
        haystack = f"{anchor.get_text(' ', strip=True)} {parts.path}".lower()
        for rank, hint in enumerate(CONTACT_HINTS):
            if hint in haystack:
                scored.append((rank, url))
                seen.add(url)
                break

    scored.sort()
    return [url for _, url in scored[:MAX_PAGES_PER_SITE]]


def harvest(soup: BeautifulSoup, page_url: str) -> tuple[dict[str, str], str | None]:
    """Published mailto addresses -> nearest link text, plus the first phone number seen."""
    found: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href.lower().startswith("mailto:"):
            continue
        email = urllib.parse.unquote(href[7:].split("?")[0]).strip().lower()
        if "@" not in email or IGNORE_EMAIL.search(email):
            continue
        label = anchor.get_text(" ", strip=True)
        found.setdefault(email, label if label and "@" not in label else "")

    text = soup.get_text(" ", strip=True)
    phone_match = PHONE_RE.search(text)
    phone = phone_match.group(0).strip() if phone_match else None
    del page_url
    return found, phone


def process(conn, org, *, dry_run: bool) -> int:
    domain = org["website_domain"]
    home = f"https://{domain}/"
    response = common.polite_get(home, timeout=30, allow_redirects=True)
    if response is None or response.status_code >= 400:
        log.warning("%-32s home page unavailable", org["name"])
        return 0

    soup = BeautifulSoup(response.text, "html.parser")
    pages = [(home, soup)]
    for url in candidate_links(soup, response.url):
        page = common.polite_get(url, timeout=30, allow_redirects=True)
        if page is not None and page.status_code < 400:
            pages.append((url, BeautifulSoup(page.text, "html.parser")))

    emails: dict[str, str] = {}
    phone = None
    for url, page_soup in pages:
        found, page_phone = harvest(page_soup, url)
        for email, label in found.items():
            emails.setdefault(email, label)
        phone = phone or page_phone

    if not emails:
        log.info("%-32s no published addresses on %d page(s)", org["name"], len(pages))
        return 0

    log.info("%-32s %d address(es): %s", org["name"], len(emails),
             ", ".join(sorted(emails)[:4]))
    if dry_run:
        return len(emails)

    for email, label in emails.items():
        role, generic = role_for(email)
        common.stage_record(conn, source=SOURCE, record_type="contact",
                            payload={"org": org["name"], "email": email, "label": label,
                                     "phone": phone},
                            source_key=f"{domain}:{email}", source_url=home)
        conn.execute(
            """INSERT INTO contacts (org_id, name, title, role_type, email, phone,
                                     is_generic, source, source_url)
               VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (org_id, email) WHERE email IS NOT NULL DO UPDATE SET
                   phone        = COALESCE(excluded.phone, contacts.phone),
                   date_updated = datetime('now')""",
            (org["id"], (label or "Published contact address")[:120], role, email, phone,
             generic, SOURCE, home))
    conn.execute("UPDATE organizations SET status = 'enriched', date_updated = datetime('now')"
                 " WHERE id = ?", (org["id"],))
    return len(emails)


def run(*, limit: int | None = None, dry_run: bool = False) -> None:
    conn = common.connect()
    orgs = conn.execute(
        """SELECT id, name, website_domain FROM organizations
            WHERE source = 'wholesalers' AND website_domain IS NOT NULL
              AND json_extract(notes, '$.site_status') = 'live'
            ORDER BY name""" + (" LIMIT ?" if limit else ""),
        (limit,) if limit else ()).fetchall()
    log.info("%d Track B companies with a reachable site", len(orgs))

    with common.ingest_run(conn, SOURCE) as counters:
        for org in orgs:
            counters["seen"] += 1
            try:
                written = process(conn, org, dry_run=dry_run)
                counters["new"] += written
            except Exception as exc:  # noqa: BLE001 — one site never kills the run
                counters["errors"] += 1
                log.warning("%s failed: %s: %s", org["name"], type(exc).__name__, exc)
            conn.commit()

    report(conn)


def report(conn) -> None:
    total = conn.execute("SELECT COUNT(*) FROM contacts WHERE source = ?", (SOURCE,)).fetchone()[0]
    orgs = conn.execute(
        "SELECT COUNT(DISTINCT org_id) FROM contacts WHERE source = ?", (SOURCE,)).fetchone()[0]
    print(f"\nTrack B contacts: {total} addresses across {orgs} companies\n")
    for row in conn.execute(
        "SELECT o.name, o.state, c.email, c.role_type, c.phone FROM contacts c"
        "  JOIN organizations o ON o.id = c.org_id WHERE c.source = ? ORDER BY o.name", (SOURCE,)
    ):
        print(f"  {row['name'][:26]:26} {row['email'][:38]:38} {row['role_type']:20}"
              f" {row['phone'] or ''}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect published Track B contact addresses")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    args = parser.parse_args()
    common.setup_logging()
    run(limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
