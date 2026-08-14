# Data sources

Every source feeding `db/leads.db`: what it gives us, how often it refreshes, and what we
deliberately skipped. Updated as each ingester lands.

| Source | Feeds | Access | Cadence | Status |
| --- | --- | --- | --- | --- |
| IPEDS (NCES) — `HD2024.zip`, `EFFY2024.zip`, `C2024_A.zip` from `nces.ed.gov/ipeds/datacenter/data/` | `organizations` (5,963 loaded) | Public ZIP download, no key | Annual (survey year; re-run with `--year`) | **Live** |
| SAM.gov Get Opportunities v2 | `signals` | Public API, needs `SAM_GOV_API_KEY` | Daily | Built; awaiting API key for first live pull |
| Cooperative purchasing bodies (E&I, Sourcewell, TIPS, state library consortia) | `organizations` | Public member lists | Quarterly | Stage 4 — pending |
| Independent college bookstores | `organizations`, `contacts` | Public directories | Quarterly | Stage 4 — pending |
| State procurement portals (TX, CA, FL, NY, GA) | `signals` | Public solicitation listings | Daily/weekly per state | Stage 4 — pending |

## Skipped / blocked

_Nothing recorded yet. Any source that requires a login, blocks scraping, or disallows us in
`robots.txt` gets a row here with the date and the reason, rather than a workaround._

## Out of scope by policy

Faculty directories, syllabus repositories, LinkedIn, and any authenticated page. Faculty are
adopters, not buyers; Phase 1 targets organizations with a purchasing function.
