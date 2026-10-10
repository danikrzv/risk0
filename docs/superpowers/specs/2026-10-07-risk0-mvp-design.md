# Risk0 Ethereum MVP Design

## 1. Purpose

Risk0 is a read-only DeFi risk dependency prototype for Belem Capital. It helps an analyst model a hypothetical lender or borrower position, understand the position's dependencies, apply explicit risk events, compare the 30-day expected loss of staying with the immediate cost of exiting, and receive an explainable `STAY` or `EXIT` recommendation.

The MVP focuses on Ethereum mainnet, Morpho lending and borrowing, and Uniswap exit liquidity. It prioritizes a reliable real-data demonstration over broad protocol or chain coverage.

## 2. Success Criteria

The MVP is successful when a user can:

1. Select a real Morpho vault or market from a curated catalog.
2. Choose lender or borrower mode.
3. Enter a position amount and, for borrowers, collateral, debt, and LTV using sensible presets.
4. View a dependency graph built from current Morpho, Ethereum, oracle, and Uniswap data.
5. Run predefined 30-day risk scenarios.
6. See scenario propagation, stressed position values, expected loss, exit cost, hard safety triggers, data freshness, and confidence.
7. Receive an explainable `STAY`, `EXIT`, or `UNAVAILABLE` result.

`UNAVAILABLE` is a data-quality state, not an investment recommendation. It is returned when critical data is missing or too stale to support a defensible decision.

## 3. Scope

### Included

- Ethereum mainnet only.
- Curated real Morpho vaults and markets.
- Hypothetical lender and borrower positions with user-entered amounts.
- Live Morpho market, vault, allocation, and liquidity data.
- Ethereum RPC reads for contracts, tokens, oracles, and gas.
- Uniswap liquidity discovery and read-only exit quote simulation.
- Explicit dependency graph construction.
- Historical and model-estimated event probabilities.
- Predefined scenarios with editable severity.
- Hybrid recommendation logic.
- Local-first development and demonstration.

### Excluded

- Wallet connection or automatic wallet position discovery.
- Transaction construction, signatures, approvals, or execution.
- Automated portfolio rebalancing.
- Chains other than Ethereum.
- Protocols other than Morpho and Uniswap except as graph dependencies or evidence sources.
- A production-grade actuarial exploit probability model.
- Persistent user profiles or simulation history.

## 4. Curated MVP Catalog

The initial catalog contains five Ethereum entries. The UI must display each entry's provenance and must not imply that representative entries are confirmed Belem holdings.

| Entry | Type | Address or market ID | Provenance |
| --- | --- | --- | --- |
| Belem RWA EURCV | Morpho Vault V2 | `0xD6d4b804014EF27836dBe9f8f6Bf6c71251548Ec` | Verified Belem-named, listed Morpho vault |
| MEV Capital wETH | Morpho Vault V1 | `0x9a8bC3B04b7f3D87cfC09ba407dCED575f2d61D8` | Public MEV Capital vault; MEV publicly states that its fund is distributed by Belem |
| wstETH collateral / WETH debt | Morpho market, 96.5% LLTV | `0xb8fc70e82bc5bb53e773626fcc6a23f7eefa036918d7ef216ecfb1950a94a85e` | Representative liquid ETH recursive-borrowing market |
| WETH collateral / USDC debt | Morpho market, 86% LLTV | `0x94b823e6bd8ea533b4e33fbc307faea0b307301bc48763acc4d4aa4def7636cd` | Representative stablecoin borrowing market |
| WBTC collateral / USDC debt | Morpho market, 86% LLTV | `0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49` | Representative BTC-backed stablecoin borrowing market |

The catalog is version-controlled data. Live metrics, allocations, and availability are fetched at runtime. If an entry becomes inactive or unsupported, the UI retains it for traceability but marks analysis unavailable until the catalog is updated.

## 5. Architecture

Risk0 uses two deployable services in one repository.

### Next.js Web Application

