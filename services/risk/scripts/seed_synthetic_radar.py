"""Seed a live-sized synthetic radar into a fresh radar database.

    uv run --project services/risk python services/risk/scripts/seed_synthetic_radar.py PATH

Writes ~20k entities, ~50k relationships, and ~20k open incidents (see
`app.radar.synthetic`) through the real repositories, for the browser-level
scale suite (`apps/web/playwright.scale.config.ts`). Refuses to touch an
existing file so it can never pollute a real radar database.
"""

import argparse
import sys
import time
from pathlib import Path

from app.api.radar import RadarContext
from app.radar.repositories import (
    ExposureRepository,
    IncidentRepository,
    RadarDatabase,
    StateRepository,
    TopologyRepository,
)
from app.radar.synthetic import build_synthetic_radar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path, help="radar SQLite database to create")
    args = parser.parse_args(argv)
    if args.path.exists():
        parser.error(f"{args.path} already exists; the synthetic radar needs a fresh database")

    started = time.perf_counter()
    database = RadarDatabase(args.path)
    database.migrate()
    seeded = build_synthetic_radar(
        RadarContext(
            topology=TopologyRepository(database),
            states=StateRepository(database),
            incidents=IncidentRepository(database),
            exposures=ExposureRepository(database),
            database=database,
        )
    )
    elapsed = time.perf_counter() - started
    print(
        f"Seeded synthetic radar into {args.path} in {elapsed:.1f}s "
        f"({len(seeded['catalog'])} catalog vaults, {len(seeded['visible'])} visible entities)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
