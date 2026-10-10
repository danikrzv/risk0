import react from "@vitejs/plugin-react"
import { configDefaults, defineConfig } from "vitest/config"

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    // Playwright golden flows run with `playwright test`, never under Vitest.
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
})
