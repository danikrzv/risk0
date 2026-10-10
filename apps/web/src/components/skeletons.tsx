// Loading placeholders shaped like the content they stand in for, so the page
// does not jump when data arrives. The shapes are hidden from assistive
// technology; a visually hidden label says what is loading.

const CATALOG_CARDS = 6

function Lines({ count }: { count: number }) {
  return (
    <>
      {Array.from({ length: count }, (_, index) => (
        <span key={index} className={`skeleton-line${index === 0 ? " skeleton-line--title" : ""}`} />
      ))}
    </>
  )
}

export function CatalogSkeleton() {
  return (
    <div className="panel catalog skeleton" aria-busy="true">
      <span className="visually-hidden">Loading curated catalog…</span>
      <div className="catalog__grid" aria-hidden="true">
        {Array.from({ length: CATALOG_CARDS }, (_, index) => (
          <div key={index} className="catalog__card skeleton-card">
            <Lines count={4} />
          </div>
        ))}
      </div>
    </div>
  )
}

export function RadarSkeleton() {
  return (
    <div className="radar-layout skeleton" aria-busy="true">
      <span className="visually-hidden">Loading radar topology…</span>
      <div className="panel skeleton-card" aria-hidden="true"><Lines count={5} /></div>
      <div className="panel skeleton-card" aria-hidden="true">
        <Lines count={2} />
        <span className="skeleton-canvas" />
      </div>
      <div className="panel skeleton-card" aria-hidden="true"><Lines count={4} /></div>
    </div>
  )
}
