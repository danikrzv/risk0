# Morpho Risk Radar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing Risk0 position-analysis MVP into a local, continuously refreshed Morpho dependency radar with similarity discovery, health states, incident propagation, notifications, and an Institutional Terminal UI.

**Architecture:** Keep the current Next.js + FastAPI modular monolith and reuse its Morpho, Ethereum, Uniswap, cache, freshness, graph, scenario, exit-cost, recommendation, fixture, and contract pipelines. Add a focused `app.radar` package, durable SQLite repositories, non-overlapping fast/slow loops, read/SSE APIs, and UI projections; preserve the existing catalog and analysis APIs.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLite, pytest, TypeScript, Next.js 16 App Router, React 19, React Flow, Vitest, Playwright, pnpm, uv.

**Spec:** `docs/superpowers/specs/2026-10-10-morpho-risk-radar-design.md`

## Global Constraints

- Preserve `GET /v1/catalog` and `POST /v1/analysis` compatibility and all existing tests.
- Keep RPC and API credentials only in `.env`; redact credentials from logs, sources, fixtures, errors, and responses.
- Use UTC-aware timestamps and `Decimal` for financial values; never use binary floats in domain calculations.
- Missing or expired critical data maps to `UNKNOWN`, never `HEALTHY` or zero risk.
- Fast monitoring runs every 5 minutes; slow discovery runs every 60 minutes; overlapping cycles are skipped and recorded.
- Store raw snapshots for 30 days, hourly aggregates for 1 year, and incidents/notifications indefinitely for the local MVP.
- Store all similarities above 33%; normally display only each catalog vault's top 10 neighbors above 60%.
- Apply warning after 2 abnormal cycles, critical immediately, and healthy recovery after 3 normal cycles.
- Keep the application read-only: no wallet connection, signing, approvals, transactions, or automated remediation.
- Follow `apps/web/AGENTS.md` and read the relevant Next.js 16 documentation under `node_modules/next/dist/docs/` before editing web code.

## Review Focus

- A partially failing chain or provider must preserve completed chain results and mark only affected entities `UNKNOWN`; pin in Tasks 3 and 6.
- An entity rediscovered with changed metadata must update topology without losing incident/history references; pin in Task 2.
- Duplicate or delayed snapshots must not regress latest state or create duplicate incidents; pin in Tasks 2 and 6.
- Insufficient independent oracle peers must produce `UNKNOWN`, not a fabricated consensus; pin in Task 4.
- A hidden 33–60% neighbor affected by an incident must be revealed while unrelated hidden nodes stay hidden; pin in Tasks 5 and 8.

---

## File Ownership and Merge Order

### Lead Agent

- `packages/contracts/**`
- `services/risk/app/api/**`
- `services/risk/app/main.py`
- `services/risk/app/radar/contracts.py`
- `apps/web/**`
- integration fixtures, E2E tests, docs, and final review

### Agent A

- `services/risk/app/radar/repositories/**`
- `services/risk/app/radar/discovery/**`
- `services/risk/app/radar/collectors/**`
- `services/risk/app/radar/scheduler.py`
- `services/risk/app/adapters/rpc_registry.py`
- corresponding Python tests

### Agent B

- `services/risk/app/radar/domain/**`
- `services/risk/app/radar/analysis/**`
- corresponding Python tests

Merge order is Task 1, then Tasks 2–5 in parallel by ownership, then Task 6, Task 7, Task 8, and Task 9. No agent edits another owner's paths without coordination.

### Task 1: Freeze Radar Contracts and Deterministic Fixtures — Lead

**Files:**
- Create: `packages/contracts/schemas/radar.schema.json`
- Modify: `packages/contracts/scripts/generate-types.mjs`
- Modify: `packages/contracts/src/generated.ts`
- Modify: `packages/contracts/src/generated.test.ts`
- Create: `services/risk/app/radar/__init__.py`
- Create: `services/risk/app/radar/contracts.py`
- Create: `services/risk/tests/radar/test_contracts.py`
- Create: `services/risk/tests/fixtures/radar/monitoring-cycle.json`
- Create: `services/risk/tests/fixtures/radar/oracle-incident.json`

