"""Build the two deployable forms of the dashboard from dashboard/index.html.

  leads.db      a pruned copy of db/leads.db (raw staging and empty Phase-2 tables removed,
                then VACUUMed) so the sql.js page downloads ~half as much.
  preview.html  a fully self-contained page with the data inlined as JSON and no external
                requests at all — for hosts that block CDNs, and for a quick local look.

Run:  python dashboard/build_static.py
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common  # noqa: E402

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"
WEB_DB = HERE / "leads.db"
PREVIEW = HERE / "preview.html"

# Kept in sync with the SQL in index.html — same shape, same column aliases.
ORG_SQL = """
  SELECT o.id, o.name, o.org_type, o.track, o.segment, o.website_domain AS domain,
         o.state, o.city, o.size_metric AS size, o.size_metric_type AS size_type,
         o.lead_score AS score, o.score_breakdown AS breakdown, o.programs_flags AS flags,
         o.coop_affiliations AS coops, o.status, o.claimed_by,
         (SELECT COUNT(*) FROM contacts c WHERE c.org_id = o.id) AS contacts,
         (SELECT COUNT(*) FROM signals s WHERE s.org_id = o.id AND s.status = 'open'
            AND (s.deadline IS NULL OR s.deadline >= date('now'))) AS open_signals,
         (SELECT MIN(s.deadline) FROM signals s WHERE s.org_id = o.id AND s.status = 'open'
            AND s.deadline >= date('now')) AS deadline
    FROM organizations o"""
CONTACT_SQL = """SELECT id, org_id, name, title, role_type, email, email_verified AS verified,
                        phone, source_url FROM contacts"""
SIGNAL_SQL = """
  SELECT s.id, s.org_id, s.signal_type AS type, s.title, s.description, s.url,
         s.reference_number AS ref, s.deadline, s.amount_estimate AS amount, s.source,
         s.state, s.status, COALESCE(o.name, s.org_name_raw) AS org_name
    FROM signals s LEFT JOIN organizations o ON o.id = s.org_id
   WHERE s.status = 'open' AND (s.deadline IS NULL OR s.deadline >= date('now'))"""


def build_web_db() -> None:
    """Copy leads.db, drop what the dashboard never reads, and compact it."""
    shutil.copyfile(common.DB_PATH, WEB_DB)
    conn = sqlite3.connect(WEB_DB)
    conn.execute("PRAGMA journal_mode = DELETE")   # WAL sidecars would not survive the upload
    conn.execute("DELETE FROM source_records")
    conn.execute("DELETE FROM ingest_runs")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    print(f"  leads.db      {WEB_DB.stat().st_size / 1e6:.1f} MB")


def collect() -> dict:
    conn = common.connect()
    fetch = lambda sql: [dict(r) for r in conn.execute(sql)]  # noqa: E731
    latest = conn.execute(
        "SELECT MAX(date(date_updated)) FROM organizations").fetchone()[0]
    return {
        "orgs": fetch(ORG_SQL),
        "contacts": fetch(CONTACT_SQL),
        "signals": fetch(SIGNAL_SQL),
        "meta": {"generated": latest, "origin": "embedded", "ipeds_year": "2024"},
    }


def build_preview(data: dict) -> None:
    """Inline the data and strip the document wrapper (the artifact host supplies it)."""
    html = INDEX.read_text(encoding="utf-8")
    payload = json.dumps(data, separators=(",", ":"), default=str)
    # </script> inside a JSON string would end the script element early.
    payload = payload.replace("</", "<\\/")
    html = html.replace(
        "<script>\n/* ---",
        f"<script>window.__LEADS_DATA__ = {payload};</script>\n<script>\n/* ---",
        1)

    head = re.search(r"<head>(.*?)</head>", html, re.S).group(1)
    body = re.search(r"<body>(.*?)</body>", html, re.S).group(1)
    head = re.sub(r'<meta[^>]*>\s*', "", head)     # charset/viewport come from the host
    PREVIEW.write_text(head.strip() + "\n" + body.strip() + "\n", encoding="utf-8")
    print(f"  preview.html  {PREVIEW.stat().st_size / 1e6:.1f} MB "
          f"({len(data['orgs'])} orgs, {len(data['signals'])} signals inlined)")


def main() -> None:
    print("building dashboard artifacts:")
    build_web_db()
    build_preview(collect())


if __name__ == "__main__":
    main()
