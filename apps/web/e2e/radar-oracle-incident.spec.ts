import { expect, test } from "@playwright/test"

test("resolved oracle incident reveals hidden propagation and affected exposure", async ({ page }) => {
  await page.goto("/")

  const toast = page.getByRole("alert").filter({ hasText: "Oracle deviation resolved" })
  await expect(toast).toContainText("deviated 8%")
  await expect(toast).toHaveAttribute("data-severity", "CRITICAL")

  await page.getByRole("button", { name: "Notifications, 1 unread" }).click()
  const history = page.getByLabel("Notification history")
  await expect(history).toContainText("Oracle deviation resolved")
  await history.getByRole("button", { name: "View incident Oracle deviation resolved" }).click()

  const graph = page.getByRole("region", { name: "Morpho radar graph" })
  const hidden = graph.getByRole("button", { name: /Hidden 48% Neighbor HEALTHY/ })
  await expect(hidden).toBeVisible()
  await expect(hidden).toHaveAttribute("data-revealed", "true")
  await expect(graph.getByRole("button", { name: /WETH \/ USDC Oracle CRITICAL/ })).toHaveAttribute("data-incident-path", "true")
  await expect(graph.getByLabel("Incident impact summary")).toContainText("153,000")
  await expect(page.getByText("No active incidents.")).toBeVisible()
})