**Interfaces:**
- Produces enums `HealthStatus`, `RadarEntityType`, `IncidentSeverity`, `IncidentStatus`, and `RadarRelationshipType`.
- Produces immutable models `RadarEntity`, `RadarRelationship`, `StateSnapshot`, `HealthState`, `SimilarityRecord`, `Incident`, `IncidentImpact`, `Notification`, `ManualExposure`, and `SchedulerRun`.
- Produces `RadarGraphResponse`, `IncidentGraphResponse`, `EntityHistoryResponse`, `SimilarityResponse`, `NotificationResponse`, `ExposureCreateRequest`, and `ExposureImpactResponse`.
- Preserves the stable ID format `<node_type>:<chain_id>:<address_or_key>`.

- [ ] **Step 1: Write failing schema and Python parity tests**

Assert exact enum parity, UTC timestamps, decimal-string fields, forbidden extras, unique graph entity IDs, and the 33/60 similarity thresholds represented as decimal strings.

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run --project services/risk pytest services/risk/tests/radar/test_contracts.py -v && pnpm --filter @risk0/contracts test`

Expected: FAIL because radar contracts do not exist.

- [ ] **Step 3: Implement schemas, Pydantic contracts, generated TypeScript, and two fixture documents**

Keep request/response DTOs separate from internal analytics types. The healthy fixture contains one catalog vault and one visible neighbor; the incident fixture contains one oracle, one market, one catalog vault, one hidden 48% neighbor, one manual exposure, and one critical propagated impact.

- [ ] **Step 4: Run contract tests**

Run: `uv run --project services/risk pytest services/risk/tests/radar/test_contracts.py -v && pnpm --filter @risk0/contracts test`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/contracts services/risk/app/radar services/risk/tests/radar services/risk/tests/fixtures/radar
git commit -m "feat: define radar contracts and fixtures"
```

### Task 2: SQLite Radar Repositories and Retention — Agent A

**Files:**
- Create: `services/risk/app/radar/repositories/__init__.py`
- Create: `services/risk/app/radar/repositories/database.py`
- Create: `services/risk/app/radar/repositories/topology.py`
- Create: `services/risk/app/radar/repositories/states.py`
- Create: `services/risk/app/radar/repositories/incidents.py`
- Create: `services/risk/app/radar/repositories/exposures.py`
- Create: `services/risk/app/radar/repositories/migrations/001_radar.sql`
- Create: `services/risk/tests/radar/repositories/test_database.py`
- Create: `services/risk/tests/radar/repositories/test_topology.py`
- Create: `services/risk/tests/radar/repositories/test_states.py`
- Create: `services/risk/tests/radar/repositories/test_incidents.py`

**Interfaces:**
- Consumes Task 1 contract models.
- Produces `RadarDatabase(path: str | Path)` with idempotent `migrate() -> None` and transaction context management.
- Produces `TopologyRepository.upsert_entities(...)`, `upsert_relationships(...)`, `replace_similarities(...)`, and filtered graph reads.
- Produces `StateRepository.append_snapshot(...)`, `set_health(...)`, `latest_health(...)`, `history(...)`, `aggregate_hour(...)`, and `prune(now)`.
- Produces `IncidentRepository.upsert_active(...)`, `resolve(...)`, `replace_impacts(...)`, and notification read/list methods.
- Produces `ExposureRepository.create(...)`, `get(...)`, and `replace_impacts(...)`.

- [ ] **Step 1: Write failing migration and repository tests**

