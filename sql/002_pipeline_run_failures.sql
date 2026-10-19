-- Migration 002 (Phase 3): persist failing dbt nodes next to their run.
--
-- Why: `dq.pipeline_runs` used to hold only status + log excerpt; the impact
-- report must read *structured* failing-test records instead of re-scraping
-- target/run_results.json (a live file that later builds overwrite and that
-- may not match the incident's batch at all, ARCHITECTURE §4.3 note). The
-- decision (Phase 3) is: every dbt build parses run_results.json immediately
-- and stores the failed/errored node ids + statuses on the same row.
--
-- Idempotent: safe to replay on fresh and existing databases (`dqs db init`
-- runs this file after 001, and may be run any number of times).

ALTER TABLE dq.pipeline_runs
    ADD COLUMN IF NOT EXISTS failed_nodes JSONB;

-- When the failing-node payload was parsed from run_results.json; NULL marks
-- a pre-002 row (no parsed record exists for it).
ALTER TABLE dq.pipeline_runs
    ADD COLUMN IF NOT EXISTS run_results_parsed_at TIMESTAMPTZ;

COMMENT ON COLUMN dq.pipeline_runs.failed_nodes IS
    'dbt nodes that failed/errored in this run: [{"unique_id","name","status","message"}]; parsed at build time';
