# Risk0 Demo Script

A reproducible 10-minute walkthrough. It follows **select → graph → scenario →
impact → exit cost → recommendation** three times: a lender `STAY`, a borrower
`EXIT`, and a degraded-data `UNAVAILABLE`. Use fixture mode so every number
is the same on every run. The same three flows are pinned by the Playwright
tests in `apps/web/e2e/`.

## Before the demo

1. **Rotate the RPC credential** that was shared during planning, if not done
   yet. Revoke the old key with the provider and put only the new URL in your
   local `.env` (`ETHEREUM_RPC_URL=...`). Fixture mode does not need it. Live
   mode (optional, last section) does.
2. Install and verify:

   ```bash
   pnpm install
   uv sync --project services/risk
   pnpm --filter @risk0/web exec playwright test   # 3 golden flows pass
   ```

3. Start both services in fixture mode, either with Docker:

   ```bash
   RISK0_DATA_MODE=fixture docker compose up --build
   ```

   or without it:

   ```bash
   RISK0_DATA_MODE=fixture uv run --project services/risk \
     uvicorn app.main:app --app-dir services/risk --port 8000
   RISK_API_URL=http://localhost:8000 pnpm --filter @risk0/web dev
   ```

4. Open <http://localhost:3000>.

Opening line: "Risk0 is read-only and non-advisory. There is no wallet and
nothing is signed. We model a *hypothetical* position on a curated Morpho entry
and compare its 30-day expected loss with the cost of exiting now."

## Flow 1: Belem RWA EURCV lender → STAY

1. **Select.** In *1. Choose a curated vault or market*, point out the
   provenance badges. Belem RWA EURCV is *Verified Belem*. MEV Capital is
   *Belem-distributed*. The three markets are *Representative*, and the twelve
   RockawayX vaults are *RockawayX-curated*. Each of those carries a "not a
   confirmed Belem holding" note. The Base vault is "listed for reference,
   never analyzed". Each card shows live metrics, freshness, and source. In
   fixture mode the source host is `morpho.fixture.invalid`. Select **Belem
   RWA EURCV**.
2. **Position.** The form is *Lender only*. Enter **100000** as the amount.
   The numeraire is filled from the vault asset: **EURCV**.
3. **Scenario.** Keep the five defaults: stablecoin depeg 10%, collateral shock
   15%, oracle failure 5%, exit-liquidity collapse 80%, and protocol impairment
   10%. Click **Run analysis**.
4. **Graph.** In *Dependency graph*, click the **Belem RWA EURCV (vault)**
   node. Show its metrics, source, and timestamp. Walk the relationships list:
   position → vault → the six allocated EURCV markets (with exposure weights) →
   their collateral assets and oracles → Morpho protocol.
5. **Impact.** In *Scenario impacts*, each card shows impact in EURCV, the
   30-day probability range, the method label (*Empirical* or *Model-estimated*),
   confidence, and the affected path. Collateral-shock impact is limited to the
   vault's allocation to the wstETH- and WBTC-backed markets. cbBTC and the RWA
   collateral are not in the shock's asset set.
6. **Exit cost.** In the recommendation's exit cost breakdown, the lender is
   already in EURCV, so there is no swap. The route is "EURCV" and the cost is
   gas only.
7. **Recommendation.** The result is **STAY** with green, safe styling. The
   expected 30-day staying loss (about 4.3 EURCV) does not exceed the exit cost
   (about 6.7 EURCV), and no hard trigger fired. Open *Data quality*: every
   source is fresh. Say that fixture probabilities are illustrative demo values.

## Flow 2: wstETH/WETH borrower → EXIT (liquidation)

1. **Select** **wstETH collateral / WETH debt** (96.50% LLTV, Representative).
   Switch **Mode** to **Borrower**.
2. **Position.** Use the preset: 10 wstETH collateral, 11.2 WETH debt, and a
   current LTV of 90% against a 96.50% LLTV.
3. **Scenario.** Keep only **Collateral shock** at **15%** and untick the other
   four. Run the analysis.
4. **Graph.** The position depends on the market, its wstETH collateral and
   WETH debt assets, the market oracle, Morpho, and Ethereum.
5. **Impact.** The collateral-shock card lists the hard trigger
   **liquidation**: a 15% fall takes the LTV from 0.90 to about 1.06, past the
   0.965 LLTV. The affected path runs from the position through wstETH.
6. **Exit cost.** The route is wstETH → WETH @0.05%, with slippage, fee, gas,
   and price impact itemized. The quote age and block are shown.
7. **Recommendation.** The result is **EXIT** with danger styling. The trigger
   `collateral-shock:liquidation` "requires exit regardless of the cost
   comparison". The expected-loss comparison is still shown for transparency.
   Optionally, set the shock to 5% and rerun: no liquidation fires, and the
   result falls back to the cost comparison.

## Flow 3: degraded oracle data → UNAVAILABLE

1. **Select** **WBTC collateral / USDC debt** and switch to **Borrower**. The
   preset is 0.1 WBTC, 6200 USDC, and 75% LTV. In fixture mode this market
   simulates an oracle outage.
2. Keep the default scenarios and run the analysis.
3. **Recommendation.** The result is **UNAVAILABLE**, styled neutral, never
   safe, with the notice "This is not a recommendation to stay." The
   explanation names what is missing: the live oracle `0x…` and a current exit
   estimate. Confidence is `unavailable`.
4. **Exit cost.** The warnings show that the quote was 120 s old, was refreshed
   once, and is still expired. The exit cost is unknown, not zero.
5. **Data quality.** It shows stale sources. The oracle row reads *Expired · 6 h
   old* (simulated outage) and the exit quote row reads *Expired · 2 min old*.
   Key message: missing or expired critical data can never become `STAY`.

## Optional: live mode

Stop the services, make sure `.env` holds the *rotated* RPC URL, and restart
in live mode (`docker compose up --build`, or the commands in the README). Run
the smoke test first:

```bash
RISK0_DATA_MODE=live uv run --project services/risk \
  pytest services/risk/tests/live/test_live_smoke.py -v
```

Expect live numbers to differ from fixture numbers. With the default five
scenarios, live analyses are usually **UNAVAILABLE**: oracle-failure and
liquidity-collapse have no live probability, and EURCV and wstETH have no
price-history feed. Use this to show the honesty of the model rather than a
`STAY`. Single-scenario runs on WETH/USDC are the most likely to produce a
decision. See *Model limitations* in the README.

## Closing points

- Every number on screen carries its source, retrieval time, and freshness.
- Empirical and model-estimated probabilities are labelled separately.
- Unknown data is never treated as zero risk. It gives `UNAVAILABLE`.
- Read-only and non-advisory. Positions are hypothetical and never stored.
