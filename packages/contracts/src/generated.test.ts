import { describe, expect, it } from "vitest";

import {
  RECOMMENDATION_DECISIONS,
  RISK_EDGE_TYPES,
  RISK_NODE_TYPES,
  type RecommendationDecision,
  type RiskEdgeType,
  type RiskNodeType,
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
type RiskNodeTypeIsExact = Assert<
  Equal<
    RiskNodeType,
    | "position"
    | "vault"
    | "market"
    | "protocol"
    | "asset"
    | "oracle"
    | "pool"
    | "chain"
  >
>;
type RiskEdgeTypeIsExact = Assert<
  Equal<
    RiskEdgeType,
    | "allocated_to"
    | "lends"
    | "borrows"
    | "uses_collateral"
    | "uses_oracle"
    | "backed_by"
    | "exits_through"
    | "deployed_on"
  >
>;

const typeParityChecks: [
  RecommendationDecisionIsExact,
  RiskNodeTypeIsExact,
  RiskEdgeTypeIsExact,
] = [true, true, true];

describe("generated contract enums", () => {
  it("keeps recommendation decisions exact", () => {
    expect(RECOMMENDATION_DECISIONS).toEqual(["STAY", "EXIT", "UNAVAILABLE"]);
  });

  it("keeps graph node and edge values aligned with Python", () => {
    expect(RISK_NODE_TYPES).toEqual([
      "position",
      "vault",
      "market",
      "protocol",
      "asset",
      "oracle",
      "pool",
      "chain",
    ]);
    expect(RISK_EDGE_TYPES).toEqual([
      "allocated_to",
      "lends",
      "borrows",
      "uses_collateral",
      "uses_oracle",
      "backed_by",
      "exits_through",
      "deployed_on",
    ]);
    expect(typeParityChecks).toEqual([true, true, true]);
  });
});
