# textbook-leads — project conventions

US textbook B2B lead database. **Phase 1 is lead generation only**: build the database,
ingesters, scoring, and a dashboard. No email sending, no CRM integration, no faculty scraping.

Two commercial tracks:

- **Track A — authorized institutional supply**: universities, community colleges, libraries and
  library consortia, college bookstores, purchasing cooperatives, hospitals, government agencies.
- **Track B — surplus / overstock wholesale**: wholesalers, exporters, jobbers, chain buyers.

Faculty are *adopters*, not buyers. We only target organizations with a purchasing function.

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
- [~] **Stage 3** — SAM.gov poller written and dry-run clean; needs SAM_GOV_API_KEY for the first live pull, then the daily workflow
- [ ] **Stage 4** — directory ingesters: cooperatives → independent bookstores → state portals
      (TX, CA, FL, NY, GA)
- [x] **Stage 5** — dedupe (dry-run by default), email-verification stub, 0–100 scoring
- [x] **Stage 6** — single-file sql.js dashboard + GitHub Pages workflow

Phase 1 is complete when: 500+ scored orgs each with ≥1 contact across both tracks; 20+ open
signals with future deadlines; every ingester runs locally and on a schedule; the dashboard is
deployed; `SOURCES.md` documents every source and every skipped one.
