-- Agent Fact Store universal core schema (v1)
-- Every field (sports, finance, health...) fits these same tables.
-- Field-specific detail lives in metrics, entity types and the JSON columns.

CREATE SEQUENCE IF NOT EXISTS obs_seq START 1;
CREATE SEQUENCE IF NOT EXISTS run_seq START 1;

-- Where data comes from, and under what license.
CREATE TABLE IF NOT EXISTS sources (
    source_id       VARCHAR PRIMARY KEY,          -- e.g. 'cricsheet'
    name            VARCHAR NOT NULL,
    homepage_url    VARCHAR,
    license         VARCHAR NOT NULL,             -- e.g. 'ODC-By-1.0'
    license_url     VARCHAR,
    commercial_use  BOOLEAN NOT NULL,             -- can facts from here be used commercially?
    attribution     VARCHAR                       -- required credit line, if any
);

-- Real-world things: a player, a team, a company, a film, a country.
CREATE TABLE IF NOT EXISTS entities (
    entity_id       VARCHAR PRIMARY KEY,          -- our internal canonical ID
    domain          VARCHAR NOT NULL,             -- 'cricket', 'finance', ...
    entity_type     VARCHAR NOT NULL,             -- 'player', 'team', 'company', ...
    canonical_name  VARCHAR NOT NULL,
    attributes      JSON,                         -- field-specific extras (birth date, country...)
    created_at      TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- ID crosswalk: one entity, many outside IDs (Cricsheet, ESPNcricinfo, ISIN, ticker...).
-- valid_from/valid_to handle IDs that change over time, like ticker renames.
CREATE TABLE IF NOT EXISTS entity_identifiers (
    entity_id       VARCHAR NOT NULL REFERENCES entities(entity_id),
    scheme          VARCHAR NOT NULL,             -- 'cricsheet', 'cricinfo', 'isin', 'ticker'
    identifier      VARCHAR NOT NULL,
    source_id       VARCHAR REFERENCES sources(source_id),
    valid_from      DATE,
    valid_to        DATE,
    PRIMARY KEY (scheme, identifier)
);

-- The metric catalog: every metric defined once.
CREATE TABLE IF NOT EXISTS metrics (
    metric_id       VARCHAR PRIMARY KEY,          -- 'cricket.batting.runs'
    domain          VARCHAR NOT NULL,
    name            VARCHAR NOT NULL,
    description     VARCHAR NOT NULL,
    unit            VARCHAR,                      -- 'runs', 'USD', 'years'
    higher_is_better BOOLEAN,
    definition      VARCHAR                       -- formula or method, for derived metrics
);

-- One row per pipeline execution, so every value traces back to the run that produced it.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id           BIGINT PRIMARY KEY DEFAULT nextval('run_seq'),
    source_id        VARCHAR NOT NULL REFERENCES sources(source_id),
    pipeline         VARCHAR NOT NULL,
    pipeline_version VARCHAR NOT NULL,
    source_url       VARCHAR,                      -- what was fetched
    raw_path         VARCHAR,                      -- where the untouched copy is kept
    raw_sha256       VARCHAR,                      -- fingerprint of that copy
    started_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
    finished_at      TIMESTAMP,
    status           VARCHAR NOT NULL DEFAULT 'running',  -- running | success | failed
    rows_written     BIGINT
);

-- The facts. Every value carries its own provenance and license (novelty row 1),
-- a confidence slot for cross-source verification (row 2), and bitemporal versioning:
-- recorded_at/superseded_at keep old values when a source corrects or restates them.
CREATE TABLE IF NOT EXISTS observations (
    obs_id          BIGINT PRIMARY KEY DEFAULT nextval('obs_seq'),
    entity_id       VARCHAR NOT NULL REFERENCES entities(entity_id),
    metric_id       VARCHAR NOT NULL REFERENCES metrics(metric_id),
    period_start    DATE,
    period_end      DATE,
    period_label    VARCHAR,                      -- '2018', '2023-Q4', 'career'
    context         JSON,                         -- {"format": "Test"}, {"currency": "USD"}
    value           DOUBLE,
    value_text      VARCHAR,                      -- for non-numeric facts
    unit            VARCHAR,
    -- provenance
    source_id       VARCHAR NOT NULL REFERENCES sources(source_id),
    source_url      VARCHAR,
    fetched_at      TIMESTAMP NOT NULL,
    run_id          BIGINT REFERENCES ingestion_runs(run_id),
    license         VARCHAR NOT NULL,
    -- verification
    confidence      DOUBLE,                       -- 0..1, filled in by the verification engine
    -- versioning
    recorded_at     TIMESTAMP NOT NULL DEFAULT current_timestamp,
    superseded_at   TIMESTAMP                     -- NULL = current version
);

-- Current facts only, which is what most queries want.
CREATE OR REPLACE VIEW current_observations AS
SELECT * FROM observations WHERE superseded_at IS NULL;
