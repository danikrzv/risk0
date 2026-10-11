# Graph-First Risk Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a full-screen, graph-first Morpho risk terminal with real header categories, health-marked entity lists, contextual dependency focus, floating controls, and a position-analysis-to-graph handoff.

**Architecture:** Keep one `Home` client workspace and one loaded radar model across category changes. Put reusable derivation logic in pure TypeScript modules, keep category lists and floating panels in focused components, and extend `RadarGraph` through explicit controlled props rather than duplicating graph state across views.

**Tech Stack:** TypeScript, React 19, Next.js 16 App Router, React Flow (`@xyflow/react`), Vitest/Testing Library, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-11-graph-first-workspace-design.md`

## Global Constraints

- Preserve the existing risk-service and contracts APIs; category lists are derived client-side.
- Preserve the existing `MAX_CANVAS_NODES = 400` browser safety cap and viewport culling.
- Health is always one of `HEALTHY`, `WARNING`, `CRITICAL`, or `UNKNOWN`; missing status must render as `UNKNOWN`, never healthy.
- Edge labels are hidden by default and appear only for focused context, selected incident paths, or explicit opt-in.
- Category changes must preserve loaded graph, selected incident, exposure form state, and completed analysis state.
- The existing regular and 20,000-entity scale browser suites remain required gates.
- Do not commit the local `.claude/` directory or `apps/web/AGENTS.md` unless the user explicitly requests it.
- Reconcile the current uncommitted `page.tsx`, `page.test.tsx`, and `globals.css` full-screen/drawer work into Task 1; do not discard it.

## Review Focus

- Entities with absent or malformed health metadata render as `UNKNOWN`; Task 2 adds the test.
- A search match beyond the first 400 ordered nodes is promoted onto the canvas; Task 4 adds the test.
- Cyclic, duplicate, self-loop, and unknown-endpoint relationships cannot make dependency focus unbounded; Task 3 adds the test.
- Category changes during an in-flight incident or analysis request preserve the latest valid selection and result; Task 7 adds the test.
- On a narrow viewport, opening one large floating panel closes the other without unmounting graph state; Task 6 adds the test.

---

### Task 1: Persistent Workspace Categories and Full-Screen Baseline

**Files:**
- Modify: `apps/web/src/components/terminal-shell.tsx`
- Modify: `apps/web/src/components/terminal-shell.test.tsx`
- Modify: `apps/web/src/app/page.tsx`
- Modify: `apps/web/src/app/page.test.tsx`
- Modify: `apps/web/src/app/globals.css`

**Interfaces:**
- Produces: `export type WorkspaceCategory = "graph" | "markets" | "oracles" | "incidents" | "position-analysis"` from `terminal-shell.tsx`.
- Produces: `TerminalShell({ activeCategory, onCategoryChange, ... })` with real category buttons.
- Consumes: current uncommitted full-screen command-surface and incident-drawer styling.

- [ ] **Step 1: Write failing shell tests**

Add tests asserting all five category buttons exist, the active category has `aria-current="page"`, and clicking `Markets` calls `onCategoryChange("markets")`.

- [ ] **Step 2: Run the shell test and verify RED**

Run: `pnpm --filter @risk0/web test -- src/components/terminal-shell.test.tsx`

Expected: FAIL because navigation is rendered as inert spans and the new props do not exist.

- [ ] **Step 3: Implement controlled category navigation**

Add `WorkspaceCategory`, category metadata, button semantics, and controlled active/click behavior in `terminal-shell.tsx`.

- [ ] **Step 4: Write failing page tests for category persistence**

Assert the masthead copy is absent, Graph is the initial category, switching to Position Analysis hides the graph workspace, and returning to Graph preserves the previously selected graph entity.

- [ ] **Step 5: Run the page test and verify RED**

Run: `pnpm --filter @risk0/web test -- src/app/page.test.tsx`

Expected: FAIL because `Home` does not yet own or render category state.

- [ ] **Step 6: Implement the full-screen category shell**

Add `activeCategory` state to `Home`, pass it through `TerminalShell`, render the graph command surface only for `graph`, and preserve component/domain state across category changes. Reconcile the existing uncommitted full-screen CSS into the new category containers.

- [ ] **Step 7: Verify Task 1 GREEN**

Run: `pnpm --filter @risk0/web test -- src/components/terminal-shell.test.tsx src/app/page.test.tsx && pnpm --filter @risk0/web exec tsc --noEmit`

Expected: all selected tests pass and type-check exits 0.

- [ ] **Step 8: Commit**

```bash
git add apps/web/src/components/terminal-shell.tsx apps/web/src/components/terminal-shell.test.tsx apps/web/src/app/page.tsx apps/web/src/app/page.test.tsx apps/web/src/app/globals.css
git commit -m "feat(web): add persistent risk workspace categories"
```

### Task 2: Health-Marked Category Lists

**Files:**
- Create: `apps/web/src/lib/radar-health.ts`
- Create: `apps/web/src/lib/radar-health.test.ts`
- Create: `apps/web/src/components/radar-entity-list.tsx`
- Create: `apps/web/src/components/radar-entity-list.test.tsx`
- Modify: `apps/web/src/app/page.tsx`
- Modify: `apps/web/src/app/page.test.tsx`
- Modify: `apps/web/src/app/globals.css`

**Interfaces:**
- Produces: `entityHealth(entity: RadarEntity): HealthStatus`.
- Produces: `incidentCountBySource(incidents: Incident[]): ReadonlyMap<string, number>`.
- Produces: `RadarEntityList({ kind, entities, incidents, onSelectEntity })`, where `kind` is `"market" | "oracle" | "position"`.
- Consumes: `WorkspaceCategory` from Task 1.

- [ ] **Step 1: Write failing health-derivation tests**

Test valid statuses, missing metadata, non-string metadata, and unsupported strings; all invalid/missing cases must return `UNKNOWN`.

- [ ] **Step 2: Verify health tests RED**

Run: `pnpm --filter @risk0/web test -- src/lib/radar-health.test.ts`

Expected: FAIL because `radar-health.ts` does not exist.

- [ ] **Step 3: Implement health derivation**

Create the two pure exported helpers with no React dependencies.

- [ ] **Step 4: Write failing entity-list tests**

Assert health text and non-color marker, entity-specific secondary metrics, incident count, search filtering, explicit empty state, and selection callback.

- [ ] **Step 5: Verify list tests RED**

Run: `pnpm --filter @risk0/web test -- src/components/radar-entity-list.test.tsx`

Expected: FAIL because `RadarEntityList` does not exist.

- [ ] **Step 6: Implement category lists and page wiring**

Render Markets, Oracles, and Position Analysis entry lists from existing graph/catalog data. A Market or Oracle row selection switches to Graph and sets the selected radar entity.

- [ ] **Step 7: Verify Task 2 GREEN**

Run: `pnpm --filter @risk0/web test -- src/lib/radar-health.test.ts src/components/radar-entity-list.test.tsx src/app/page.test.tsx`

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add apps/web/src/lib/radar-health.ts apps/web/src/lib/radar-health.test.ts apps/web/src/components/radar-entity-list.tsx apps/web/src/components/radar-entity-list.test.tsx apps/web/src/app/page.tsx apps/web/src/app/page.test.tsx apps/web/src/app/globals.css
git commit -m "feat(web): add health-marked radar category lists"
```

