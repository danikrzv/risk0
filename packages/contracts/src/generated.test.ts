import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  CARRY_STATUSES,
  CATALOG_AVAILABILITIES,
  CATALOG_CHAIN_IDS,
  CATALOG_ENTITY_TYPES,
  CATALOG_PROVENANCE,
  CONFIDENCE_LEVELS,
  FRESHNESS_STATUSES,
  POSITION_MODES,
  PROBABILITY_METHODS,
  RADAR_ENTITY_TYPES,
  RADAR_RELATIONSHIP_TYPES,
  RECOMMENDATION_DECISIONS,
  RISK_EDGE_TYPES,
  RISK_NODE_TYPES,
  SCENARIO_IDS,
  HEALTH_STATUSES,
  INCIDENT_SEVERITIES,
  INCIDENT_STATUSES,
  SIMILARITY_STORAGE_THRESHOLD,
  SIMILARITY_VISIBILITY_THRESHOLD,
  SIMILARITY_VISIBLE_LIMIT,
  MAX_BOOK_POCKETS,
  type AnalysisRequest,
  type BookRequest,
  type CarryStatus,
  type BookResponse,
  type CatalogEntry,
  type CatalogResponse,
  type ExitEstimate,
  type MetricValue,
  type PositionInput,
  type ProbabilityEstimate,
  type RadarGraphResponse,
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
type CarryStatusIsExact = Assert<
  Equal<CarryStatus, "known" | "unknown" | "not_applicable">
>;
const carryStatusIsExact: CarryStatusIsExact = true;
type ProbabilityHorizonIsFixed = Assert<
  Equal<ProbabilityEstimate["horizon_days"], 30>
>;
type DomainMetricsExcludeNumbers = Assert<
  Equal<MetricValue, string | boolean | null>
>;
type QuoteTimestampIsRequired = Assert<Equal<ExitEstimate["quoted_at"], string>>;
type CatalogVersionIsFixed = Assert<
  Equal<CatalogResponse["schema_version"], "1.0.0">
>;
type CatalogChainIdsAreExact = Assert<
  Equal<CatalogEntry["chain_id"], 1 | 8453>
>;
type RadarTimestampIsString = Assert<
  Equal<RadarGraphResponse["generated_at"], string>
>;

const typeParityChecks: [
  RecommendationDecisionIsExact,
  ProbabilityHorizonIsFixed,
  DomainMetricsExcludeNumbers,
  QuoteTimestampIsRequired,
  CatalogVersionIsFixed,
  CatalogChainIdsAreExact,
  RadarTimestampIsString,
] = [true, true, true, true, true, true, true];

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
const bookRequest = {
  pockets: [{ position: lenderPosition, weight: "0.1" }],
  scenarios: [{ scenario_id: "stablecoin-depeg", severity: "0.10" }],
} satisfies BookRequest;
// @ts-expect-error a book value needs its numeraire
const invalidBook: BookRequest = { ...bookRequest, book_value: "1000000" };
type BookVersionIsFixed = Assert<Equal<BookResponse["schema_version"], "1.0.0">>;
const bookVersionIsFixed: BookVersionIsFixed = true;

// @ts-expect-error an analysis request requires at least one scenario
const invalidRequest: AnalysisRequest = { position: lenderPosition, scenarios: [] };

const pythonModels = [
  new URL("../../../services/risk/app/contracts/models.py", import.meta.url),
  new URL("../../../services/risk/app/radar/contracts.py", import.meta.url),
]
  .map((url) => readFileSync(url, "utf8"))
  .join("\n");

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

function pythonCatalogChainIds(): number[] {
  const literal = pythonModels.match(/^CatalogChainId = Literal\[([0-9, ]+)\]$/m);
  if (!literal) {
    throw new Error("Unable to parse Python CatalogChainId");
  }
  return literal[1].split(",").map((value) => Number(value.trim()));
}

describe("generated contract parity", () => {
  it("fails check mode when generated types drift from JSON Schema", () => {
    const schemaUrl = new URL(
      "../schemas/analysis-response.schema.json",
      import.meta.url,
    );
    const generatedUrl = new URL("./generated.ts", import.meta.url);
    const generatorUrl = new URL(
      "../scripts/generate-types.mjs",
      import.meta.url,
    );
    const originalSchema = readFileSync(schemaUrl, "utf8");
    const originalGenerated = readFileSync(generatedUrl, "utf8");
    const schema = JSON.parse(originalSchema);

    schema.$defs.Recommendation.properties.decision.enum.push(
      "DRIFT_SENTINEL",
    );

    try {
      writeFileSync(schemaUrl, `${JSON.stringify(schema, null, 2)}\n`);

      expect(() =>
        execFileSync(
          process.execPath,
          [fileURLToPath(generatorUrl), "--check"],
          { stdio: "pipe" },
        ),
      ).toThrow();
      expect(readFileSync(generatedUrl, "utf8")).toBe(originalGenerated);
    } finally {
      writeFileSync(schemaUrl, originalSchema);
      writeFileSync(generatedUrl, originalGenerated);
    }
  });

  it("uses exact contract shapes", () => {
    expect(typeParityChecks).toEqual([true, true, true, true, true, true, true]);
    expect(lenderPosition.mode).toBe("lender");
    expect(borrowerPosition.mode).toBe("borrower");
    expect(invalidLender).toBeDefined();
    expect(invalidBorrower).toBeDefined();
    expect(invalidRequest).toBeDefined();
    expect(bookRequest.pockets).toHaveLength(1);
    expect(invalidBook).toBeDefined();
    expect(bookVersionIsFixed).toBe(true);
  });

  it("freezes the book pocket limit shared with the Python model", () => {
    const limit = pythonModels.match(/^MAX_BOOK_POCKETS = (\d+)$/m);
    expect(limit).not.toBeNull();
    expect(MAX_BOOK_POCKETS).toBe(Number(limit![1]));
  });

  it("matches the Python CatalogChainId literal", () => {
    expect([...CATALOG_CHAIN_IDS]).toEqual(pythonCatalogChainIds());
    expect([...CATALOG_CHAIN_IDS]).toEqual([1, 8453]);
  });

  it.each([
    ["CatalogAvailability", CATALOG_AVAILABILITIES],
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
    ["CarryStatus", CARRY_STATUSES],
    ["HealthStatus", HEALTH_STATUSES],
    ["RadarEntityType", RADAR_ENTITY_TYPES],
    ["IncidentSeverity", INCIDENT_SEVERITIES],
    ["IncidentStatus", INCIDENT_STATUSES],
    ["RadarRelationshipType", RADAR_RELATIONSHIP_TYPES],
  ] as const)("matches the Python %s enum", (enumName, generatedValues) => {
    expect([...generatedValues]).toEqual(pythonEnumValues(enumName));
  });

  it("freezes decimal-string similarity policy thresholds", () => {
    expect(SIMILARITY_STORAGE_THRESHOLD).toBe("0.33");
    expect(SIMILARITY_VISIBILITY_THRESHOLD).toBe("0.60");
    expect(SIMILARITY_VISIBLE_LIMIT).toBe(10);
  });
});
