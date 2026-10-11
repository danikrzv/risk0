import { expect, type Page, test } from "@playwright/test"

// Budgets for a live-sized radar (~20k entities, ~50k relationships, ~20k open
// incidents; see services/risk/app/radar/synthetic.py). Generous enough for a
// cold CI runner, tight enough to catch an unpruned graph or a canvas that
// draws everything: before pruning the payload was tens of megabytes.
const GRAPH_PAYLOAD_BUDGET_BYTES = 500_000
const FIRST_CANVAS_NODES_BUDGET_MS = 10_000
const MAX_CANVAS_NODES = 400
const MAX_LISTED_INCIDENTS = 50
const SELECTION_LATENCY_BUDGET_MS = 1_000
const SEARCH_LATENCY_BUDGET_MS = 2_000
// Incident selection fetches the incident's full graph (2.7 MB for the hub).
const INCIDENT_LATENCY_BUDGET_MS = 3_000
// Longest main-thread task while typing or drawing an incident: anything longer is a hang.
const LONGEST_TASK_BUDGET_MS = 500

// Synthetic catalog vaults are vault-00000 … vault-00014, labelled "Vault 00000" ….
const CATALOG_VAULT_ID = "vault:1:vault-00000"
const CATALOG_VAULT_LABEL = "Vault 00000"
const SEARCHED_VAULT_LABEL = "Vault 00003"
// The stablecoin hub's incident, announced by the only notification.
const HUB_INCIDENT_TITLE = "Stablecoin hub incident"
const HUB_INCIDENT_REACH = 5_000

function record(name: string, value: number, unit: string): void {
  test.info().annotations.push({ type: name, description: `${Math.round(value)} ${unit}` })
  console.log(`[scale] ${name}: ${Math.round(value).toLocaleString("en-US")} ${unit}`)
}

type LongTaskWindow = { __risk0LongTasks: number[]; __risk0LongTaskObserver: PerformanceObserver }

/** Collects main-thread long tasks (> 50 ms) from page start. */
async function observeLongTasks(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const target = window as unknown as LongTaskWindow
    target.__risk0LongTasks = []
    target.__risk0LongTaskObserver = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) target.__risk0LongTasks.push(entry.duration)
    })
    target.__risk0LongTaskObserver.observe({ type: "longtask", buffered: true })
  })
}

/** Long tasks since the last call, including entries not yet delivered to the observer. */
async function takeLongTasks(page: Page): Promise<number[]> {
  return page.evaluate(async () => {
    // Let a task that is still running finish and be reported first.
    await new Promise((resolve) => setTimeout(resolve, 100))
    const target = window as unknown as LongTaskWindow
    for (const entry of target.__risk0LongTaskObserver.takeRecords()) target.__risk0LongTasks.push(entry.duration)
    return target.__risk0LongTasks.splice(0)
  })
}

