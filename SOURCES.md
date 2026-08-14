# Data sources

Every source feeding `db/leads.db`: what it gives us, how often it refreshes, and what we
deliberately skipped. Updated as each ingester lands.

## Live

| Source | Feeds | Access | Cadence | Notes |
| --- | --- | --- | --- | --- |
| IPEDS (NCES) — `HD2024.zip`, `EFFY2024.zip`, `C2024_A.zip` from `nces.ed.gov/ipeds/datacenter/data/` | `organizations` (5,963) | Public ZIP download, no key | Annual (`--year` selects the survey year) | Enrollment → `size_metric`; CIP completions → `programs_flags` |
| OhioLINK member institutions — `ohiolink.edu/members` | `organizations`, `contacts` (99 library addresses across both consortia) | Public HTML, robots-allowed | Quarterly | 132 matched to existing IPEDS orgs, 18 created (hospital + independent libraries) |
| CARLI participating libraries — `carli.illinois.edu/membership/mem-libs` | `organizations` | Public HTML, robots-allowed | Quarterly | Names + membership class only; 106 matched, 16 created |
| Georgia Procurement Registry — `POST ssl.doas.state.ga.us/gpr/eventSearch` | `signals` | Public JSON endpoint, no key, robots-allowed | Daily | 515 open events statewide; relevance filter applied locally because the portal's title filter misses rephrasings |
| Florida MyFloridaMarketPlace — `POST vendor.myfloridamarketplace.com/mfmp/pub/search/bids` | `signals` | Public JSON endpoint, no key, robots-allowed | Daily | One query per keyword (an empty title returns nothing), de-duplicated on advertisementId |

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

## Investigated, not yet built

Both respond and do not block us, but each serves a JavaScript shell — the listings arrive
from a back-end call, so the ingester needs that endpoint rather than HTML parsing.

| Source | State | Status |
| --- | --- | --- |
| Cal eProcure public search | CA | Responds, JS shell; search endpoint not yet mapped |
| NY State Contract Reporter (`nyscr.ny.gov`) | NY | Responds; listing endpoint not yet mapped |

## Out of scope by policy

Faculty directories, syllabus repositories, LinkedIn, and any authenticated page. Faculty are
adopters, not buyers; Phase 1 targets organizations with a purchasing function.

## Politeness

Every fetch goes through `common.polite_get`: `robots.txt` is checked with our real
User-Agent (2xx parses the rules, 404 means no rules, 401/403 means the host is closed to us),
requests to one host are spaced 4 seconds apart, and no authenticated or paywalled page is
ever touched.

## Signal yield, measured

Open, textbook-relevant public solicitations are genuinely rare on any given day. As of
2026-08-14 the two live state portals together hold **two**: 515 open Georgia events and 190
Florida advertisements matching our keywords produced 1 signal each. That is the market, not
a parsing failure — the pollers exist to catch the ones that do appear. Reaching the Phase 1
target of 20+ open signals needs SAM.gov (federal VA/DoD/BOP buys) plus the remaining states.