The web application owns:

- Catalog browsing and selection.
- Lender and borrower input forms.
- Client-side input validation and presets.
- Interactive dependency graph rendering.
- Scenario controls.
- Result visualization and explanation.
- Freshness, confidence, provenance, and warning presentation.

The browser never receives the Ethereum RPC credential. Browser requests go only to the Next.js server or the FastAPI service through a configured server-side URL.

### FastAPI Risk Service

The Python service is the authority for:

- Morpho, Ethereum RPC, oracle, Uniswap, and risk-evidence adapters.
- Data normalization and caching.
- Catalog hydration.
- Dependency graph construction.
- Historical event estimation.
- Protocol-risk scoring and score-to-probability mapping.
- Scenario propagation.
- Expected-loss and exit-cost calculations.
- Hybrid recommendation logic.
- Data quality and confidence decisions.

### Repository Layout

The planned structure is:

```text
risk0/
  apps/
    web/                 Next.js application
  services/
    risk/                FastAPI service and Python risk engine
  packages/
    contracts/           Versioned JSON Schemas and generated TypeScript types
  data/
    catalog/             Curated entities and provenance
    scenarios/           Versioned scenario definitions
  docs/
    superpowers/specs/   Approved designs
  docker-compose.yml     Local two-service startup
```

## 6. Service Contract

The services communicate through versioned JSON payloads validated by JSON Schema, Pydantic, and generated TypeScript types.

### Core Types

- `CatalogEntry`: entity identifier, chain, type, address or market ID, display metadata, provenance, and support status.
- `PositionInput`: mode, selected entity, amount, collateral amount, debt amount, LTV, and numeraire.
- `RiskNode`: stable identifier, node type, label, current metrics, provenance, and freshness.
- `RiskEdge`: source, target, relationship type, and exposure weight when applicable.
- `RiskGraph`: nodes, edges, graph timestamp, and completeness warnings.
- `ScenarioDefinition`: event family, affected node types, severity parameters, probability method, and thresholds.
- `ScenarioResult`: affected paths, stressed values, hard triggers, impact, probability range, confidence, and explanation.
- `ExitEstimate`: withdrawability, route, slippage, fees, gas, price impact, total cost, quote age, and warnings.
- `Recommendation`: `STAY`, `EXIT`, or `UNAVAILABLE`; expected loss; exit cost; triggers; dominant dependency; confidence; and explanation.

### MVP Endpoints

- `GET /v1/health`: service and dependency health.
- `GET /v1/catalog`: curated entries hydrated with current availability and summary metrics.
- `POST /v1/analysis`: validates a position and scenario selection, then synchronously returns the graph, scenario results, exit estimate, and recommendation.

The synchronous analysis endpoint keeps the MVP simple. A background job system is deferred until calculations exceed acceptable interactive latency.

## 7. Dependency Graph

### Node Types

- `position`
- `vault`
- `market`
- `protocol`
- `asset`
- `oracle`
- `pool`
- `chain`

### Edge Types

- `allocated_to`
- `lends`
- `borrows`
- `uses_collateral`
- `uses_oracle`
- `backed_by`
- `exits_through`
- `deployed_on`

Vault allocation weights are taken from current Morpho allocations. Market graphs include loan asset, collateral asset, oracle, Morpho protocol, Ethereum, and candidate Uniswap exit pools. Every node and material metric carries a source and timestamp.

## 8. Data Sources

### Morpho

Use Morpho's public REST and GraphQL APIs for catalog hydration, vault state, allocations, market parameters, rates, utilization, liquidity, historical metrics, and oracle metadata. API responses are treated as indexed data and include the latest indexed block when available.

### Ethereum RPC

Use a server-side Alchemy Ethereum endpoint from `.env` for current block, contract reads, oracle state, token metadata, and gas estimation. The credential is never hardcoded, committed, logged, or sent to the browser.

### Uniswap

