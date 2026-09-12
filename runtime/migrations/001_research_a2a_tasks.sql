BEGIN;

CREATE TABLE IF NOT EXISTS research_a2a_tasks (
    id text PRIMARY KEY,
    message_id text NOT NULL,
    submission jsonb NOT NULL,
    submission_commitment character(64) NOT NULL,
    state text NOT NULL,
    artifact jsonb,
    error character varying(500),
    run_count integer NOT NULL DEFAULT 0,
    run_id text,
    lease_owner character varying(200),
    lease_expires_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);

ALTER TABLE research_a2a_tasks ADD COLUMN IF NOT EXISTS run_id text;
ALTER TABLE research_a2a_tasks ADD COLUMN IF NOT EXISTS lease_owner character varying(200);
ALTER TABLE research_a2a_tasks ADD COLUMN IF NOT EXISTS lease_expires_at timestamp with time zone;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
        WHERE c.conrelid = 'research_a2a_tasks'::regclass AND c.contype = 'u'
        GROUP BY c.oid HAVING array_agg(a.attname ORDER BY a.attname) = ARRAY['message_id'::name]
    ) THEN
        ALTER TABLE research_a2a_tasks
            ADD CONSTRAINT research_a2a_tasks_message_id_key UNIQUE (message_id);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'research_a2a_tasks'::regclass
          AND conname = 'research_a2a_tasks_state_check'
    ) THEN
        ALTER TABLE research_a2a_tasks
            ADD CONSTRAINT research_a2a_tasks_state_check
            CHECK (state IN ('submitted', 'working', 'completed', 'failed', 'canceled'))
            NOT VALID;
    END IF;
END $$;

ALTER TABLE research_a2a_tasks VALIDATE CONSTRAINT research_a2a_tasks_state_check;

COMMIT;