test("a live-sized radar stays small on the wire and interactive in the browser", async ({ page }) => {
  await observeLongTasks(page)
  const graph = page.getByRole("region", { name: "Morpho radar graph" })
  const canvasNodes = graph.locator(".react-flow__node")
  const detail = graph.getByRole("region", { name: "Selected radar entity" })

  await test.step("the pruned graph payload stays under budget", async () => {
    const graphResponse = page.waitForResponse((response) => {
      const url = new URL(response.url())
      return url.pathname === "/api/graph" && url.search === "" && response.request().method() === "GET"
    })
    const started = Date.now()
    await page.goto("/")
    const response = await graphResponse
    expect(response.status()).toBe(200)
    const bytes = (await response.body()).byteLength
    record("graph payload", bytes, "bytes")
    expect(bytes).toBeLessThan(GRAPH_PAYLOAD_BUDGET_BYTES)

    await expect(canvasNodes.first()).toBeVisible({ timeout: FIRST_CANVAS_NODES_BUDGET_MS })
    const firstNodesMs = Date.now() - started
    record("navigation to first canvas nodes", firstNodesMs, "ms")
    expect(firstNodesMs).toBeLessThan(FIRST_CANVAS_NODES_BUDGET_MS)
    record("longest main-thread task until first nodes", Math.max(0, ...(await takeLongTasks(page))), "ms")
    const drawn = await canvasNodes.count()
    record("canvas nodes", drawn, "nodes")
    expect(drawn).toBeLessThanOrEqual(MAX_CANVAS_NODES)
  })

  await test.step("catalog vaults are drawn and selection follows connections", async () => {
    const catalogNode = canvasNodes.and(page.locator(`[data-id="${CATALOG_VAULT_ID}"]`))
    await expect(catalogNode).toContainText(CATALOG_VAULT_LABEL)

    const started = Date.now()
    await catalogNode.click()
    await expect(detail).toBeVisible()
    await expect(detail.getByRole("heading", { level: 3 })).toHaveText(CATALOG_VAULT_LABEL)
    const selectionMs = Date.now() - started
    record("node click to detail", selectionMs, "ms")
    expect(selectionMs).toBeLessThan(SELECTION_LATENCY_BUDGET_MS)

    const connection = detail.getByRole("list", { name: "Entity connections" }).getByRole("button").first()
    const neighbour = (await connection.textContent())?.trim() ?? ""
    expect(neighbour).not.toBe("")
    expect(neighbour).not.toBe(CATALOG_VAULT_LABEL)
    const followed = Date.now()
    await connection.click()
    await expect(detail.getByRole("heading", { level: 3 })).toHaveText(neighbour)
    const followMs = Date.now() - followed
    record("connection click to detail", followMs, "ms")
    expect(followMs).toBeLessThan(SELECTION_LATENCY_BUDGET_MS)
    await detail.getByRole("button", { name: "Close entity details" }).click()
    await expect(detail).toHaveCount(0)
  })

  await test.step("the incident rail is capped and an incident highlights its path", async () => {
    // The incident drawer starts closed; it shares the canvas edge with the exposure panel.
    await page.getByRole("button", { name: /^Incidents \(\d+\)$/ }).click()
    const incidents = page.getByRole("list", { name: "Active incidents" }).getByRole("button")
    await expect(incidents.first()).toBeVisible()
    const listed = await incidents.count()
    record("listed incidents", listed, "incidents")
    expect(listed).toBeLessThanOrEqual(MAX_LISTED_INCIDENTS)

    const incident = incidents.first()
    const started = Date.now()
    await incident.click()
    await expect(incident).toHaveAttribute("aria-pressed", "true")
    // Node buttons live in the collapsed "Explore topology as lists" section.
    await graph.getByText(/^Explore topology as lists/).click()
    await expect(graph.getByRole("list", { name: "Radar nodes" }).locator('[data-incident-path="true"]').first()).toBeVisible()
    await expect(graph.getByLabel("Incident impact summary")).toBeVisible()
    const incidentMs = Date.now() - started
    record("incident click to highlighted path", incidentMs, "ms")
    expect(incidentMs).toBeLessThan(INCIDENT_LATENCY_BUDGET_MS)
    await expect(canvasNodes.first()).toBeVisible()
    expect(await canvasNodes.count()).toBeLessThanOrEqual(MAX_CANVAS_NODES)
  })

  await test.step("searching the graph ranks matches without hanging the page", async () => {
    const search = graph.getByLabel("Search graph")
    const results = graph.getByRole("list", { name: "Graph search results" })
    await takeLongTasks(page)
    const started = Date.now()
    await search.pressSequentially(SEARCHED_VAULT_LABEL)
    // Search ranks matches without changing the drawn graph; the exact match leads.
    await expect(results.getByRole("button").first()).toContainText(SEARCHED_VAULT_LABEL)
    const searchMs = Date.now() - started
    const longest = Math.max(0, ...(await takeLongTasks(page)))
    record("typing search to ranked results", searchMs, "ms")
    record("longest main-thread task while typing", longest, "ms")
    expect(searchMs).toBeLessThan(SEARCH_LATENCY_BUDGET_MS)
    expect(longest).toBeLessThan(LONGEST_TASK_BUDGET_MS)
    // The page still answers immediately once ranking settles.
    await expect(page.evaluate(() => document.readyState)).resolves.toBe("complete")
    await search.fill("")
    await expect(results).toHaveCount(0)
  })

  let hubFootprint: { entities: Array<{ entity_id: string; entity_type: string; label: string }> } = { entities: [] }
  await test.step("the hub incident's 5,000 propagated vaults stay capped and selectable", async () => {
    await page.getByRole("button", { name: "Notifications, 1 unread" }).click()
    await takeLongTasks(page)
    const incidentGraph = page.waitForResponse((response) => (
      new URL(response.url()).searchParams.get("incident_graph_id") !== null
    ))
    const started = Date.now()
    await page.getByLabel("Notification history").getByRole("button", { name: `View incident ${HUB_INCIDENT_TITLE}` }).click()
    await expect(graph.getByLabel("Incident impact summary")).toContainText(`${HUB_INCIDENT_REACH} impacted entities`)
    await expect(canvasNodes.first()).toBeVisible()
    const shownMs = Date.now() - started
    const longest = Math.max(0, ...(await takeLongTasks(page)))
    record("hub incident click to drawn path", shownMs, "ms")
    const hubBody = await (await incidentGraph).body()
    hubFootprint = JSON.parse(hubBody.toString()) as { entities: Array<{ entity_id: string; entity_type: string; label: string }> }
    record("hub incident graph payload", hubBody.byteLength, "bytes")
    record("longest main-thread task drawing the hub incident", longest, "ms")
    expect(shownMs).toBeLessThan(INCIDENT_LATENCY_BUDGET_MS)
    expect(longest).toBeLessThan(LONGEST_TASK_BUDGET_MS)
    const drawn = await canvasNodes.count()
    record("canvas nodes with the hub incident", drawn, "nodes")
    expect(drawn).toBeLessThanOrEqual(MAX_CANVAS_NODES)
    await expect(graph.getByText(/^The canvas shows the first 400 of /)).toBeVisible()

    const revealed = canvasNodes.filter({ hasText: /^Vault / }).first()
    const label = (await revealed.textContent())?.split(" · ")[0] ?? ""
    const selected = Date.now()
    await revealed.click()
    await expect(detail.getByRole("heading", { level: 3 })).toHaveText(label)
    const selectionMs = Date.now() - selected
    record("node click to detail with the hub incident", selectionMs, "ms")
    expect(selectionMs).toBeLessThan(SELECTION_LATENCY_BUDGET_MS)
  })

  await test.step("search reaches a vault beyond the 400-node canvas cap and focuses it", async () => {
    const drawnIds = new Set(await canvasNodes.evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-id"))))
    const beyondCap = hubFootprint.entities.find((entity) => entity.entity_type === "vault" && !drawnIds.has(entity.entity_id))
    expect(beyondCap, "the hub incident reveals more vaults than the canvas draws").toBeDefined()
    const target = beyondCap!

    const search = graph.getByLabel("Search graph")
    await search.fill(target.label)
    const started = Date.now()
    await graph.getByRole("list", { name: "Graph search results" })
      .getByRole("button", { name: new RegExp(`^${target.label}\\b`) }).first().click()
    const promoted = canvasNodes.and(page.locator(`[data-id="${target.entity_id}"]`))
    await expect(promoted).toBeVisible()
    await expect(promoted).toHaveClass(/\bselected\b/)
    await expect(detail.getByRole("heading", { level: 3 })).toHaveText(target.label)
    const promotedMs = Date.now() - started
    record("search result beyond the cap to focused node", promotedMs, "ms")
    expect(promotedMs).toBeLessThan(SELECTION_LATENCY_BUDGET_MS)
    expect(await canvasNodes.count()).toBeLessThanOrEqual(MAX_CANVAS_NODES)
  })
})