Pin empty-database migration, cache-only-database migration, idempotence, upsert without history loss, delayed snapshot rejection, duplicate incident suppression, read-state persistence, 30-day pruning, and one-year hourly retention.

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run --project services/risk pytest services/risk/tests/radar/repositories -v`

Expected: FAIL because repository modules do not exist.

- [ ] **Step 3: Implement the migration and focused repositories**

Use foreign keys, stable text IDs, ISO-8601 UTC values, decimal strings, JSON metric payloads, and uniqueness constraints for source-event identity. Keep `cache_entries` unchanged.

- [ ] **Step 4: Run repository tests**

Run: `uv run --project services/risk pytest services/risk/tests/radar/repositories -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/risk/app/radar/repositories services/risk/tests/radar/repositories
git commit -m "feat: persist radar topology and history"
```

### Task 3: Multi-Chain Discovery, RPC Registry, and Collectors — Agent A

**Files:**
- Create: `services/risk/app/adapters/rpc_registry.py`
- Create: `services/risk/app/radar/discovery/__init__.py`
- Create: `services/risk/app/radar/discovery/morpho.py`
- Create: `services/risk/app/radar/collectors/__init__.py`
- Create: `services/risk/app/radar/collectors/chain.py`
- Create: `services/risk/app/radar/collectors/morpho.py`
- Modify: `services/risk/app/adapters/morpho.py`
- Modify: `services/risk/app/config.py`
- Create: `services/risk/tests/radar/discovery/test_morpho.py`
- Create: `services/risk/tests/radar/collectors/test_chain.py`
- Create: `services/risk/tests/radar/collectors/test_morpho.py`
- Create: `services/risk/tests/adapters/test_rpc_registry.py`

**Interfaces:**
- Consumes Task 1 models and Task 2 repositories.
- Produces `RpcRegistry.from_environment()`, `providers(chain_id)`, and `call(chain_id, method, params)` with ordered fallback and redacted failures.
- Produces `MorphoDiscovery.discover() -> DiscoveryBatch` containing chains, vaults, markets, assets, oracles, IRMs, relationships, and source metadata.
- Produces `ChainStateCollector.collect(chain_id, now) -> StateSnapshot`.
- Produces `MorphoStateCollector.collect(batch, now) -> tuple[StateSnapshot, ...]` with per-chain and per-entity isolation.
- Extends `MorphoClient` with paginated discovery and position methods while preserving current public methods.

- [ ] **Step 1: Write failing discovery and registry tests**

Use sanitized transports to test pagination, all discovered chain IDs, cache keys including chain ID, ordered RPC fallback, total RPC failure as `UNKNOWN`, and credential redaction.

- [ ] **Step 2: Write failing collector isolation tests**

Assert one failed chain does not discard snapshots from another chain and one malformed entity does not abort the batch.

- [ ] **Step 3: Run tests and verify failure**

Run: `uv run --project services/risk pytest services/risk/tests/radar/discovery services/risk/tests/radar/collectors services/risk/tests/adapters/test_rpc_registry.py -v`

Expected: FAIL because discovery, collectors, and registry do not exist.

- [ ] **Step 4: Implement paginated discovery, chain-aware RPC fallback, and collectors**

Reuse existing timeout, retry, cache, normalization, envelope, and source-sanitization patterns. Configuration accepts `RISK0_RPC_<CHAIN_ID>_URLS` as comma-separated ordered URLs; public defaults remain code configuration without credentials.

- [ ] **Step 5: Run focused and existing adapter tests**

Run: `uv run --project services/risk pytest services/risk/tests/radar/discovery services/risk/tests/radar/collectors services/risk/tests/adapters -v`

Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add services/risk/app/config.py services/risk/app/adapters services/risk/app/radar/discovery services/risk/app/radar/collectors services/risk/tests
git commit -m "feat: discover and collect multi-chain Morpho state"
```

### Task 4: Health, Oracle Consensus, and Similarity — Agent B

**Files:**
- Create: `services/risk/app/radar/domain/__init__.py`
- Create: `services/risk/app/radar/domain/observations.py`
- Create: `services/risk/app/radar/domain/rules.py`
- Create: `services/risk/app/radar/analysis/__init__.py`
- Create: `services/risk/app/radar/analysis/health.py`
- Create: `services/risk/app/radar/analysis/oracle_consensus.py`
- Create: `services/risk/app/radar/analysis/similarity.py`
- Create: `services/risk/tests/radar/analysis/test_health.py`
- Create: `services/risk/tests/radar/analysis/test_oracle_consensus.py`
- Create: `services/risk/tests/radar/analysis/test_similarity.py`

**Interfaces:**
- Consumes Task 1 models; has no database or network dependency.
- Produces `HealthEvaluator.evaluate(entity, snapshot, dependencies, history) -> HealthState`.
- Produces `OracleConsensus.evaluate(group, now) -> ConsensusResult`.
- Produces `VaultSimilarity.score(left, right) -> SimilarityRecord` and `select_neighbors(catalog_id, scores) -> NeighborSelection`.
- Encodes similarity weights `35/20/15/10/10/5/5`, backend threshold `0.33`, visible threshold `0.60`, and visible limit `10`.

- [ ] **Step 1: Write failing health and hysteresis tests**

Pin immediate critical, warning after 2 abnormal cycles, recovery after 3 normal cycles, missing critical metric as `UNKNOWN`, market use of lender and borrower metrics, and vault material unknown allocation behavior.

