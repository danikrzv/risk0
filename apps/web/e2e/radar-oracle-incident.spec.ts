import { expect, test } from "@playwright/test"

const riskUrl = `http://127.0.0.1:${process.env.RISK0_E2E_RISK_PORT ?? 8710}`

test("oracle incident opens, propagates to exposure, and resolves", async ({ page, request }) => {
  await page.goto("/")

  const exposure = page.getByRole("region", { name: "Exposure" })
  await exposure.getByLabel("Exposure target").selectOption({ label: "Belem Sentinel USDC" })
  await exposure.getByRole("button", { name: "Add exposure" }).click()
  await expect(page.getByRole("status")).toContainText("Exposure recorded: 100000 USDC")
  const impact = page.getByRole("region", { name: "Exposure impact" })
  await expect(impact).toContainText("No active incident reaches this exposure.")

  const opened = await request.post(`${riskUrl}/v1/fixtures/radar/oracle-incident/open`)
  expect(opened.status()).toBe(201)

  const toast = page.getByRole("alert").filter({ hasText: "Critical oracle deviation" })
  await expect(toast).toContainText("deviated 8%")
  await expect(toast).toHaveAttribute("data-severity", "CRITICAL")

  // The incident's propagation reaches the recorded exposure: the panel refreshes live.
  await expect(impact).toContainText("Estimated affected: 60,000 USDC across 1 incident")
  await expect(impact).toContainText("Path: WETH / USDC Oracle → WETH / USDC 86% → Belem Sentinel USDC")

  await page.getByRole("button", { name: "Notifications, 1 unread" }).click()
  const history = page.getByLabel("Notification history")
  await expect(history).toContainText("Critical oracle deviation")
  await history.getByRole("button", { name: "View incident Critical oracle deviation" }).click()

  const graph = page.getByRole("region", { name: "Morpho radar graph" })
  // Node buttons live in the collapsed "Explore topology as lists" section.
  await graph.getByText(/^Explore topology as lists/).click()
  const hidden = graph.getByRole("button", { name: /Hidden 48% Neighbor HEALTHY/ })
  await expect(hidden).toBeVisible()
  await expect(hidden).toHaveAttribute("data-revealed", "true")
  await expect(graph.getByRole("button", { name: /WETH \/ USDC Oracle CRITICAL/ })).toHaveAttribute("data-incident-path", "true")
  await expect(graph.getByLabel("Incident impact summary")).toContainText("60,000")
  await page.getByRole("button", { name: "Incidents (1)" }).click()
  await expect(page.getByRole("list", { name: "Active incidents" }).getByRole("button", { name: "WETH / USDC Oracle CRITICAL" })).toHaveAttribute("aria-pressed", "true")

  const resolved = await request.post(`${riskUrl}/v1/fixtures/radar/oracle-incident/resolve`)
  expect(resolved.ok()).toBe(true)
  await expect(page.getByText("No active incidents.")).toBeVisible()
  await expect(hidden).toBeVisible()
})
