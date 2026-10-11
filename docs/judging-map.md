# Risk0 × Belem Capital — Judging Map

How Risk0 answers the **DeFi Risk Dependency Challenge**, criterion by criterion, with what to show live.
Scope: Morpho on Ethereum mainnet. Read-only and non-advisory: no wallet, no signature, no transaction.

| Criterion (weight) | What Risk0 does | Where to show it |
|---|---|---|
| Dependency discovery: depth and correctness (30%) | Builds each position's graph automatically from live data, down to the layers in the brief's worked example | Graph view; Position Analysis → *Dependency graph* |
| Impact estimation: is the number defensible (30%) | Mechanism-specific impacts and empirical 30-day probabilities, each with its method, source, and interval | *Scenario impacts*, *Data quality* |
| Decision usefulness: stay or exit, and why (25%) | Expected loss vs exit cost **plus forgone yield**, hard safety triggers, break-even probability, and a book of pockets | Recommendation card; *Book* panel |
| Working demo on real data (15%) | Live Morpho API, on-chain Chainlink reads, live Uniswap quotes, DeFiLlama evidence; works on a free RPC key | Live mode (below) |

## 1. Dependency discovery (30%)

The brief's worked example is five layers deep. Risk0 covers each layer for Morpho positions, read from live sources:

| Brief's layer | Risk0 graph | Source |
|---|---|---|
| Settlement chain | `chain` node (Ethereum) | static (Ethereum mainnet; the live smoke test checks the RPC reports chain id 1) |
| Underlying yield source and its issuer | asset —backed_by→ underlying asset —backed_by→ **issuer** (e.g. wstETH → stETH → Lido; USDC → Circle; EURCV → Société Générale-FORGE; WBTC → custodians), up to 4 hops, cycle-safe | `data/catalog/assets.ethereum.json`, each claim with a cited source |
| Oracle pricing the asset | market oracle **decomposed on-chain into its feeds** (Chainlink proxies, exchange-rate adapters, third-party AggregatorV3 feeds such as RedStone or Ojo), each with description, heartbeat and its source, last-update age | `MorphoChainlinkOracleV2` getters read by `eth_call` |
| AMM curve | exit route priced by a live Uniswap V3 QuoterV2 simulation (route, slippage, fees, gas, price impact, quote block shown with the recommendation); pool depth is not yet attached to the graph as a node | on-chain, current block |
| Lending market holding it | position → vault —allocated_to→ markets (with live allocation weights) → collateral and loan assets | Morpho API |

Beyond a single position, the **radar** maps about 5,000 mainnet Morpho entities, scores their health every five minutes, finds
similar vaults (hidden until an incident reveals a shared dependency), and propagates incidents to manual exposures.
Concentration (brief scope bullet): Morpho's share of Ethereum DeFi TVL and the analyzed vault's share of Morpho TVL
(DeFiLlama), with a warning above 5% (live: Morpho Blue ≈ 10% of Ethereum DeFi TVL).

Correctness rules: unknown exposure is never treated as zero; an asset with no registry entry is flagged "issuer unmodelled,
not assumed safe"; an oracle that cannot be decoded blocks a decision instead of being assumed healthy.

## 2. Impact estimation (30%)

Five scenarios, each propagated through the graph with exposure weights. The impact model depends on the mechanism:

| Scenario | Lender impact | Borrower impact | 30-day probability (live) |
|---|---|---|---|
| Stablecoin depeg | 1:1 on the held claim; **bad debt** when the depegged asset is collateral | collateral/debt revaluation, liquidation check | empirical: rolling 30-day windows over a year of Chainlink rounds (USDC/USD) |
| Collateral shock | **bad debt** of each supplied market: Σ debtᵢ × max(0, 1 − (1−s)·HFᵢ/LLTV) over live Morpho borrower positions; worst-case all-at-LLTV bound only when positions are unavailable (confidence lowered) | liquidation check against LLTV | empirical from ETH/USD, stETH/USD, WBTC/BTC × BTC/USD rounds |
| Oracle failure | loss given failure × exposure | same | empirical: share of 30-day windows in which a feed was stale beyond heartbeat + 30 min, from exact round timestamps; on-chain rate adapters are structurally 0 |
| Exit-liquidity collapse | withdrawable-liquidity stress (shortfall reported, not counted as loss) | exit pool depth stress | unavailable (no pool-depth history); lender impact is 0, so it does not block a decision |
| Protocol impairment | haircut × exposure | same | empirical industry base rate: 2 material exploit years in 76 lending/CDP protocol-years ≥ $100M TVL, 2021–2025 (DeFiLlama); Wilson interval; LOW confidence |

Every probability carries its method (*Empirical*, *Model-estimated*, *Structural*, *Unavailable*), source, sample size and
interval; every input carries its source, timestamp and freshness. The 30-day expected loss is Σ probability × impact, with
low/high bounds from the intervals.

