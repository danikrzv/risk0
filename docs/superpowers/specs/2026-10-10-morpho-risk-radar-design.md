# Risk0 Morpho Risk Radar — Design

## 1. Objective

Risk0 evolves from a single-position scenario simulator into a local, read-only Morpho dependency radar. The radar continuously observes the vaults and markets closest to the curated RockawayX/Belem catalog, detects abnormal oracle, token, chain, market, and vault states, propagates incidents through the dependency graph, and shows which manually entered exposures may be affected.

The first release must start quickly by extending the existing pipelines instead of replacing them. It keeps the current 30-day `STAY`, `EXIT`, and `UNAVAILABLE` analysis for a selected exposure while adding continuous monitoring, similarity discovery, incident propagation, notifications, and the approved Institutional Terminal interface.

## 2. Success Criteria

The release is successful when a local user can:

1. Load the existing 17-entry curated catalog and discover related Morpho vaults and markets on every chain exposed by the Morpho API.
2. See the 10 nearest vault neighbors per catalog vault when similarity exceeds 60%.
3. Keep neighbors above 33% in backend processing even when hidden from the normal graph.
4. Observe fresh state for chains, tokens, oracles, markets, and vaults on a five-minute cycle.
5. Detect and classify `HEALTHY`, `WARNING`, `CRITICAL`, and `UNKNOWN` states without treating missing data as healthy.
6. Compare equivalent oracle feeds and flag material deviation from their consensus.
7. Reveal a normally hidden related vault when an incident propagates through it.
8. Enter a manual lender or borrower exposure and see the incident path and estimated impact.
9. Receive a temporary top-right toast and a persistent in-app notification entry.
10. Continue to run the existing 30-day hybrid stay-versus-exit analysis where its required data is available.

The MVP remains analytical and non-custodial. It never connects a wallet, signs a message, constructs a transaction, or claims that a catalog entry is a confirmed Belem position unless its provenance says so.

## 3. Delivery Strategy

Use a modular local monolith:

- **Next.js web app:** Institutional Terminal UI, graph interaction, exposure form, notification center, and live updates.
- **FastAPI risk service:** discovery, source adapters, state normalization, health computation, similarity, propagation, scheduling, persistence, and API/SSE delivery.
- **SQLite:** cache plus durable topology, snapshots, health transitions, similarities, incidents, notifications, and manual exposures.
- **In-memory graph:** NetworkX or an equivalent small graph layer rebuilt from SQLite topology for propagation and API projection.

This is an incremental extension of the current two-service architecture. No additional deployable service, queue, graph database, or frontend state framework is introduced for the MVP.

## 4. Existing Pipelines to Reuse

| Existing capability | Reuse decision | Required extension |
| --- | --- | --- |
| Versioned catalog in `data/catalog/ethereum.json` | Keep as the seed catalog | Generalize storage to multi-chain catalog seeds without breaking current entries |
| `MorphoClient` and catalog hydration | Keep request, normalization, freshness, and degradation patterns | Add paginated discovery, positions, all supported chain IDs, and batch snapshot reads |
| `Ethereum` adapter | Keep RPC safety, oracle reads, historical prices, and redaction | Introduce a chain-aware RPC registry and chain-state probes |
| `Uniswap` adapter | Keep quote and executable-depth logic | Use its current observations as token-liquidity inputs; other venues remain future adapters |
| `DeFiScan` evidence | Keep protocol evidence and model-estimated labels | Feed protocol state into the new health layer without changing probability semantics |
| `SQLiteCache` | Keep as the HTTP/provider cache | Add separate repository tables; do not overload the cache table with domain history |
| `Freshness` and `DataQuality` | Keep semantics and conservative failure behavior | Reuse for every observed state and map expired critical state to `UNKNOWN` |
| `GraphContext` and `build_risk_graph` | Keep position-analysis graph behavior | Add a separate topology builder that shares stable node IDs and contract enums |
| `ScenarioEngine`, history estimator, exit-cost, recommendation | Keep unchanged initially | Consume a selected manual exposure and incident context through an adapter layer |
| JSON Schema → Pydantic/TypeScript contracts | Keep as the only service boundary definition | Add radar schemas and regenerate both language bindings |
| Next.js proxy routes and API client | Keep server-side proxy and secret isolation | Add graph, entity history, exposure, notification, and SSE routes |
| React Flow graph component | Keep renderer and node-selection patterns | Add graph modes, severity styling, hidden-node reveal, and propagation focus |
| Fixture mode and deterministic E2E harness | Keep dependency injection and golden flows | Add one deterministic monitoring cycle and one incident propagation fixture |