- [ ] **Step 2: Write failing oracle consensus tests**

Pin robust median, stale-feed exclusion, deviation severity, derived-reference labeling, and fewer than two independent fresh peers as `UNKNOWN`.

- [ ] **Step 3: Write failing similarity tests**

Pin every weighted component, exact total, missing dimension explanation, storage above 33%, top 10 above 60%, deterministic tie ordering, and exclusion at exactly/below policy boundaries as specified by the contract.

- [ ] **Step 4: Run tests and verify failure**

Run: `uv run --project services/risk pytest services/risk/tests/radar/analysis/test_health.py services/risk/tests/radar/analysis/test_oracle_consensus.py services/risk/tests/radar/analysis/test_similarity.py -v`

Expected: FAIL because analytics modules do not exist.

- [ ] **Step 5: Implement pure domain analytics**

Keep thresholds in a versioned `RuleSet` and return explicit reasons and missing fields. Do not import FastAPI, SQLite, HTTP, or environment configuration.

- [ ] **Step 6: Run analysis tests**

Run: `uv run --project services/risk pytest services/risk/tests/radar/analysis -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add services/risk/app/radar/domain services/risk/app/radar/analysis services/risk/tests/radar/analysis
git commit -m "feat: evaluate radar health and similarity"
```

### Task 5: Incident Propagation and Exposure Impact — Agent B

**Files:**
- Create: `services/risk/app/radar/analysis/incidents.py`
- Create: `services/risk/app/radar/analysis/propagation.py`
- Create: `services/risk/app/radar/analysis/exposure.py`
- Create: `services/risk/tests/radar/analysis/test_incidents.py`
- Create: `services/risk/tests/radar/analysis/test_propagation.py`
- Create: `services/risk/tests/radar/analysis/test_exposure.py`

**Interfaces:**
- Consumes Task 1 models and Task 4 health results.
- Produces `IncidentDetector.reconcile(previous, health_states, now) -> IncidentChanges`.
- Produces `PropagationEngine.propagate(incident, entities, relationships, similarities) -> tuple[IncidentImpact, ...]`.
- Produces `ExposureImpactEstimator.estimate(exposure, incident, impacts) -> ExposureImpactResponse`.
- Produces shortest high-confidence path, alternate path count, distance, confidence, affected amount, and `revealed` state.

- [ ] **Step 1: Write failing lifecycle tests**

Pin open, stable update without duplicate, severity escalation, automatic resolution, and delayed duplicate observation behavior.

- [ ] **Step 2: Write failing propagation tests**

Pin oracle → market → vault → exposure, token and chain propagation, path cycle protection, unrelated-node exclusion, shortest-path selection, and hidden 48% neighbor reveal while an unrelated hidden neighbor stays hidden.

- [ ] **Step 3: Write failing lender and borrower impact tests**

Pin allocation-weighted lender amount, borrower collateral/debt/LTV context, unavailable impact when required exposure data is missing, and no invented monetary impact for chain-only incidents.

- [ ] **Step 4: Run tests and verify failure**

Run: `uv run --project services/risk pytest services/risk/tests/radar/analysis/test_incidents.py services/risk/tests/radar/analysis/test_propagation.py services/risk/tests/radar/analysis/test_exposure.py -v`

Expected: FAIL because incident modules do not exist.

- [ ] **Step 5: Implement lifecycle, typed traversal, and exposure impact**

Use deterministic adjacency maps and breadth-first traversal from the Python standard library. Expose only contract models and sorted results so the traversal implementation can change without affecting callers.

- [ ] **Step 6: Run analysis tests**

Run: `uv run --project services/risk pytest services/risk/tests/radar/analysis -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add services/risk/app/radar/analysis services/risk/tests/radar/analysis
git commit -m "feat: propagate radar incidents to exposures"
```

### Task 6: Radar Orchestration and Non-Overlapping Scheduler — Agent A, then Lead Integration

**Files:**
- Create: `services/risk/app/radar/service.py`
- Create: `services/risk/app/radar/scheduler.py`
- Modify: `services/risk/app/main.py`
- Create: `services/risk/tests/radar/test_service.py`
- Create: `services/risk/tests/radar/test_scheduler.py`

