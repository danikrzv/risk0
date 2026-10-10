# Risk0

Risk0 is an Ethereum mainnet prototype for exploring the risk of a hypothetical
Morpho lender or borrower position. You pick a curated vault or market, describe
a position, and Risk0:

1. builds the position's dependency graph (vault → allocated markets → assets,
   oracles, protocol, chain);
2. propagates 30-day risk scenarios through that graph;
3. estimates the position's impact and expected loss per scenario;
4. prices an exit now through Uniswap (slippage, fees, gas, price impact);
5. returns an explainable `STAY`, `EXIT`, or `UNAVAILABLE` recommendation.

**Scope: read-only and non-advisory.** Risk0 never asks for a wallet, private
key, signature, or approval, and never sends a transaction. Positions are
hypothetical and are not stored. Results are a model's output for a fixed
30-day horizon, not investment advice.

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

## Running

### Fixture mode (deterministic, no network)

```bash
RISK0_DATA_MODE=fixture uv run --project services/risk \
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
RISK0_DATA_MODE=live uv run --project services/risk \
  pytest services/risk/tests/live/test_live_smoke.py -v   # opt-in live smoke test
```

- **Python** tests use recorded fixtures and injected fakes. They never touch
  the network.
- **Contracts** check that the generated TypeScript types match the JSON
  Schemas in `packages/contracts/schemas`.
- **End-to-end** (Playwright 1.56.1, headless Chromium): `apps/web/playwright.config.ts`
  starts the risk service in fixture mode on port 8710 and builds and starts
  Next on port 3710. It then runs three golden flows: a Belem RWA EURCV lender
  `STAY`, a wstETH/WETH borrower liquidated by a 15% shock (`EXIT`), and the
  degraded WBTC/USDC market (`UNAVAILABLE`, never styled as safe). Headless runs
  use Playwright's `chromium-headless-shell` build. On a new machine, run
  `pnpm --filter @risk0/web exec playwright install chromium-headless-shell` once.
- **Live smoke** is skipped unless `RISK0_DATA_MODE=live` and `ETHEREUM_RPC_URL`
  are set. It checks Morpho catalog hydration, that the RPC reports chain ID 1,
  and a WETH→USDC Uniswap quote. If no route exists, the quote check is skipped
  with the reason. Failures report only the exception type, so the RPC URL is
  never printed.

## Architecture

```text
browser ──► Next.js (apps/web) ──/api/catalog, /api/analysis──► FastAPI risk service (services/risk)
            server-side proxy routes read RISK_API_URL           ├─ adapters: Morpho API, Ethereum RPC (Chainlink), Uniswap QuoterV2, DeFiScan
                                                                 ├─ SQLite cache (snapshots 24 h, quotes 15 s, history 365 d, < 100 MB)
                                                                 ├─ graph builder → scenario engine → exit cost → recommendation
                                                                 └─ contracts: packages/contracts/schemas (JSON Schema → TS types)
```

- `apps/web`: Next.js App Router UI. It handles catalog selection, position
  form and presets, scenario controls, the `@xyflow/react` dependency graph,
  the recommendation card, and freshness and provenance display. Only its
  server-side proxy routes read `RISK_API_URL`. Upstream errors and bodies are
  never forwarded.
- `services/risk`: FastAPI service. `GET /v1/health`, `GET /v1/catalog`
  (hydrated catalog), and `POST /v1/analysis`. It owns every external adapter,
  caching, graph construction, probabilities, and the hybrid rule.
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

- **Two kinds of probability.** Stablecoin depeg and collateral shock use
  *empirical* rolling 30-day windows over hourly Chainlink history. Protocol
  impairment is *model-estimated* from protocol evidence with a demonstration
  heuristic (`protocol-evidence-v1`), not an actuarial estimate. The UI labels
  each estimate with its method.
- **Coverage gaps become UNAVAILABLE.** Only USDC (USDC/USD) and WETH (ETH/USD)
  have price-history and oracle-health feeds. EURCV (no EURCV/EUR feed), wstETH
  (its risk is the stETH/ETH ratio), WBTC, cbBTC, and other collateral have
  none. Their empirical probabilities and live oracle observations are
  unavailable rather than approximated by an unrelated feed. In live mode,
  oracle-failure and liquidity-collapse probabilities are always unavailable:
  hourly history cannot resolve a 30-minute staleness window, and no pool-depth
  history is collected. A default five-scenario live run therefore usually
  returns `UNAVAILABLE`. Live oracle breaches still act as hard triggers.
- **Oracle staleness is an interpretation.** The spec's "stale for 30 minutes"
  is read as *older than the feed's heartbeat plus 30 minutes* (ETH/USD 1 h,
  USDC/USD and EUR/USD 24 h). A literal 30 minutes would fire on healthy feeds.
  This interpretation is a deliberate choice, flagged for review.
- **DeFiSafety scores are not captured.** `data/evidence/defisafety.ethereum.json`
  records Morpho and Uniswap V3 as `not_captured`, because defisafety.com has no
  documented data endpoint. Protocol scoring relies on DeFiScan with lower
  confidence.
- **No exit-pool depth source.** Exit cost comes from a live QuoterV2 simulation
  at the current block, refreshed once if older than 30 s or 20 blocks. There
  is no historical depth, so liquidity-collapse impact cannot be weighted by an
  observed probability.
- **Fixture-mode numbers are illustrative.** In fixture mode, the probabilities,
  quotes, gas, and oracle observations are fixed demo values labelled `fixture:*`.

## Further reading

- Design spec: `docs/superpowers/specs/2026-10-07-risk0-mvp-design.md`
- Implementation plan: `docs/superpowers/plans/2026-10-07-risk0-mvp.md`
- Demo script: `docs/demo-script.md`
