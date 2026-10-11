# Graph-First Risk Workspace Design

## Purpose

Risk0 should operate as a graph-first monitoring terminal rather than a long dashboard page. The primary experience is a full-screen dependency topology that helps a user locate an entity, understand its relevant causal dependencies, inspect incident propagation, and connect the result to a manually entered portfolio exposure.

The design must remain responsive at the current live-radar scale, preserve the existing risk-service contracts, and reuse the radar and analysis pipelines already present in the project.

## Success Criteria

- The dependency graph occupies the full available viewport beneath the application header and ticker.
- Users can search any loaded graph entity from an upper-right global search control.
- The default graph displays as many useful entities as the existing browser safety cap permits.
- The graph is sparse enough to inspect without pervasive edge overlap.
- Relationship labels are hidden by default and shown only in an explicit contextual mode.
- Selecting a node emphasizes only its relevant dependency context and active incident paths.
- Exposure and incident controls float over the graph and can be hidden independently.
- Markets, Oracles, Incidents, and Position Analysis are real header categories with health-marked lists.
- Selecting an item in a list can take the user to the graph focused on that item.
- Selecting a position for analysis can return the user to the graph with its relevant nodes focused.
- Existing functional, responsive, and live-scale browser tests continue to pass.

## Workspace Architecture

Risk0 uses one persistent client-side workspace with shared radar state. Header categories change the active workspace view without discarding the loaded topology, selected entity, selected incident, manual exposure, or analysis result.

The header categories are:

1. **Graph** — the full dependency topology and contextual graph controls.
2. **Markets** — a searchable health-marked market list.
3. **Oracles** — a searchable health-marked oracle list.
4. **Incidents** — a severity-marked active-incident list.
5. **Position Analysis** — supported vaults and markets followed by the existing analysis workflow.

`TerminalShell` renders these categories as real buttons and reports category changes to the main workspace. `Home` owns the active category and the shared domain state. Switching categories does not refetch the graph solely because of navigation.

## Category Views

### Graph

The Graph category fills the available viewport. The descriptive masthead is removed. The graph toolbar, search, status legend, and floating-panel controls occupy compact overlays rather than separate page sections.

### Markets

Markets are derived from the loaded radar entities. Each row shows:

- health marker and textual health status;
- market label and identifier;
- chain;
- liquidity/collateral pair when available;
- utilization when available;
- active-incident count.

Selecting a market switches to Graph and focuses that market's relevant dependency context.

### Oracles

Oracles are derived from the loaded radar entities. Each row shows:

- health marker and textual health status;
- oracle label and identifier;
- chain;
- latest deviation or staleness metrics when available;
- number of affected markets;
- active-incident count.

Selecting an oracle switches to Graph and focuses its relevant dependency context.

### Incidents

The Incidents category lists active incidents with:

- severity marker and text;
- source entity;
- reason;
- observation time or age;
- propagation distance or affected-entity count when available;
- affected manual exposure when available.

Selecting an incident switches to Graph, loads its detail graph, and emphasizes its propagation paths. The floating incident drawer uses the same incident-list component.

### Position Analysis

Position Analysis begins with a health-marked list of supported vaults and markets. Selecting an entry opens the existing position inputs, scenario controls, recommendation, result graph, and data-quality details within this category.

The analysis result exposes a `View dependencies` action. It switches to Graph, focuses the matching radar entity and its relevant causal context, and preserves the completed analysis state when the user returns.

## Graph Search

A search field is fixed in the upper-right graph toolbar. It matches case-insensitively against entity label, ID, address or key, and entity type.

Search results are ranked with exact label and identifier matches first, then prefix matches, then substring matches. Selecting a result focuses the node and its relevant dependency context.

Search must be able to surface an entity that would otherwise fall beyond the canvas node cap. A matching selected entity is promoted into the rendered canvas set rather than remaining inaccessible behind ordering limits.

## Relevant Dependency Context

When a node is selected, its relevant context contains:

- the selected entity;
- its complete causal dependency path, following dependency relationships such as vault to market and market to asset, oracle, IRM, protocol, and chain;
- active incident paths that affect the selected entity;
- any manually entered exposure attached to the selected entity;
- similar vaults only when they share a dependency involved in the selected context or incident.

Unrelated nodes and edges are strongly faded by default to preserve spatial orientation. A `Show only relevant` control removes faded elements from the rendered graph. A `Clear focus` control restores the complete graph.

Selection must not treat arbitrary similarity edges as causal dependencies. Similarity peers enter context only through the explicit shared-dependency or incident rule above.

## Layout and Edge Rendering

The layered layout remains deterministic but uses larger horizontal and vertical separation. Node layers retain semantic ordering so repeated selections do not cause the topology to jump unpredictably.