The current position-analysis API remains compatible during the radar work. New radar code calls existing domain services; it does not fork their business rules.

## 5. Scope

### Included

- Morpho vaults and markets on all chains returned by Morpho discovery.
- The current 17 catalog entries as the seed set.
- Similar Morpho vaults and their markets, tokens, oracle references, IRMs, and LLTVs.
- Manual lender and borrower exposure amounts.
- Fast five-minute monitoring loop and slow hourly discovery loop.
- Oracle, token, chain, market, vault, and protocol health.
- Lender and borrower distribution metrics when Morpho exposes the underlying positions.
- Incident propagation, hidden-neighbor reveal, toasts, notification history, and SSE updates.
- Current 30-day hybrid recommendation for compatible selected exposures.
- Local SQLite persistence and local execution.

### Deferred

- The other eight RockawayX protocols.
- Automated wallet or portfolio discovery.
- Automated remediation or transaction execution.
- Mobile-specific UI and hosted multi-user operation.
- Push, email, Slack, or Telegram alerts.
- A production distributed scheduler, message broker, graph database, or observability stack.
- Historical backfill beyond data already available from providers during the MVP.

## 6. Domain Graph

### Structural Nodes

- `chain`
- `protocol`
- `vault`
- `market`
- `asset`
- `oracle`
- `irm`
- `position`

A market is represented by its canonical tuple: loan asset, collateral asset, oracle, IRM, and LLTV. Stable node IDs retain the current `<node_type>:<chain_id>:<address_or_key>` convention.

### Structural Relationships

- `allocated_to`
- `lends`
- `borrows`
- `uses_collateral`
- `uses_oracle`
- `uses_irm`
- `backed_by`
- `deployed_on`
- `exits_through`
- `similar_to`

### State Records

State is time-varying data attached to stable entities rather than a second permanent graph hierarchy. The API may project virtual state nodes for visualization, but SQLite stores typed snapshots for chain, token, oracle, market, vault, and protocol state.

Every state record carries:

- entity ID and state type;
- observed and retrieved timestamps;
- source and source identity;
- freshness;
- normalized metrics;
- health status;
- health score where meaningful;
- reasons and missing fields.

`UNKNOWN` is explicit and propagates conservatively. It never becomes a zero-risk metric.

## 7. Discovery and Similarity

### Discovery

The slow loop runs every 60 minutes:

1. Query Morpho for supported chains, listed vaults, markets, allocations, and required metadata.
2. Upsert entities and structural relationships.
3. Start from each catalog vault and compute candidate neighbors.
4. Save all similarities above 33%.
5. Mark the top 10 neighbors above 60% per catalog vault as normally visible.
6. Keep the remaining qualifying neighbors hidden but monitored.

Provider pagination, retry, timeout, cache, and stale fallback follow the current `MorphoClient` patterns. One failing chain or entity is isolated from the others.

### Similarity Score

The score is deterministic and versioned as `vault-similarity-v1`:

- 35% shared markets, weighted by current allocation;
- 20% loan and collateral asset overlap;
- 15% oracle overlap or declared equivalence;
- 10% IRM match;
- 10% LLTV proximity;
- 5% same chain;
- 5% same curator or vault family.

The stored record includes the total score and each component so the UI can explain why two vaults are related. Missing dimensions contribute zero and are listed as missing; they are not silently imputed.

## 8. Monitoring and Health

### Fast Loop

Every five minutes, without overlapping runs:

1. Fetch chain head/finality and source availability.
2. Refresh token price, depeg, volatility, and liquidity observations.
3. Refresh oracle value, age, heartbeat, and comparable-feed consensus.
4. Refresh market supply, borrow, utilization, liquidity, LLTV, positions, and bad-debt indicators.
5. Derive lender and borrower counts, concentration, LTV distribution, near-liquidation debt, and HHI when position data is available.
6. Aggregate market state into vault state using current allocations.
7. Evaluate health rules and hysteresis.
8. Open, update, or resolve incidents.
9. Propagate active incidents and update affected manual exposures.
10. Persist notifications and publish SSE events.