### Task 3: Relevant Dependency Context Algorithm

**Files:**
- Create: `apps/web/src/lib/radar-focus.ts`
- Create: `apps/web/src/lib/radar-focus.test.ts`

**Interfaces:**
- Produces: `relevantDependencyContext(graph: Pick<RadarGraphResponse, "entities" | "relationships" | "similarities">, selectedEntityId: string, impacts: readonly IncidentImpact[], exposureTargetIds: ReadonlySet<string>): { entityIds: ReadonlySet<string>; relationshipKeys: ReadonlySet<string> }`.
- Produces: `radarRelationshipKey(relationship: RadarRelationship, index: number): string` for shared renderer identity.
- Consumes: relationship direction `dependent -> dependency` and the spec's explicit similarity rule.

- [ ] **Step 1: Write failing causal-context tests**

Cover vault → market → assets/oracle/IRM → protocol/chain traversal, active incident paths, exposure targets, exclusion of unrelated vaults, and inclusion of a similar vault only when it shares an involved dependency.

- [ ] **Step 2: Add the boundedness regression test**

Use cycles, duplicate relationships, self-loops, and relationships with unknown endpoints; assert each known entity is visited at most once and unknown IDs are excluded.

- [ ] **Step 3: Verify focus tests RED**

Run: `pnpm --filter @risk0/web test -- src/lib/radar-focus.test.ts`

