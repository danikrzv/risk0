import { describe, expect, it } from "vitest"

import { layeredLayout } from "./layered-layout"

const options = { columnWidth: 100, rowHeight: 10, layerGap: 0 }

describe("layeredLayout", () => {
  it("uses sparse default separation while preserving deterministic ordering", () => {
    const ids = ["vault", "market-a", "market-b"]
    const edges = [
      { source: "vault", target: "market-a" },
      { source: "vault", target: "market-b" },
    ]
    const first = layeredLayout(ids, edges)
    const second = layeredLayout(ids, edges)

    expect([...first.entries()]).toEqual([...second.entries()])
    expect(first.get("market-a")!.x - first.get("vault")!.x).toBe(360)
    expect(Math.abs(first.get("market-b")!.y - first.get("market-a")!.y)).toBe(110)
  })

  it("places dependents left of their dependencies by longest path", () => {
    const positions = layeredLayout(
      ["vault", "market", "oracle", "protocol", "chain"],
      [
        { source: "vault", target: "market" },
        { source: "market", target: "oracle" },
        { source: "market", target: "protocol" },
        { source: "protocol", target: "chain" },
        { source: "market", target: "chain" },
      ],
      options,
    )

    expect(positions.get("vault")!.x).toBe(0)
    expect(positions.get("market")!.x).toBe(100)
    expect(positions.get("oracle")!.x).toBe(200)
    expect(positions.get("protocol")!.x).toBe(200)
    // Longest path: chain sits after protocol even though market also points at it.
    expect(positions.get("chain")!.x).toBe(300)
  })

  it("is deterministic and survives cycles, self loops, duplicates, and unknown endpoints", () => {
    const ids = ["a", "b", "c", "a"]
    const edges = [
      { source: "a", target: "b" },
      { source: "b", target: "c" },
      { source: "c", target: "a" },
      { source: "a", target: "a" },
      { source: "a", target: "b" },
      { source: "a", target: "missing" },
    ]
    const first = layeredLayout(ids, edges, options)
    const second = layeredLayout(ids, edges, options)

    expect([...first.entries()]).toEqual([...second.entries()])
    expect(first.size).toBe(3)
    expect(new Set([...first.values()].map((position) => position.x)).size).toBe(3)
  })

  it("wraps a tall layer into sub-columns instead of one endless column", () => {
    const markets = Array.from({ length: 100 }, (_, index) => `market:${index}`)
    const positions = layeredLayout(
      ["vault", ...markets],
      markets.map((market) => ({ source: "vault", target: market })),
      { ...options, maxRows: 10 },
    )

    const marketPositions = markets.map((market) => positions.get(market)!)
    expect(Math.max(...marketPositions.map((position) => position.y))).toBe(90)
    expect(new Set(marketPositions.map((position) => position.x)).size).toBe(10)
    expect(Math.min(...marketPositions.map((position) => position.x))).toBeGreaterThan(positions.get("vault")!.x)
  })

  it("orders a layer by its neighbors to reduce crossings", () => {
    // Input order would cross both edges; barycenter ordering untangles them.
    const positions = layeredLayout(
      ["v1", "v2", "m2", "m1"],
      [
        { source: "v1", target: "m1" },
        { source: "v2", target: "m2" },
      ],
      options,
    )

    expect(positions.get("m1")!.y).toBeLessThan(positions.get("m2")!.y)
  })

  it("puts isolated nodes on their hinted layer", () => {
    const positions = layeredLayout(
      ["vault", "market", "oracle", "lonely-oracle"],
      [
        { source: "vault", target: "market" },
        { source: "market", target: "oracle" },
      ],
      { ...options, rankHint: (id) => (id.includes("oracle") ? 2 : 0) },
    )

    expect(positions.get("lonely-oracle")!.x).toBe(positions.get("oracle")!.x)
  })
})
