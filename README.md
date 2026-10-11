# Risk0

Risk0 is a multi-chain Morpho risk radar and an Ethereum-mainnet decision-support
prototype. It continuously maps vault, market, asset, oracle, protocol, and chain
dependencies while retaining the original lender/borrower analysis flow. Risk0:

1. builds the position's dependency graph (vault → allocated markets → assets,
   oracles, protocol, chain);
2. propagates 30-day risk scenarios through that graph;
3. estimates the position's impact and expected loss per scenario;
4. prices an exit now through Uniswap (slippage, fees, gas, price impact);
5. returns an explainable `STAY`, `EXIT`, or `UNAVAILABLE` recommendation.

Built for the Belem Capital *DeFi Risk Dependency Challenge*: see
[`docs/judging-map.md`](docs/judging-map.md) for how each judging criterion is
met and what to show live.

**Scope: read-only and non-advisory.** Risk0 never asks for a wallet, private
key, signature, or approval, and never sends a transaction. Manual exposures are
hypothetical records stored only in the local radar database. Results are a
model's output for a fixed 30-day horizon, not investment advice.

## Prerequisites

- Node.js 22+ and pnpm 11 (`corepack enable` picks the version pinned in `package.json`)
- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- For live mode: an Ethereum mainnet JSON-RPC URL (for example an Alchemy or Infura endpoint)
- Optional: Docker with Compose v2.24+ (for `docker compose up --build`)

## Setup

```bash
cp .env.example .env
pnpm install
uv sync --project services/risk
```

Put the Ethereum RPC URL in `.env` as `ETHEREUM_RPC_URL=...`. That file is the
only place it belongs: `.env` is git-ignored and Docker-ignored, only the risk
service reads it, and the URL never reaches the web app, the browser, logs, or
fixtures. **The RPC credential shared during planning must be rotated before
the demo**: create a new key with your provider, revoke the old one, and store
only the new URL in your local `.env`. Never commit a real key, and never paste
it into docs, issues, or pull requests.

`.env` variables:

| Variable | Used by | Purpose |
| --- | --- | --- |
| `ETHEREUM_RPC_URL` | risk service | Mainnet RPC for Chainlink oracle reads, gas, price history, and Uniswap quotes. |
| `MORPHO_API_URL` | risk service | Morpho public API (default `https://api.morpho.org`). |
| `RISK_API_URL` | web (server side only) | Where the Next.js proxy routes reach the risk service (default `http://localhost:8000`). |
| `RISK0_DATA_MODE` | risk service | `live` (default) or `fixture`. |
| `RISK0_RADAR_ENABLED` | risk service | Enables the continuous five-minute monitoring and hourly discovery loops. |
| `RISK0_RADAR_CHAIN_IDS` | risk service | Comma-separated chain IDs the radar discovers and monitors (default `1`, Ethereum mainnet). Invalid values stop startup. |
| `RISK0_RADAR_DB_PATH` | risk service | Optional SQLite path (default: `risk0-radar.sqlite3` beside the cache) for radar topology, history, incidents, notifications, and manual exposures. |
| `RISK0_RADAR_SEED_FIXTURES` | risk service | Fixture/demo only: seeds the deterministic healthy radar topology. |
| `RISK0_CATALOG_PATH` | risk service | Optional override for the version-controlled catalog JSON. |

## Running

### Fixture mode (deterministic, no network)

```bash
RISK0_DATA_MODE=fixture RISK0_RADAR_SEED_FIXTURES=1 uv run --project services/risk \
  uvicorn app.main:app --app-dir services/risk --port 8000
RISK_API_URL=http://localhost:8000 pnpm --filter @risk0/web dev
```

Open <http://localhost:3000>. Fixture mode needs no `.env` and no RPC URL. It
runs the same FastAPI routes, services, adapters, and contracts as live mode;
only the outermost data sources are replaced through dependency injection
(`services/risk/app/fixture_mode.py`):

- **Morpho**: the real `MorphoClient` reads recorded, sanitized public Morpho
  responses (`services/risk/tests/fixtures/morpho/`) through an in-process HTTP
  transport. Source URLs show the reserved host `morpho.fixture.invalid`, so
  fixture data never looks live.
