import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  CATALOG_ENTITY_TYPES,
  CATALOG_PROVENANCE,
  CONFIDENCE_LEVELS,
  FRESHNESS_STATUSES,
  POSITION_MODES,
  PROBABILITY_METHODS,
  RECOMMENDATION_DECISIONS,
  RISK_EDGE_TYPES,
  RISK_NODE_TYPES,
  SCENARIO_IDS,
  type AnalysisRequest,
  type ExitEstimate,
  type MetricValue,
  type PositionInput,
  type ProbabilityEstimate,
  type RecommendationDecision,
} from "./generated";

type Equal<Left, Right> =
  (<Value>() => Value extends Left ? 1 : 2) extends
  (<Value>() => Value extends Right ? 1 : 2)
    ? true
    : false;
type Assert<Value extends true> = Value;

type RecommendationDecisionIsExact = Assert<
  Equal<RecommendationDecision, "STAY" | "EXIT" | "UNAVAILABLE">
>;
type ProbabilityHorizonIsFixed = Assert<
  Equal<ProbabilityEstimate["horizon_days"], 30>
>;
type DomainMetricsExcludeNumbers = Assert<
  Equal<MetricValue, string | boolean | null>
>;
type QuoteTimestampIsRequired = Assert<Equal<ExitEstimate["quoted_at"], string>>;

const typeParityChecks: [
  RecommendationDecisionIsExact,
  ProbabilityHorizonIsFixed,
  DomainMetricsExcludeNumbers,
  QuoteTimestampIsRequired,
] = [true, true, true, true];

const lenderPosition = {
  mode: "lender",
  entity_id: "belem-rwa-eurcv",
  amount: "1000",
  numeraire: "EURCV",
} satisfies PositionInput;

const borrowerPosition = {
  mode: "borrower",
  entity_id: "weth-usdc-860",
  amount: "3000",
  collateral_amount: "2.5",
  debt_amount: "3000",
  ltv: "0.60",
  numeraire: "USDC",
} satisfies PositionInput;

// @ts-expect-error borrower fields are forbidden for lender positions
const invalidLender: PositionInput = { ...lenderPosition, debt_amount: "1" };
// @ts-expect-error borrower positions require collateral, debt, and LTV
const invalidBorrower: PositionInput = {
  mode: "borrower",
  entity_id: "weth-usdc-860",
  amount: "3000",
  numeraire: "USDC",
};
// @ts-expect-error an analysis request requires at least one scenario
const invalidRequest: AnalysisRequest = { position: lenderPosition, scenarios: [] };

const pythonModels = readFileSync(
  new URL("../../../services/risk/app/contracts/models.py", import.meta.url),
  "utf8",
);

function pythonEnumValues(enumName: string): string[] {
  const enumBlock = pythonModels.match(
    new RegExp(
      `class ${enumName}\\(StrEnum\\):\\n((?:    [A-Z][A-Z0-9_]* = "[^"]+"\\n)+)`,
    ),
  );
  if (!enumBlock) {
    throw new Error(`Unable to parse Python enum ${enumName}`);
  }
  return [...enumBlock[1].matchAll(/= "([^"]+)"/g)].map((match) => match[1]);
}

describe("generated contract parity", () => {
  it("uses exact contract shapes", () => {
    expect(typeParityChecks).toEqual([true, true, true, true]);
    expect(lenderPosition.mode).toBe("lender");
    expect(borrowerPosition.mode).toBe("borrower");
    expect(invalidLender).toBeDefined();
    expect(invalidBorrower).toBeDefined();
    expect(invalidRequest).toBeDefined();
  });

  it.each([
    ["CatalogEntityType", CATALOG_ENTITY_TYPES],
    ["CatalogProvenance", CATALOG_PROVENANCE],
    ["PositionMode", POSITION_MODES],
    ["ScenarioId", SCENARIO_IDS],
    ["RiskNodeType", RISK_NODE_TYPES],
    ["RiskEdgeType", RISK_EDGE_TYPES],
    ["FreshnessStatus", FRESHNESS_STATUSES],
    ["ConfidenceLevel", CONFIDENCE_LEVELS],
    ["ProbabilityMethod", PROBABILITY_METHODS],
    ["RecommendationDecision", RECOMMENDATION_DECISIONS],
  ] as const)("matches the Python %s enum", (enumName, generatedValues) => {
    expect([...generatedValues]).toEqual(pythonEnumValues(enumName));
  });
});
