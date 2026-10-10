import type { CatalogAvailability, CatalogEntry, CatalogProvenance, HydratedCatalogEntry } from "@risk0/contracts"

import { formatPercent, formatQuantity } from "../lib/format"
import { FreshnessBadge } from "./data-quality"

const PROVENANCE_BADGES: Record<CatalogProvenance, string> = {
  verified_belem: "Verified Belem",
  belem_distributed: "Belem-distributed",
  representative_proxy: "Representative",
}

const AVAILABILITY_REASONS: Record<Exclude<CatalogAvailability, "available">, string> = {
  catalog_inactive: "Inactive in catalog",
  not_found: "No longer resolves on Morpho",
  delisted: "Delisted by Morpho",
  data_conflict: "Conflicting source data",
  critical_data_missing: "Critical data missing",
  provider_unavailable: "Morpho data unavailable",
}

export function canAnalyze(entry: HydratedCatalogEntry): boolean {
  return entry.supported && entry.analysis_available
}

function unavailableLabel(entry: HydratedCatalogEntry): string {
  return entry.availability === "available"
    ? "Analysis unavailable"
    : `Analysis unavailable · ${AVAILABILITY_REASONS[entry.availability]}`
}

function metricsSummary(entry: HydratedCatalogEntry): string | null {
  const metrics = entry.metrics
  if (!metrics) return null
  const parts = [
    `Supplied ${formatQuantity(metrics.total_assets)} ${metrics.asset_symbol}`,
    `liquidity ${formatQuantity(metrics.liquidity_assets)} ${metrics.asset_symbol}`,
  ]
  if (metrics.utilization !== null) parts.push(`utilization ${formatPercent(metrics.utilization)}`)
  return parts.join(" · ")
}

export function entityTypeLabel(entry: CatalogEntry): string {
  switch (entry.entity_type) {
    case "morpho_vault_v2":
      return "Morpho Vault V2"
    case "morpho_vault_v1":
      return "Morpho Vault V1"
    case "morpho_market":
      return entry.lltv ? `Morpho market · ${formatPercent(entry.lltv)} LLTV` : "Morpho market"
  }
}

export function CatalogSelector({
  entries,
  selectedId,
  onSelect,
  now,
}: {
  entries: HydratedCatalogEntry[]
  selectedId: string | null
  onSelect: (entityId: string) => void
  now: number
}) {
  return (
    <fieldset className="panel catalog">
      <legend>1. Choose a curated vault or market</legend>
      <div className="catalog__grid">
        {entries.map((entry) => {
          const summary = metricsSummary(entry)
          return (
            <label
              key={entry.entity_id}
              className={`catalog__card${entry.entity_id === selectedId ? " catalog__card--selected" : ""}${canAnalyze(entry) ? "" : " catalog__card--disabled"}`}
            >
              <input
                type="radio"
                name="catalog-entry"
                value={entry.entity_id}
                checked={entry.entity_id === selectedId}
                disabled={!canAnalyze(entry)}
                onChange={() => onSelect(entry.entity_id)}
              />
              <span className="catalog__name">{entry.name}</span>
              <span className="catalog__type">{entityTypeLabel(entry)}</span>
              <span className={`badge badge--provenance badge--${entry.provenance}`}>
                {PROVENANCE_BADGES[entry.provenance]}
              </span>
              <span className="catalog__provenance">{entry.provenance_label}</span>
              {entry.provenance === "representative_proxy" && (
                <span className="catalog__note">Representative proxy — not a confirmed Belem holding</span>
              )}
              <span className="catalog__metrics">{summary ?? "Live metrics unavailable"}</span>
              {entry.freshness && entry.fetched_at && entry.source && (
                <FreshnessBadge freshness={entry.freshness} observedAt={entry.fetched_at} source={entry.source} now={now} />
              )}
              {!canAnalyze(entry) && <span className="catalog__unavailable">{unavailableLabel(entry)}</span>}
              {entry.warnings.length > 0 && (
                <ul className="warnings catalog__warnings">
                  {entry.warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              )}
              <code className="catalog__address">{entry.address_or_market_id}</code>
            </label>
          )
        })}
      </div>
    </fieldset>
  )
}
