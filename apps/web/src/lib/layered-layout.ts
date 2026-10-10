// Small deterministic layered (Sugiyama-style) layout for dependency graphs.
//
// Edges point from a dependent to its dependency (vault → market → oracle), so
// layers run left to right. Steps:
//   1. layering: longest path from the sources; cycles are broken by taking
//      the earliest unplaced node in input order;
//   2. ordering: barycenter sweeps (down, then up) to reduce edge crossings;
//   3. wrapping: a layer taller than `maxRows` is split into side-by-side
//      sub-columns, so hundreds of markets never form one endless column.
// The same input always yields the same positions.

export interface LayoutEdge {
  source: string
  target: string
}

export interface LayoutPosition {
  x: number
  y: number
}

export interface LayeredLayoutOptions {
  columnWidth?: number
  rowHeight?: number
  /** Extra horizontal space between layers (on top of `columnWidth`). */
  layerGap?: number
  /** Nodes per sub-column before a layer wraps; defaults from the node count. */
  maxRows?: number
  /** Layer for nodes without any edge, e.g. a type depth; clamped to the deepest layer. */
  rankHint?: (id: string) => number
  sweeps?: number
}

const DEFAULTS = { columnWidth: 230, rowHeight: 90, layerGap: 70, sweeps: 4 }

export function defaultMaxRows(nodeCount: number): number {
  return Math.max(8, Math.ceil(Math.sqrt(nodeCount) * 1.2))
}

export function layeredLayout(
  inputIds: readonly string[],
  edges: readonly LayoutEdge[],
  options: LayeredLayoutOptions = {},
): Map<string, LayoutPosition> {
  const { columnWidth, rowHeight, layerGap, sweeps } = { ...DEFAULTS, ...options }
  const nodeIds = [...new Set(inputIds)]
  const count = nodeIds.length
  const positions = new Map<string, LayoutPosition>()
  if (count === 0) return positions
  const maxRows = Math.max(1, options.maxRows ?? defaultMaxRows(count))

  const index = new Map(nodeIds.map((id, position) => [id, position]))
  const outgoing: number[][] = Array.from({ length: count }, () => [])
  const incoming: number[][] = Array.from({ length: count }, () => [])
  const seen = new Set<string>()
  for (const edge of edges) {
    const source = index.get(edge.source)
    const target = index.get(edge.target)
    if (source === undefined || target === undefined || source === target) continue
    const key = `${source}:${target}`
    if (seen.has(key)) continue
    seen.add(key)
    outgoing[source].push(target)
    incoming[target].push(source)
  }

  // 1. Longest-path layering (Kahn's algorithm, cycles broken in input order).
  const layer = new Array<number>(count).fill(0)
  const remaining = incoming.map((list) => list.length)
  const placed = new Array<boolean>(count).fill(false)
  const queue: number[] = []
  for (let node = 0; node < count; node += 1) if (remaining[node] === 0) queue.push(node)
  let head = 0
  let placedCount = 0
  let nextUnplaced = 0
  while (placedCount < count) {
    if (head === queue.length) {
      while (placed[nextUnplaced]) nextUnplaced += 1
      queue.push(nextUnplaced)
    }
    const node = queue[head]
    head += 1
    if (placed[node]) continue
    placed[node] = true
    placedCount += 1
    for (const next of outgoing[node]) {
      if (placed[next]) continue
      layer[next] = Math.max(layer[next], layer[node] + 1)
      remaining[next] -= 1
      if (remaining[next] === 0) queue.push(next)
    }
  }

  const isolated = (node: number) => outgoing[node].length === 0 && incoming[node].length === 0
  let deepest = 0
  for (let node = 0; node < count; node += 1) if (!isolated(node)) deepest = Math.max(deepest, layer[node])
  const anyConnected = layer.some((_, node) => !isolated(node))
  if (options.rankHint) {
    for (let node = 0; node < count; node += 1) {
      if (!isolated(node)) continue
      const hint = Math.max(0, Math.floor(options.rankHint(nodeIds[node])))
      layer[node] = anyConnected ? Math.min(hint, deepest) : hint
    }
  }

  const layerCount = layer.reduce((max, value) => Math.max(max, value), 0) + 1
  const layers: number[][] = Array.from({ length: layerCount }, () => [])
  for (let node = 0; node < count; node += 1) layers[layer[node]].push(node)

  // 2. Barycenter ordering: normalized rank within the layer, 0..1.
  const rank = new Array<number>(count).fill(0)
  const updateRanks = (members: number[]) => {
    const span = Math.max(1, members.length - 1)
    members.forEach((node, position) => {
      rank[node] = members.length === 1 ? 0.5 : position / span
    })
  }
  for (const members of layers) updateRanks(members)
  const reorder = (members: number[], neighbors: (node: number) => number[], keep: (other: number) => boolean) => {
    const keyed = members.map((node, position) => {
      const related = neighbors(node).filter(keep)
      const key = related.length === 0
        ? rank[node]
        : related.reduce((sum, other) => sum + rank[other], 0) / related.length
      return { node, key, position }
    })
    keyed.sort((left, right) => left.key - right.key || left.position - right.position)
    keyed.forEach((item, position) => {
      members[position] = item.node
    })
    updateRanks(members)
  }
  for (let sweep = 0; sweep < sweeps; sweep += 1) {
    for (let current = 1; current < layerCount; current += 1) {
      reorder(layers[current], (node) => incoming[node], (other) => layer[other] < current)
    }
    for (let current = layerCount - 2; current >= 0; current -= 1) {
      reorder(layers[current], (node) => outgoing[node], (other) => layer[other] > current)
    }
  }

  // 3. Wrap tall layers into sub-columns and center each layer vertically.
  const tallest = Math.min(maxRows, layers.reduce((max, members) => Math.max(max, members.length), 0))
  let x = 0
  for (const members of layers) {
    if (members.length === 0) continue
    const rows = Math.min(maxRows, members.length)
    const columns = Math.ceil(members.length / rows)
    const offset = ((tallest - rows) * rowHeight) / 2
    members.forEach((node, position) => {
      const column = Math.floor(position / rows)
      const row = position % rows
      positions.set(nodeIds[node], { x: x + column * columnWidth, y: offset + row * rowHeight })
    })
    x += columns * columnWidth + layerGap
  }
  return positions
}