**Interfaces:**
- Consumes repositories from Task 2, collection from Task 3, and analytics from Tasks 4–5.
- Produces `RadarService.run_discovery(now) -> SchedulerRun` and `run_monitoring(now) -> SchedulerRun`.
- Produces `RadarScheduler.start()`, `stop()`, `run_fast_once()`, and `run_slow_once()`.
- Fast interval is 300 seconds; slow interval is 3600 seconds; the lock is per loop and skipped cycles are persisted.

- [x] **Step 1: Write failing vertical-cycle service test**

Use the monitoring fixture to assert snapshot persistence, health update, one incident, one propagated exposure impact, one notification, and deterministic rerun without duplication.

- [x] **Step 2: Write failing scheduler tests**

Pin interval registration, start/stop lifecycle, non-overlap skip, persisted skipped run, partial-chain completion, and one collector exception not terminating future cycles.

- [x] **Step 3: Run tests and verify failure**

Run: `uv run --project services/risk pytest services/risk/tests/radar/test_service.py services/risk/tests/radar/test_scheduler.py -v`

Expected: FAIL because service and scheduler do not exist.

- [x] **Step 4: Implement orchestration and lifecycle wiring**

Keep scheduling behind FastAPI lifespan and disabled in fixture tests unless explicitly started. The lead resolves imports and lifecycle changes in `main.py` after Agent A's branch is reviewed.

- [x] **Step 5: Run all Python tests**

Run: `uv run --project services/risk pytest services/risk/tests -m "not live" -v`

Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add services/risk/app/radar services/risk/app/main.py services/risk/tests/radar
git commit -m "feat: orchestrate continuous radar cycles"
```

### Task 7: Radar Read APIs and SSE — Lead

**Files:**
- Create: `services/risk/app/api/radar.py`
- Create: `services/risk/app/api/events.py`
- Modify: `services/risk/app/main.py`
- Create: `services/risk/tests/api/test_radar.py`
- Create: `services/risk/tests/api/test_events.py`
- Create: `apps/web/src/app/api/graph/route.ts`
- Create: `apps/web/src/app/api/notifications/route.ts`
- Create: `apps/web/src/app/api/exposures/route.ts`
- Create: `apps/web/src/app/api/events/route.ts`
- Modify: `apps/web/src/app/api/routes.test.ts`
- Modify: `apps/web/src/lib/api.ts`
- Modify: `apps/web/src/lib/api.test.ts`

**Interfaces:**
- Implements the endpoints listed in Spec §11.
- SSE publishes `graph.updated`, `incident.opened`, `incident.updated`, `incident.resolved`, and `notification.created` with resource IDs and sequence numbers.
- Graph filters are chain, entity type, health status, visibility, and incident ID.

- [x] **Step 1: Write failing FastAPI read/write endpoint tests**

Pin filters, pagination, invalid IDs, exposure validation, notification read idempotence, source redaction, and empty-database responses.

- [x] **Step 2: Write failing SSE tests**

Pin event shape, monotonic sequence, heartbeat, reconnect with `Last-Event-ID`, and no full graph payload.

- [x] **Step 3: Implement API routers and register them**

Map repository/domain failures to public-safe 4xx/5xx responses without provider bodies.

- [x] **Step 4: Write and implement Next.js proxy/API-client tests**

Follow existing catalog/analysis proxy behavior: server-side URL only, bounded timeout, generic upstream errors, and typed response validation.

- [x] **Step 5: Run API, contract, and web client tests**

Run: `uv run --project services/risk pytest services/risk/tests/api -v && pnpm --filter @risk0/contracts test && pnpm --filter @risk0/web test -- routes.test.ts api.test.ts`

Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add services/risk/app/api services/risk/app/main.py services/risk/tests/api apps/web/src/app/api apps/web/src/lib
git commit -m "feat: expose radar API and event stream"
```

### Task 8: Institutional Terminal UI — Lead

**Files:**
- Modify: `apps/web/src/app/page.tsx`
- Modify: `apps/web/src/app/globals.css`
- Create: `apps/web/src/components/terminal-shell.tsx`
- Create: `apps/web/src/components/system-ticker.tsx`
- Create: `apps/web/src/components/exposure-rail.tsx`
- Create: `apps/web/src/components/radar-graph.tsx`
- Create: `apps/web/src/components/incident-rail.tsx`
- Create: `apps/web/src/components/notification-center.tsx`
- Create: `apps/web/src/components/notification-toast.tsx`
- Create: `apps/web/src/hooks/use-radar-events.ts`
- Create: focused `.test.tsx` files beside each component

