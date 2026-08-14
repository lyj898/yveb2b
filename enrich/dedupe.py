"""Deduplicate organizations.

Two passes:

  1. **Exact domain** — same normalized ``website_domain`` is the same org. Auto-merged.
  2. **Fuzzy name** — same state + same org_type + near-identical normalized name.
     Auto-merged ONLY when the city also matches, because multi-campus systems share a
     name across cities ("Ivy Tech Community College", Indianapolis vs Fort Wayne) and
     those are genuinely different buyers. Everything else is reported for review.

Merging keeps the lowest id as survivor, re-points contacts / signals / interactions, fills
blank fields from the loser, and unions programs_flags and coop_affiliations.

Run:  python -m enrich.dedupe [--apply] [--threshold 0.93]
Dry-run by default — it prints what it would merge and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common  # noqa: E402

log = logging.getLogger("textbook-leads.dedupe")

DEFAULT_THRESHOLD = 0.93
# Fields copied from loser to survivor when the survivor's value is NULL/blank.
FILL_FIELDS = ("website_domain", "city", "address", "state", "size_metric",
               "size_metric_type", "segment", "source")


def normalize_domains(conn) -> int:
    """Re-normalize every stored domain. Ingesters should already do this; this pass
    catches anything hand-entered or imported before the rule existed."""
    fixed = 0
    for row in conn.execute(
        "SELECT id, website_domain FROM organizations WHERE website_domain IS NOT NULL"
    ).fetchall():
        clean = common.normalize_domain(row["website_domain"])
        if clean != row["website_domain"]:
            # A collision here means the clean domain is already taken — leave it for the
            # merge pass rather than violating the unique index.
            taken = conn.execute(
                "SELECT 1 FROM organizations WHERE website_domain = ? AND id != ?",
                (clean, row["id"])).fetchone()
            if not taken:
                conn.execute("UPDATE organizations SET website_domain = ? WHERE id = ?",
                             (clean, row["id"]))
                fixed += 1
    return fixed


def merge(conn, survivor_id: int, loser_id: int) -> None:
    survivor = conn.execute("SELECT * FROM organizations WHERE id = ?", (survivor_id,)).fetchone()
    loser = conn.execute("SELECT * FROM organizations WHERE id = ?", (loser_id,)).fetchone()

    for field in FILL_FIELDS:
        if not survivor[field] and loser[field]:
            if field == "website_domain":
                taken = conn.execute(
                    "SELECT 1 FROM organizations WHERE website_domain = ? AND id != ?",
                    (loser[field], survivor_id)).fetchone()
                if taken:
                    continue
            conn.execute(f"UPDATE organizations SET {field} = ? WHERE id = ?",
                         (loser[field], survivor_id))

    # Union the JSON sets rather than letting the survivor's copy win.
    flags = json.loads(survivor["programs_flags"] or "{}")
    for flag, value in json.loads(loser["programs_flags"] or "{}").items():
        flags[flag] = max(int(value or 0), int(flags.get(flag, 0) or 0))
    affiliations = sorted(set(json.loads(survivor["coop_affiliations"] or "[]"))
                          | set(json.loads(loser["coop_affiliations"] or "[]")))
    conn.execute(
        "UPDATE organizations SET programs_flags = ?, coop_affiliations = ?,"
        "       date_updated = datetime('now') WHERE id = ?",
        (json.dumps(flags), json.dumps(affiliations), survivor_id))

    for table in ("contacts", "signals", "interactions"):
        conn.execute(f"UPDATE OR IGNORE {table} SET org_id = ? WHERE org_id = ?",
                     (survivor_id, loser_id))
    conn.execute("DELETE FROM organizations WHERE id = ?", (loser_id,))


def find_domain_duplicates(conn) -> list[tuple[int, int, str]]:
    pairs = []
    for row in conn.execute(
        "SELECT website_domain, GROUP_CONCAT(id) ids, COUNT(*) n FROM organizations"
        " WHERE website_domain IS NOT NULL GROUP BY website_domain HAVING n > 1"
    ):
        ids = sorted(int(i) for i in row["ids"].split(","))
        pairs += [(ids[0], other, f"same domain {row['website_domain']}") for other in ids[1:]]
    return pairs


def find_name_duplicates(conn, threshold: float) -> tuple[list, list]:
    """Returns (auto_merge, review) pairs. Auto-merge requires a matching city."""
    auto, review = [], []
    rows = conn.execute(
        "SELECT id, name, name_normalized, city, state, org_type FROM organizations"
        " WHERE name_normalized IS NOT NULL ORDER BY state, org_type, name_normalized"
    ).fetchall()

    groups: dict[tuple, list] = {}
    for row in rows:
        groups.setdefault((row["state"], row["org_type"]), []).append(row)

    for members in groups.values():
        for i, left in enumerate(members):
            for right in members[i + 1:]:
                ratio = SequenceMatcher(None, left["name_normalized"],
                                        right["name_normalized"]).ratio()
                if ratio < threshold:
                    continue
                first, second = sorted((left, right), key=lambda r: r["id"])
                same_city = (first["city"] or "").lower() == (second["city"] or "").lower()
                # Only an *identical* normalized name in the same city is safe to merge
                # automatically. Near-misses are usually real siblings that differ by one
                # word — "Chicago NW" vs "Chicago NE", "Marian" vs "Martin" — so they go
                # to review instead of quietly collapsing two distinct buyers into one.
                identical = first["name_normalized"] == second["name_normalized"]
                target = auto if (same_city and identical) else review
                target.append((first["id"], second["id"],
                               f"{ratio:.2f} {first['name']} <> {second['name']}"
                               f" ({first['city']} / {second['city']}, {first['state']})"))
    return auto, review


def run(*, apply: bool = False, threshold: float = DEFAULT_THRESHOLD) -> None:
    conn = common.connect()
    before = conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]

    if apply:
        fixed = normalize_domains(conn)
        log.info("re-normalized %d domains", fixed)

    domain_pairs = find_domain_duplicates(conn)
    auto_pairs, review_pairs = find_name_duplicates(conn, threshold)

    print(f"organizations: {before}")
    print(f"  exact-domain duplicates : {len(domain_pairs)}")
    print(f"  identical name+city     : {len(auto_pairs)}  (auto-merge)")
    print(f"  near-match names        : {len(review_pairs)}  (review only - siblings/branch campuses)")

    for label, pairs in (("DOMAIN", domain_pairs), ("NAME", auto_pairs)):
        for survivor, loser, why in pairs[:15]:
            print(f"  [{label}] keep {survivor}, drop {loser}: {why}")
        if len(pairs) > 15:
            print(f"  ... and {len(pairs) - 15} more")

    for survivor, loser, why in review_pairs[:15]:
        print(f"  [REVIEW] {survivor} vs {loser}: {why}")
    if len(review_pairs) > 15:
        print(f"  ... and {len(review_pairs) - 15} more")

    if not apply:
        print("\nDry run — nothing changed. Re-run with --apply to merge.")
        return

    with common.ingest_run(conn, "dedupe") as counters:
        for survivor, loser, _ in domain_pairs + auto_pairs:
            counters["seen"] += 1
            try:
                if conn.execute("SELECT 1 FROM organizations WHERE id = ?", (loser,)).fetchone():
                    merge(conn, survivor, loser)
                    counters["updated"] += 1
            except Exception as exc:  # noqa: BLE001
                counters["errors"] += 1
                log.warning("merge %s<-%s failed: %s: %s", survivor, loser, type(exc).__name__, exc)
        conn.commit()

    after = conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
    print(f"\nmerged {before - after} organizations ({before} -> {after})")
    print("Re-run `python -m enrich.score` — merged orgs need rescoring.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deduplicate organizations")
    parser.add_argument("--apply", action="store_true", help="actually merge (default: dry run)")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="fuzzy name similarity 0-1 (default 0.93)")
    args = parser.parse_args()
    common.setup_logging()
    run(apply=args.apply, threshold=args.threshold)


if __name__ == "__main__":
    main()
