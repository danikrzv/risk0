# Graph-First Risk Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the requested full-screen graph workspace, health-marked categories, contextual dependency focus, floating controls, and position-analysis handoff with the smallest safe change set.

**Architecture:** Keep one `Home` client workspace and the existing components. Add only one pure graph-focus/search helper module; implement the remaining behavior by extending `TerminalShell`, `RadarGraph`, and `Home` rather than creating a larger component hierarchy.

**Tech Stack:** TypeScript, React 19, Next.js 16, React Flow, Vitest/Testing Library, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-11-graph-first-workspace-design.md`

## Global Constraints

- Reuse existing radar/catalog responses; no backend or contract changes.
- Preserve `MAX_CANVAS_NODES = 400` and viewport culling.
- Missing or malformed health renders `UNKNOWN`, never healthy.
- Edge labels are hidden by default and shown only for focused/incident context or explicit opt-in.
- Category changes preserve graph, incident, exposure-form, and analysis state.
- Reconcile the existing uncommitted `page.tsx`, `page.test.tsx`, and `globals.css` work in Task 1; do not discard it.
- Each task ends in a commit so another agent can resume immediately.
- Never add `.claude/` or `apps/web/AGENTS.md` to a commit.

## Review Focus

- Search must surface a matching node beyond the first 400.
- Cycles, duplicates, self-loops, and unknown relationship endpoints must not make focus traversal unbounded.
- Health markers must remain explicit when metadata is missing.
- Late incident/analysis responses must not overwrite newer state after category changes.
- On narrow screens only one large graph overlay is open at once.

---

### Task 1: Full-Screen Workspace, Real Categories, and Health Lists

**Files:**
- Modify: `apps/web/src/components/terminal-shell.tsx`
- Modify: `apps/web/src/components/terminal-shell.test.tsx`
- Modify: `apps/web/src/app/page.tsx`
- Modify: `apps/web/src/app/page.test.tsx`
- Modify: `apps/web/src/app/globals.css`

**Interfaces:**
- Produce `WorkspaceCategory = "graph" | "markets" | "oracles" | "incidents" | "position-analysis"` in `terminal-shell.tsx`.
- Extend `TerminalShell` with `activeCategory` and `onCategoryChange`.
- Keep category rows local to `page.tsx`; do not create a generic list abstraction for this MVP.

- [ ] **Step 1: Write failing navigation and list tests**

Test real category buttons and active state in `terminal-shell.test.tsx`. In `page.test.tsx`, test: no masthead, Graph initial view, Markets/Oracles/Incidents/Position Analysis switching, health text/marker, `UNKNOWN` fallback, empty lists, and selecting a row returns to Graph with that entity selected.

- [ ] **Step 2: Verify RED**

Run: `pnpm --filter @risk0/web test -- src/components/terminal-shell.test.tsx src/app/page.test.tsx`

Expected: FAIL because navigation is inert and categories/lists are not implemented.

- [ ] **Step 3: Implement the minimal shared workspace**

Convert header spans to buttons, add category state in `Home`, derive compact filtered rows directly from existing graph/catalog data, and reconcile the current full-screen CSS. Every row displays status text plus a non-color marker. Position Analysis owns the existing catalog/form/results section.

- [ ] **Step 4: Verify GREEN**

Run: `pnpm --filter @risk0/web test -- src/components/terminal-shell.test.tsx src/app/page.test.tsx && pnpm --filter @risk0/web exec tsc --noEmit`

Expected: selected tests pass and type-check exits 0.

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/components/terminal-shell.tsx apps/web/src/components/terminal-shell.test.tsx apps/web/src/app/page.tsx apps/web/src/app/page.test.tsx apps/web/src/app/globals.css
git commit -m "feat(web): add graph-first workspace categories"
```

### Task 2: Search, Relevant Focus, Sparse Layout, and Edge Readability

**Files:**
- Create: `apps/web/src/lib/radar-focus.ts`
- Create: `apps/web/src/lib/radar-focus.test.ts`
- Modify: `apps/web/src/lib/layered-layout.ts`
- Modify: `apps/web/src/lib/layered-layout.test.ts`
- Modify: `apps/web/src/components/radar-graph.tsx`
- Modify: `apps/web/src/components/radar-graph.test.tsx`
- Modify: `apps/web/src/app/globals.css`

**Interfaces:**
- Produce `rankRadarEntities(entities, query): RadarEntity[]`.
- Produce `relevantDependencyContext(graph, selectedEntityId, impacts): ReadonlySet<string>`.
- `RadarGraph` owns its compact upper-right search, `Show only relevant`, `Clear focus`, and `Show relationship labels` controls.

- [ ] **Step 1: Write failing pure-helper tests**

Test exact/prefix/substring search across label, ID, address/key, and type. Test causal traversal, incident paths, exclusion of unrelated nodes, and bounded behavior for cycles, duplicates, self-loops, and unknown endpoints.

- [ ] **Step 2: Verify helper RED**

Run: `pnpm --filter @risk0/web test -- src/lib/radar-focus.test.ts`

Expected: FAIL because the helper module does not exist.

- [ ] **Step 3: Implement pure helpers**

Use normalized stable ranking and iterative visited-set traversal. Relationship direction remains dependent → dependency; similarity alone is not causal.

- [ ] **Step 4: Write failing graph behavior tests**

Test: search result 401 is promoted into a 400-node canvas; unrelated nodes/edges fade on selection; relevant-only hides them; clear restores them; labels are absent by default and contextual/opt-in when requested; layout spacing increases without losing deterministic ordering.

- [ ] **Step 5: Verify graph RED**

