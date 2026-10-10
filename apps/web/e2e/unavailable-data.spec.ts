import { expect, test } from "@playwright/test"

// Golden flow 3 (fixture mode): the WBTC/USDC market is the fixture world's
// degraded entry. Its live oracle observation is expired and its exit quote is
// still expired after the single refresh, so the recommendation is UNAVAILABLE
// and must not look safe.
test("expired oracle data and a failed quote refresh are UNAVAILABLE, never safe", async ({ page }) => {
  await page.goto("/")

  await page.getByRole("radio", { name: /WBTC collateral \/ USDC debt/ }).check()
  await page.getByRole("radio", { name: "Borrower", exact: true }).check()
  await expect(page.getByLabel("Collateral amount")).toHaveValue("0.1")
  await page.getByRole("button", { name: "Run analysis" }).click()

  const card = page.locator("section.recommendation")
  await expect(card.getByRole("heading", { level: 3 })).toHaveText("UNAVAILABLE")
  await expect(card).toHaveAttribute("data-tone", "unavailable")
  await expect(card).not.toHaveClass(/recommendation--safe/)
  await expect(card).toContainText("This is not a recommendation to stay.")
  await expect(card).toContainText("Confidence: unavailable")
  await expect(card).toContainText(/live oracle 0x[0-9a-f]{40}/)
  await expect(card).toContainText("a current exit estimate")
  await expect(card.getByRole("list", { name: "Exit warnings" })).toContainText("refreshed once; it is still expired")
  await expect(page.locator('[data-tone="safe"]')).toHaveCount(0)

  // Source ages are shown, and the expired sources are flagged.
  const quality = page.getByRole("region", { name: "Data quality" })
  await expect(quality.locator(".quality-summary--degraded")).toBeVisible()
  const quote = quality.getByRole("listitem").filter({ hasText: "Exit quote" })
  await expect(quote).toContainText(/Expired · (\d+ min )?\d+ s old/)
  const oracle = quality.getByRole("listitem").filter({ hasText: "simulated outage" })
  await expect(oracle).toHaveCount(1)
  await expect(oracle).toContainText(/Expired · 6 h( \d+ s)? old/)
})
