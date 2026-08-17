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
| SAM.gov Get Opportunities v2 | `signals` (28 notices, 13 open), `contacts` (25 contracting officers) | Public API, `SAM_GOV_API_KEY` | Daily | 8 queries/run: 3 NAICS + 5 PSC. Title sweeps are opt-in (`--with-titles`) because a non-federal key allows only **10 requests per day**. `--reprocess` re-normalizes staged notices for free |
| Track B seed list — `ingest/wholesalers.py` | `organizations` (30 wholesalers, jobbers, exporters) | Curated, checked by DNS + HTTP | Monthly | No registry exists for this trade; entries are stored with a `site_status` and only dropped when the domain stops resolving |
| Track B published contacts — `ingest/wholesaler_contacts.py` | `contacts` (27 addresses, 8 companies) | Company contact pages, robots-checked | Monthly | Only `mailto:` links the company published; nothing guessed, robots-blocked hosts never fetched |
| USAspending federal awards — `POST api.usaspending.gov/api/v2/search/spending_by_award/` | `organizations` (171 book vendors) | Public API, **no key**, no rate cap in practice | Monthly | PSC 7610/7630/7640/7670 + NAICS 424920 over 3 years; relevance-filtered, publishers excluded |

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

## Track B has no registry

There is no IPEDS for book wholesalers. NACS and the ABA block us, and the ranked
"directories" are lead-generation spam. The real universe is also small — dozens of
companies. So `ingest/wholesalers.py` carries a named seed list (Ingram, Baker & Taylor,
Rittenhouse, Matthews, MBS, ThriftBooks, Better World, Half Price, Alibris…) and checks each
domain on every run. Of 31 entries: 14 answered, 12 refuse bots, 4 timed out, and 1
(`nebook.com`, Nebraska Book Company) no longer resolves and was dropped — it needs its
current domain found by hand, or removal.

A blocked site is not evidence against a company we want to *sell to*, so `blocked` and
`unreachable` are recorded and kept; only a domain with no DNS record is disqualifying.

`ingest/wholesaler_contacts.py` then reads the contact pages of the 14 that answer, following
at most three contact/wholesale/buyback links per site and collecting only addresses the
company published as `mailto:` links — 27 addresses across 8 companies, including
`buybacks@textbookrush.com` and `bulkseller@textbookrush.com`. Six of the 14 publish no
address at all and use a web form instead; those, plus the 16 whose sites block us, remain a
manual job.

## Finding Track B systematically

The curated seed list was hand-written, which does not scale and cannot be audited. USAspending
fixes both: every federal contract is public, and filtering to the book product-service codes
returns the companies that *demonstrably* move books at scale, each with what they sold and for
how much. 1,062 awards over three years, 415 reading like book buys, 186 distinct vendors, 14
of them publishers (McGraw Hill, Sage, Pearson — our suppliers, not counterparties) which are
logged and skipped. **171 stored.**

The top of that list is exactly right: EBSCO ($56M), Cox Subscriptions ($51M), ProQuest ($40M),
Ovid ($28M — Wolters Kluwer's own platform), MBS Direct ($21M), Prenax, Mackin Book Company.

Caveat worth reading before working the list: roughly half of these are digital subscription
and database vendors rather than print counterparties, so each row carries a `likely_digital`
flag in `notes` derived from its largest award description. And USAspending publishes no
website or contact for a recipient, so these 171 have names, states and spend but no way to
reach them yet — matching them to domains is the next piece of work.

Directories checked and rejected for Track B: ABAA (Cloudflare 403), CIROBE (domain no longer
resolves — the remainder expo ended), IOBA (reachable, but a 40-page Wix directory of
sole-proprietor rare-book dealers: high effort, marginal buyers).

## Signal yield, measured

State portals are thin: on 2026-08-14, 515 open Georgia events and 190 Florida advertisements
produced one relevant signal each. That is the market, not a parsing failure.

SAM.gov is where the volume is. The first live pull (2026-08-17, 30-day window) returned 28
notices, **13 of them open with a future deadline** — library subscriptions and periodicals
for Air Force education services, NOAA's Elsevier ScienceDirect renewal, NIH journal hosting,
USDA Annual Reviews. Combined open signals with future deadlines: **14**.

The binding constraint is the key, not the filters: a non-federal API key allows 10 requests
per day, and each paged query spends one. The poller enforces that budget itself and logs
anything it drops, because a silent truncation is indistinguishable from a quiet day.
