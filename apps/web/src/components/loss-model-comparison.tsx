"use client"

import type { AnalysisResponse, MetricValue } from "@risk0/contracts"
import { useMemo } from "react"

import { formatPercent, formatQuantity } from "../lib/format"
import { scenarioModelView, solvencyView } from "../lib/loss-models"

const SCENARIO_LABELS: Record<string, string> = {
  B_degraded: "B · degraded normal",
  A_systemic: "A · systemic (bank run)",
  unavailable: "Unavailable",
}

function amount(value: MetricValue | string | null | undefined, unit = ""): string {
  if (value === null || value === undefined || typeof value === "boolean") return "—"
  const formatted = formatQuantity(String(value))
  return unit ? `${formatted} ${unit}` : formatted
}

function percent(value: MetricValue | string | null | undefined, digits = 2): string {
  if (value === null || value === undefined || typeof value === "boolean") return "—"
  return formatPercent(String(value), digits)
}

/**
 * The two lender-loss models side by side: the scenario model (probability-
 * weighted 30-day stress, which drives STAY/EXIT) and the deterministic solvency
 * model (what liquidating today's at-risk collateral would leave unrecovered).
 */
export function LossModelComparison({ analysis, numeraire }: { analysis: AnalysisResponse; numeraire: string }) {
  const scenario = useMemo(() => scenarioModelView(analysis), [analysis])
  const solvency = useMemo(() => solvencyView(analysis.graph), [analysis])
  const shock = scenario.collateralShock
  const notApplicable = solvency?.model === "not_applicable"

  return (
    <section className="panel loss-models" aria-labelledby="loss-models-heading">
      <span className="eyebrow">LENDER LOSS</span>
      <h2 id="loss-models-heading">Two loss models</h2>
      <div className="loss-models__grid">
        <section className="loss-models__model" aria-labelledby="loss-model-scenario">
          <h3 id="loss-model-scenario">Scenario model <small>drives STAY / EXIT</small></h3>
          <p className="loss-models__method">
            Probability-weighted 30-day stress: under the selected collateral price fall, the bad debt of the
            supplied markets from each borrower&apos;s health; the probability comes from a year of price history.
          </p>
          <dl>
            <dt>Collateral-shock bad debt</dt>
            <dd>{shock ? amount(shock.impact, numeraire) : "Collateral shock not selected"}</dd>
            <dt>30-day probability</dt>
            <dd>{shock?.probability.central != null ? percent(shock.probability.central) : "—"}</dd>
            <dt>Contribution (p × impact)</dt>
            <dd>{amount(scenario.collateralContribution, numeraire)}</dd>
            <dt>Expected 30-day loss, all scenarios</dt>
            <dd><strong>{amount(scenario.expectedLoss, numeraire)}</strong> · {scenario.decision}</dd>
          </dl>
        </section>

        <section className="loss-models__model" aria-labelledby="loss-model-solvency">
          <h3 id="loss-model-solvency">Solvency model <small>deterministic, now</small></h3>
          <p className="loss-models__method">
            If the collateral in the liquidation zone (LTV ≥ 95% of LLTV) were liquidated now: with exit depth R,
            branch B (Q ≤ R/2) recovers Q·P<sub>real</sub>·(R/(R+Q))², branch A (Q &gt; R/2, bank run) recovers a fixed
            share. No probability: this is a loss level, not an expectation, and it does not change the decision.
          </p>
          {!solvency ? (
            <p className="loss-models__none">The solvency model did not run for this analysis.</p>
          ) : notApplicable ? (
            <p className="loss-models__none">Not applicable to a borrower position (it prices supplier losses).</p>
          ) : (
            <>
              <dl>
                <dt>Loss if liquidated now</dt>
                <dd><strong>{amount(solvency.loss, numeraire)}</strong></dd>
                <dt>Branch</dt>
                <dd data-solvency-scenario={solvency.scenario ?? "unavailable"}>
                  {SCENARIO_LABELS[solvency.scenario ?? "unavailable"] ?? solvency.scenario}
                  {solvency.vulnerable ? " · vulnerable" : ""}
                </dd>
                <dt>Your share of the pool (W<sub>pool</sub>)</dt>
                <dd>{percent(solvency.wPool, 4)}</dd>
                {solvency.vault ? (
                  <>
                    <dt>Vault bad debt</dt>
                    <dd>{amount(solvency.vault.badDebt)} ({percent(solvency.vault.badDebtShare)} of its assets)</dd>
                  </>
                ) : null}
                <dt>Bank-run recovery (assumption)</dt>
                <dd>{percent(solvency.rPanic, 0)}</dd>
              </dl>
              {solvency.markets.length > 0 ? (
                <div className="loss-models__table-wrap">
                  <table className="loss-models__table">
                    <caption>Per market (highest supplier loss first)</caption>
                    <thead>
                      <tr>
                        <th scope="col">Market</th>
                        <th scope="col">Branch</th>
                        <th scope="col">In zone Q (borrowers)</th>
                        <th scope="col">Exit depth R</th>
                        <th scope="col">Q ÷ (R/2)</th>
                        <th scope="col">P<sub>contract</sub> / P<sub>real</sub></th>
                        <th scope="col">Bad debt</th>
                        <th scope="col">Supplier loss</th>
                      </tr>
                    </thead>
                    <tbody>
                      {solvency.markets.map((row) => (
                        <tr key={row.id} data-solvency-scenario={row.scenario ?? "unavailable"}>
                          <th scope="row">
                            {row.label}
                            {row.vaultShare !== null ? <small> · vault holds {percent(row.vaultShare, 4)} of supply</small> : null}
                            {row.missing ? <small> · missing: {row.missing}</small> : null}
                          </th>
                          <td>{SCENARIO_LABELS[row.scenario ?? "unavailable"] ?? row.scenario}</td>
                          <td>{amount(row.q)} ({amount(row.borrowersInZone)}){row.qBound ? " · bound" : ""}</td>
                          <td>{amount(row.r)}</td>
                          <td>{amount(row.qOverHalfR)}</td>
                          <td>
                            {amount(row.pContract)} / {amount(row.pReal)}
                            {row.priceDeviation !== null ? <small> ({percent(row.priceDeviation)})</small> : null}
                          </td>
                          <td>
                            {row.badDebt === null && row.scenario === "A_systemic"
                              ? <small>n/a: fixed recovery</small>
                              : amount(row.badDebt)}
                          </td>
                          <td>{percent(row.supplierLossFraction)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : null}
              {solvency.sources.length > 0 ? (
                <details className="loss-models__sources">
                  <summary>Where each solvency input comes from</summary>
                  <ul>
                    {solvency.sources.filter((item) => item.provenance.kind !== "derived").map((item) => (
                      <li key={`${item.node}:${item.metric}`}>
                        <code>{item.metric}</code> ({item.node}): {item.provenance.kind} · {item.provenance.source}
                        {item.provenance.fetched_at ? ` · ${item.provenance.fetched_at}` : ""}
                        {item.provenance.block_number != null ? ` · block ${item.provenance.block_number}` : ""}
                      </li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </>
          )}
        </section>
      </div>
      <p className="loss-models__note">
        <strong>Why they can differ:</strong> the scenario model asks how likely a price fall is and what bad debt it
        creates; the solvency model ignores likelihood and asks whether today&apos;s at-risk collateral could be sold
        through the available exit liquidity. A large solvency loss next to a small scenario loss means liquidity, not
        price, is the risk. Only the scenario model feeds the STAY / EXIT decision.
      </p>
    </section>
  )
}
