-- textbook-leads :: schema.sql
-- SQLite schema for the US textbook B2B lead database (Phase 1: lead generation only).
--
-- Design notes:
--   * SQLite has no native ENUM; allowed values are enforced with CHECK constraints so
--     bad ingest data fails loudly instead of silently polluting the DB.
--   * JSON columns (programs_flags, score_breakdown, raw_payload) are TEXT holding JSON.
--     SQLite's json1 extension is compiled into the stdlib sqlite3 module on all
--     supported platforms, so json_extract() works in queries and in sql.js.
--   * Dates are ISO-8601 TEXT ('YYYY-MM-DD' or full timestamp) — sortable and sql.js-safe.
--   * website_domain is the primary merge key for dedupe; it is stored normalized
--     (lowercase, no scheme, no 'www.', no trailing slash) by enrich/dedupe.py.
--
-- Apply with:  sqlite3 db/leads.db < db/schema.sql   (idempotent — safe to re-run)

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- source_records :: raw staging
-- Every ingester writes here FIRST, verbatim, before any normalization.
-- This lets us re-normalize historically without re-hitting the source, and gives
-- an audit trail for "where did this org come from".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,          -- 'ipeds', 'sam_gov', 'state_portal_tx', 'coop_ei', ...
    source_version  TEXT,                   -- e.g. IPEDS survey year '2023', API version, file date
    source_url      TEXT,
    source_key      TEXT,                   -- natural key at the source (UNITID, noticeId, solicitation #)
    record_type     TEXT NOT NULL           -- what this row will become downstream
                    CHECK (record_type IN ('organization', 'contact', 'signal')),
    raw_payload     TEXT NOT NULL,          -- JSON blob of the row exactly as ingested
    payload_hash    TEXT NOT NULL,          -- sha256 of raw_payload — cheap change detection
    processed       INTEGER NOT NULL DEFAULT 0 CHECK (processed IN (0, 1)),
    processed_at    TEXT,
    process_error   TEXT,                   -- normalization failure message; row stays for retry
    date_ingested   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, source_key, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_source_records_unprocessed
    ON source_records (source, processed) WHERE processed = 0;
CREATE INDEX IF NOT EXISTS idx_source_records_key
    ON source_records (source, source_key);

-- ---------------------------------------------------------------------------
-- organizations :: the backbone. One row per buying entity.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS organizations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    name_normalized   TEXT,                 -- lowercased, punctuation/stopwords stripped; fuzzy-match key
    org_type          TEXT NOT NULL
                      CHECK (org_type IN (
                          'university', 'community_college', 'library', 'library_consortium',
                          'bookstore_independent', 'bookstore_chain', 'wholesaler', 'exporter',
                          'jobber', 'hospital', 'gov_agency', 'coop', 'prison_education',
                          'career_college', 'other')),
    track             TEXT NOT NULL DEFAULT 'A'
                      CHECK (track IN ('A', 'B', 'both')),
    segment           TEXT,                 -- free-text sub-segment: 'nursing school', 'ARL library', ...
    website_domain    TEXT,                 -- normalized; primary merge key (see dedupe.py)
    state             TEXT CHECK (state IS NULL OR length(state) = 2),
    city              TEXT,
    address           TEXT,
    size_metric       REAL,                 -- enrollment / staffed beds / est. annual revenue USD
    size_metric_type  TEXT CHECK (size_metric_type IS NULL OR size_metric_type IN (
                          'enrollment', 'bed_count', 'est_revenue_usd', 'member_count', 'other')),
    programs_flags    TEXT NOT NULL DEFAULT '{}',  -- JSON: {"nursing":1,"allied_health":1,"medical":0,...}
    lead_score        INTEGER CHECK (lead_score IS NULL OR (lead_score BETWEEN 0 AND 100)),
    score_breakdown   TEXT,                 -- JSON: {"segment_fit":32,"size":18,"signal":25,...}
    scored_at         TEXT,
    source            TEXT,                 -- primary/originating ingester
    coop_affiliations TEXT NOT NULL DEFAULT '[]',  -- JSON array: ["E&I","Sourcewell","TIPS"]
    notes             TEXT,
    status            TEXT NOT NULL DEFAULT 'new'
                      CHECK (status IN ('new', 'enriched', 'claimed', 'disqualified')),
    claimed_by        TEXT,                 -- commercial-team territory owner
    claimed_at        TEXT,
    date_added        TEXT NOT NULL DEFAULT (datetime('now')),
    date_updated      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Domain is "unique-ish": many orgs legitimately have no site, so NULLs must be allowed
-- and must not collide. A partial unique index gives exactly that.
CREATE UNIQUE INDEX IF NOT EXISTS uq_organizations_domain
    ON organizations (website_domain) WHERE website_domain IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_org_state           ON organizations (state);
CREATE INDEX IF NOT EXISTS idx_org_type            ON organizations (org_type);
CREATE INDEX IF NOT EXISTS idx_org_track           ON organizations (track);
CREATE INDEX IF NOT EXISTS idx_org_status          ON organizations (status);
CREATE INDEX IF NOT EXISTS idx_org_score           ON organizations (lead_score DESC);
CREATE INDEX IF NOT EXISTS idx_org_name_norm_state ON organizations (name_normalized, state);
CREATE INDEX IF NOT EXISTS idx_org_type_state      ON organizations (org_type, state);
-- IPEDS UNITID lives in notes JSON and is the authoritative identity for institutions
-- (branch campuses legitimately share one website domain, so domain alone cannot key them).
CREATE INDEX IF NOT EXISTS idx_org_ipeds_unitid
    ON organizations (json_extract(notes, '$.ipeds_unitid'));

CREATE TRIGGER IF NOT EXISTS trg_org_touch
AFTER UPDATE ON organizations
FOR EACH ROW WHEN NEW.date_updated = OLD.date_updated
BEGIN
    UPDATE organizations SET date_updated = datetime('now') WHERE id = NEW.id;
END;

-- ---------------------------------------------------------------------------
-- contacts :: purchasing-function people only (no faculty — they are adopters, not buyers)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contacts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id         INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name           TEXT,
    title          TEXT,
    role_type      TEXT NOT NULL DEFAULT 'other'
                   CHECK (role_type IN (
                       'acquisitions_librarian', 'procurement_officer', 'bookstore_buyer',
                       'owner', 'purchasing_manager', 'other')),
    email          TEXT,
    email_verified TEXT NOT NULL DEFAULT 'unverified'
                   CHECK (email_verified IN ('unverified', 'valid', 'invalid', 'catch_all')),
    verified_at    TEXT,
    phone          TEXT,
    linkedin_url   TEXT,
    is_generic     INTEGER NOT NULL DEFAULT 0 CHECK (is_generic IN (0, 1)),  -- e.g. purchasing@…
    source         TEXT,
    source_url     TEXT,
    date_added     TEXT NOT NULL DEFAULT (datetime('now')),
    date_updated   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per (org, email). Partial unique so contacts without an email are still allowed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_contacts_org_email
    ON contacts (org_id, email) WHERE email IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_contacts_org      ON contacts (org_id);
CREATE INDEX IF NOT EXISTS idx_contacts_role     ON contacts (role_type);
CREATE INDEX IF NOT EXISTS idx_contacts_verified ON contacts (email_verified);

-- ---------------------------------------------------------------------------
-- signals :: time-sensitive buying triggers (tenders, RFPs, new programs, ...)
-- org_id is nullable: a SAM.gov notice may arrive before we can match the agency.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id           INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
    org_name_raw     TEXT,                  -- agency/buyer name as published, for later matching
    signal_type      TEXT NOT NULL
                     CHECK (signal_type IN (
                         'open_tender', 'rfp', 'new_program', 'inclusive_access',
                         'buyback_season', 'expansion', 'other')),
    title            TEXT NOT NULL,
    description      TEXT,
    url              TEXT,
    reference_number TEXT,                  -- solicitation / notice ID (copy-to-clipboard in dashboard)
    deadline         TEXT,                  -- ISO date — drives urgency scoring; critical field
    posted_date      TEXT,
    amount_estimate  REAL,
    naics_code       TEXT,
    psc_code         TEXT,
    state            TEXT CHECK (state IS NULL OR length(state) = 2),
    source           TEXT NOT NULL,
    source_key       TEXT,                  -- natural key at source; used for upsert
    date_found       TEXT NOT NULL DEFAULT (datetime('now')),
    date_updated     TEXT NOT NULL DEFAULT (datetime('now')),
    status           TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'expired', 'actioned'))
);

