"""Track B — surplus / overstock wholesale counterparties.

Unlike Track A, this side of the business has no registry to scrape. There is no IPEDS for
book wholesalers: the trade associations that list them (NACS, ABA) block us or sit behind a
login, and the "directories" that rank on search are lead-generation spam. The real universe
is also small — dozens of companies, not thousands.

So this ingester works from a curated seed list of named US companies, and checks each one
before storing it. The check records rather than gatekeeps: most of these sites refuse bots,
which says nothing about whether the company is a real buyer, so a 403 is stored as
``site_status: blocked`` and kept. Only a domain that does not resolve at all is dropped —
that is an error in the seed list, and it surfaces on the next run instead of rotting.

What it deliberately does not do: guess contact addresses. A `info@`-shaped guess is not a
contact, and these companies publish their buyer contacts on pages that vary too much to
parse reliably. Contacts here are a manual research job on a short, high-value list.

Run:  python -m ingest.wholesalers [--check-only]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common  # noqa: E402

SOURCE = "wholesalers"
log = logging.getLogger("textbook-leads.wholesalers")


@dataclass(frozen=True)
class Company:
    name: str
    domain: str
    org_type: str      # wholesaler | jobber | exporter | bookstore_chain
    track: str         # 'B' surplus/used, 'both' when they also supply institutions
    segment: str
    state: str | None = None


# --- Seed list -------------------------------------------------------------
# Grouped by what they are to us. Edit freely; every entry is verified at run time.
COMPANIES: list[Company] = [
    # Medical / nursing / allied-health jobbers — the LWW-relevant end of the trade.
    Company("Rittenhouse Book Distributors", "rittenhouse.com", "jobber", "both",
            "Health sciences book and e-book distributor", "PA"),
    Company("Matthews Book Company", "matthewsbooks.com", "jobber", "both",
            "Medical and health sciences distributor", "MO"),
    Company("Majors Books", "majors.com", "jobber", "both",
            "Medical and nursing textbook distributor", "TX"),

    # Institutional wholesalers and library jobbers — Track A supply, Track B overstock.
    Company("Ingram Content Group", "ingramcontent.com", "wholesaler", "both",
            "Trade and academic book wholesaler", "TN"),
    Company("Baker & Taylor", "baker-taylor.com", "wholesaler", "both",
            "Library and institutional book wholesaler", "NC"),
    Company("Follett", "follett.com", "wholesaler", "both",
            "Campus store operator and education distributor", "IL"),
    Company("Brodart Books & Library Services", "brodart.com", "wholesaler", "both",
            "Library book jobber and collection services", "PA"),
    Company("Mackin Educational Resources", "mackin.com", "wholesaler", "both",
            "K-12 and academic library supplier", "MN"),
    Company("Perma-Bound Books", "perma-bound.com", "wholesaler", "both",
            "Prebound and library-edition supplier", "IL"),
    Company("Bound to Stay Bound Books", "btsb.com", "wholesaler", "both",
            "Library prebound book supplier", "IL"),
    Company("National Book Network", "nbnbooks.com", "wholesaler", "both",
            "Independent publisher distributor", "MD"),
    Company("Independent Publishers Group", "ipgbook.com", "wholesaler", "both",
            "Independent publisher distributor", "IL"),
    Company("Bookazine", "bookazine.com", "wholesaler", "both",
            "Trade book wholesaler", "NJ"),

    # Textbook wholesalers and campus-store suppliers.
    Company("MBS Textbook Exchange", "mbsbooks.com", "wholesaler", "both",
            "Used and new textbook wholesaler to campus stores", "MO"),
    Company("Nebraska Book Company", "nebook.com", "wholesaler", "both",
            "Campus store systems and textbook wholesale", "NE"),
    Company("Texas Book Company", "texasbook.com", "wholesaler", "both",
            "Campus store operator and textbook wholesaler", "TX"),
    Company("Akademos", "akademos.com", "wholesaler", "both",
            "Online campus bookstore and course materials", "CT"),
    Company("eCampus.com", "ecampus.com", "wholesaler", "both",
            "Textbook retailer and campus store services", "KY"),

    # Surplus / overstock / used — the core of Track B.
    Company("ThriftBooks", "thriftbooks.com", "wholesaler", "B",
            "Large-scale used book reseller", "WA"),
    Company("Better World Books", "betterworldbooks.com", "wholesaler", "B",
            "Used book reseller and library discard partner", "IN"),
    Company("Half Price Books", "hpb.com", "bookstore_chain", "B",
            "Used book chain and bulk buyer", "TX"),
    Company("Alibris", "alibris.com", "wholesaler", "B",
            "Used and out-of-print marketplace", "CA"),
    Company("Biblio", "biblio.com", "wholesaler", "B",
            "Used and rare book marketplace", "NC"),
    Company("SecondSale", "secondsale.com", "wholesaler", "B",
            "Bulk used book reseller", "VA"),
    Company("TextbookRush", "textbookrush.com", "wholesaler", "B",
            "Textbook buyback and resale", "OH"),
    Company("BooksRun", "booksrun.com", "wholesaler", "B",
            "Textbook buyback and resale", "DE"),
    Company("ValoreBooks", "valorebooks.com", "wholesaler", "B",
            "Textbook marketplace and buyback", "MA"),
    Company("Zubal Books", "zubalbooks.com", "wholesaler", "B",
            "Bulk and scholarly remainder dealer", "OH"),
    Company("Powell's Books", "powells.com", "bookstore_chain", "B",
            "Large independent buying used stock", "OR"),
    Company("World of Books USA", "wob.com", "exporter", "B",
            "International used book reseller", None),
    Company("Book Depot", "bookdepot.com", "exporter", "B",
            "Remainder and overstock wholesaler (Ontario, ships US)", None),
]


def verify(company: Company) -> tuple[str, str]:
    """Check the company's domain. Returns (status, note).

    Status drives whether the row is stored:

        live        the site answered — everything is as expected
        blocked     the domain exists but the site refuses bots (403/406/503, or robots.txt)
        unreachable the domain exists but did not answer this time
        missing     the domain does not resolve at all — the seed entry is wrong

    Only ``missing`` is disqualifying. These companies are counterparties we want to sell to,
    not sources we scrape, so a Cloudflare block says nothing about whether they are a real
    business — but a name with no DNS record is a mistake in the list, and gets dropped.
    """
    import socket

    try:
        socket.gethostbyname(company.domain)
    except OSError:
        return "missing", "domain does not resolve"

    url = f"https://{company.domain}/"
    try:
        response = common.polite_get(url, timeout=30, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001 — a refusal is data, not a crash
        return "unreachable", f"{type(exc).__name__}"
    if response is None:
        return "blocked", "robots.txt disallows the home page"
    if response.status_code >= 400:
        return "blocked", f"HTTP {response.status_code}"
    return "live", f"HTTP {response.status_code} via {common.normalize_domain(response.url)}"


UPSERT = """
INSERT INTO organizations
    (name, name_normalized, org_type, track, segment, website_domain, state, source, status,
     programs_flags, notes)
