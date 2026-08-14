"""Shared helpers for ingesters and enrichers: DB access, run logging, polite HTTP.

Kept as a single top-level module (rather than a package) so both ``ingest.*`` and
``enrich.*`` can ``import common`` without either depending on the other.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.robotparser
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("LEADS_DB", REPO_ROOT / "db" / "leads.db"))
SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"

USER_AGENT = os.environ.get(
    "LEADS_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
)
REQUEST_DELAY_SECONDS = float(os.environ.get("LEADS_REQUEST_DELAY", "4.0"))

log = logging.getLogger("textbook-leads")


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open the leads DB, creating it from schema.sql if it does not exist yet."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not db_path.exists()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if fresh:
        apply_schema(conn)
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    """Apply db/schema.sql. Idempotent — every statement is CREATE ... IF NOT EXISTS."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


@contextmanager
def ingest_run(conn: sqlite3.Connection, source: str) -> Iterator[dict[str, int]]:
    """Open/close an ingest_runs row around an ingester.

    Yields a mutable counters dict; update ``seen`` / ``new`` / ``updated`` / ``errors``
    as you go and the final row is written on exit (even if the body raises).
    """
    cur = conn.execute("INSERT INTO ingest_runs (source) VALUES (?)", (source,))
    run_id = cur.lastrowid
    conn.commit()
    counters = {"seen": 0, "new": 0, "updated": 0, "errors": 0}
    status = "success"
    notes = None
    try:
        yield counters
    except Exception as exc:  # noqa: BLE001 — recorded, then re-raised
        status, notes = "failed", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if status == "success" and counters["errors"]:
            status = "partial"
        conn.execute(
            """UPDATE ingest_runs
                  SET finished_at = datetime('now'), status = ?, records_seen = ?,
                      records_new = ?, records_updated = ?, error_count = ?, notes = ?
                WHERE id = ?""",
            (status, counters["seen"], counters["new"], counters["updated"],
             counters["errors"], notes, run_id),
        )
        conn.commit()
        log.info("[%s] run %s: %s %s", source, run_id, status, counters)


def stage_record(
    conn: sqlite3.Connection,
    *,
    source: str,
    record_type: str,
    payload: dict[str, Any],
    source_key: str | None = None,
    source_url: str | None = None,
    source_version: str | None = None,
) -> int | None:
    """Write a raw row to source_records. Returns the row id, or None if unchanged.

    The (source, source_key, payload_hash) unique constraint makes re-ingesting an
    identical row a no-op, so pollers can run daily without bloating the staging table.
    """
    raw = json.dumps(payload, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    cur = conn.execute(
        """INSERT OR IGNORE INTO source_records
               (source, source_version, source_url, source_key, record_type,
                raw_payload, payload_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (source, source_version, source_url, source_key, record_type, raw, digest),
    )
    return cur.lastrowid if cur.rowcount else None


# ---------------------------------------------------------------------------
# Normalization helpers (shared by ingesters and enrich/dedupe.py)
# ---------------------------------------------------------------------------

_NAME_STOPWORDS = {
    "the", "of", "and", "at", "a", "inc", "incorporated", "llc", "ltd", "co",
    "corp", "corporation", "campus", "main",
}


def normalize_domain(value: str | None) -> str | None:
    """'https://WWW.Example.edu/admissions/' -> 'example.edu'. Returns None if unusable."""
    if not value:
        return None
    value = value.strip().lower()
    if not value:
        return None
    if "@" in value and "://" not in value:          # someone handed us an email
        value = value.rsplit("@", 1)[1]
    if "://" not in value:
        value = "http://" + value
    host = urllib.parse.urlparse(value).netloc.split(":")[0]
    host = host.removeprefix("www.").strip(".")
    return host if "." in host else None


def normalize_name(value: str | None) -> str | None:
    """Fuzzy-match key: lowercase, punctuation stripped, stopwords dropped, sorted-stable."""
    if not value:
        return None
    text = re.sub(r"[^a-z0-9\s]", " ", value.lower())
    text = re.sub(r"\bsaint\b", "st", text)
    tokens = [t for t in text.split() if t and t not in _NAME_STOPWORDS]
    return " ".join(tokens) or None


def normalize_state(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().upper()
    return value if len(value) == 2 and value.isalpha() else None


# ---------------------------------------------------------------------------
# Polite HTTP
# ---------------------------------------------------------------------------

_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_last_request_at: dict[str, float] = {}


def robots_allows(url: str, user_agent: str = USER_AGENT) -> bool:
    """Check robots.txt before fetching.

    robots.txt is fetched with our own User-Agent rather than through
    ``RobotFileParser.read()``, which sends ``Python-urllib`` and gets a 403 from any
    WAF-fronted host — and a 403 makes the parser disallow the entire site. Status
    handling follows the standard: 2xx parses the rules, 404/410 means no rules at all,
    and 401/403 means we are not welcome (recorded, not worked around).
    """
    import requests

    parts = urllib.parse.urlparse(url)
    origin = f"{parts.scheme}://{parts.netloc}"

    if origin not in _robots_cache:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"{origin}/robots.txt")
        try:
            response = requests.get(f"{origin}/robots.txt", timeout=20,
                                    headers={"User-Agent": USER_AGENT})
            if response.status_code in (401, 403):
                log.warning("robots.txt returns %s for %s — treating the host as closed",
                            response.status_code, origin)
                parser.disallow_all = True
            elif response.status_code >= 400:
                parser = None       # no robots.txt published; nothing to obey
            else:
                parser.parse(response.text.splitlines())
        except Exception as exc:  # noqa: BLE001
            log.warning("robots.txt unreadable for %s (%s) — proceeding", origin, exc)
            parser = None
        _robots_cache[origin] = parser

    parser = _robots_cache[origin]
    return True if parser is None else parser.can_fetch(user_agent, url)


def polite_get(url: str, *, session=None, timeout: int = 30, **kwargs):
    """GET with robots.txt check, per-host rate limiting, and a desktop UA.

    Returns a ``requests.Response``, or None if robots.txt disallows the URL.
    """
    import requests  # imported lazily so schema-only work needs no third-party deps

    if not robots_allows(url):
        log.warning("robots.txt disallows %s — skipping", url)
        return None

    host = urllib.parse.urlparse(url).netloc
    elapsed = time.monotonic() - _last_request_at.get(host, 0.0)
    if elapsed < REQUEST_DELAY_SECONDS:
        time.sleep(REQUEST_DELAY_SECONDS - elapsed)

    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    getter = session.get if session is not None else requests.get
    try:
        response = getter(url, headers=headers, timeout=timeout, **kwargs)
    finally:
        _last_request_at[host] = time.monotonic()
    return response
