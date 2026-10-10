import "@testing-library/jest-dom/vitest"

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest"

import { NOW, analysisResponse } from "../test-fixtures"
import { RiskGraph, layoutGraph } from "./risk-graph"

beforeAll(() => {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", ResizeObserverStub)
})

afterEach(cleanup)

const graph = analysisResponse.graph

describe("RiskGraph", () => {
  it("lays out nodes deterministically by dependency depth from the position", () => {
    const { nodes, edges } = layoutGraph(graph)

    expect(nodes.map((node) => [node.id, node.position.x, node.position.y])).toEqual([
      ["position:1:borrower", 0, 0],
      ["market:1:wsteth-weth-965", 240, 0],
      ["protocol:1:morpho", 480, 0],
      ["asset:1:wsteth", 480, 100],
      ["oracle:1:0xoracle", 480, 200],
      ["chain:1:ethereum", 720, 0],
    ])
    expect(nodes[0].className).toBe("graph-node graph-node--position")
    expect(edges.map((edge) => edge.label)).toEqual([
      "borrows",
      "uses collateral · 100.00%",
      "uses oracle",
      "deployed on",
      "deployed on",
    ])
  })

  it("places nodes the position cannot reach after the deepest layer", () => {
    const detached = {
      ...graph,
      nodes: [
        ...graph.nodes,
        { ...graph.nodes[5], node_id: "pool:1:0xpool", node_type: "pool" as const, label: "Exit pool" },
      ],
    }

    const pool = layoutGraph(detached).nodes.find((node) => node.id === "pool:1:0xpool")!

    expect(pool.position).toEqual({ x: 960, y: 0 })
  })

  it("marks the selected node", () => {
    const { nodes } = layoutGraph(graph, "asset:1:wsteth")

    expect(nodes.find((node) => node.id === "asset:1:wsteth")!.className).toBe(
      "graph-node graph-node--asset graph-node--selected",
    )
  })

  it("lists every node and labelled relationship accessibly", () => {
    render(<RiskGraph graph={graph} selectedNodeId={null} onSelectNode={() => {}} now={NOW} />)

    const nodeList = screen.getByRole("list", { name: "Dependency nodes" })
    expect(within(nodeList).getAllByRole("button")).toHaveLength(6)
    expect(within(nodeList).getByRole("button", { name: /Morpho oracle/ })).toBeInTheDocument()

    const edgeList = screen.getByRole("list", { name: "Dependency relationships" })
    expect(within(edgeList).getByText("wstETH / WETH market → wstETH: uses collateral · 100.00%")).toBeInTheDocument()
    expect(within(edgeList).getByText("Borrower position → wstETH / WETH market: borrows")).toBeInTheDocument()
  })

  it("selects a node from the list", () => {
    const onSelectNode = vi.fn()
    render(<RiskGraph graph={graph} selectedNodeId={null} onSelectNode={onSelectNode} now={NOW} />)

    const nodeList = screen.getByRole("list", { name: "Dependency nodes" })
    fireEvent.click(within(nodeList).getByRole("button", { name: /Morpho oracle/ }))

    expect(onSelectNode).toHaveBeenCalledWith("oracle:1:0xoracle")
  })

  it("shows the selected node's metrics, source, timestamp, and freshness", () => {
    render(
      <RiskGraph
        graph={graph}
        selectedNodeId="market:1:wsteth-weth-965"
        onSelectNode={() => {}}
        now={NOW}
      />,
    )

    const panel = screen.getByRole("region", { name: "Selected dependency" })
    expect(within(panel).getByRole("heading", { name: "wstETH / WETH market" })).toBeInTheDocument()
    expect(within(panel).getByText("market")).toBeInTheDocument()
    expect(within(panel).getByText("available_liquidity")).toBeInTheDocument()
    expect(within(panel).getByText("1520.5")).toBeInTheDocument()
    expect(within(panel).getByText("https://api.morpho.org/markets/0xb8fc")).toBeInTheDocument()
    expect(within(panel).getByText("2026-10-10 11:55:00 UTC")).toBeInTheDocument()
    expect(within(panel).getByText(/Stale · 5 min 0 s old/)).toBeInTheDocument()
  })

  it("shows completeness warnings", () => {
    render(<RiskGraph graph={graph} selectedNodeId={null} onSelectNode={() => {}} now={NOW} />)

    expect(screen.getByText("No Uniswap exit pool was found for wstETH.")).toBeInTheDocument()
  })
})