Expected: FAIL because `radar-focus.ts` does not exist.

- [ ] **Step 4: Implement iterative context traversal**

Use adjacency maps and visited sets. Follow dependency direction from the selected entity, merge incident paths and exposure targets, then add only similarity peers satisfying the shared-involved-dependency rule.

- [ ] **Step 5: Verify Task 3 GREEN**

Run: `pnpm --filter @risk0/web test -- src/lib/radar-focus.test.ts`

Expected: all focus tests pass.

- [ ] **Step 6: Commit**

```bash
git add apps/web/src/lib/radar-focus.ts apps/web/src/lib/radar-focus.test.ts
git commit -m "feat(web): derive relevant graph dependency context"
```

### Task 4: Global Graph Search and Canvas Promotion

**Files:**
- Create: `apps/web/src/lib/radar-search.ts`
- Create: `apps/web/src/lib/radar-search.test.ts`
- Create: `apps/web/src/components/graph-search.tsx`
- Create: `apps/web/src/components/graph-search.test.tsx`
- Modify: `apps/web/src/components/radar-graph.tsx`
- Modify: `apps/web/src/components/radar-graph.test.tsx`

**Interfaces:**
- Produces: `rankRadarSearch(entities: readonly RadarEntity[], query: string, limit?: number): RadarEntity[]`.
- Produces: `promoteSelectedEntity(entities: readonly RadarEntity[], selectedEntityId: string | null, limit: number): RadarEntity[]`.
- Produces: controlled `GraphSearch({ entities, value, onChange, onSelect })`.
- `RadarGraph` consumes controlled `searchQuery`, `onSearchQueryChange`, and selected entity promotion.

- [ ] **Step 1: Write failing ranking tests**

Assert exact label/ID, prefix, and substring ranking across label, ID, address/key, and entity type; blank query returns no suggestions.

- [ ] **Step 2: Write the beyond-cap promotion test**

Create 401 ordered entities, select entity 401, and assert `promoteSelectedEntity(..., 400)` includes it while returning exactly 400 unique entities.

- [ ] **Step 3: Verify search tests RED**

Run: `pnpm --filter @risk0/web test -- src/lib/radar-search.test.ts`

Expected: FAIL because search helpers do not exist.

- [ ] **Step 4: Implement pure ranking and promotion helpers**

Use stable original order as the final tie-breaker and case-insensitive normalized text.

- [ ] **Step 5: Write failing GraphSearch interaction tests**

Assert upper-right search semantics, keyboard result selection, no-results copy, and `onSelect` delivery.

- [ ] **Step 6: Implement GraphSearch and RadarGraph integration**

Move the graph search input into `GraphSearch`, control its value from `Home`, and promote the selected match before applying `MAX_CANVAS_NODES`.

- [ ] **Step 7: Verify Task 4 GREEN**

Run: `pnpm --filter @risk0/web test -- src/lib/radar-search.test.ts src/components/graph-search.test.tsx src/components/radar-graph.test.tsx`

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add apps/web/src/lib/radar-search.ts apps/web/src/lib/radar-search.test.ts apps/web/src/components/graph-search.tsx apps/web/src/components/graph-search.test.tsx apps/web/src/components/radar-graph.tsx apps/web/src/components/radar-graph.test.tsx
git commit -m "feat(web): add global graph entity search"
```

### Task 5: Sparse Focus Rendering and Conditional Edge Labels

**Files:**
- Modify: `apps/web/src/lib/layered-layout.ts`
- Modify: `apps/web/src/lib/layered-layout.test.ts`
- Modify: `apps/web/src/components/radar-graph.tsx`
- Modify: `apps/web/src/components/radar-graph.test.tsx`
- Modify: `apps/web/src/app/globals.css`

**Interfaces:**
- Consumes: `relevantDependencyContext` and `radarRelationshipKey` from Task 3.
- `RadarGraph` produces controls `Show only relevant`, `Clear focus`, and `Show relationship labels`.
- `RadarGraph` accepts `exposureTargetIds?: ReadonlySet<string>` and retains existing incident props.

- [ ] **Step 1: Write failing sparse-layout tests**

Assert the default horizontal and vertical gaps exceed the prior values while deterministic layer ordering and cycle handling remain unchanged.

- [ ] **Step 2: Write failing focus-rendering tests**

Assert selection assigns faded classes to unrelated nodes/edges, relevant nodes remain opaque, relevant-only removes unrelated elements, and Clear focus restores them.

- [ ] **Step 3: Write failing edge-label tests**

Assert labels are absent by default, present only on focused/incident edges in context mode, and present on all visible edges after explicit opt-in.

- [ ] **Step 4: Verify graph tests RED**

Run: `pnpm --filter @risk0/web test -- src/lib/layered-layout.test.ts src/components/radar-graph.test.tsx`

Expected: FAIL on spacing, focus classes/controls, and edge-label behavior.

- [ ] **Step 5: Implement sparse contextual rendering**

Increase deterministic layout spacing, derive context through Task 3, apply opacity/removal before flow-node and flow-edge construction, and compute each edge label from the three allowed modes.

- [ ] **Step 6: Verify Task 5 GREEN**

Run: `pnpm --filter @risk0/web test -- src/lib/layered-layout.test.ts src/components/radar-graph.test.tsx`

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add apps/web/src/lib/layered-layout.ts apps/web/src/lib/layered-layout.test.ts apps/web/src/components/radar-graph.tsx apps/web/src/components/radar-graph.test.tsx apps/web/src/app/globals.css
git commit -m "feat(web): focus and declutter radar dependencies"
```

