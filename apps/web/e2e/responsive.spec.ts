import { expect, test } from "@playwright/test"

test.use({ viewport: { width: 375, height: 812 } })

test("phone width has no horizontal scroll and keeps the disclaimer visible", async ({ page }) => {
  await page.goto("/")
  await expect(page.getByRole("region", { name: "Morpho radar graph" })).toBeVisible()
  await expect(page.getByText("LIVE", { exact: true })).toBeVisible()

  const overflow = await page.evaluate(() => {
    const root = document.scrollingElement ?? document.documentElement
    return root.scrollWidth - root.clientWidth
  })
  expect(overflow).toBeLessThanOrEqual(0)
  await expect(page.getByRole("contentinfo").getByText(/NOT INVESTMENT ADVICE/)).toBeVisible()
})
