// Display helpers for decimal strings from the risk service. All arithmetic is
// done on scaled BigInts so values are never routed through binary floats.

// Decimal strings as the risk service emits them: Python's Decimal may use
// exponent notation ("0E-18", "1E-7"), so the exponent is accepted and folded
// into the scale. The contract's request pattern is stricter (no exponent, no
// leading zeros); normalizeDecimal() produces that form.
const DECIMAL = /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/
const POSITIVE_DECIMAL = /^\d+(\.\d+)?([eE][+-]?\d+)?$/

// BigInt() rather than literals: the app's tsconfig targets ES2017.
const ZERO = BigInt(0)
const TWO = BigInt(2)
const TEN = BigInt(10)

interface Scaled {
  units: bigint
  scale: number
}

function parse(value: string): Scaled {
  if (!DECIMAL.test(value)) throw new Error(`Not a decimal: ${value}`)
  const negative = value.startsWith("-")
  const [mantissa, exponentText = "0"] = (negative ? value.slice(1) : value).split(/[eE]/)
  const [whole, fraction = ""] = mantissa.split(".")
  let units = BigInt(whole + fraction)
  let scale = fraction.length - Number(exponentText)
  if (scale < 0) {
    units *= TEN ** BigInt(-scale)
    scale = 0
  }
  return { units: negative ? -units : units, scale }
}

function rescale({ units, scale }: Scaled, target: number): bigint {
  if (target >= scale) return units * TEN ** BigInt(target - scale)
  const divisor = TEN ** BigInt(scale - target)
  const magnitude = units < ZERO ? -units : units
  const rounded = (magnitude + divisor / TWO) / divisor
  return units < ZERO ? -rounded : rounded
}

function render(units: bigint, scale: number, trim: boolean, group = true): string {
  const negative = units < ZERO
  const digits = (negative ? -units : units).toString().padStart(scale + 1, "0")
  let whole = digits.slice(0, digits.length - scale)
  if (group) whole = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")
  let fraction = scale > 0 ? digits.slice(digits.length - scale) : ""
  if (trim) fraction = fraction.replace(/0+$/, "")
  const sign = negative ? "-" : ""
  return `${sign}${whole}${fraction ? `.${fraction}` : ""}`
}

/** value x 10^exponent, exact. */
function shift(value: string, exponent: number): string {
  const { units, scale } = parse(value)
  const newScale = scale - exponent
  if (newScale >= 0) return trimDecimal(render(units, newScale, false, false))
  return (units * TEN ** BigInt(-newScale)).toString()
}

function trimDecimal(value: string): string {
  return value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value
}

/** Fixed fraction digits, half-up rounding, thousands separators. */
export function formatAmount(value: string, fractionDigits = 2): string {
  return render(rescale(parse(value), fractionDigits), fractionDigits, false)
}

/** Token quantity: up to six fraction digits with trailing zeros trimmed. */
export function formatQuantity(value: string): string {
  return render(rescale(parse(value), 6), 6, true)
}

/** A ratio such as "0.965" rendered as "96.50%". */
export function formatPercent(ratio: string, fractionDigits = 2): string {
  return `${formatAmount(shift(ratio, 2), fractionDigits)}%`
}

export function formatProbabilityRange(estimate: {
  low: string | null
  central: string | null
  high: string | null
}): string {
  if (estimate.central === null) return "Unavailable"
  const central = formatPercent(estimate.central)
  if (estimate.low === null || estimate.high === null) return central
  return `${central} (${formatPercent(estimate.low)}–${formatPercent(estimate.high)})`
}

/** "15" -> "0.15"; the input must already be a plain decimal. */
export function percentToRatio(percent: string): string {
  return shift(percent, -2)
}

/** "0.15" -> "15". */
export function ratioToPercent(ratio: string): string {
  return shift(ratio, 2)
}

export function compareDecimal(left: string, right: string): -1 | 0 | 1 {
  const a = parse(left)
  const b = parse(right)
  const scale = Math.max(a.scale, b.scale)
  const difference = rescale(a, scale) - rescale(b, scale)
  return difference === ZERO ? 0 : difference < ZERO ? -1 : 1
}

export function isPositiveDecimal(value: string): boolean {
  return POSITIVE_DECIMAL.test(value) && compareDecimal(value, "0") === 1
}

/**
 * Trim and rewrite a user-typed number in the contract's plain form: no
 * exponent, no leading zeros, fraction digits kept as typed. Anything that is
 * not a decimal is returned trimmed so validation can reject it.
 */
export function normalizeDecimal(value: string): string {
  const trimmed = value.trim()
  if (!DECIMAL.test(trimmed)) return trimmed
  const { units, scale } = parse(trimmed)
  return render(units, scale, false, false)
}

/** Exact age: every non-zero unit from days down to seconds. */
export function formatAge(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds))
  const parts = [
    [Math.floor(seconds / 86400), "d"],
    [Math.floor((seconds % 86400) / 3600), "h"],
    [Math.floor((seconds % 3600) / 60), "min"],
    [seconds % 60, "s"],
  ] as const
  const shown = parts.filter(([count]) => count > 0).map(([count, unit]) => `${count} ${unit}`)
  return shown.length ? shown.join(" ") : "0 s"
}

/** Seconds elapsed on this device since `receivedAt`, or 0 when unknown. */
export function elapsedSeconds(receivedAt: number | undefined, now: number): number {
  return receivedAt === undefined ? 0 : Math.max(0, Math.floor((now - receivedAt) / 1000))
}

export function ageSeconds(isoTimestamp: string, now: number): number {
  return Math.max(0, Math.floor((now - Date.parse(isoTimestamp)) / 1000))
}

/** "2026-10-10T11:55:00Z" -> "2026-10-10 11:55:00 UTC". */
export function formatTimestamp(isoTimestamp: string): string {
  return `${new Date(isoTimestamp).toISOString().slice(0, 19).replace("T", " ")} UTC`
}

export function humanize(identifier: string): string {
  return identifier.replace(/_/g, " ")
}