### Task 6: Persistent Floating Exposure and Incident Controls

**Files:**
- Create: `apps/web/src/components/floating-radar-panels.tsx`
- Create: `apps/web/src/components/floating-radar-panels.test.tsx`
- Modify: `apps/web/src/app/page.tsx`
- Modify: `apps/web/src/app/page.test.tsx`
- Modify: `apps/web/src/app/globals.css`

**Interfaces:**
- Produces: `FloatingRadarPanels({ exposure, incidents, exposureOpen, incidentsOpen, onExposureOpenChange, onIncidentsOpenChange })`.
- Consumes: existing `ExposureRail` and `IncidentRail` as content, not duplicated forms/lists.

- [ ] **Step 1: Write failing panel tests**

Assert exposure starts open, both launchers expose `aria-expanded`/`aria-controls`, hiding preserves form values, and reopening restores the same form instance state.

- [ ] **Step 2: Add narrow-viewport exclusivity test**

Mock `matchMedia("(max-width: 48rem)")` as matching; opening Incidents must close Exposure while the graph selection state remains unchanged in the page integration test.

- [ ] **Step 3: Verify panel tests RED**

Run: `pnpm --filter @risk0/web test -- src/components/floating-radar-panels.test.tsx src/app/page.test.tsx`

Expected: FAIL because the unified persistent panel controller does not exist.

- [ ] **Step 4: Implement floating panel controller and CSS**

Keep panel content mounted and visually hidden where state preservation requires it; use responsive exclusive-open behavior only below 48rem. Ensure overlays do not resize the graph canvas.

- [ ] **Step 5: Verify Task 6 GREEN**