-- Upsert key for pollers re-reading the same notice every day.
CREATE UNIQUE INDEX IF NOT EXISTS uq_signals_source_key
    ON signals (source, source_key) WHERE source_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_signals_org       ON signals (org_id);
CREATE INDEX IF NOT EXISTS idx_signals_deadline  ON signals (deadline);
CREATE INDEX IF NOT EXISTS idx_signals_status    ON signals (status, deadline);
CREATE INDEX IF NOT EXISTS idx_signals_type      ON signals (signal_type);
CREATE INDEX IF NOT EXISTS idx_signals_state     ON signals (state);

-- ---------------------------------------------------------------------------
-- interactions :: empty in Phase 1, schema-ready for Phase 2 outreach logging.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS interactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    signal_id  INTEGER REFERENCES signals(id) ON DELETE SET NULL,
    type       TEXT NOT NULL CHECK (type IN ('email', 'call', 'meeting', 'note')),
    direction  TEXT CHECK (direction IS NULL OR direction IN ('outbound', 'inbound')),
    summary    TEXT,
    outcome    TEXT,
    date       TEXT NOT NULL DEFAULT (datetime('now')),
    logged_by  TEXT
);

CREATE INDEX IF NOT EXISTS idx_interactions_org     ON interactions (org_id, date);
CREATE INDEX IF NOT EXISTS idx_interactions_contact ON interactions (contact_id);

