import { expect, test } from "@playwright/test"

test("healthy radar shows the catalog topology and accepts a manual exposure", async ({ page }) => {
  await page.goto("/")

  const graph = page.getByRole("region", { name: "Morpho radar graph" })
  // Node buttons live in the collapsed "Explore topology as lists" section.
  await graph.getByText(/^Explore topology as lists/).click()
  await expect(graph.getByRole("button", { name: /Belem Sentinel USDC HEALTHY/ })).toBeVisible()
  await expect(graph.getByRole("button", { name: /Institutional USDC Peer HEALTHY/ })).toBeVisible()
  await expect(graph.getByRole("button", { name: /Hidden 48% Neighbor/ })).toHaveCount(0)

  // The exposure panel floats open by default and keeps its values while hidden.
  const exposure = page.getByRole("region", { name: "Exposure" })
  await expect(exposure.getByLabel("Exposure amount")).toHaveValue("100000")
  await exposure.getByLabel("Exposure amount").fill("120000")
  await page.getByRole("button", { name: "Hide exposure" }).click()
  await expect(exposure).toBeHidden()
  await page.getByRole("button", { name: "Exposure", exact: true }).click()
  await expect(exposure.getByLabel("Exposure amount")).toHaveValue("120000")
  await exposure.getByRole("button", { name: "Add exposure" }).click()
  await expect(page.getByRole("status")).toContainText("Exposure recorded: 120000 USDC")

  // Opening the incident drawer closes the exposure panel: they share the canvas edge.
  await page.getByRole("button", { name: "Incidents (0)" }).click()
  await expect(page.getByText("No active incidents.")).toBeVisible()
  await expect(exposure).toBeHidden()

  // A health-marked category row hands off to the focused graph.
  await page.getByRole("button", { name: "Markets", exact: true }).click()
  const markets = page.getByRole("list", { name: "Market health" })
  await markets.getByRole("button", { name: /WETH \/ USDC 86%/ }).click()
  await expect(page.getByRole("button", { name: "Graph", exact: true })).toHaveAttribute("aria-current", "page")
  const detail = page.getByRole("region", { name: "Selected radar entity" })
  await expect(detail.getByRole("heading", { name: "WETH / USDC 86%" })).toBeVisible()

  // Clicking a node explains how its health was computed.
  await page.locator(".react-flow__node", { hasText: "Belem Sentinel USDC" }).click()
  await expect(detail.getByRole("heading", { name: "Belem Sentinel USDC" })).toBeVisible()
  const computation = detail.getByRole("region", { name: "Health computation" })
  await expect(computation.getByText("score 1 = HEALTHY")).toBeVisible()
  await expect(
    computation.getByText("HEALTHY because every check is within its threshold and dependencies are normal."),
  ).toBeVisible()
  await expect(computation.getByRole("table", { name: /Checks in evaluation order/ })).toContainText(
    "Allocation concentration (HHI) at or above the warning threshold",
  )
  await expect(computation.getByRole("table", { name: "Dependency aggregation" })).toContainText("WETH / USDC 86%")
  await expect(computation.getByText(/raw HEALTHY, no confirmation needed → HEALTHY/)).toBeVisible()
})
