import { expect, test } from "@playwright/test"

test("healthy radar shows the catalog topology and accepts a manual exposure", async ({ page }) => {
  await page.goto("/")

  const graph = page.getByRole("region", { name: "Morpho radar graph" })
  // Node buttons live in the collapsed "Explore topology as lists" section.
  await graph.getByText(/^Explore topology as lists/).click()
  await expect(graph.getByRole("button", { name: /Belem Sentinel USDC HEALTHY/ })).toBeVisible()
  await expect(graph.getByRole("button", { name: /Institutional USDC Peer HEALTHY/ })).toBeVisible()
  await expect(graph.getByRole("button", { name: /Hidden 48% Neighbor/ })).toHaveCount(0)
  await expect(page.getByText("No active incidents.")).toBeVisible()

  const exposure = page.getByRole("region", { name: "Exposure" })
  await expect(exposure.getByLabel("Exposure amount")).toHaveValue("100000")
  await exposure.getByRole("button", { name: "Add exposure" }).click()
  await expect(page.getByRole("status")).toContainText("Exposure recorded: 100000 USDC")
})