-- ---------------------------------------------------------------------------
-- ingest_runs :: operational log. Answers "did the Tuesday SAM.gov job actually run?"
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,
    started_at     TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at    TEXT,
    status         TEXT NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running', 'success', 'partial', 'failed')),
    records_seen   INTEGER NOT NULL DEFAULT 0,
    records_new    INTEGER NOT NULL DEFAULT 0,
    records_updated INTEGER NOT NULL DEFAULT 0,
    error_count    INTEGER NOT NULL DEFAULT 0,
    notes          TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingest_runs_source ON ingest_runs (source, started_at DESC);

-- ---------------------------------------------------------------------------
-- Convenience views (the dashboard reads these directly via sql.js)
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_org_summary;
CREATE VIEW v_org_summary AS
SELECT
    o.*,
    (SELECT COUNT(*) FROM contacts c WHERE c.org_id = o.id)                        AS contact_count,
    (SELECT COUNT(*) FROM contacts c WHERE c.org_id = o.id AND c.email IS NOT NULL) AS email_count,
    (SELECT COUNT(*) FROM signals s
       WHERE s.org_id = o.id AND s.status = 'open'
         AND (s.deadline IS NULL OR s.deadline >= date('now')))                    AS open_signal_count,
    (SELECT MIN(s.deadline) FROM signals s
       WHERE s.org_id = o.id AND s.status = 'open' AND s.deadline >= date('now'))  AS next_deadline
FROM organizations o;

DROP VIEW IF EXISTS v_open_signals;
CREATE VIEW v_open_signals AS
SELECT
    s.*,
    o.name  AS org_name,
    o.state AS org_state,
    CAST(julianday(s.deadline) - julianday(date('now')) AS INTEGER) AS days_to_deadline
FROM signals s
LEFT JOIN organizations o ON o.id = s.org_id
WHERE s.status = 'open'
  AND (s.deadline IS NULL OR s.deadline >= date('now'));