- **Quotes, gas, prices, oracle health, probabilities**: small deterministic
  providers labelled `fixture:*`. The 30-day probabilities are illustrative
  fixture values, not estimates.
- **One degraded entry**: the WBTC/USDC market simulates an oracle outage. Its
  oracle observation is expired and its exit quote is still expired after the
  one refresh, so every analysis of it is `UNAVAILABLE`.

The Belem RWA EURCV, wstETH/WETH, WETH/USDC, and WBTC/USDC entries have
complete fixture data. The other vaults have recorded identity data only. Their
allocations or allocated markets are not recorded, so their analyses come back
`UNAVAILABLE` (unknown exposure is never treated as zero).

### Live mode

```bash
uv run --project services/risk uvicorn app.main:app --app-dir services/risk --port 8000
RISK_API_URL=http://localhost:8000 pnpm --filter @risk0/web dev
```

Live mode is the default (`RISK0_DATA_MODE=live`). It reads `ETHEREUM_RPC_URL`
from the environment: export it from `.env` first, for example
`set -a; source .env; set +a`. Without the URL the service still starts, but
oracle health, quotes, gas, and price history are missing, so analyses are
`UNAVAILABLE` and never `STAY`. The SQLite cache defaults to
`services/risk/.cache/risk0.sqlite3` (git-ignored). Set `RISK0_CACHE_PATH` to
move it.

The continuous Morpho radar is opt-in. Set `RISK0_RADAR_ENABLED=true` to run
its fast monitoring loop every 300 seconds and its slow discovery/similarity
loop every 3,600 seconds inside the FastAPI lifespan. Overlapping runs are
skipped and recorded rather than executed concurrently. Radar tables live in
their own SQLite file next to the cache (`risk0-radar.sqlite3` by default); set
`RISK0_RADAR_DB_PATH` to move it. Keep the two apart: the cache enforces its
100 MB cap on its whole file. Migrations run idempotently at service startup. Only chains listed
in `RISK0_RADAR_CHAIN_IDS` (default: Ethereum mainnet) are discovered and
monitored; open incidents on other chains are resolved. A snapshot or health
evaluation is stored only when the observed state changes, plus an hourly
heartbeat so every hour keeps a sample. Each discovery cycle also aggregates
completed hours and applies retention: raw snapshots and superseded health
history are retained for 30 days; hourly aggregates are kept for 365 days;
incident records are retained. Missing, incomplete, or expired data makes an
entity `UNKNOWN` but never opens an incident or notification; only observed
`WARNING`/`CRITICAL` health does. A cycle emits at most one `graph.updated`
event and a bounded number of incident and notification events, coalescing the
rest into one summary event. Fixture and test runs leave the
scheduler disabled unless explicitly enabled.

### Docker

```bash
docker compose up --build                            # live mode, reads .env
RISK0_DATA_MODE=fixture docker compose up --build    # fixture mode
```

- `risk` (port 8000) is built from `services/risk/Dockerfile`. It is the only
  service with `env_file: .env`. `.env` is optional, so fixture mode works
  without it. The health check calls `/v1/health`. The cache lives on the
  `risk0-cache` named volume, not in the repository.
- `web` (port 3000) is built from `apps/web/Dockerfile`. It runs `next start`
  with `RISK_API_URL=http://risk:8000` and starts only after `risk` is healthy.
- `.dockerignore` keeps `.env`, caches, virtualenvs, and `node_modules` out of
  the build context.

## Tests

```bash
uv run --project services/risk pytest services/risk/tests -m "not live" -v   # Python unit + API tests
pnpm test                                                 # contracts + web (Vitest)
pnpm --filter @risk0/web exec playwright test             # golden end-to-end flows
pnpm --filter @risk0/web test:e2e:scale                   # browser scale regression (live-sized radar)
RISK0_DATA_MODE=live uv run --project services/risk \
  pytest services/risk/tests/live/test_live_smoke.py -v   # opt-in live smoke test
```

- **Python** tests use recorded fixtures and injected fakes. They never touch
  the network.
