import { expect, test } from "@playwright/test"

// Golden flow 2 (fixture mode): a wstETH/WETH borrower at the preset LTV is
// liquidated by the 15% collateral shock, so the recommendation is EXIT.
test("wstETH/WETH borrower at the preset LTV exits on a 15% collateral shock", async ({ page }) => {
  await page.goto("/")

  await page.getByRole("radio", { name: /wstETH collateral \/ WETH debt/ }).check()
  await page.getByRole("radio", { name: "Borrower", exact: true }).check()

  // The preset position, unchanged.
  await expect(page.getByLabel("Collateral amount")).toHaveValue("10")
  await expect(page.getByLabel("Debt amount")).toHaveValue("11.2")
  await expect(page.getByLabel("Current LTV (%)")).toHaveValue("90")
  await expect(page.getByText("Market LLTV 96.50%")).toBeVisible()

  // Only the 15% collateral shock.
  for (const name of ["Stablecoin depeg", "Oracle failure", "Exit-liquidity collapse", "Protocol impairment"]) {
    await page.getByRole("checkbox", { name }).uncheck()
  }
  await expect(page.getByRole("checkbox", { name: "Collateral shock" })).toBeChecked()
  await page.getByLabel("Collateral shock severity (%)").fill("15")
  await page.getByRole("button", { name: "Run analysis" }).click()

  const card = page.locator("section.recommendation")
  await expect(card.getByRole("heading", { level: 3 })).toHaveText("EXIT")
  await expect(card).toHaveAttribute("data-tone", "danger")
  await expect(card.getByRole("list", { name: "Recommendation triggers" })).toContainText(
    "collateral-shock:liquidation",
  )
  await expect(card).toContainText("A hard safety trigger requires exit regardless of the cost comparison.")

  // The liquidation path runs from the position through the shocked wstETH collateral.
  const results = page.getByRole("region", { name: "Scenario impacts" })
  const shock = results.getByRole("article", { name: "Collateral shock" })
  await expect(shock.getByRole("list", { name: "Hard triggers" })).toHaveText("liquidation")
  await expect(shock).toContainText(/Affected path[\s\S]*wstETH/)
  await expect(card).toContainText(/Route: wstETH → WETH/)
})
