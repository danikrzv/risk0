import type { ReactNode } from "react"

export type TerminalStatus = "connecting" | "live" | "degraded"
export type WorkspaceCategory = "graph" | "markets" | "oracles" | "incidents" | "position-analysis"

const WORKSPACE_CATEGORIES: ReadonlyArray<{ id: WorkspaceCategory; label: string }> = [
  { id: "graph", label: "Graph" },
  { id: "markets", label: "Markets" },
  { id: "oracles", label: "Oracles" },
  { id: "incidents", label: "Incidents" },
  { id: "position-analysis", label: "Position Analysis" },
]

const STATUS_LABELS: Record<TerminalStatus, string> = {
  connecting: "CONNECTING",
  live: "LIVE",
  degraded: "DEGRADED",
}

export function TerminalShell({
  children,
  status,
  ticker,
  activeCategory,
  onCategoryChange,
}: {
  children: ReactNode
  status: TerminalStatus
  ticker?: ReactNode
  activeCategory: WorkspaceCategory
  onCategoryChange: (category: WorkspaceCategory) => void
}) {
  return (
    <div className="terminal-shell">
      <header className="terminal-header">
        <div className="terminal-brand">
          <h1>Risk0</h1>
          <span>MORPHO RISK TERMINAL</span>
        </div>
        <nav className="terminal-nav" aria-label="Primary">
          {WORKSPACE_CATEGORIES.map((category) => (
            <button
              key={category.id}
              type="button"
              aria-current={activeCategory === category.id ? "page" : undefined}
              onClick={() => onCategoryChange(category.id)}
            >
              {category.label}
            </button>
          ))}
        </nav>
        <div className={`terminal-runtime terminal-runtime--${status}`}>
          <span className="terminal-runtime__dot" aria-hidden="true" />
          {STATUS_LABELS[status]}
        </div>
      </header>
      {ticker}
      <main className="terminal-workspace">{children}</main>
      <footer className="terminal-statusbar">
        <span>ENGINE v0.1.0 · LOCAL</span>
        <span>READ ONLY · 30-DAY HORIZON · NOT INVESTMENT ADVICE</span>
        <span>RPC SECRETS SERVER-SIDE</span>
      </footer>
    </div>
  )
}
