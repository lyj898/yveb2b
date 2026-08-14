"""Email verification — pluggable, and OFF by default.

Phase 1 ships the dry-run provider: it applies syntax and disposable-domain checks locally,
marks everything else ``unverified``, and never calls a paid API. Wire a real provider later
by setting the env vars below; no other code changes.

    NEVERBOUNCE_API_KEY   -> --provider neverbounce
    ZEROBOUNCE_API_KEY    -> --provider zerobounce

Run:  python -m enrich.verify_emails [--provider dry_run|neverbounce|zerobounce] [--limit N]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common  # noqa: E402

log = logging.getLogger("textbook-leads.verify")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)
ROLE_PREFIXES = ("info", "purchasing", "procurement", "orders", "library", "bookstore",
                 "acquisitions", "admin", "contact", "sales")
DISPOSABLE_DOMAINS = {"mailinator.com", "guerrillamail.com", "10minutemail.com",
                      "tempmail.com", "trashmail.com", "yopmail.com"}

# Schema allows exactly these four states.
VALID, INVALID, CATCH_ALL, UNVERIFIED = "valid", "invalid", "catch_all", "unverified"


class Verifier(Protocol):
    name: str

    def verify(self, email: str) -> str:
        """Return one of valid / invalid / catch_all / unverified."""


class DryRunVerifier:
    """Local checks only. Never leaves the machine, never costs anything."""

    name = "dry_run"

    def verify(self, email: str) -> str:
        email = (email or "").strip()
        if not EMAIL_RE.match(email):
            return INVALID
        domain = email.rsplit("@", 1)[1].lower()
        if domain in DISPOSABLE_DOMAINS:
            return INVALID
        return UNVERIFIED       # deliverability is unknowable without an API


class NeverBounceVerifier:
    """https://api.neverbounce.com/v4/single/check — not exercised in Phase 1."""

    name = "neverbounce"
    ENDPOINT = "https://api.neverbounce.com/v4/single/check"
    RESULT_MAP = {"valid": VALID, "invalid": INVALID, "catchall": CATCH_ALL,
                  "disposable": INVALID, "unknown": UNVERIFIED}

    def __init__(self) -> None:
        self.key = os.environ.get("NEVERBOUNCE_API_KEY", "").strip()
        if not self.key:
            raise SystemExit("NEVERBOUNCE_API_KEY is not set")

    def verify(self, email: str) -> str:
        import requests
        response = requests.get(self.ENDPOINT, timeout=30,
                                params={"key": self.key, "email": email})
        response.raise_for_status()
        return self.RESULT_MAP.get(response.json().get("result"), UNVERIFIED)


class ZeroBounceVerifier:
    """https://api.zerobounce.net/v2/validate — not exercised in Phase 1."""

    name = "zerobounce"
    ENDPOINT = "https://api.zerobounce.net/v2/validate"
    RESULT_MAP = {"valid": VALID, "invalid": INVALID, "catch-all": CATCH_ALL,
                  "spamtrap": INVALID, "abuse": INVALID, "do_not_mail": INVALID,
                  "unknown": UNVERIFIED}

    def __init__(self) -> None:
        self.key = os.environ.get("ZEROBOUNCE_API_KEY", "").strip()
        if not self.key:
            raise SystemExit("ZEROBOUNCE_API_KEY is not set")

    def verify(self, email: str) -> str:
        import requests
        response = requests.get(self.ENDPOINT, timeout=30,
                                params={"api_key": self.key, "email": email})
        response.raise_for_status()
        return self.RESULT_MAP.get(response.json().get("status"), UNVERIFIED)


PROVIDERS = {
    "dry_run": DryRunVerifier,
    "neverbounce": NeverBounceVerifier,
    "zerobounce": ZeroBounceVerifier,
}


def run(provider_name: str = "dry_run", *, limit: int | None = None,
        recheck: bool = False) -> None:
    verifier: Verifier = PROVIDERS[provider_name]()
    conn = common.connect()

    query = ("SELECT id, email FROM contacts WHERE email IS NOT NULL"
             + ("" if recheck else " AND email_verified = 'unverified'")
             + (" LIMIT ?" if limit else ""))
    rows = conn.execute(query, (limit,) if limit else ()).fetchall()
    log.info("provider=%s candidates=%d", verifier.name, len(rows))

    with common.ingest_run(conn, f"verify_emails:{verifier.name}") as counters:
        for row in rows:
            counters["seen"] += 1
            try:
                status = verifier.verify(row["email"])
            except Exception as exc:  # noqa: BLE001 — one bad address must not kill the run
                counters["errors"] += 1
                log.warning("verify %s failed: %s: %s", row["email"], type(exc).__name__, exc)
                continue
            conn.execute(
                "UPDATE contacts SET email_verified = ?, verified_at = datetime('now'),"
                "       is_generic = ?, date_updated = datetime('now') WHERE id = ?",
                (status,
                 int(row["email"].split("@")[0].lower().startswith(ROLE_PREFIXES)),
                 row["id"]))
            counters["updated"] += 1
        conn.commit()

    for row in conn.execute(
        "SELECT email_verified, COUNT(*) n FROM contacts GROUP BY email_verified"
    ):
        print(f"  {row['email_verified']:12} {row['n']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify contact emails")
    parser.add_argument("--provider", choices=sorted(PROVIDERS), default="dry_run")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--recheck", action="store_true",
                        help="re-verify addresses already carrying a verdict")
    args = parser.parse_args()
    common.setup_logging()
    run(args.provider, limit=args.limit, recheck=args.recheck)


if __name__ == "__main__":
    main()