Use current on-chain pool state and read-only quote simulation for candidate exit routes. For each relevant token pair, compare supported fee tiers and use the best valid quote. Quote results include route, expected output, estimated gas, price impact, slippage tolerance, and block number.

### Historical Prices and Liquidity

Use Ethereum-native evidence wherever feasible: Chainlink-compatible feed history through RPC logs for reference prices, Lido conversion state for wstETH/WETH, and Uniswap pool observations and events for market price and liquidity depth. Normalize these series to hourly observations before caching. If an asset or pool lacks enough on-chain history for the selected event, the estimator must return lower confidence or `UNAVAILABLE`; it must not invent a complete series.

### Protocol-Risk Evidence

- DeFiScan supplies evidence about admin powers, upgradeability, governance, exit windows, and external dependencies.
- DeFiSafety supplies evidence about development process, documentation, testing, audits, bug bounties, access controls, monitoring, and oracle documentation.
- Official protocol audit reports, bug-bounty terms, and documented incidents are primary evidence when available.

External ratings are evidence inputs, not exploit probabilities. Each adapter preserves the source URL, source methodology, retrieval timestamp, covered dimensions, and freshness.

## 9. Cache Design

The Python service owns a local SQLite cache. It stores reproducible market data only.

| Data | Refresh or TTL | Retention |
| --- | --- | --- |
| Morpho, oracle, price, gas, and pool snapshots | 30–60 seconds | 24 hours for raw snapshots |
| Uniswap exit quotes | 15 seconds | Expire immediately after TTL |
| Hourly historical price and market metrics | Daily refresh | Up to 365 days |
| Risk evidence and methodology metadata | Daily refresh | Current and previous version |
| Derived probability estimates | Recompute when inputs or history change | Current model version plus prior version |

Expected storage for the curated catalog is 10–50 MB. The cache has a 100 MB hard cap and prunes expired raw responses before historical or model data. The SQLite file is ignored by Git.

User position inputs and simulations are not persisted by default.

## 10. Risk Events

Each event is specific to a node or dependency and has a named threshold, probability method, impact rule, and confidence.

### Default Scenario Set

1. **Stablecoin depeg:** EURCV or USDC falls 10% from its reference value. Severity is editable.
2. **Collateral shock:** wstETH, WETH, or WBTC falls 15% over 24 hours. Severity is editable.
3. **Oracle failure:** price is stale for 30 minutes or deviates by at least 5% from the comparison market.
4. **Exit-liquidity collapse:** executable Uniswap depth falls 80%, or modeled price impact crosses the configured exit threshold.
5. **Protocol impairment:** a smart-contract or control failure causes at least a 10% loss on exposed capital. Severity is editable up to total impairment.

### Historical Probability Method

For measurable events, the service evaluates historical rolling 30-day windows. The probability is the fraction of windows containing the defined event. The response includes:

- Observation period.
- Sampling interval.
- Number of windows.
- Number of event windows.
- Threshold definition.
- Data gaps.
- Low, central, and high estimates when sample uncertainty is material.

The UI labels historical estimates as empirical and does not imply independence between overlapping windows.

### Protocol Score-to-Probability Mapping

The risk service normalizes protocol evidence into a 0–100 evidence score using these dimensions:

- Audits, testing, and verification: 30%.
- Upgradeability, admin powers, and governance: 25%.
- Track record and incidents: 20%.
- Oracle and external dependencies: 15%.
- Monitoring and bug bounty: 10%.

The initial demonstration mapping is:

| Evidence score | Annual central impairment probability | 30-day conversion |
| --- | --- | --- |
| 85–100 | 0.5% | `1 - (1 - 0.005)^(30/365)` |
| 70–84 | 1.0% | `1 - (1 - 0.01)^(30/365)` |
| 50–69 | 3.0% | `1 - (1 - 0.03)^(30/365)` |
| 0–49 | 8.0% | `1 - (1 - 0.08)^(30/365)` |

