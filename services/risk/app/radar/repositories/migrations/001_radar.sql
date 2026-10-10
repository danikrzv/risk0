-- Radar domain tables. Timestamps are ISO-8601 UTC strings with microseconds so
-- lexical order equals chronological order; money, ratios, and metrics are stored
-- as exact decimal strings (or tagged JSON) and never as SQLite REAL values.
-- The provider cache table `cache_entries` is intentionally untouched.

CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    chain_id INTEGER NOT NULL CHECK (chain_id > 0),
    address_or_key TEXT NOT NULL,
    label TEXT NOT NULL,
    source TEXT NOT NULL,
    catalog INTEGER NOT NULL CHECK (catalog IN (0, 1)),
    visible INTEGER NOT NULL CHECK (visible IN (0, 1)),
    metadata TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    tombstoned_at TEXT
);
CREATE INDEX IF NOT EXISTS entities_by_chain_type
    ON entities (chain_id, entity_type);

CREATE TABLE IF NOT EXISTS relationships (
    source_entity_id TEXT NOT NULL REFERENCES entities (entity_id),
    target_entity_id TEXT NOT NULL REFERENCES entities (entity_id),
    relationship_type TEXT NOT NULL,
    weight TEXT,
    metadata TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    tombstoned_at TEXT,
    PRIMARY KEY (source_entity_id, target_entity_id, relationship_type)
);
CREATE INDEX IF NOT EXISTS relationships_by_target
    ON relationships (target_entity_id);

CREATE TABLE IF NOT EXISTS similarities (
    catalog_entity_id TEXT NOT NULL REFERENCES entities (entity_id),
    candidate_entity_id TEXT NOT NULL REFERENCES entities (entity_id),
    model_version TEXT NOT NULL,
    score TEXT NOT NULL,
    components TEXT NOT NULL,
    missing_dimensions TEXT NOT NULL,
    normally_visible INTEGER NOT NULL CHECK (normally_visible IN (0, 1)),
    computed_at TEXT NOT NULL,
    PRIMARY KEY (catalog_entity_id, candidate_entity_id, model_version)
);
CREATE INDEX IF NOT EXISTS similarities_by_candidate
    ON similarities (candidate_entity_id);

CREATE TABLE IF NOT EXISTS state_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities (entity_id),
    state_type TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    source TEXT NOT NULL,
    source_identity TEXT NOT NULL,
    freshness TEXT NOT NULL,
    metrics TEXT NOT NULL,
    missing_fields TEXT NOT NULL,
    UNIQUE (entity_id, state_type, source_identity, observed_at)
);
CREATE INDEX IF NOT EXISTS state_snapshots_by_entity_time
    ON state_snapshots (entity_id, observed_at);
CREATE INDEX IF NOT EXISTS state_snapshots_by_time
    ON state_snapshots (observed_at);

CREATE TABLE IF NOT EXISTS state_hourly_aggregates (
    entity_id TEXT NOT NULL REFERENCES entities (entity_id),
    state_type TEXT NOT NULL,
    hour_start TEXT NOT NULL,
    sample_count INTEGER NOT NULL CHECK (sample_count > 0),
    worst_freshness TEXT NOT NULL,
    metrics TEXT NOT NULL,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    PRIMARY KEY (entity_id, state_type, hour_start)
);
CREATE INDEX IF NOT EXISTS state_hourly_aggregates_by_hour
    ON state_hourly_aggregates (hour_start);

CREATE TABLE IF NOT EXISTS health_states (
    entity_id TEXT NOT NULL REFERENCES entities (entity_id),
    evaluated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    score TEXT,
    reasons TEXT NOT NULL,
    missing_fields TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    PRIMARY KEY (entity_id, evaluated_at)
);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    source_entity_id TEXT NOT NULL REFERENCES entities (entity_id),
    severity TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'RESOLVED')),
    trigger_metrics TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    resolved_at TEXT,
    reasons TEXT NOT NULL,
    CHECK ((status = 'OPEN') = (resolved_at IS NULL))
);
-- Source-event identity: at most one open incident per source entity and rule.
CREATE UNIQUE INDEX IF NOT EXISTS incidents_one_open_per_source_rule
    ON incidents (source_entity_id, rule_version) WHERE status = 'OPEN';

CREATE TABLE IF NOT EXISTS incident_impacts (
    incident_id TEXT NOT NULL REFERENCES incidents (incident_id),
    target_entity_id TEXT NOT NULL,
    path TEXT NOT NULL,
    distance INTEGER NOT NULL CHECK (distance >= 0),
    confidence TEXT NOT NULL,
    estimated_affected_amount TEXT,
    reasons TEXT NOT NULL,
    revealed INTEGER NOT NULL CHECK (revealed IN (0, 1)),
    alternate_path_count INTEGER NOT NULL CHECK (alternate_path_count >= 0),
    PRIMARY KEY (incident_id, target_entity_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents (incident_id),
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS notifications_by_time
    ON notifications (created_at, notification_id);

CREATE TABLE IF NOT EXISTS manual_exposures (
    exposure_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    amount TEXT NOT NULL,
    numeraire TEXT NOT NULL,
    collateral_amount TEXT,
    debt_amount TEXT,
    ltv TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exposure_impacts (
    exposure_id TEXT NOT NULL REFERENCES manual_exposures (exposure_id),
    incident_id TEXT NOT NULL REFERENCES incidents (incident_id),
    target_entity_id TEXT NOT NULL,
    path TEXT NOT NULL,
    distance INTEGER NOT NULL CHECK (distance >= 0),
    confidence TEXT NOT NULL,
    estimated_affected_amount TEXT,
    reasons TEXT NOT NULL,
    revealed INTEGER NOT NULL CHECK (revealed IN (0, 1)),
    alternate_path_count INTEGER NOT NULL CHECK (alternate_path_count >= 0),
    PRIMARY KEY (exposure_id, incident_id, target_entity_id)
);

CREATE TABLE IF NOT EXISTS scheduler_runs (
    run_id TEXT PRIMARY KEY,
    loop TEXT NOT NULL CHECK (loop IN ('fast', 'slow')),
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    processed_count INTEGER NOT NULL CHECK (processed_count >= 0),
    error_count INTEGER NOT NULL CHECK (error_count >= 0),
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS scheduler_runs_by_loop_time
    ON scheduler_runs (loop, started_at);