- **Contracts** check that the generated TypeScript types match the JSON
  Schemas in `packages/contracts/schemas`.
- **End-to-end** (Playwright 1.56.1, headless Chromium): `apps/web/playwright.config.ts`
  starts the risk service in fixture mode on port 8710 and builds and starts
  Next on port 3710. It then runs five golden flows: a healthy radar with manual
  exposure, resolved oracle propagation revealing a hidden neighbor, a Belem RWA EURCV lender
  `STAY`, a wstETH/WETH borrower liquidated by a 15% shock (`EXIT`), and the
  degraded WBTC/USDC market (`UNAVAILABLE`, never styled as safe). Headless runs
  use Playwright's `chromium-headless-shell` build. On a new machine, run
  `pnpm --filter @risk0/web exec playwright install chromium-headless-shell` once.
- **Scale** (`apps/web/playwright.scale.config.ts`, specs in `apps/web/e2e-scale`):
  its own risk service and Next servers on ports 8720 and 3720
  (`RISK0_E2E_SCALE_RISK_PORT`, `RISK0_E2E_SCALE_WEB_PORT`) and its own temp radar
  database, seeded by `services/risk/scripts/seed_synthetic_radar.py` with a
  live-sized synthetic radar (~20k entities, ~50k relationships, ~20k open
  incidents; `app/radar/synthetic.py`). It checks that `/api/graph` stays under
  500 kB, the canvas draws at most 400 nodes within 10 s, the incident rail lists
  at most 50 incidents, and selection, search, and a 5,000-vault incident stay
  responsive. The golden flows never see this data.
- **Live smoke** is skipped unless `RISK0_DATA_MODE=live` and `ETHEREUM_RPC_URL`
  are set. It checks Morpho catalog hydration, that the RPC reports chain ID 1,
  and a WETH→USDC Uniswap quote. If no route exists, the quote check is skipped
  with the reason. Failures report only the exception type, so the RPC URL is
  never printed.

## Architecture

```text
browser ──► Next.js (apps/web) ──/api/* proxies + SSE──► FastAPI risk service (services/risk)
            server-side proxy routes read RISK_API_URL           ├─ adapters: Morpho API, Ethereum RPC (Chainlink), Uniswap QuoterV2, DeFiScan
                                                                 ├─ SQLite cache + radar topology/history/incidents/exposures
                                                                 ├─ discovery → health → incidents → reverse propagation
                                                                 ├─ graph builder → scenario engine → exit cost → recommendation
                                                                 └─ contracts: packages/contracts/schemas (JSON Schema → TS types)
```

- `apps/web`: Next.js App Router Institutional Terminal. It displays the radar
  graph, manual exposures, active incidents, transient toasts, persistent
  notification history, and the existing position/scenario analysis. Only its
  server-side proxy routes read `RISK_API_URL`. Upstream errors and bodies are
  never forwarded.
- `services/risk`: FastAPI service. In addition to catalog and analysis, it
  exposes `/v1/graph`, incident subgraphs, entity history, similarities,
  notifications, manual exposures, and `/v1/events` SSE. It owns every external
  adapter, scheduler, cache, health rule, propagation algorithm, probability
  estimator, and hybrid recommendation rule.
- `packages/contracts`: versioned JSON Schemas for the service boundary and the
  TypeScript types generated from them.
- `data/`: version-controlled catalog (`catalog/ethereum.json`), Chainlink feed
  list, default scenarios, and the DeFiSafety evidence snapshot.

The hybrid rule: missing or expired critical data (entity snapshot, vault
allocations, the live observation of every graph oracle, the exit estimate)
gives `UNAVAILABLE`. A hard trigger (liquidation, live oracle breach, protocol
emergency, liquidity threshold) gives `EXIT`. Otherwise Risk0 compares the
30-day expected loss with the cost of exiting now. A probability that is
unavailable for a scenario with non-zero impact also gives `UNAVAILABLE`.

## Catalog provenance

The catalog (`data/catalog/ethereum.json`) holds **17 entries**. Every entry
shows its provenance in the UI:

- **Belem RWA EURCV**: verified Belem-named, listed Morpho Vault V2 (also
  RockawayX-curated).
