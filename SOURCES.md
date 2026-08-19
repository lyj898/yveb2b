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
| Track B published contacts — `ingest/wholesaler_contacts.py` | `contacts` (43 addresses, 9 of 32 companies) | Company contact pages, robots-checked | Monthly | Published `mailto:` links plus same-domain addresses in page text; organizational mailboxes only, robots-blocked hosts never fetched |
| Campus stores — `ingest/campus_stores.py` over each institution's own site | `organizations` (656 leads, 929 locked/incumbent, 3,513 tried-no-contact), `contacts` (store mailboxes) | Institution home + store page, robots-checked, 2-4 requests/host | Initial pull complete (2026-08-19); monthly workflow rechecks the 400 oldest-checked, full rotation ~12 runs | Independent = lead; managed (Follett `efollett.com`, B&N College `bkstr.com`, Akademos, eCampus…) = do-not-chase. Campuses examined but with no publishable contact are shown on the "No contact found" tab, not silently dropped |
| USAspending federal awards — `POST api.usaspending.gov/api/v2/search/spending_by_award/` | `organizations` (3 distributors, 168 competitors held as intelligence) | Public API, **no key** | Monthly | PSC 7610/7630/7640/7670 + NAICS 424920 over 3 years; awards de-duplicated across queries, publishers excluded |

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

`ingest/wholesaler_contacts.py` reads the contact pages of every counterparty whose host is
not robots-blocked, following contact/wholesale/buyback links and falling back to conventional
paths (`/contact`, `/sell-to-us`, `/buyback`) when the markup exposes none. It takes `mailto:`
links plus addresses written in page text **on the company's own domain**, so a partner's or
customer's address is never picked up in passing.

**Organizational mailboxes only.** Mackin publishes its full 21-person sales roster; harvesting
that is useless to us — reps do not purchase — and not what the page is for. An address is kept
when it is a mailbox (`bids@`, `buybacks@`, `bulkseller@`, `inquire@`) and a named individual
only when the page ties them to buying, capped at two per company. 23 of Mackin's 39 addresses
were skipped on that rule.

Coverage: **9 of 32 counterparties, 43 addresses.** The best of them are exactly on target —
`bids@mackin.com`, `buybacks@textbookrush.com`, `bulkseller@textbookrush.com`. Of the 23 with
none: 13 are robots-blocked (never fetched), 4 refuse connections from us at the network level
(Baker & Taylor, Majors, NBN, eCampus — the domains resolve but time out), 4 publish only a web
form (Follett, IPG, Akademos, MBS, World of Books), and 2 have no domain at all. Those are a
phone-call list, not a scraping problem.

## Finding Track B systematically

The curated seed list was hand-written, which does not scale and cannot be audited. USAspending
fixes both: every federal contract is public, and filtering to the book product-service codes
returns the companies that *demonstrably* move books at scale, each with what they sold and for
how much. 1,062 awards over three years, 415 reading like book buys, 186 distinct vendors, 14
of them publishers (McGraw Hill, Sage, Pearson — our suppliers, not counterparties) which are
logged and skipped. **171 stored.**

**What these companies are matters more than how many there are.** Winning a federal book
contract makes a company an *incumbent supplier* — a competitor — not a buyer of surplus.
Of 171, only **3** describe distribution, jobbing or buyback work: MBS Direct ($10.7M,
textbook ordering and buyback for the Merchant Marine Academy), Mackin Book Company ($5.3M,
school library jobber) and Southwest Distribution. The other 168 are subscription agents and
database vendors — Cox ($51M), ProQuest ($40M), EBSCO ($38.5M), Ovid ($28M, Wolters Kluwer's
own platform), Relx. They are stored `disqualified`: out of every lead count, kept as market
intelligence for when an agency's renewal shows up in the signals list.

Track B counterparties are therefore **33**, not 201: 30 from the seed list plus these 3.

Each row also carries a `likely_digital` flag derived from its largest award. USAspending
publishes no website or contact for a recipient, so none of these can be contacted directly
yet — matching them to domains is the next piece of work, and only worth doing for the 3.

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
