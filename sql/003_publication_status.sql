-- Publication hardening (Phase 6b). Idempotent: safe to replay on every db init.
--
-- Why: Phase 6a inserted the dq.publications row only AFTER the external call,
-- so a crash in that window could publish twice on retry. The publisher now
-- writes a `pending` row first, then flips it to `done`; a unique key makes
-- "one publication per incident+kind" a database invariant instead of an
-- application convention.

ALTER TABLE dq.publications ADD COLUMN IF NOT EXISTS status text;

-- Rows created before this migration are finished publications by definition.
UPDATE dq.publications SET status = 'done' WHERE status IS NULL;

ALTER TABLE dq.publications ALTER COLUMN status SET DEFAULT 'pending';
ALTER TABLE dq.publications ALTER COLUMN status SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'publications_status_check'
    ) THEN
        ALTER TABLE dq.publications
            ADD CONSTRAINT publications_status_check
            CHECK (status IN ('pending', 'done'));
    END IF;
END $$;

-- Pre-003 databases have no unique key, so a legacy duplicate could exist.
-- Keep the newest row per (incident_id, kind); the index would otherwise fail.
DELETE FROM dq.publications
WHERE ctid IN (
    SELECT ctid FROM (
        SELECT ctid,
               row_number() OVER (
                   PARTITION BY incident_id, kind
                   ORDER BY created_at DESC NULLS LAST, ctid DESC
               ) AS row_no
        FROM dq.publications
    ) ranked
    WHERE ranked.row_no > 1
);

CREATE UNIQUE INDEX IF NOT EXISTS publications_incident_kind_uniq
    ON dq.publications (incident_id, kind);