Run: `pnpm --filter @risk0/web test -- src/components/radar-graph.test.tsx src/lib/layered-layout.test.ts`

Expected: FAIL on promotion, focus rendering, labels, and spacing.

- [ ] **Step 6: Implement graph behavior**

Integrate helpers into `RadarGraph`, promote the selected search result before the cap, add focus classes/removal, hide labels by default, and increase deterministic layer spacing. Do not add a new layout dependency.

- [ ] **Step 7: Verify GREEN**

Run: `pnpm --filter @risk0/web test -- src/lib/radar-focus.test.ts src/components/radar-graph.test.tsx src/lib/layered-layout.test.ts && pnpm --filter @risk0/web exec tsc --noEmit`

Expected: selected tests pass and type-check exits 0.

- [ ] **Step 8: Commit**

```bash
git add apps/web/src/lib/radar-focus.ts apps/web/src/lib/radar-focus.test.ts apps/web/src/lib/layered-layout.ts apps/web/src/lib/layered-layout.test.ts apps/web/src/components/radar-graph.tsx apps/web/src/components/radar-graph.test.tsx apps/web/src/app/globals.css
git commit -m "feat(web): focus and declutter radar topology"
```

### Task 3: Floating Exposure/Incident Controls and Analysis Handoff

**Files:**
- Modify: `apps/web/src/components/recommendation-card.tsx`
- Modify: `apps/web/src/components/recommendation-card.test.tsx`
- Modify: `apps/web/src/app/page.tsx`
- Modify: `apps/web/src/app/page.test.tsx`
- Modify: `apps/web/src/app/globals.css`

**Interfaces:**
- Extend `RecommendationCard` with `onViewDependencies?: (entityId: string) => void`.
- `Home` owns `exposureOpen` and `incidentsOpen`; panels remain mounted to preserve form/list state.
- `Home.focusRadarEntity(entityId)` switches to Graph and selects the matching radar node.

- [ ] **Step 1: Write failing panel and handoff tests**

Test: Exposure floats right and is open by default; hiding/reopening preserves edited values; Incidents is independently foldable; narrow viewport makes large overlays mutually exclusive; Position Analysis result exposes View dependencies and returns to a focused Graph.

- [ ] **Step 2: Add late-response category tests**

Extend existing race tests so category changes do not allow an older incident response to replace the latest selection and do not discard a valid completed analysis.

- [ ] **Step 3: Verify RED**

Run: `pnpm --filter @risk0/web test -- src/components/recommendation-card.test.tsx src/app/page.test.tsx`

Expected: FAIL because panel persistence/exclusivity and graph handoff are incomplete.

- [ ] **Step 4: Implement minimal floating controls and handoff**

Keep `ExposureRail` and `IncidentRail` mounted in CSS overlays, add compact launchers, close the competing overlay below 48rem, and wire View dependencies through `focusRadarEntity`. Preserve all existing risk calculations and form validation.

- [ ] **Step 5: Verify GREEN**

Run: `pnpm --filter @risk0/web test -- src/components/recommendation-card.test.tsx src/components/exposure-rail.test.tsx src/components/incident-rail.test.tsx src/app/page.test.tsx && pnpm --filter @risk0/web exec tsc --noEmit`

Expected: selected tests pass and type-check exits 0.

- [ ] **Step 6: Commit**

```bash
git add apps/web/src/components/recommendation-card.tsx apps/web/src/components/recommendation-card.test.tsx apps/web/src/app/page.tsx apps/web/src/app/page.test.tsx apps/web/src/app/globals.css
git commit -m "feat(web): float controls and link analysis to graph"
```

### Task 4: Integration and Scale Verification

**Files:**
- Modify: existing files under `apps/web/e2e/` only where behavior changed.
- Modify: `apps/web/e2e-scale/large-radar.spec.ts`.

**Interfaces:**
- Consume Tasks 1–3; add no new production abstraction unless a failing browser test proves it necessary.

- [ ] **Step 1: Update existing E2E flows first**

Test category navigation, health-list-to-graph selection, panel hiding, Position Analysis handoff, responsive exclusivity, and no horizontal overflow by extending existing specs rather than creating a large new suite.

- [ ] **Step 2: Extend the scale test**

Search for a node beyond the initial cap, assert it reaches the canvas and can be focused, then retain existing payload, render-time, long-task, click-time, and 400-node budgets.

- [ ] **Step 3: Verify browser RED where integration is incomplete**

Run: `UV_CACHE_DIR=/private/tmp/risk0-uv-cache pnpm --filter @risk0/web test:e2e`

Run: `UV_CACHE_DIR=/private/tmp/risk0-uv-cache pnpm --filter @risk0/web test:e2e:scale`

- [ ] **Step 4: Apply only integration fixes and verify the full matrix**

Run: `pnpm --filter @risk0/contracts test`

Run: `pnpm --filter @risk0/web test`

Run: `pnpm --filter @risk0/web exec tsc --noEmit`

Run: `pnpm --filter @risk0/web build`

Run: `UV_CACHE_DIR=/private/tmp/risk0-uv-cache pnpm --filter @risk0/web test:e2e`

Run: `UV_CACHE_DIR=/private/tmp/risk0-uv-cache pnpm --filter @risk0/web test:e2e:scale`

Expected: all commands exit 0 and scale budgets remain enforced.

- [ ] **Step 5: Commit and request whole-branch review**

```bash
git add apps/web/e2e apps/web/e2e-scale
git commit -m "test(web): verify graph-first workspace"
```

Resolve all Critical/Important review findings, rerun the matrix, and use `superpowers:finishing-a-development-branch` before integration.