VALUES (:name, :name_normalized, :org_type, :track, :segment, :domain, :state, :source, 'new',
        '{}', :notes)
ON CONFLICT (website_domain) WHERE website_domain IS NOT NULL DO UPDATE SET
    name         = excluded.name,
    org_type     = excluded.org_type,
    track        = excluded.track,
    segment      = excluded.segment,
    state        = COALESCE(excluded.state, organizations.state),
    notes        = excluded.notes,
    date_updated = datetime('now')
"""


def run(check_only: bool = False) -> None:
    conn = common.connect()
    stored, dropped = [], []

    with common.ingest_run(conn, SOURCE) as counters:
        for company in COMPANIES:
            counters["seen"] += 1
            status, note = verify(company)
            if status == "missing":
                counters["errors"] += 1
                dropped.append((company, note))
                log.warning("%-34s DROPPED — %s", company.name, note)
                continue
            stored.append((company, status, note))
            log.info("%-34s %-11s %s", company.name, status, note)

            if check_only:
                continue

            payload = {"name": company.name, "domain": company.domain,
                       "org_type": company.org_type, "track": company.track,
                       "segment": company.segment, "state": company.state,
                       "site_status": status, "site_note": note}
            common.stage_record(conn, source=SOURCE, record_type="organization",
                                payload=payload, source_key=company.domain,
                                source_url=f"https://{company.domain}/")

            existed = conn.execute("SELECT 1 FROM organizations WHERE website_domain = ?",
                                   (company.domain,)).fetchone()
            conn.execute(UPSERT, {
                "name": company.name,
                "name_normalized": common.normalize_name(company.name),
                "org_type": company.org_type,
                "track": company.track,
                "segment": company.segment,
                "domain": company.domain,
                "state": company.state,
                "source": SOURCE,
                "notes": json.dumps({"checked_at": common.today(),
                                     "site_status": status, "site_note": note}),
            })
            counters["updated" if existed else "new"] += 1
        conn.commit()

    by_status: dict[str, int] = {}
    for _, status, _ in stored:
        by_status[status] = by_status.get(status, 0) + 1
    print(f"\nstored {len(stored)} of {len(COMPANIES)} companies "
          f"({', '.join(f'{n} {s}' for s, n in sorted(by_status.items()))})"
          + (" — check only, nothing written" if check_only else ""))
    if dropped:
        print("\ndropped (no DNS — fix or remove the seed entry):")
        for company, note in dropped:
            print(f"  {company.name:34} {company.domain:26} {note}")
    report(conn)


def report(conn) -> None:
    print("\nTrack B / dual-track organizations:")
    for row in conn.execute(
        "SELECT track, org_type, COUNT(*) n FROM organizations WHERE track IN ('B', 'both')"
        " GROUP BY track, org_type ORDER BY track, n DESC"
    ):
        print(f"  track {row['track']:5} {row['org_type']:16} {row['n']}")
    total = conn.execute(
        "SELECT COUNT(*) FROM organizations WHERE track IN ('B', 'both')").fetchone()[0]
    print(f"  total {total}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Load Track B wholesalers / exporters / jobbers")
    parser.add_argument("--check-only", action="store_true",
                        help="verify the domains but write nothing")
    args = parser.parse_args()
    common.setup_logging()
    run(check_only=args.check_only)


if __name__ == "__main__":
    main()
