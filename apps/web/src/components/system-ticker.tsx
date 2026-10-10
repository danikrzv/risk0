type CatalogStatus = "loading" | "ready" | "error"
type AnalysisStatus = "idle" | "running" | "error" | "done"

const ANALYSIS_LABELS: Record<AnalysisStatus, string> = {
  idle: "IDLE",
  running: "RUNNING",
  error: "FAILED",
  done: "COMPLETE",
}

export function SystemTicker({
  catalogStatus,
  catalogEntries,
  supportedEntries,
  selectedEntry,
  analysisStatus,
}: {
  catalogStatus: CatalogStatus
  catalogEntries: number
  supportedEntries: number
  selectedEntry: string | null
  analysisStatus: AnalysisStatus
}) {
  const catalogValue = catalogStatus === "ready" ? String(catalogEntries) : "—"
  const supportedValue = catalogStatus === "ready" ? String(supportedEntries) : "—"

  return (
    <section className="system-ticker" aria-label="System status">
      <div><span>MODE</span><strong>POSITION ANALYSIS</strong></div>
      <div><span>CATALOG</span><strong>{catalogValue}</strong></div>
      <div><span>SUPPORTED</span><strong>{supportedValue}</strong></div>
      <div><span>SELECTION</span><strong title={selectedEntry ?? undefined}>{selectedEntry ?? "—"}</strong></div>
      <div>
        <span>ANALYSIS</span>
        <strong className={`system-ticker__analysis system-ticker__analysis--${analysisStatus}`}>
          {ANALYSIS_LABELS[analysisStatus]}
        </strong>
      </div>
    </section>
  )
}
