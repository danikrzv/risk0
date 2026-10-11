# Judging-Readiness Implementation Plan

**Goal:** Maximise Risk0's fit to the Belem Capital "DeFi Risk Dependency Challenge" judging grid on real Ethereum data:
dependency discovery (30%), defensible impact (30%), stay/exit usefulness (25%), working demo on real data (15%).

**Branch:** `feat/judging-readiness` (from `main` at `b52ef37`).

## Findings that drive the plan (verified 2026-10-11)

- Every live analysis returns `UNAVAILABLE`. Root cause: the configured RPC is an Alchemy free-tier key, which allows
  `eth_getLogs` over at most 10 blocks. Live oracle health (26 h lookback) and probability history (365 d lookback)
  both scan Chainlink logs, so every oracle is "missing critical data" and every empirical probability is unavailable.
- The same key serves `eth_call` fine: one Multicall3 `aggregate3` call returned 400 ETH/USD `getRoundData` rounds
  (13.5 days) in 0.2 s. A year of ETH/USD is about 25 calls. No paid RPC and no Chainlink-specific endpoint is needed.
- Even with history, the default scenario set stays `UNAVAILABLE`: `oracle-failure` has no probability by design
  (hourly history cannot resolve the 30-minute staleness threshold), and an unknown probability with a non-zero impact
  makes the expected loss incomplete.
- `protocol-impairment` is unavailable: DeFiScan's index (20 protocols) has no Morpho review and the DeFiSafety
  snapshot has no transcribed score. DeFiLlama's public API (`/hacks`, 1,299 incidents including the 2024-10 Morpho
  market oracle misconfiguration; `/protocols`; `/v2/chains`) is reachable without a key.
- Oracle health only covers markets whose assets are WETH/ETH/USDC. Morpho market oracles are on-chain
  `MorphoChainlinkOracleV2` contracts whose underlying feeds can be read directly, which would cover most markets.
- The brief's worked example is five layers deep (chain, yield source and issuer, oracle, AMM, lending market). Risk0
  has no issuer/underlying layer and treats an oracle as a single opaque address.
- The brief's decision example is a book of ten 10% pockets; Risk0 decides per position only.

## Workstreams

Each runs in its own worktree from this branch and owns the files listed. Every change ships with tests (no network
in unit tests), and all existing suites keep passing.

### W1 — On-chain Chainlink reads and oracle decomposition (blocker; 15% + 30% + 25%)

Owns: `app/adapters/ethereum.py`, new `app/adapters/multicall.py`, new `app/adapters/morpho_oracle.py`,
`app/history/*`, in `app/analysis/service.py` only `ChainlinkLatest`, `ChainlinkPrices`, `ChainlinkOracleHealth`,
`HybridProbabilityProvider` (oracle-failure branch) and the feed/heartbeat constants, plus the oracle part of
`app/analysis/graph_builder.py`, and `app/api/analysis.py` wiring.

1. Replace log scanning with round reads: `latestRoundData()` for the latest answer; `getRoundData(roundId)` batched
   through Multicall3 (`0xcA11bde05977b3631167028862bE2a173976CA11`) walking back across Chainlink phases
   (`phaseId`, `phaseAggregators`) until the lookback start. Same `Observation` output, exact update timestamps.
   Cache by feed and extend incrementally instead of refetching.
2. Live oracle health from `latestRoundData`: one call per feed, no log range.
3. Oracle decomposition: read each Morpho market oracle's `BASE_FEED_1/2`, `QUOTE_FEED_1/2`, vault and
   conversion-sample getters on-chain; resolve each feed's `description()`, `decimals()` and heartbeat (inferred
   from round history as the 99th-percentile update interval, capped by a documented table for known feeds).
   Live health then covers any Chainlink-backed Morpho oracle; unknown oracle types stay explicitly unknown.
   The analysis graph shows feeds under the oracle node.
