"""Library consortium / cooperative member lists.

Consortium membership is a strong Track A signal: these libraries buy through a shared
agreement, so one relationship reaches many campuses, and the member lists publish
organizational library contact addresses (library@, ill@) rather than personal ones.

Most members are already in ``organizations`` from IPEDS. This ingester therefore matches
first — by library-site domain, then by normalized name within the state — and only creates
an org when nothing matches (hospital libraries, public libraries, museums, seminaries).

Adding a source is one entry in SOURCES: a URL, a parser, and the labels to stamp on it.
Anything that blocks us goes in SOURCES.md instead of getting a workaround.

Run:  python -m ingest.coop_members [--source ohiolink] [--limit 20]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common  # noqa: E402

SOURCE = "coop_members"
log = logging.getLogger("textbook-leads.coops")

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Consortium infrastructure hosts — never the member's own website.
INFRA_HOSTS = ("exlibrisgroup.com", "libguides.com", "libraryguides", "primo",
               "worldcat.org", "oclc.org", "consortiamanager.com", "illinoisdelivers.net",
               "carli.illinois.edu", "ohiolink.edu", "facebook.com", "twitter.com",
               "linkedin.com", "vimeo.com", "youtube.com", "instagram.com")

# Name fragments that reveal what kind of buyer a non-IPEDS member is.
TYPE_HINTS = (
    ("hospital", "hospital"), ("medical center", "hospital"), ("health system", "hospital"),
    ("children's", "hospital"), ("clinic", "hospital"),
    ("public library", "library"), ("county library", "library"),
    ("historical", "library"), ("museum", "library"), ("archives", "library"),
    ("seminary", "university"), ("school of law", "university"),
)


@dataclass
class Member:
    name: str
    domain: str | None = None
    email: str | None = None
    phone: str | None = None
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Parsers — one per source layout
# ---------------------------------------------------------------------------

def parse_ohiolink(soup: BeautifulSoup) -> list[Member]:
    """Two-column grid; each cell is one library: <h3>name</h3>, phone, mailto, site links."""
    members = []
    for cell in soup.find_all("td"):
        heading = cell.find("h3")
        if not heading:
            continue
        name = heading.find("h3").get_text(strip=True) if heading.find("h3") \
            else heading.get_text(strip=True)
        if not name:
            continue
        text = cell.get_text(" ", strip=True)
        email = next(iter(EMAIL_RE.findall(text)), None)
        phone = next(iter(re.findall(r"\(\d{3}\)\s*\d{3}-\d{4}", text)), None)
        site = None
        for link in cell.find_all("a", href=True):
            href = link["href"]
            label = link.get_text(strip=True).lower()
            if href.startswith("http") and not any(h in href for h in INFRA_HOSTS) \
                    and ("web site" in label or "website" in label):
                site = href
                break
        members.append(Member(name=name, domain=common.normalize_domain(site),
                              email=email, phone=phone,
                              raw={"name": name, "site": site, "email": email, "phone": phone}))
    return members


def parse_carli(soup: BeautifulSoup) -> list[Member]:
    """Plain table: institution name, membership class."""
    members = []
    for row in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if len(cells) < 1 or not cells[0]:
            continue
        name = cells[0]
        if name.lower().startswith(("institution", "library name")):
            continue
        members.append(Member(name=name,
                              raw={"name": name,
                                   "membership": cells[1] if len(cells) > 1 else None}))
    return members


@dataclass
class Source:
    key: str
    label: str            # what goes into coop_affiliations
    url: str
    parser: Callable[[BeautifulSoup], list[Member]]
    state: str | None     # consortium's home state; members are overwhelmingly in-state
    note: str


SOURCES: list[Source] = [
    Source("ohiolink", "OhioLINK", "https://www.ohiolink.edu/members", parse_ohiolink, "OH",
           "Ohio academic + hospital libraries; publishes library email and website per member"),
    Source("carli", "CARLI", "https://www.carli.illinois.edu/membership/mem-libs", parse_carli,
           "IL", "Consortium of Academic and Research Libraries in Illinois; names only"),
]


# ---------------------------------------------------------------------------
# Matching and load
# ---------------------------------------------------------------------------

def guess_org_type(name: str) -> str:
    lowered = name.lower()
    for fragment, org_type in TYPE_HINTS:
        if fragment in lowered:
            return org_type
    return "library"


def match_org(conn, member: Member, state: str | None) -> int | None:
    """Find the existing org this member already is. Domain first, then name within state."""
    if member.domain:
        hit = conn.execute("SELECT id FROM organizations WHERE website_domain = ?",
                           (member.domain,)).fetchone()
        if hit:
            return hit[0]
    normalized = common.normalize_name(member.name)
    if not normalized:
        return None
    hit = conn.execute(
        "SELECT id FROM organizations WHERE name_normalized = ? AND (state IS ? OR ? IS NULL)"
        " ORDER BY size_metric DESC LIMIT 1", (normalized, state, state)).fetchone()
    if hit:
        return hit[0]

    # IPEDS suffixes its campuses ("Midwestern University-Downers Grove") while consortia
    # list the parent name. Accept a prefix match only when exactly one org matches, so an
    # ambiguous stem never silently attaches to the wrong campus.
    prefix = conn.execute(
        "SELECT id FROM organizations WHERE state IS ? AND name_normalized LIKE ? LIMIT 2",
        (state, normalized + " %")).fetchall()
    if len(prefix) == 1:
        return prefix[0][0]

    # Library members are often named for the library, not the institution
    # ("Olive Kettering Library, Antioch College"). Try the institution half.
    for part in re.split(r"[,–—-]| at ", member.name):
        part = part.strip()
        if len(part) < 6 or "librar" in part.lower():
            continue
        candidate = common.normalize_name(part)
        if not candidate:
            continue
        hit = conn.execute(
            "SELECT id FROM organizations WHERE name_normalized = ? AND (state IS ? OR ? IS NULL)"
            " LIMIT 1", (candidate, state, state)).fetchone()
        if hit:
            return hit[0]
    return None


def add_affiliation(conn, org_id: int, label: str) -> bool:
    row = conn.execute("SELECT coop_affiliations FROM organizations WHERE id = ?",
                       (org_id,)).fetchone()
    current = json.loads(row[0] or "[]")
    if label in current:
        return False
    current.append(label)
    conn.execute("UPDATE organizations SET coop_affiliations = ?, status = 'enriched',"
                 "       date_updated = datetime('now') WHERE id = ?",
                 (json.dumps(sorted(current)), org_id))
    return True


def upsert_contact(conn, org_id: int, member: Member, source_url: str) -> None:
    if not member.email:
        return
    conn.execute(
        """INSERT INTO contacts (org_id, name, title, role_type, email, phone, is_generic,
                                 source, source_url)
           VALUES (?, ?, 'Library contact', 'acquisitions_librarian', ?, ?, 1, ?, ?)
           ON CONFLICT (org_id, email) WHERE email IS NOT NULL DO UPDATE SET
               phone = COALESCE(excluded.phone, contacts.phone),
               date_updated = datetime('now')""",
        (org_id, f"{member.name} library", member.email.lower(), member.phone,
         SOURCE, source_url))


def run(only: str | None = None, limit: int | None = None) -> None:
    conn = common.connect()
    sources = [s for s in SOURCES if not only or s.key == only]
    if not sources:
        raise SystemExit(f"unknown source {only!r}; known: {', '.join(s.key for s in SOURCES)}")

    with common.ingest_run(conn, SOURCE) as counters:
        for source in sources:
            log.info("fetching %s (%s)", source.label, source.url)
            response = common.polite_get(source.url, timeout=45)
            if response is None:
                counters["errors"] += 1
                log.warning("%s is disallowed by robots.txt — skipped, record it in SOURCES.md",
                            source.label)
                continue
            if response.status_code >= 400:
                counters["errors"] += 1
                log.warning("%s returned HTTP %s — skipped", source.label, response.status_code)
                continue

            members = source.parser(BeautifulSoup(response.text, "html.parser"))
            if limit:
                members = members[:limit]
            log.info("%s: parsed %d members", source.label, len(members))

            matched = created = 0
            for member in members:
                counters["seen"] += 1
                try:
                    common.stage_record(
                        conn, source=f"{SOURCE}:{source.key}", record_type="organization",
                        payload=member.raw, source_key=member.name, source_url=source.url)

                    org_id = match_org(conn, member, source.state)
                    if org_id:
                        matched += 1
                    else:
                        cur = conn.execute(
                            """INSERT INTO organizations
                                   (name, name_normalized, org_type, track, segment,
                                    website_domain, state, source, coop_affiliations, status)
                               VALUES (?, ?, ?, 'A', ?, ?, ?, ?, '[]', 'new')""",
                            (member.name, common.normalize_name(member.name),
                             guess_org_type(member.name), f"{source.label} member",
                             member.domain, source.state, f"{SOURCE}:{source.key}"))
                        org_id = cur.lastrowid
                        created += 1
                        counters["new"] += 1

                    if add_affiliation(conn, org_id, source.label):
                        counters["updated"] += 1
                    upsert_contact(conn, org_id, member, source.url)
                except Exception as exc:  # noqa: BLE001 — one bad row never kills a run
                    counters["errors"] += 1
                    log.warning("%s / %s failed: %s: %s", source.label, member.name,
                                type(exc).__name__, exc)
            conn.commit()
            log.info("%s: %d matched to existing orgs, %d new", source.label, matched, created)

    report(conn)


def report(conn) -> None:
    print("\nCo-op / consortium affiliations now in the DB:")
    for row in conn.execute(
        "SELECT value AS label, COUNT(*) n FROM organizations,"
        "       json_each(organizations.coop_affiliations) GROUP BY value ORDER BY n DESC"
    ):
        print(f"  {row['label']:12} {row['n']} orgs")
    total = conn.execute(
        "SELECT COUNT(*) FROM organizations WHERE coop_affiliations != '[]'").fetchone()[0]
    contacts = conn.execute(
        f"SELECT COUNT(*) FROM contacts WHERE source = '{SOURCE}'").fetchone()[0]
    print(f"\n  {total} affiliated organizations, {contacts} library contacts")
    print("\nSample:")
    for row in conn.execute(
        "SELECT o.name, o.org_type, o.state, o.coop_affiliations, c.email"
        "  FROM organizations o LEFT JOIN contacts c ON c.org_id = o.id"
        " WHERE o.coop_affiliations != '[]' ORDER BY o.id DESC LIMIT 8"
    ):
        print(f"  {row['name'][:44]:44} {row['org_type']:12} {row['state']} "
              f"{row['coop_affiliations']} {row['email'] or ''}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import consortium / cooperative member lists")
    parser.add_argument("--source", help="only this source key")
    parser.add_argument("--limit", type=int, help="first N members per source (smoke test)")
    args = parser.parse_args()
    common.setup_logging()
    run(args.source, args.limit)


if __name__ == "__main__":
    main()