The probability range is initially one-half to twice the central estimate, capped to valid probability bounds. Missing, stale, or conflicting evidence lowers confidence and widens the range; it never silently improves the score. The UI labels all such probabilities `model-estimated` and displays the model version and factor breakdown.

This mapping is an explicit hackathon heuristic, not an actuarial claim. Its isolation behind a versioned interface allows later calibration against exploit and incident datasets.

## 11. Scenario Propagation and Impact

### Lender Positions

For a vault deposit, exposure is distributed across underlying markets using current allocation weights. For direct market supply, exposure is assigned to that market.

Scenario impact includes:

- Asset impairment propagated through weighted exposures.
- Market bad-debt or protocol impairment haircuts.
- Reduced immediately withdrawable liquidity.
- Exit slippage and gas when conversion is required.

Unavailable liquidity is reported separately from permanent loss. It can trigger an exit warning without being counted as an immediate realized loss.

### Borrower Positions

The core stressed metric is:

```text
stressed_ltv = stressed_debt_value / stressed_collateral_value
```

A hard liquidation trigger fires when stressed LTV is greater than or equal to the market LLTV. Scenario impact includes liquidation incentive, collateral sold, swap price impact, gas, and residual collateral or debt.

Inputs that already exceed the market LLTV are rejected. Presets target a current LTV with enough room to demonstrate both surviving and liquidating scenarios.

## 12. Exit Cost

Exit cost is calculated in the position's numeraire.

### Lender Exit

- Validate immediately withdrawable vault or market liquidity.
- Withdraw the requested amount when feasible.
- Add gas.
- Add Uniswap fees, slippage, and price impact only when conversion to the selected numeraire is required.
- Report partial or unavailable withdrawal separately.

### Borrower Exit

- Estimate debt repayment.
- Determine whether existing assets repay the debt or collateral must be swapped.
- Quote the required Uniswap route.
- Include swap fees, slippage, price impact, Morpho interaction gas, and residual exposure.

Quotes older than 30 seconds cannot support a final recommendation and must be refreshed.

## 13. Hybrid Recommendation Rule

The service computes:

```text
expected_staying_loss_30d = sum(event_probability_30d * event_impact)
```

The central recommendation is:

```text
if critical_data_missing:
    UNAVAILABLE
elif any_hard_safety_trigger:
    EXIT
elif expected_staying_loss_30d > exit_cost_now:
    EXIT
else:
    STAY
```

Hard safety triggers are:

- Borrower liquidation under any selected stress scenario.
- Oracle stale or deviation threshold breached in live data.
- Current protocol emergency or critical incident evidence.
- Requested lender withdrawal cannot be fully satisfied and stressed available liquidity falls below 20% of the requested position.

Every recommendation includes the dominant dependency, triggering events, expected-loss range, exit-cost estimate, confidence, timestamps, and a plain-language explanation. If exit is recommended but current liquidity is insufficient, the explanation says so explicitly.

## 14. User Experience

The primary flow is:

1. Choose a curated vault or market.
2. Choose lender or borrower mode when supported.
3. Enter amount and borrower LTV, or accept presets.
4. Load current data and display freshness.
5. Explore the dependency graph.
6. Select or edit scenarios.
7. Run analysis.
8. Review propagated impacts, expected loss, exit cost, and recommendation.

The recommendation card is not a black box. It shows the comparison numerically and links each important value to its graph dependency and data source.

## 15. Failure Handling

- If a live source fails and valid cached data exists, use it with a visible stale badge and exact age.
- If critical state, oracle, or quote data is absent or outside its maximum age, return `UNAVAILABLE`.
- Never convert missing data into zero risk or a `STAY` result.
- If sources conflict, retain both values, use the conservative value for the decision, lower confidence, and show the conflict.
- If a catalog entry no longer resolves, keep its metadata visible but disable analysis.
- External payloads are validated before entering the domain model.
- Provider errors are sanitized; secrets and full RPC URLs are not logged.