4. Empirical oracle-failure probability from round history: a window is an event when any gap between consecutive
   updates exceeds heartbeat + 30 minutes (the spec's staleness trigger); rolling 30-day windows with the existing
   Wilson interval. This removes the structural `UNAVAILABLE`.
5. Extend price-history feeds for collateral shocks where Chainlink has mainnet feeds (wstETH via stETH/USD,
   WBTC via WBTC/BTC × BTC/USD), each address verified on-chain by `description()`.

Acceptance: against the live RPC, WETH/USDC lender and MEV Capital WETH vault analyses return `STAY` or `EXIT`
with non-null probabilities for stablecoin depeg, collateral shock, and oracle failure.

### W2 — Protocol evidence and TVL concentration (30% + 25%)

Owns: new `app/adapters/defillama.py`, `app/evidence/*`, `FileAndDefiScanEvidence` in `app/analysis/service.py`,
`data/evidence/*`, protocol-node metrics in the graph builder's protocol section.

1. DeFiLlama adapter for `/hacks`, `/protocols`, `/v2/chains` (cached daily, sanitized errors).
2. Empirical protocol-impairment base rate: for lending protocols above a TVL floor, the annual share of
   protocol-years with an exploit loss above a materiality threshold, with a Wilson interval; Morpho's own incident
   record listed as evidence. Combined with DeFiScan/DeFiSafety when those exist, otherwise used alone at LOW
   confidence, never fabricated.
3. TVL concentration: Morpho's share of Ethereum DeFi TVL and the vault/market share of Morpho TVL as protocol
   metrics, surfaced in the graph and as a concentration warning when above a named threshold.

### W3 — Issuer and underlying look-through (30%)

Owns: new `data/catalog/assets.ethereum.json`, the asset section of `app/analysis/graph_builder.py`, radar
discovery asset look-through, and their tests.

1. A version-controlled registry of the catalog's assets (USDC, WETH, wstETH, WBTC, EURCV and the other catalog
   assets): underlying asset, issuer, issuer type (protocol / custodial / wrapper), redemption mechanism, source URL,
   and DeFiScan slug when one exists (Lido). Only facts with a cited source.
2. Graph builder adds `asset —backed_by→ underlying asset` and `asset —backed_by→ issuer` (protocol node with an
   `issuer_type` metric), so the worked example's "yield source and its issuer" layer exists and scenarios
   propagate through it.

### W4 — Book view and break-even (25%)

Owns: new `app/analysis/book.py`, new `app/api/book.py`, contracts schema additions, new web components and the
Position Analysis book panel.

1. `POST /v1/book`: a list of pockets (position + weight) runs the existing analysis per pocket and returns per-pocket
   expected loss, exit cost, decision and the book-level totals, as fractions of pocket and of book, matching the
   brief's "ten pockets of 10%, 11% slippage" framing.
2. Break-even probability per decision: the probability at which the dominant scenario's expected loss equals the exit
   cost, so a manager sees how far the estimate is from flipping the decision.
3. Web: a Book panel in Position Analysis (add pockets from the catalog, run, read a ranked table with
   stay/exit per pocket and book totals).

### W5 — Integration, live verification, demo (15%)

Owned by the coordinator after W1–W4 merge: resolve conflicts, run the full matrix (Python, contracts, web unit,
type-check, build, e2e, scale), run live analyses on the catalog's mainstream entries, and document a judging map and
demo script in the README (criterion → feature → how to show it, plus the fixture fallback).

## Out of scope

Pendle PT and non-Morpho venues, non-EVM chains, perp-venue vaults, Uniswap depth history for liquidity-collapse
probabilities (stays explicitly unavailable; its lender impact excludes the shortfall, so it does not block decisions).

## Outcome (2026-10-11)

W1–W4 shipped as planned. Live verification then exposed modelling defects, fixed on this branch: lenders are charged
bad debt instead of the collateral fall; the decision counts 30-day forgone yield; inferred heartbeats tolerate one
missed cycle and are judged over their inference window; exchange-rate adapters are a structural zero; oracle deviation
is judged by the side of the market it hurts; non-Chainlink feeds (RedStone, Ojo, custom) are observed with cited
heartbeats or an explained industry prior; EUR stablecoins without a Chainlink feed use DeFiLlama market prices for
depeg history. Result with the UI defaults: every analyzable catalog entry decides on live data (11 STAY, 5 EXIT,
0 UNAVAILABLE). See `docs/judging-map.md`.
