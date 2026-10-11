import { expect, test } from "@playwright/test"

// Golden flow 1 (fixture mode): a Belem RWA EURCV lender with the default
// scenarios stays, and the graph shows the vault's dependencies.
test("Belem RWA EURCV lender with default scenarios is a fixture-defined STAY", async ({ page }) => {
  await page.goto("/")
  await page.getByRole("button", { name: "Position Analysis", exact: true }).click()
  await expect(page.getByRole("button", { name: "Position Analysis", exact: true })).toHaveAttribute("aria-current", "page")
  // Entries without radar health are marked UNKNOWN, never healthy.
  await expect(page.getByRole("list", { name: "Position analysis health" }).getByRole("button", { name: /Belem RWA EURCV.*UNKNOWN/ })).toBeVisible()

  const belem = page.getByRole("radio", { name: /Belem RWA EURCV/ })
  await expect(belem).toBeEnabled()
  // Fixture data is labelled as such: recorded responses served from a reserved host.
  await expect(belem).toHaveAccessibleName(/morpho\.fixture\.invalid/)
  await belem.check()

  await expect(page.getByText("Lender only")).toBeVisible()
  await page.getByLabel("Amount", { exact: true }).fill("100000")
  await expect(page.getByLabel("Numeraire", { exact: true })).toHaveValue("EURCV")
  // Defaults: every scenario is selected at its default severity.
  for (const name of ["Stablecoin depeg", "Collateral shock", "Oracle failure", "Exit-liquidity collapse", "Protocol impairment"]) {
    await expect(page.getByRole("checkbox", { name })).toBeChecked()
  }
  await page.getByRole("button", { name: "Run analysis" }).click()

  const card = page.locator("section.recommendation")
  await expect(card.getByRole("heading", { level: 3 })).toHaveText("STAY")
  await expect(card).toHaveAttribute("data-tone", "safe")
  await expect(card).toContainText("Expected 30-day loss does not exceed the cost of exiting now.")
  await expect(card).toContainText("No hard safety trigger fired.")
  await expect(card).toContainText(/expected 30-day staying loss [0-9.]+ EURCV .* versus exit cost now [0-9.]+ EURCV/)
  await expect(card).toContainText("Confidence:")
  // Break-even: how far the estimate is from flipping the decision.
  await expect(card.locator(".break-even")).toContainText(/EXIT becomes optimal above [0-9.]+% probability of .+; current estimate [0-9.]+%/)

  // Inspect the graph: the position depends on the vault, which depends on Morpho.
  const graph = page.locator("section.graph")
  await expect(graph.getByRole("heading", { name: "Dependency graph" })).toBeVisible()
  const nodes = graph.getByRole("list", { name: "Dependency nodes" })
  await expect(nodes.getByRole("button", { name: /\(position\)$/ })).toHaveCount(1)
  // Morpho is the one deployment protocol; issuer look-through adds issuer protocol nodes.
  await expect(nodes.getByRole("button", { name: "Morpho (protocol)", exact: true })).toHaveCount(1)
  const vault = nodes.getByRole("button", { name: /Belem RWA EURCV.*\(vault\)$/ })
  await vault.click()
  await expect(vault).toHaveAttribute("aria-pressed", "true")
  const detail = graph.getByRole("region", { name: "Selected dependency" })
  await expect(detail.getByRole("heading", { level: 3 })).toContainText("Belem RWA EURCV")
  await expect(graph.getByRole("list", { name: "Dependency relationships" })).toContainText("Belem RWA EURCV")

  // Every scenario has an impact and a labelled probability method.
  const results = page.getByRole("region", { name: "Scenario impacts" })
  await expect(results.getByRole("article")).toHaveCount(5)
  await expect(results).toContainText("Model-estimated")
  await expect(page.getByRole("region", { name: "Data quality" })).toBeVisible()

  // Book view: the brief's ten pockets of 10% each, ranked, with book totals.
  const book = page.getByRole("region", { name: "Book" })
  await book.getByRole("button", { name: "Load brief example" }).click()
  await expect(book.getByRole("table", { name: /Pockets to analyze \(10, 100.00% of the book\)/ })).toBeVisible()
  await book.getByRole("button", { name: "Run book analysis" }).click()
  await expect(book.getByText(/^Book analyzed: \d+ STAY, \d+ EXIT, \d+ UNAVAILABLE\.$/)).toBeVisible()
  const ranked = book.getByRole("table", { name: /Pockets ranked by expected loss minus exit cost/ })
  await expect(ranked.locator("tbody tr")).toHaveCount(10)
  // Belem stays; it is no longer the first STAY since lenders are charged bad debt, not the
  // collateral fall, which lowers the other lender pockets' expected loss too.
  await expect(ranked.locator('tbody tr[data-decision="STAY"]', { hasText: "Belem RWA EURCV" })).toHaveCount(1)
  // UNAVAILABLE pockets are never styled as safe.
  for (const marker of await ranked.locator('tr[data-decision="UNAVAILABLE"] [data-tone]').all()) {
    await expect(marker).toHaveAttribute("data-tone", "unavailable")
  }
  await expect(book.getByRole("table", { name: "Book totals" })).toContainText("Expected loss if staying in every pocket")
})
