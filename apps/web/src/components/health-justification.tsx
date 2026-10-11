"use client"

import type { HealthStatus, RadarEntity } from "@risk0/contracts"
import { useState } from "react"

import { HealthComputation } from "./health-computation"

/**
 * "Why HEALTHY?" disclosure for a health-marked list row. The radar's recorded
 * health computation is loaded only when opened, so a long list costs nothing
 * until a reader asks. An entry without a radar entity is UNKNOWN by rule and
 * says so instead of loading anything.
 */
export function HealthJustification({
  entity,
  status,
  labelFor,
}: {
  /** The radar entity behind the row, when the radar topology contains it. */
  entity: RadarEntity | undefined
  status: HealthStatus
  labelFor: (entityId: string) => string
}) {
  const [open, setOpen] = useState(false)
  const evaluatedAt = typeof entity?.metadata.health_evaluated_at === "string"
    ? entity.metadata.health_evaluated_at
    : null

  return (
    <details className="health-justification" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>Why {status}?</summary>
      {open ? (
        entity ? (
          <HealthComputation
            entityId={entity.entity_id}
            evaluatedAt={evaluatedAt}
            displayedStatus={status}
            labelFor={labelFor}
          />
        ) : (
          <p className="health-justification__none">
            This entry is not in the radar topology, so no health evaluation exists for it. Its state is
            UNKNOWN by rule: missing data is never shown as healthy.
          </p>
        )
      ) : null}
    </details>
  )
}
