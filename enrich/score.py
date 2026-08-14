"""Lead scoring — 0-100 per organization, re-runnable after every ingest.

    segment fit   0-40   what they teach and what kind of buyer they are
    size          0-25   size_metric percentile *within org_type* (a big CC != a small CC)
    signal        0-35   open tenders, weighted by how soon the deadline is

Run:  python -m enrich.score [--explain <org_id>]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from bisect import bisect_left
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common  # noqa: E402

log = logging.getLogger("textbook-leads.score")

# --- Segment fit (0-40) ----------------------------------------------------
# Program flags. Nursing/allied health is the LWW priority; law/business/engineering are
# T&F and Wiley territory and worth less per seat.
PROGRAM_POINTS = {
    "nursing": 10,
    "allied_health": 7,
    "medical": 8,
    "pharmacy": 5,
    "dentistry": 3,
    "veterinary": 2,
    "law": 4,
    "business": 3,
    "engineering": 3,
    "education": 2,
    "psychology": 2,
}
PROGRAM_CAP = 24

# Buyer type. Ranked by how directly the org places bulk book orders: a bookstore or a
# wholesaler buys books for a living; a university buys them through several intermediaries.
ORG_TYPE_POINTS = {
    "bookstore_independent": 14,
    "wholesaler": 14,
    "exporter": 13,
    "jobber": 13,
    "bookstore_chain": 12,
    "library_consortium": 12,
    "library": 11,
    "coop": 11,
    "community_college": 9,
    "university": 8,
    "hospital": 8,
    "career_college": 6,
    "prison_education": 6,
    "gov_agency": 5,
    "other": 2,
}
COOP_AFFILIATION_POINTS = 4      # per affiliation, capped
COOP_CAP = 8

# --- Size (0-25) -----------------------------------------------------------
SIZE_MAX = 25

# --- Signal urgency (0-35) -------------------------------------------------
# An open tender closing inside a month is the single most actionable thing in the DB.
DEADLINE_BANDS = [(30, 30), (60, 22), (90, 15), (180, 10)]
DEADLINE_FAR = 6
DEADLINE_UNKNOWN = 3             # a signal without a deadline is barely actionable
MULTI_SIGNAL_BONUS = 3
AMOUNT_BONUS_THRESHOLD = 100_000
AMOUNT_BONUS = 2
SIGNAL_MAX = 35


def size_percentiles(conn) -> dict[str, list[float]]:
    """Sorted size_metric values per org_type, for percentile ranking."""
    buckets: dict[str, list[float]] = {}
    for row in conn.execute(
        "SELECT org_type, size_metric FROM organizations"
        " WHERE size_metric IS NOT NULL AND size_metric > 0 ORDER BY size_metric"
    ):
        buckets.setdefault(row["org_type"], []).append(float(row["size_metric"]))
    return buckets


def score_segment(org, flags: dict) -> tuple[int, dict]:
    program_points = min(
        PROGRAM_CAP,
        sum(points for flag, points in PROGRAM_POINTS.items() if flags.get(flag)),
    )
    type_points = ORG_TYPE_POINTS.get(org["org_type"], 2)
    try:
        affiliations = json.loads(org["coop_affiliations"] or "[]")
    except (TypeError, ValueError):
        affiliations = []
    coop_points = min(COOP_CAP, COOP_AFFILIATION_POINTS * len(affiliations))

    total = min(40, program_points + type_points + coop_points)
    return total, {
        "programs": program_points,
        "org_type": type_points,
        "coop_affiliations": coop_points,
    }


def score_size(org, buckets: dict[str, list[float]]) -> tuple[int, dict]:
    size = org["size_metric"]
    if not size or size <= 0:
        return 0, {"percentile": None, "reason": "no size_metric"}
    peers = buckets.get(org["org_type"], [])
    if len(peers) < 2:
        return SIZE_MAX // 2, {"percentile": None, "reason": "too few peers to rank"}
    percentile = bisect_left(peers, float(size)) / len(peers)
    return round(SIZE_MAX * percentile), {
        "percentile": round(percentile, 3),
        "peer_group": org["org_type"],
        "peers": len(peers),
    }


def score_signals(conn, org_id: int) -> tuple[int, dict]:
    rows = conn.execute(
        """SELECT deadline, amount_estimate,
                  CAST(julianday(deadline) - julianday(date('now')) AS INTEGER) AS days
             FROM signals
            WHERE org_id = ? AND status = 'open'
              AND (deadline IS NULL OR deadline >= date('now'))""",
        (org_id,)).fetchall()
    if not rows:
        return 0, {"open_signals": 0}

    best, best_days = 0, None
    for row in rows:
        if row["days"] is None:
            points = DEADLINE_UNKNOWN
        else:
            points = DEADLINE_FAR
            for limit, value in DEADLINE_BANDS:
                if row["days"] <= limit:
                    points = value
                    break
        if (row["amount_estimate"] or 0) >= AMOUNT_BONUS_THRESHOLD:
            points += AMOUNT_BONUS
        if points > best:
            best, best_days = points, row["days"]

    if len(rows) > 1:
        best += MULTI_SIGNAL_BONUS
    return min(SIGNAL_MAX, best), {
        "open_signals": len(rows),
        "soonest_deadline_days": best_days,
    }


def run(explain: int | None = None) -> None:
    conn = common.connect()
    buckets = size_percentiles(conn)
    orgs = conn.execute(
        "SELECT id, org_type, size_metric, programs_flags, coop_affiliations, name"
        "  FROM organizations" + (" WHERE id = ?" if explain else ""),
        (explain,) if explain else ()).fetchall()

    updated = 0
    for org in orgs:
        try:
            flags = json.loads(org["programs_flags"] or "{}")
        except (TypeError, ValueError):
            flags = {}

        segment, segment_detail = score_segment(org, flags)
        size, size_detail = score_size(org, buckets)
        signal, signal_detail = score_signals(conn, org["id"])
        total = min(100, segment + size + signal)

        breakdown = {
            "segment_fit": {"points": segment, "max": 40, **segment_detail},
            "size": {"points": size, "max": 25, **size_detail},
            "signal_urgency": {"points": signal, "max": 35, **signal_detail},
            "total": total,
        }
        conn.execute(
            "UPDATE organizations SET lead_score = ?, score_breakdown = ?,"
            "       scored_at = datetime('now'), date_updated = datetime('now')"
            " WHERE id = ?",
            (total, json.dumps(breakdown), org["id"]))
        updated += 1

        if explain:
            print(f"{org['name']} (id {org['id']}) -> {total}")
            print(json.dumps(breakdown, indent=2))

    conn.commit()
    if explain:
        return

    log.info("scored %d organizations", updated)
    report(conn)


def report(conn) -> None:
    print("\nScore distribution:")
    for row in conn.execute(
        """SELECT CASE WHEN lead_score >= 80 THEN '80-100'
                       WHEN lead_score >= 60 THEN '60-79'
                       WHEN lead_score >= 40 THEN '40-59'
                       WHEN lead_score >= 20 THEN '20-39'
                       ELSE '0-19' END AS band,
                  COUNT(*) n
             FROM organizations GROUP BY band ORDER BY band DESC"""
    ):
        print(f"  {row['band']:8} {row['n']:6}")

    print("\nTop 15 organizations:")
    for row in conn.execute(
        "SELECT lead_score, name, org_type, state, CAST(size_metric AS INT) size"
        "  FROM organizations ORDER BY lead_score DESC, size_metric DESC LIMIT 15"
    ):
        print(f"  {row['lead_score']:3}  {row['name'][:44]:44} {row['org_type']:18}"
              f" {row['state'] or '--'}  {row['size']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Score organizations 0-100")
    parser.add_argument("--explain", type=int, metavar="ORG_ID",
                        help="score one org and print its full breakdown")
    args = parser.parse_args()
    common.setup_logging()
    run(explain=args.explain)


if __name__ == "__main__":
    main()
