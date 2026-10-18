-- Steward metadata tables (docs/ARCHITECTURE.md §4.3). Idempotent.
--
-- Only schemas + dq tables live here. The five raw.* tables are created by
-- the loader on first sight (schema-evolving, Phase 1); staging/marts are
-- owned by dbt (Phase 1). No destructive statements in this file.

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS marts;
CREATE SCHEMA IF NOT EXISTS dq;

CREATE TABLE IF NOT EXISTS dq.batches (
    batch_id       text PRIMARY KEY,
    business_date  date NOT NULL,
    arrived_at     timestamptz NOT NULL,
    -- loaded | checked
    status         text NOT NULL DEFAULT 'loaded'
);

CREATE TABLE IF NOT EXISTS dq.batch_tables (
    batch_id    text REFERENCES dq.batches,
    table_name  text,
    row_count   integer NOT NULL,
    columns     text[] NOT NULL,
    PRIMARY KEY (batch_id, table_name)
);

CREATE TABLE IF NOT EXISTS dq.pipeline_runs (
    run_id        bigserial PRIMARY KEY,
    batch_id      text,
    command       text,
    -- success | failed
    status        text,
    started_at    timestamptz,
    finished_at   timestamptz,
    log_excerpt   text
);

CREATE TABLE IF NOT EXISTS dq.incidents (
    incident_id   uuid PRIMARY KEY,
    fingerprint   text NOT NULL,
    batch_id      text,
    table_name    text,
    -- open | investigating | awaiting_approval | resolved | rejected | failed
    status        text NOT NULL,
    -- breaking | warning
    severity      text NOT NULL,
    violations    jsonb NOT NULL,
    root_cause    jsonb,
    impact        jsonb,
    resolution    jsonb,
    created_at    timestamptz DEFAULT now(),
    updated_at    timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dq.agent_steps (
    step_id       bigserial PRIMARY KEY,
    incident_id   uuid,
    seq           integer,
    node          text,
    -- llm | tool | system
    kind          text,
    name          text,
    input         jsonb,
    output        jsonb,
    tokens_in     integer,
    tokens_out    integer,
    latency_ms    integer,
    created_at    timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dq.fix_attempts (
    attempt_id    bigserial PRIMARY KEY,
    incident_id   uuid,
    attempt_no    integer,
    plan          jsonb,
    verification  jsonb,
    passed        boolean,
    created_at    timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dq.publications (
    incident_id   uuid,
    kind          text,
    -- dry_run | live
    mode          text,
    url           text,
    created_at    timestamptz DEFAULT now()
);

-- Read access for the read-only agent role, now and for tables created later
-- (by dbt builds in Phase 1+). Write privileges are deliberately never granted
-- to dqs_ro (AGENTS.md §7.2).
GRANT USAGE ON SCHEMA raw, staging, marts, dq TO dqs_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA raw, staging, marts, dq TO dqs_ro;

ALTER DEFAULT PRIVILEGES FOR ROLE dqs IN SCHEMA raw     GRANT SELECT ON TABLES TO dqs_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE dqs IN SCHEMA staging GRANT SELECT ON TABLES TO dqs_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE dqs IN SCHEMA marts   GRANT SELECT ON TABLES TO dqs_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE dqs IN SCHEMA dq      GRANT SELECT ON TABLES TO dqs_ro;