The default view renders the maximum useful server-provided topology up to the existing 400-node browser safety cap. Viewport culling remains enabled at scale.

Edge labels are hidden by default. They appear only when one of these modes is active:

- a node is focused, for edges in its relevant context;
- an incident is selected, for edges on the selected propagation path;
- the user explicitly enables `Show relationship labels`.

Unrelated edges fade with unrelated nodes. Focused edges use stronger contrast and directional arrows. Parallel relationships must use distinct routing or curvature where supported so they do not render as one indistinguishable line.

## Floating Panels

### Exposure Panel

The manual exposure form is a floating panel on the right side of the Graph category. It is open by default and independently hideable. A compact `Exposure` launcher remains visible when the panel is closed.

The panel keeps its form values and last submission state when hidden or when the user visits another category.

### Incident Drawer

Incidents use a collapsible right-side drawer over the graph. It must not permanently reduce the canvas dimensions. On large screens, exposure and incident controls are positioned to avoid covering each other. On narrower screens, only one large drawer is open at a time.

### Notifications and Graph Controls

Notifications remain available in the upper-right toolbar beside search. Pan, zoom, fit-view, minimap, clear-focus, relevant-only, and relationship-label controls remain usable while floating panels are open.

On mobile, floating panels become near-full-width overlays. The graph stays mounted underneath so pan, zoom, selection, and loaded topology state are preserved.

## State and Data Flow

No new backend endpoint is required for the initial implementation.

`Home` continues to load catalog, graph, incident detail, exposure impact, and analysis data. It additionally derives category lists and health markers from the existing responses. Shared state includes:

- active category;
- global graph search and selected result;
- selected radar entity;
- selected incident and detailed incident graph;
- graph focus mode and relevant-only preference;
- exposure panel and incident drawer visibility;
- manual exposure and propagated impacts;
- selected catalog entry and completed position analysis.

Derived lists and graph focus sets must be memoized. Category changes must not recompute graph layout unless the rendered entity or relationship set changes.

## Empty and Error States

- Graph discovery pending: keep controls visible and explain that topology has not arrived yet.
- Empty Market or Oracle category: explain that no entities of that type are currently available.
- Health unavailable: display `UNKNOWN`; never imply healthy status.
- Incident detail failure: retain the base graph and show a retryable error without clearing the selected incident.
- Exposure submission failure: retain entered values and show the existing explicit error.
- Search with no result: show an empty-result message without mutating current graph focus.

## Accessibility

- Header categories use buttons with an active-state indicator and appropriate tab or navigation semantics.
- Health and severity use text or symbols in addition to color.
- Floating panels expose expanded state and controlled-region relationships.
- Keyboard users can open and close panels, operate graph controls, and select list entries.
- Focus is moved to the destination heading or selected graph context after a category handoff.
- Reduced-motion preferences continue to disable nonessential animation.

## Components

The implementation should keep responsibilities separated:

- `TerminalShell`: category navigation and application chrome.
- `Home`: shared data, active category, and cross-view handoffs.
- `RadarGraph`: graph filters, focus rendering, labels, and canvas interaction.
- `GraphSearch`: ranked global entity search and selection.
- `FloatingExposurePanel`: exposure launcher and persistent form container.
- `FloatingIncidentDrawer`: incident launcher and list container.
- `RadarEntityList`: reusable health-marked list for Markets, Oracles, and Position Analysis.
- Existing analysis components: unchanged where possible, composed inside the Position Analysis category.

## Testing Strategy

Tests must be written before each behavior change and observed failing for the intended reason.

Component and page tests cover:

- real header category switching and active states;
- Market, Oracle, Incident, and Position Analysis lists with health markers;
- list-row handoff to a focused graph;
- exposure and incident floating-panel visibility and state preservation;
- global search ranking and promotion beyond the canvas cap;
- relevant dependency-set calculation;
- fade, relevant-only, clear-focus, and conditional edge-label behavior;
- analysis-to-graph handoff;
- loading, empty, unknown-health, and retryable-error behavior.

Browser tests cover:

- desktop full-viewport graph layout;
- mobile overlay behavior and no horizontal overflow;
- keyboard operation of categories and drawers;
- the complete position-analysis-to-focused-graph flow;
- the existing live-sized radar performance limits with search and focus enabled.

The production build, TypeScript check, web test suite, regular Playwright suite, and 20,000-entity scale suite must pass before integration.

## Out of Scope

- New radar backend endpoints solely for category lists.
- Removing the 400-node browser safety cap.
- Editing topology relationships from the UI.
- Persisting personal UI layout preferences across devices.
- Replacing React Flow or the existing risk-analysis service.