- **MEV Capital wETH**: a public MEV Capital vault. MEV publicly states that
  its fund is distributed by Belem. This is not a confirmed Belem holding.
- **wstETH/WETH (96.5% LLTV), WETH/USDC (86%), WBTC/USDC (86%)**: representative
  Morpho markets chosen as proxies. They are *not* confirmed Belem holdings.
- **12 RockawayX-curated Morpho Vault V2s** from the RockawayX Dune dashboard,
  verified against the Morpho API on 2026-10-10. They are *not* confirmed
  Belem holdings. One of them, **RockawayX Midas USDC Prime (Base)**, is on Base
  (chain 8453). It is listed and hydrated for display only and is never
  analyzed, because the oracle, Uniswap, and Ethereum adapters are mainnet-only.

Nothing in Risk0 implies that the representative or RockawayX-curated entries
are Belem positions. There is no wallet discovery. Analysis is limited to these
version-controlled entries.

## Model limitations

- **Local MVP persistence.** Radar data is held in SQLite and the scheduler runs
  in the API process. This is appropriate for one local instance, not a
  horizontally scaled production deployment. There is no broker, distributed
  lock, managed backup, authentication, or external notification channel yet.
- **Graph visibility is policy, not coverage.** At most ten neighbors above 60%
  similarity are normally shown per catalog vault. Neighbors above 33% remain
  processed and hidden until an incident path reveals them. A hidden node is not
  ignored by the backend.

- **Probability methods are labelled.** Stablecoin depeg, collateral shock, and
  oracle failure are *empirical*: rolling 30-day windows over a year of Chainlink
  rounds read on-chain (Multicall3-batched `getRoundData`, so a free-tier RPC is
  enough). Oracle failure is staleness beyond the feed's heartbeat plus 30
  minutes, from exact round timestamps; an on-chain exchange-rate adapter cannot
  be stale (*structural* 0). Protocol impairment is an empirical *industry base
  rate* from DeFiLlama (lending/CDP protocols ≥ $100M TVL, 2021–2025), labelled
  LOW confidence because DeFiScan has no Morpho review and no DeFiSafety score is
  captured. Liquidity collapse has no probability (no pool-depth history).
- **Heartbeats.** Documented heartbeats (ETH/USD, stETH/USD, BTC/USD 1 h;
  USDC/USD, EUR/USD, WBTC/BTC, BTC/ETH 24 h) are strict. An undocumented feed's
  heartbeat is inferred from its last 90 days of rounds, tolerates one missed
  cycle, is judged over the same 90 days, and caps confidence at LOW. A
  third-party feed with no published heartbeat and no readable history is
  reported with its age but never judged stale.
- **Lender losses are bad debt.** A collateral price fall costs a lender only the
  bad debt of the markets it supplies, from live Morpho borrower positions under
  an instantaneous gap with no liquidation (conservative); without positions, an
  explicit all-at-LLTV worst-case bound with lowered confidence.
- **The decision counts forgone yield.** EXIT requires the expected 30-day loss
  to exceed the exit cost plus the 30-day yield given up (live Morpho net APY;
  exited capital assumed idle). An unknown APY counts as 0 (toward EXIT) with a
  warning and lower confidence.
- **Coverage gaps stay UNAVAILABLE.** Assets without a price-history feed (e.g.
  EURCV) and oracles that cannot be decoded are reported as unavailable, never
  approximated by an unrelated feed or assumed healthy.
- **No exit-pool depth history.** Exit cost comes from a live QuoterV2 simulation
  at the current block, refreshed once if older than 30 s or 20 blocks.
- **Fixture-mode numbers are illustrative.** In fixture mode, the probabilities,
  quotes, gas, and oracle observations are fixed demo values labelled `fixture:*`.

## Further reading

- Design spec: `docs/superpowers/specs/2026-10-07-risk0-mvp-design.md`
- Implementation plan: `docs/superpowers/plans/2026-10-07-risk0-mvp.md`
- Belem challenge judging map: `docs/judging-map.md`
- Demo script: `docs/demo-script.md`
- Algorithms and formulas: `docs/risk0-algorithms.tex`
