# textbook-leads — project conventions

US textbook B2B lead database. **Phase 1 is lead generation only**: build the database,
ingesters, scoring, and a dashboard. No email sending, no CRM integration, no faculty scraping.

Two commercial tracks:

- **Track A — authorized institutional supply**: universities, community colleges, libraries and
  library consortia, college bookstores, purchasing cooperatives, hospitals, government agencies.
- **Track B — surplus / overstock wholesale**: wholesalers, exporters, jobbers, chain buyers.

Faculty are *adopters*, not buyers. We only target organizations with a purchasing function.

**Who buys physical textbooks, in practice:** campus stores (the core buyer — but only
*independent* ones decide locally; Follett/Barnes & Noble-managed stores buy centrally and are
do-not-chase), higher-ed institutions via procurement, and Track B buyback/wholesale programs.
**University and above only — K-12 is not our market** and is excluded at the source level.
Libraries buy monographs and serials, not adoption stock — a secondary play. Electronic
subscription renewals are unfulfillable and never signals.

## Layout

```
db/schema.sql          canonical schema; apply with sqlite3 db/leads.db < db/schema.sql
db/leads.db            SQLite, committed to the repo
ingest/                one module per source; writes raw rows to source_records first
enrich/                dedupe, email-verification stub, scoring
dashboard/index.html   single-file sql.js dashboard, loads db/leads.db client-side
.github/workflows/     one workflow per source, independent schedules
SOURCES.md             every data source, refresh cadence, and anything skipped/blocked
```

## Stack

- Python 3.11+ (repo is developed against 3.14; nothing version-specific is used)
- `requests` + `beautifulsoup4` for scraping, `pandas` for transforms, stdlib `sqlite3` for the DB
- No ORM. Plain SQL, parameterized. Schema changes go in `db/schema.sql` and nowhere else.

## Ingester contract

Every ingester must:

1. Open an `ingest_runs` row at start, close it with `success` / `partial` / `failed` at the end.
2. Write the **raw** source row to `source_records` (JSON `raw_payload` + sha256 `payload_hash`)
   before any normalization. Re-normalizing must never require re-hitting the source.
3. Normalize into `organizations` / `contacts` / `signals` via idempotent upserts keyed on
   `website_domain` (orgs) or `(source, source_key)` (signals). Re-running must not duplicate.
4. Log errors and continue — one bad row never kills a run.
5. Be runnable both locally (`python -m ingest.<module>`) and from GitHub Actions.

## Scraping politeness (non-negotiable)

- Check `robots.txt` before fetching any host; skip disallowed paths and note it in `SOURCES.md`.
- 3–5 s delay between requests to the same host.
- Realistic desktop User-Agent, with a contact URL where the source expects one.
- **Never** touch authenticated pages, paywalled data, or anything behind a login.
- If a source blocks us or requires a login, skip it and record that in `SOURCES.md`.
  Do not fight anti-bot measures.

## Secrets

`SAM_GOV_API_KEY`, email-verification keys, and anything else sensitive come from environment
variables locally and GitHub Actions secrets in CI. **Never hardcode a key or commit a `.env`.**

## Data hygiene

- `website_domain` is normalized (lowercase, no scheme, no `www.`, no trailing slash) — it is the
  primary merge key.
- Dates are ISO-8601 TEXT. `signals.deadline` is the most important field in the DB; a signal
  without a usable deadline scores near zero on urgency.
- `programs_flags`, `coop_affiliations`, and `score_breakdown` are JSON stored as TEXT.
- Enumerations are enforced by CHECK constraints. Adding a value means editing `schema.sql`.

## Explicitly out of scope for Phase 1

Faculty directory scrapers · syllabus scrapers · LinkedIn scraping · anything requiring login ·
sending email · CRM integration.

## Build checklist

- [x] **Stage 1** — schema + repo scaffold
- [x] **Stage 2** — IPEDS import — 5,963 active institutions loaded from IPEDS 2024
- [x] **Stage 3** — SAM.gov poller live; 28 notices, 13 open. Non-federal key = 10 requests/day,
      so the daily run issues 8 queries and title sweeps are opt-in (`--with-titles`)
- [~] **Stage 4** — consortium members live (OhioLINK, CARLI); GA and FL portals live;
      Track B seed list live (30 wholesalers/jobbers/exporters); bookstore directories blocked
      or personal-data only; TX/NY-OGS robots-blocked; CA and NY-SCR endpoints still to map.
      See SOURCES.md.
- [x] **Stage 5** — dedupe (dry-run by default), email-verification stub, 0–100 scoring
- [x] **Stage 6** — single-file sql.js dashboard + GitHub Pages workflow
- [x] **Stage 7 (revamp)** — the desk is six plays for the commercial team: campus stores
      (independent = lead, Follett/B&N-managed = do-not-chase), live bids (physical materials
      only), surplus buyers, library accounts (secondary), no-contact-found (tried, came up
      empty — transparency, not silence), do-not-chase. `ingest/campus_stores.py` has examined
      all 4,930 IPEDS institutions with a website (2026-08-19): 656 store leads (119 with email,
      537 phone-only), 929 locked/incumbent, 3,513 with no publishable contact found. Monthly
      workflow rotates a recheck of the 400 oldest-checked campuses, full rotation ~12 runs.
      Every tab has a client-side "Download Excel" export (dependency-free .xlsx writer, no
      CDN, so the page stays a single self-contained file); works on the live site, not inside
      the artifact preview sandbox which blocks script-initiated downloads.

Phase 1 is complete when: 500+ scored orgs each with ≥1 contact across both tracks; 20+ open
signals with future deadlines; every ingester runs locally and on a schedule; the dashboard is
deployed; `SOURCES.md` documents every source and every skipped one.
