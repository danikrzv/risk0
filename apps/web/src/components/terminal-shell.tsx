import type { ReactNode } from "react"

export function TerminalShell({
  children,
  status,
  ticker,
}: {
  children: ReactNode
  status: "live" | "degraded"
  ticker?: ReactNode
}) {
  return (
    <main className="terminal-shell">
      <header className="terminal-header">
        <div className="terminal-brand">
          <h1>Risk0</h1>
          <span>MORPHO RISK TERMINAL</span>
        </div>
        <nav className="terminal-nav" aria-label="Primary">
          <span aria-current="page">Analysis</span>
          <span aria-disabled="true">Graph</span>
          <span aria-disabled="true">Markets</span>
          <span aria-disabled="true">Oracles</span>
          <span aria-disabled="true">Incidents</span>
        </nav>
        <div className={`terminal-runtime terminal-runtime--${status}`}>
          <span className="terminal-runtime__dot" aria-hidden="true" />
          {status === "live" ? "LIVE" : "DEGRADED"}
        </div>
      </header>
      {ticker}
      <div className="terminal-workspace">{children}</div>
      <footer className="terminal-statusbar">
        <span>ENGINE v0.1.0 · LOCAL</span>
        <span>READ ONLY · 30-DAY HORIZON · NOT INVESTMENT ADVICE</span>
        <span>RPC SECRETS SERVER-SIDE</span>
      </footer>
    </main>
  )
}