**Interfaces:**
- Consumes Task 7 API client and generated contracts.
- Reuses current catalog selector, position presets, graph selection, recommendation, scenario results, and data-quality behavior through composition or focused extraction.
- Produces the approved Institutional Terminal layout and accessible keyboard navigation.

- [ ] **Step 1: Write failing shell, filter, and graph tests**

Pin navigation labels, ticker metrics, default catalog/visible-neighbor display, hidden healthy secondary nodes, status legend, selected incident path, and unknown-not-healthy styling.

- [ ] **Step 2: Write failing notification tests**

Use fake timers to pin toast appearance and disappearance, persistent notification history, unread count, mark-read behavior, and incident-click graph focus.

- [ ] **Step 3: Implement the terminal shell and responsive component composition**

Use CSS variables and existing React Flow; do not add a UI framework or state library. Preserve current position analysis under the selected exposure panel.

- [ ] **Step 4: Implement SSE reconnect and resource refetch**

The hook reconnects with bounded exponential backoff, deduplicates sequence IDs, and refetches only affected resources.

- [ ] **Step 5: Run all web tests**

Run: `pnpm --filter @risk0/web test`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/web/src
git commit -m "feat: add institutional risk terminal"
```

### Task 9: End-to-End Integration, Regression, and Handoff — Lead

**Files:**
- Create: `apps/web/e2e/radar-healthy.spec.ts`
- Create: `apps/web/e2e/radar-oracle-incident.spec.ts`
- Modify: `services/risk/app/fixture_mode.py`
- Modify: `apps/web/playwright.config.ts`
- Modify: `README.md`
- Modify: `docs/demo-script.md`

**Interfaces:**
- Consumes all previous tasks.
- Produces deterministic fixture-mode radar behavior and documented local operation.

- [ ] **Step 1: Add the healthy-cycle E2E flow**

Assert catalog topology, one visible related vault, fresh state, no active critical incident, and manual exposure creation.

- [ ] **Step 2: Add the oracle-incident E2E flow**

Assert critical toast, persistent notification, revealed hidden 48% neighbor, highlighted propagation path, affected amount, and incident history after resolution.

- [ ] **Step 3: Run focused E2E tests**

Run: `pnpm --filter @risk0/web exec playwright test e2e/radar-healthy.spec.ts e2e/radar-oracle-incident.spec.ts`

Expected: PASS.

- [ ] **Step 4: Run complete regression**

Run: `uv run --project services/risk pytest services/risk/tests -m "not live" -v`

Run: `pnpm test`

Run: `pnpm --filter @risk0/web exec playwright test`

Expected: all existing and radar tests PASS.

- [ ] **Step 5: Run optional live smoke when configured**

Run: `RISK0_DATA_MODE=live uv run --project services/risk pytest services/risk/tests/live/test_live_smoke.py -v`

Expected: PASS or documented provider-specific SKIP; no secret output.

- [ ] **Step 6: Update operator and demo documentation**

Document new environment variables, migrations, fast/slow loops, local retention, Institutional Terminal flow, limitations, and source provenance.

- [ ] **Step 7: Commit**

```bash
git add apps/web/e2e apps/web/playwright.config.ts services/risk/app/fixture_mode.py README.md docs/demo-script.md
git commit -m "test: verify radar monitoring and propagation"
```

## Collaboration Workflow

1. The lead creates and pushes `risk0-radar` from the approved main commit.
2. Task 1 lands on `risk0-radar` before either partner begins implementation.
3. Agent A branches `agent-a/radar-data-scheduler` from `origin/risk0-radar`.
4. Agent B branches `agent-b/radar-health-propagation` from `origin/risk0-radar`.
5. Each agent follows TDD, commits once per plan task, pushes their branch, and opens a PR targeting `risk0-radar`.
6. Agents must not merge their own PRs. They report test commands, results, changed interfaces, limitations, and the PR URL.
7. The lead reviews Task 2/3 and Task 4/5 independently, requests fixes on the same branch, then merges in dependency order.
8. Before Task 6 integration, both partner branches merge the latest `origin/risk0-radar` and resolve only files they own.
9. The lead runs the complete regression suite before merging `risk0-radar` to `main`.