If a cycle still runs when the next tick arrives, the next tick is skipped and the scheduler records a warning. Work is isolated by chain so one slow network does not block completed chain results.

### Health Status

- `HEALTHY`: all required measurements are fresh and within limits.
- `WARNING`: a warning condition persisted for two consecutive cycles.
- `CRITICAL`: a critical condition is applied immediately.
- `UNKNOWN`: required data is missing, expired, contradictory, or unreachable.

Recovery to `HEALTHY` requires three consecutive normal cycles. Status rules and thresholds are configuration records with a version, not scattered UI constants.

### Oracle Consensus

Comparable feeds are grouped by chain and represented pair, including direct and explicitly supported derived references. For each group, the service computes a robust median reference from fresh feeds. A feed is classified using deviation from consensus, heartbeat/freshness, and the number and quality of peers.

No consensus is emitted when too few independent fresh feeds exist. In that case the oracle state is `UNKNOWN`, unless a direct hard failure independently makes it `CRITICAL`.

### Market and Vault Health

Market health combines both sides of the market:

- supply and borrowing liquidity;
- utilization and rate stress;
- lender and borrower concentration;
- borrower LTV distribution;
- debt close to liquidation;
- bad debt or insolvency indicators;
- collateral, loan-asset, oracle, IRM, and chain state.

Vault health is an allocation-weighted aggregation of its markets plus withdrawal liquidity, allocation concentration, pending changes, and unmodelled exposure. An unknown material allocation prevents a fully healthy classification.

## 9. Incidents and Propagation

An incident is an immutable source event plus mutable lifecycle state. It records source entity, first/last observation, severity, triggering metrics, rule version, status, and resolution time.

Propagation traverses typed relationships with deterministic rules:

- oracle incident → markets using the oracle → allocating vaults → manual exposures;
- token incident → markets using it as loan or collateral → vaults → exposures;
- chain incident → all entities on the chain;
- market incident → allocating vaults and direct positions;
- vault incident → manual vault exposures;
- protocol incident → all protocol entities, with relationship-specific impact labels.

Each impact stores the path, distance, confidence, estimated affected amount, and reasons. The shortest high-confidence path is the default UI path; alternate paths remain queryable.

A hidden neighbor is revealed whenever an active incident reaches it or originates from it, regardless of whether its similarity is below 60%, provided its stored score exceeds 33%.

## 10. Persistence and Retention

The existing `cache_entries` table remains a bounded provider-response cache. New repositories own these tables:

- `entities`
- `relationships`
- `state_snapshots`
- `health_states`
- `similarities`
- `incidents`
- `incident_impacts`
- `notifications`
- `manual_exposures`
- `scheduler_runs`

Retention:

- raw five-minute snapshots: 30 days;
- hourly aggregates: 1 year;
- topology and latest health: retained while discoverable plus tombstone metadata;
- incidents and notifications: retained indefinitely for the local MVP;
- provider cache: continues to use its existing namespace-specific policy.

SQLite migrations are explicit, idempotent, and tested against an empty database and the current cache-only database.

## 11. API and Contracts

Add versioned contracts and endpoints:

- `GET /v1/graph`
- `GET /v1/graph/incidents/{incident_id}`
- `GET /v1/entities/{entity_id}/history`
- `GET /v1/similarities/{vault_id}`
- `GET /v1/notifications`
- `POST /v1/notifications/{notification_id}/read`
- `POST /v1/exposures`
- `GET /v1/exposures/{exposure_id}/impact`
- `GET /v1/events` using Server-Sent Events

The existing `GET /v1/catalog` and `POST /v1/analysis` remain available and backward compatible. The graph endpoint supports chain, node type, status, visibility, and incident filters. Large histories are paginated.

SSE events carry identifiers and change summaries, not complete graph payloads. The client refetches the relevant resource after an event, which keeps recovery and reconnect behavior simple.

## 12. Institutional Terminal UI

The approved visual direction is **Institutional Terminal A**:

- dark, dense, low-decoration interface;
- monospace typography and strict alignment;
- thin borders and restrained cyan, green, amber, and red status colors;
- persistent top navigation and system ticker;
- left exposure/filter rail;
- central dependency graph;
- right alert, incident, propagation, and decision-support rail;
- bottom engine and data-source status bar.

The default graph shows catalog vaults and up to 10 neighbors above 60% per catalog vault. Healthy secondary nodes are collapsed by default. Selecting an incident centers and highlights its propagation path. A newly revealed hidden neighbor is visually distinguished and explains its similarity and role in the incident.

