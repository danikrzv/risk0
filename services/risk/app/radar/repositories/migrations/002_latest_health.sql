-- Materialize one current health row per entity so monitoring cycles do not
-- repeatedly group the full retained health history.
CREATE TABLE IF NOT EXISTS latest_health_states (
    entity_id TEXT PRIMARY KEY REFERENCES entities (entity_id),
    evaluated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    score TEXT,
    reasons TEXT NOT NULL,
    missing_fields TEXT NOT NULL,
    rule_version TEXT NOT NULL
);

INSERT OR REPLACE INTO latest_health_states (
    entity_id, evaluated_at, status, score, reasons, missing_fields, rule_version
)
SELECT h.entity_id, h.evaluated_at, h.status, h.score, h.reasons,
       h.missing_fields, h.rule_version
FROM health_states AS h
WHERE NOT EXISTS (
    SELECT 1 FROM health_states AS newer
    WHERE newer.entity_id = h.entity_id
      AND newer.evaluated_at > h.evaluated_at
);
