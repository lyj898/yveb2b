# Data sources

Every source feeding `db/leads.db`: what it gives us, how often it refreshes, and what we
deliberately skipped. Updated as each ingester lands.

## Live

| Source | Feeds | Access | Cadence | Notes |
| --- | --- | --- | --- | --- |
| IPEDS (NCES) — `HD2024.zip`, `EFFY2024.zip`, `C2024_A.zip` from `nces.ed.gov/ipeds/datacenter/data/` | `organizations` (5,963) | Public ZIP download, no key | Annual (`--year` selects the survey year) | Enrollment → `size_metric`; CIP completions → `programs_flags` |
| OhioLINK member institutions — `ohiolink.edu/members` | `organizations`, `contacts` (99 library addresses across both consortia) | Public HTML, robots-allowed | Quarterly | 132 matched to existing IPEDS orgs, 18 created (hospital + independent libraries) |
| CARLI participating libraries — `carli.illinois.edu/membership/mem-libs` | `organizations` | Public HTML, robots-allowed | Quarterly | Names + membership class only; 106 matched, 16 created |

## Built, not yet running

| Source | Feeds | Blocker |
| --- | --- | --- |
| SAM.gov Get Opportunities v2 | `signals` | Needs `SAM_GOV_API_KEY`. Poller and daily workflow are written and dry-run clean. |

## Skipped / blocked

Checked 2026-08-14. Nothing here gets a workaround — a block is a block.

| Source | What happened | Decision |
| --- | --- | --- |
| E&I Cooperative Services (`eandi.org`) | `robots.txt` returns HTTP 403 to any non-browser client, which the standard treats as site-wide disallow | Skipped |
| TIPS (`tips-usa.com`) | Cloudflare challenge on `robots.txt` and on the member pages | Skipped |
| NACS (`nacs.org`) | `robots.txt` returns 403; the store directory also sits behind member login | Skipped |
| Amigos Library Services (`amigos.org`) | Member directory returns 403 behind a bot challenge | Skipped |
| LYRASIS (`lyrasis.org`) | Bot-protection interstitial on every path including `robots.txt` | Skipped |
| ICBA member directory (`icbainc.com/members`) | Robots-allowed and reachable, **but** it is a BuddyPress directory of ~1,635 *individual people* — personal accounts and personal work emails, not an organizational store directory | Skipped on privacy grounds, not technical ones. We target organizations with a purchasing function; harvesting a membership community's personal accounts is out of scope for Phase 1. |
| Texas ESBD (`txsmartbuy.com`, `txsmartbuy.gov`) | `robots.txt` disallows the solicitation paths | Skipped — revisit only if Texas publishes a documented API |
| NY OGS bid opportunities (`ogs.ny.gov`) | `robots.txt` returns 403 | Skipped; NYSCR (`nyscr.ny.gov`) responds and is the better target |

## Investigated, needs an API path before it can be built

These three respond and do not block us, but each serves a JavaScript shell — the listings
arrive from a back-end call, so the ingester needs that endpoint rather than HTML parsing.

| Source | State | Status |
| --- | --- | --- |
| Cal eProcure public search | CA | 200, ~5 KB JS shell — needs the search endpoint |
| MyFloridaMarketPlace Vendor Bid System | FL | 200, ~1 KB SPA shell — needs the search endpoint |
| NY State Contract Reporter (`nyscr.ny.gov`) | NY | 200, ~12 KB — public listings exist; pagination not yet mapped |
| Georgia Procurement Registry (`ssl.doas.state.ga.us/gpr/`) | GA | 200, ~43 KB of server-rendered HTML — the most likely of the five to parse directly |

## Out of scope by policy

Faculty directories, syllabus repositories, LinkedIn, and any authenticated page. Faculty are
adopters, not buyers; Phase 1 targets organizations with a purchasing function.

## Politeness

Every fetch goes through `common.polite_get`: `robots.txt` is checked with our real
User-Agent (2xx parses the rules, 404 means no rules, 401/403 means the host is closed to us),
requests to one host are spaced 4 seconds apart, and no authenticated or paywalled page is
ever touched.
