"use client"

import "@xyflow/react/dist/style.css"

import type { RiskGraph as RiskGraphData } from "@risk0/contracts"
import { Background, Controls, type Edge, MarkerType, type Node, Position, ReactFlow } from "@xyflow/react"

import { formatPercent, formatTimestamp, humanize } from "../lib/format"
import { FreshnessBadge } from "./data-quality"

const COLUMN_WIDTH = 240
const ROW_HEIGHT = 100

/**
 * Column = shortest edge distance from a position node, so the position sits
 * on the left and its dependencies fan out to the right. Nodes the position
 * cannot reach are placed in one column after the deepest reachable layer.
 */
function dependencyDepths(graph: RiskGraphData): Map<string, number> {
  const depths = new Map<string, number>()
  const queue = graph.nodes.filter((node) => node.node_type === "position").map((node) => node.node_id)
  for (const id of queue) depths.set(id, 0)
  for (let index = 0; index < queue.length; index += 1) {
    const current = queue[index]
    for (const edge of graph.edges) {
      if (edge.source !== current || depths.has(edge.target)) continue
      depths.set(edge.target, depths.get(current)! + 1)
      queue.push(edge.target)
    }
  }
  const unreachable = depths.size ? Math.max(...depths.values()) + 1 : 0
  for (const node of graph.nodes) if (!depths.has(node.node_id)) depths.set(node.node_id, unreachable)
  return depths
}

export function edgeLabel(relationship: string, exposureWeight: string | null): string {
  const label = humanize(relationship)
  return exposureWeight === null ? label : `${label} · ${formatPercent(exposureWeight)}`
}

export function layoutGraph(graph: RiskGraphData, selectedNodeId: string | null = null): { nodes: Node[]; edges: Edge[] } {
  const depths = dependencyDepths(graph)
  const rows = new Map<number, number>()
  const nodes = graph.nodes.map((node): Node => {
    const depth = depths.get(node.node_id)!
    const row = rows.get(depth) ?? 0
    rows.set(depth, row + 1)
    const selected = node.node_id === selectedNodeId
    return {
      id: node.node_id,
      position: { x: depth * COLUMN_WIDTH, y: row * ROW_HEIGHT },
      data: { label: node.label },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      className: `graph-node graph-node--${node.node_type}${selected ? " graph-node--selected" : ""}`,
      selected,
    }
  })
  const edges = graph.edges.map(
    (edge, index): Edge => ({
      id: `${edge.source}->${edge.target}:${edge.relationship}:${index}`,
      source: edge.source,
      target: edge.target,
      label: edgeLabel(edge.relationship, edge.exposure_weight),
      className: `graph-edge graph-edge--${edge.relationship}`,
      markerEnd: { type: MarkerType.ArrowClosed },
    }),
  )
  return { nodes, edges }
}

function metricText(value: string | boolean | null): string {
  if (value === null) return "—"
  return String(value)
}

export function RiskGraph({
  graph,
  selectedNodeId,
  onSelectNode,
  now,
}: {
  graph: RiskGraphData
  selectedNodeId: string | null
  onSelectNode: (nodeId: string) => void
  now: number
}) {
  const { nodes, edges } = layoutGraph(graph, selectedNodeId)
  const labels = new Map(graph.nodes.map((node) => [node.node_id, node.label]))
  const selected = graph.nodes.find((node) => node.node_id === selectedNodeId) ?? null

  return (
    <section className="panel graph" aria-labelledby="graph-heading">
      <h2 id="graph-heading">Dependency graph</h2>
      {graph.completeness_warnings.length > 0 && (
        <ul className="warnings" aria-label="Graph completeness warnings">
          {graph.completeness_warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}
      <div className="graph__layout">
        <div className="graph__canvas">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodeClick={(_, node) => onSelectNode(node.id)}
            nodesDraggable={false}
            nodesConnectable={false}
            fitView
            fitViewOptions={{ padding: 0.15 }}
          >
            <Background />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
        <section className="graph__detail" aria-label="Selected dependency">
          {selected ? (
            <>
              <h3>{selected.label}</h3>
              <dl>
                <dt>Type</dt>
                <dd>{selected.node_type}</dd>
                <dt>Source</dt>
                <dd>{selected.provenance}</dd>
                <dt>Observed</dt>
                <dd>{formatTimestamp(selected.observed_at)}</dd>
                <dt>Freshness</dt>
                <dd>
                  <FreshnessBadge
                    freshness={selected.freshness}
                    observedAt={selected.observed_at}
                    source={selected.provenance}
                    now={now}
                  />
                </dd>
              </dl>
              {Object.keys(selected.metrics).length > 0 && (
                <table className="metrics">
                  <caption>Metrics</caption>
                  <tbody>
                    {Object.entries(selected.metrics).map(([name, value]) => (
                      <tr key={name}>
                        <th scope="row">{name}</th>
                        <td>{metricText(value)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          ) : (
            <p>Select a node to inspect its metrics, source, and timestamp.</p>
          )}
        </section>
      </div>
      <div className="graph__lists">
        <ul aria-label="Dependency nodes" className="node-list">
          {graph.nodes.map((node) => (
            <li key={node.node_id}>
              <button
                type="button"
                className={`node-chip node-chip--${node.node_type}`}
                aria-pressed={node.node_id === selectedNodeId}
                onClick={() => onSelectNode(node.node_id)}
              >
                {`${node.label} (${node.node_type})`}
              </button>
            </li>
          ))}
        </ul>
        <ul aria-label="Dependency relationships" className="edge-list">
          {graph.edges.map((edge, index) => (
            <li key={`${edge.source}-${edge.target}-${index}`}>
              {`${labels.get(edge.source) ?? edge.source} → ${labels.get(edge.target) ?? edge.target}: ${edgeLabel(edge.relationship, edge.exposure_weight)}`}
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