Notifications appear as dismissing top-right toasts and remain in a persistent notification panel with read state. Resolved incidents remain in history and no longer appear as active alerts.

## 13. Failure and Security Behavior

- Continue serving the last good snapshot with age and stale status where policy allows.
- Change expired critical measurements to `UNKNOWN`.
- Isolate failures by provider, chain, and entity.
- Support ordered public RPC fallbacks and optional private RPC URLs from `.env`.
- Never return RPC credentials in source fields, logs, errors, graph payloads, fixtures, or screenshots.
- Reject overlapping scheduler runs and record skipped cycles.
- Use UTC internally and render local time only in the UI.
- Keep all analysis read-only and avoid wallet permissions.
- Preserve current public-safe API error handling and do not proxy provider response bodies.

## 14. Testing Strategy

### Unit Tests

- similarity component scoring, thresholds, top-10 selection, and missing dimensions;
- health rules, hysteresis, `UNKNOWN`, and recovery;
- oracle consensus and insufficient-peer behavior;
- lender/borrower concentration and near-liquidation derivations;
- propagation paths, hidden-node reveal, and exposure impact;
- snapshot retention and hourly aggregation;
- scheduler locking and skipped cycles.

### Adapter and Repository Tests

- paginated multi-chain Morpho discovery with sanitized fixtures;
- chain-aware RPC registry and fallback;
- cache namespace isolation and credential redaction;
- SQLite migration, upsert, pagination, and retention behavior;
- partial provider and partial chain failures.

### Contract and API Tests

- JSON Schema, Pydantic, and generated TypeScript parity;
- backward compatibility for catalog and analysis responses;
- graph filters, incident projection, notification read state, exposure validation, and SSE reconnect.

### Frontend Tests

- terminal layout and accessible navigation;
- graph status and visibility filters;
- propagation focus and hidden-neighbor reveal;
- toast expiry with persistent notification history;
- stale and unknown data never styled as healthy.

### End-to-End Tests

Fixture mode adds two golden paths while keeping all current golden tests:

1. A normal monitoring cycle displays healthy catalog topology and related neighbors.
2. An oracle deviation opens a critical incident, reveals a hidden related vault, propagates to a manual exposure, displays a toast, persists a notification, and focuses the graph path.

Live smoke tests remain opt-in and verify only source connectivity and contract shape. Deterministic CI tests never require network access.

## 15. Parallel Work Boundaries

The implementation plan should split work across three long-lived feature branches with short integration checkpoints:

### Agent A — Data, Persistence, and Scheduling

Owns multi-chain discovery, RPC registry, domain repositories, migrations, retention, and fast/slow schedulers. Primary paths: `services/risk/app/adapters`, `services/risk/app/infrastructure`, new `services/risk/app/radar/repositories`, and related Python tests.

### Agent B — Health, Similarity, and Propagation

Owns pure domain logic for similarity, normalized state, health rules, oracle consensus, incidents, propagation, and exposure impact. Primary paths: new `services/risk/app/radar/domain`, `services/risk/app/radar/analysis`, and related Python tests.

### Lead Agent — Contracts, API, UI, Integration, and Quality

Owns schemas/generated types, API composition, SSE, Next.js Institutional Terminal, integration fixtures, E2E tests, reviews, and merges. Primary paths: `packages/contracts`, `services/risk/app/api`, `services/risk/app/main.py`, `apps/web`, and integration documentation.

Agents do not edit another owner’s paths without agreement. Shared contract changes land first on the integration branch. Every branch rebases or merges the latest integration branch before opening a PR. The lead agent reviews contracts, tests, failure semantics, secrets, and merge compatibility before integration.

## 16. Delivery Order

1. Freeze contracts, table schemas, package boundaries, and deterministic fixtures.
2. Parallelize persistence/discovery and pure health/propagation engines.
3. Integrate the radar orchestration and read APIs.
4. Add scheduler and SSE.
5. Replace the current page shell with the Institutional Terminal while reusing existing components.
6. Add the two radar E2E flows and run the complete existing regression suite.

The first integration milestone is intentionally narrow: one deterministic Ethereum monitoring cycle over the current catalog, one similar vault, one oracle incident, one manual exposure, and one propagated notification. Multi-chain breadth is added only after this vertical slice passes end to end.