## 16. Security and Configuration

- Store the Ethereum RPC URL in a local `.env` file.
- Commit only `.env.example` with placeholder values.
- Ignore `.env`, SQLite files, generated caches, and `.superpowers/`.
- Keep all provider access server-side.
- Use read-only calls; no private keys or signing capabilities exist in the MVP.
- Apply request timeouts, bounded retries, and response-size limits to external adapters.
- Validate amounts, addresses, market IDs, percentages, and scenario bounds at both service boundaries.

Because a credential was shared during planning, it should be rotated before the final demo and the replacement stored only in the local `.env`.

## 17. Testing Strategy

### Python Unit Tests

- LTV and liquidation boundaries.
- Vault allocation exposure calculations.
- Historical window event counting.
- Score normalization and 30-day probability conversion.
- Scenario propagation by node and edge type.
- Expected-loss aggregation.
- Lender and borrower exit-cost calculations.
- Hybrid recommendation outcomes, including `UNAVAILABLE`.

### TypeScript Unit and Component Tests

- Input validation and presets.
- Catalog and provenance labels.
- Graph node and edge rendering.
- Scenario controls.
- Recommendation explanation and stale-data warnings.

### Contract Tests

Validate all example and generated payloads against the shared JSON Schemas in Python and TypeScript.

### Integration Tests

Use recorded, sanitized Morpho, Ethereum RPC, and Uniswap fixtures for deterministic adapter and analysis tests. Keep a small opt-in live-data smoke test for development.

### End-to-End Tests

Maintain golden lender and borrower flows covering:

- A healthy `STAY` result.
- Expected loss exceeding exit cost.
- Borrower liquidation trigger.
- Lender liquidity trigger.
- Stale cached data.
- Missing critical data returning `UNAVAILABLE`.

## 18. Team Split

### Person 1: Data and Risk Service

- External adapters and cache.
- Domain models and graph builder.
- Historical and protocol-risk estimators.
- Scenario propagation, exit estimates, and recommendation engine.
- Python, contract, and integration tests.

### Person 2: Web and Graph Experience

- Next.js application and forms.
- Curated selection experience.
- Interactive dependency graph.
- Scenario and result views.
- TypeScript, component, and end-to-end tests.

Both people agree on the shared schemas and two golden examples before building their respective sides.

## 19. Implementation Order

1. Repository and two-service skeleton.
2. Shared contracts and curated catalog.
3. Morpho live snapshot adapter.
4. Lender and borrower domain calculations with fixtures.
5. Dependency graph API and visualization.
6. Ethereum RPC and Uniswap exit quote adapters.
7. Historical estimators and protocol-risk evidence model.
8. Hybrid recommendation and explanation.
9. Cache, stale-data handling, and resilience.
10. Golden end-to-end flows and demo polish.

The first vertical slice is one curated market using recorded fixtures, then live Morpho data, then live RPC and Uniswap quotes. Additional catalog entries reuse the same interfaces.

## 20. Sources and Provenance

- [Belem Capital](https://www.belemcapital.com/) describes its market-neutral lending, borrowing, liquidity provision, and arbitrage mandate.
- [MEV Capital](https://www.mevcapital.com/solutions) states that its discretionary on-chain strategies are distributed through Belem Capital and include liquidity provision, arbitrage, fixed income, and recursive borrowing.
- [Morpho API documentation](https://docs.morpho.org/developers/api/get-started/) documents public GraphQL and REST access for Ethereum vaults, markets, positions, and historical data.
- [DeFiScan](https://www.defiscan.info/about) documents its on-chain analysis of admin controls, governance, upgradeability, and dependencies.
- [DeFiSafety](https://defisafety.com/documentation-09/) documents its process-quality methodology covering code, testing, audits, bounties, controls, and oracles.

Live catalog addresses, market IDs, LLTVs, and initial liquidity checks were verified against Morpho's public API on 2026-10-07. They remain runtime data and must be refreshed by the application.