## 3. Decision usefulness (25%)

The brief's question — "is the expected loss high enough to justify exiting despite execution costs?" — is the rule:

```
critical data missing                         → UNAVAILABLE (never STAY)
hard safety trigger (liquidation, live oracle
  breach, withdrawal shortfall under stress)  → EXIT
expected 30-day loss > exit cost + 30-day forgone yield → EXIT
otherwise                                     → STAY
```

- **Exit cost** is a live Uniswap quote: slippage, fees, gas, price impact.
- **Forgone yield** is what exiting gives up: amount × live Morpho net APY × 30/365 (exited capital assumed idle). Without it,
  a lender's gas-only exit would make any risk an EXIT.
- **Break-even**: every recommendation says at which probability the decision flips ("EXIT becomes optimal above X%;
  current estimate Y%"), so a manager sees how far the estimate is from changing the answer.
- **Book** (the brief's "ten pockets of 10%"): Position Analysis → *Book* runs every pocket, ranks pockets by expected loss
  minus the cost of exiting, and totals the book (loss if staying, cost of exiting the EXIT pockets, net benefit).

## 4. Working demo on real data (15%)

Requirements: an Ethereum mainnet RPC URL in `.env` (`ETHEREUM_RPC_URL`). A **free Alchemy key is enough**: Chainlink history
is read with Multicall3-batched `getRoundData` calls (`eth_call`), not `eth_getLogs`, so the free tier's 10-block log limit
does not apply. No Chainlink-specific endpoint or API key is needed; Morpho and DeFiLlama APIs are public.

```bash
set -a; source .env; set +a
uv run --project services/risk uvicorn app.main:app --app-dir services/risk --port 8000
RISK_API_URL=http://localhost:8000 pnpm --filter @risk0/web dev
```

The first analysis of a position takes ~10–15 s (a year of price rounds and the DeFiLlama snapshot are fetched and cached);
repeat analyses take ~1–3 s, also after a restart (SQLite cache).

### Live results (2026-10-11, UI default scenarios, 100,000 per position; 40 WETH for WETH vaults)

Default scenarios: stablecoin depeg 10%, collateral shock 15%, oracle failure 5%, exit-liquidity collapse 80%,
protocol impairment 10%. Every analyzable catalog entry reaches a decision on live data: **11 STAY, 5 EXIT,
0 UNAVAILABLE**.

| Entry | Decision | Expected 30-day loss | Exit cost + forgone yield | Why |
|---|---|---|---|---|
| Belem RWA EURCV (vault) | STAY | 21.90 EURCV | 210.51 EURCV | Protocol prior dominates; EURCV depeg 0 in 8,040 windows (EURCV/EUR from DeFiLlama ÷ Chainlink EUR/USD) |
| WETH/USDC 86% (market) | STAY | 21.90 USDC | 371.07 USDC | Collateral shock costs lenders ≈ 0: live borrower positions leave ~0 bad debt at a 15% fall |
| MEV Capital WETH (vault) | STAY | 0.01 WETH | 0.05 WETH | wstETH/WETH markets: collateral and debt share ETH, no relative fall |
| huma USDC main (vault) | STAY | 427.86 USDC | 578.38 USDC | Its PST NAV oracle missed > 1 update cycle in ~8% of recent 30-day windows (feed-quality signal), still below the cost of exiting |
| RockawayX WETH, Prime USDC, EUROP; Midas Prime; Figure (vaults) | EXIT | — | — | Hard trigger: withdrawable liquidity is already ~0 (0–2,511 units vs a 100,000 position), below 20% of the position under the stress |

The five EXITs are deliberate safety triggers, not expected-value calls: the position cannot be withdrawn now, so the
card says "Exit recommended, but full exit is currently unavailable". In a ten-pocket USDC book of 10% pockets
(Position Analysis → *Book*), the totals show this trade-off explicitly: following the triggers costs more than the
expected loss it avoids (net benefit −753.60 USDC on a 1,000,000 book), which is the number a manager overrides or
accepts.

Before the demo, run each position once to warm the cache (the first Belem vault analysis takes ~30 s; later ~5 s).

## Honest limits

- Morpho on Ethereum only; Pendle PT, perp venues and non-EVM chains are out of scope.
- Liquidity-collapse has no probability (no historical pool depth); it never counts as zero, but its lender impact is 0.
- The protocol base rate is closer to a floor than a central estimate (survivorship: failed protocols now below $100M TVL are
  not counted); it is labelled LOW confidence.
- Bad debt assumes an instantaneous gap move with no liquidation (conservative).
- Forgone yield assumes exited capital earns nothing; Morpho's net APY may include reward incentives.
