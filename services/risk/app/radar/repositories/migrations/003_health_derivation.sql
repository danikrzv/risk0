-- How each recorded health state was computed (HealthDerivation as JSON).
-- Nullable: rows written before this migration, and states recorded without
-- an explanation, have none. SQLite has no ADD COLUMN IF NOT EXISTS; the
-- migration runner re-checks the applied version under the write lock, so
-- this script runs at most once per database.
ALTER TABLE health_states ADD COLUMN derivation TEXT;
ALTER TABLE latest_health_states ADD COLUMN derivation TEXT;
