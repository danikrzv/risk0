import "@testing-library/jest-dom/vitest"

import { render, screen } from "@testing-library/react"
import { it, expect } from "vitest"

import Home from "./page"

it("renders the Risk0 heading", () => {
  render(<Home />)
  expect(screen.getByRole("heading", { name: "Risk0" })).toBeInTheDocument()
})
