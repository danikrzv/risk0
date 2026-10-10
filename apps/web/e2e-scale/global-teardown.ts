import { rm } from "node:fs/promises"

/** Remove the synthetic radar database (and its SQLite sidecars) after the run. */
export default async function globalTeardown(): Promise<void> {
  const path = process.env.RISK0_E2E_SCALE_RADAR_DB_PATH
  if (!path) return
  await Promise.all(["", "-wal", "-shm", "-journal"].map((suffix) => rm(`${path}${suffix}`, { force: true })))
}
