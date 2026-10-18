-- Roles for the steward (docs/ARCHITECTURE.md §3). Idempotent.
--
-- `dqs` is normally created by the docker-compose POSTGRES_USER env; the DO
-- block only covers running this file in other contexts. `dqs_ro` is the
-- read-only role used by agent tools (run_readonly_sql) from Phase 4.
--
-- NOTE: this file also creates the steward schemas. The docker entrypoint
-- runs files alphabetically (000 before 001) and the GRANTs below reference
-- the schemas, so they must exist here.

DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dqs') THEN
        CREATE ROLE dqs LOGIN PASSWORD 'dqs';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dqs_ro') THEN
        CREATE ROLE dqs_ro LOGIN PASSWORD 'dqs_ro';
    END IF;
END
$do$;

-- the read-only role is always exactly this password in this setup
ALTER ROLE dqs_ro LOGIN PASSWORD 'dqs_ro';

GRANT CONNECT ON DATABASE warehouse TO dqs_ro;

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS marts;
CREATE SCHEMA IF NOT EXISTS dq;
