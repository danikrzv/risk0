import "@testing-library/jest-dom/vitest"

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest"

import { NOW, analysisResponse } from "../test-fixtures"
import { RiskGraph, edgeLabel, layoutGraph } from "./risk-graph"

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
    expect(within(panel).getByText(/Stale · 5 min old/)).toBeInTheDocument()
  })

  it("shows completeness warnings", () => {
    render(<RiskGraph graph={graph} selectedNodeId={null} onSelectNode={() => {}} now={NOW} />)

    expect(screen.getByText("No Uniswap exit pool was found for wstETH.")).toBeInTheDocument()
  })

  it("adds the solvency loss share to edge labels when present", () => {
    expect(edgeLabel("lends", null, "0.01")).toBe("lends · loss share 1.00%")
    expect(edgeLabel("allocated_to", "0.5", "0.2")).toBe("allocated to · 50.00% · loss share 20.00%")
    expect(edgeLabel("lends", null, null)).toBe("lends")
    expect(edgeLabel("lends", null)).toBe("lends")
  })

  it("shows the solvency result and each metric's own source", () => {
    const solvencyGraph = {
      ...graph,
      nodes: graph.nodes.map((node) =>
        node.node_id === "market:1:wsteth-weth-965"
          ? {
              ...node,
              metrics: {
                ...node.metrics,
                solvency_scenario: "B_degraded",
                solvency_q: "12.5",
                solvency_bad_debt: "0",
              },
              metric_provenance: {
                solvency_q: {
                  kind: "data" as const,
                  source: "https://api.morpho.org/graphql",
                  fetched_at: "2026-10-10T11:58:00Z",
                  freshness: "fresh" as const,
                  block_number: 21000000,
                },
                solvency_bad_debt: { kind: "derived" as const, source: "risk0 solvency model" },
              },
            }
          : node,
      ),
    }
    render(
      <RiskGraph graph={solvencyGraph} selectedNodeId="market:1:wsteth-weth-965" onSelectNode={() => {}} now={NOW} />,
    )

    const panel = screen.getByRole("region", { name: "Selected dependency" })
    expect(within(panel).getByText("Solvency: B degraded")).toBeInTheDocument()
    const row = within(panel).getByText("solvency_q").closest("tr")!
    expect(within(row).getByText("12.5")).toBeInTheDocument()
    expect(within(row).getByText("data · https://api.morpho.org/graphql · block 21000000")).toBeInTheDocument()
    const derived = within(panel).getByText("solvency_bad_debt").closest("tr")!
    expect(within(derived).getByText("derived · risk0 solvency model")).toBeInTheDocument()
  })

  it("flags a vulnerable lender position and a non-applicable borrower", () => {
    const lender = {
      ...graph,
      nodes: graph.nodes.map((node) =>
        node.node_type === "position"
          ? {
              ...node,
              metrics: {
                ...node.metrics,
                solvency_scenario: "A_systemic",
                solvency_expected_loss: "7000",
                solvency_vulnerable: true,
              },
            }
          : node,
      ),
    }
    const { unmount } = render(
      <RiskGraph graph={lender} selectedNodeId="position:1:borrower" onSelectNode={() => {}} now={NOW} />,
    )
    expect(screen.getByText("Solvency: A systemic · expected loss 7,000 · vulnerability alert")).toBeInTheDocument()
    unmount()

    const borrower = {
      ...graph,
      nodes: graph.nodes.map((node) =>
        node.node_type === "position"
          ? { ...node, metrics: { ...node.metrics, solvency_model: "not_applicable" } }
          : node,
      ),
    }
    render(<RiskGraph graph={borrower} selectedNodeId="position:1:borrower" onSelectNode={() => {}} now={NOW} />)
    expect(screen.getByText("Solvency model: not applicable to borrowers")).toBeInTheDocument()
  })
})