Run: `pnpm --filter @risk0/web test -- src/components/floating-radar-panels.test.tsx src/components/exposure-rail.test.tsx src/components/incident-rail.test.tsx src/app/page.test.tsx`

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add apps/web/src/components/floating-radar-panels.tsx apps/web/src/components/floating-radar-panels.test.tsx apps/web/src/app/page.tsx apps/web/src/app/page.test.tsx apps/web/src/app/globals.css
git commit -m "feat(web): float persistent radar controls"
```

### Task 7: Position Analysis Category and Graph Handoff

**Files:**
- Modify: `apps/web/src/components/recommendation-card.tsx`
- Modify: `apps/web/src/components/recommendation-card.test.tsx`
- Modify: `apps/web/src/app/page.tsx`
- Modify: `apps/web/src/app/page.test.tsx`
- Modify: `apps/web/src/app/globals.css`

**Interfaces:**
- `RecommendationCard` accepts `onViewDependencies?: (entityId: string) => void`.
- `Home` produces `focusRadarEntity(entityId: string): void`, setting Graph category, selected entity, and controlled search/focus state.
- Consumes: Position Analysis list from Task 2 and category state from Task 1.

- [ ] **Step 1: Write failing recommendation handoff test**

Assert `View dependencies` is rendered for the dominant dependency and calls `onViewDependencies` with its entity ID.

- [ ] **Step 2: Verify recommendation test RED**

Run: `pnpm --filter @risk0/web test -- src/components/recommendation-card.test.tsx`

Expected: FAIL because the callback/action does not exist.

- [ ] **Step 3: Implement the recommendation action**

Add the optional callback without changing existing recommendation calculations or safety styling.

- [ ] **Step 4: Write failing page handoff and race tests**

Assert selecting a Position Analysis entry opens its form in-category; completing analysis then clicking View dependencies switches to Graph and focuses matching nodes. Start an analysis, change category, resolve it, and assert the valid result remains available when returning. Repeat the existing late-incident-response test across a category change.

- [ ] **Step 5: Verify page tests RED**

Run: `pnpm --filter @risk0/web test -- src/app/page.test.tsx`

Expected: FAIL because Position Analysis is not yet an isolated category with graph handoff.

- [ ] **Step 6: Implement Position Analysis composition and handoff**

Move the existing catalog/form/scenarios/results block into the Position Analysis category, preserve its state, and implement `focusRadarEntity` with focus transfer to the Graph heading after render.

- [ ] **Step 7: Verify Task 7 GREEN**

Run: `pnpm --filter @risk0/web test -- src/components/recommendation-card.test.tsx src/app/page.test.tsx`

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add apps/web/src/components/recommendation-card.tsx apps/web/src/components/recommendation-card.test.tsx apps/web/src/app/page.tsx apps/web/src/app/page.test.tsx apps/web/src/app/globals.css
git commit -m "feat(web): connect position analysis to radar focus"
```

### Task 8: End-to-End, Accessibility, and Scale Gates

**Files:**
- Modify: `apps/web/e2e/radar-healthy.spec.ts`
- Modify: `apps/web/e2e/radar-oracle-incident.spec.ts`
- Modify: `apps/web/e2e/responsive.spec.ts`
- Create: `apps/web/e2e/workspace-navigation.spec.ts`
- Modify: `apps/web/e2e-scale/large-radar.spec.ts`

**Interfaces:**
- Consumes: all UI and helper interfaces from Tasks 1–7.
- Produces: browser-level regression coverage only.

- [ ] **Step 1: Write failing workspace browser flow**

Cover category navigation, health-marked list selection, graph focus, clear focus, floating Exposure visibility, and Position Analysis → View dependencies handoff.

- [ ] **Step 2: Update responsive test expectations**

Assert no horizontal overflow, floating panels remain keyboard-operable, and narrow-screen panel exclusivity works.

- [ ] **Step 3: Extend scale test before implementation tuning**

Search for an entity beyond the initial 400, assert it appears on the canvas, focus it, and retain the existing payload, first-render, long-task, click, and 400-node limits.

- [ ] **Step 4: Run browser tests and verify RED where expected**

Run: `UV_CACHE_DIR=/private/tmp/risk0-uv-cache pnpm --filter @risk0/web test:e2e`

Run: `UV_CACHE_DIR=/private/tmp/risk0-uv-cache pnpm --filter @risk0/web test:e2e:scale`

Expected: new assertions fail until all integration details are wired; existing unrelated flows stay green.

- [ ] **Step 5: Make only integration-level corrections**

Adjust selectors, focus transfer, responsive CSS, or memoization. Do not introduce new product behavior outside the approved spec.

- [ ] **Step 6: Run the complete verification matrix**

Run: `pnpm --filter @risk0/contracts test`

Run: `pnpm --filter @risk0/web test`

Run: `pnpm --filter @risk0/web exec tsc --noEmit`

Run: `pnpm --filter @risk0/web build`

Run: `UV_CACHE_DIR=/private/tmp/risk0-uv-cache pnpm --filter @risk0/web test:e2e`

Run: `UV_CACHE_DIR=/private/tmp/risk0-uv-cache pnpm --filter @risk0/web test:e2e:scale`

Expected: 0 failures; production build succeeds; scale test retains its response-size, responsiveness, long-task, and canvas-cap budgets.

- [ ] **Step 7: Commit**

```bash
git add apps/web/e2e apps/web/e2e-scale
git commit -m "test(web): verify graph-first workspace flows"
```

- [ ] **Step 8: Request whole-branch review**

Review against `docs/superpowers/specs/2026-10-11-graph-first-workspace-design.md`, resolve all P0/P1 findings, rerun the complete matrix, then follow `superpowers:finishing-a-development-branch` for integration.
