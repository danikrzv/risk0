import sqlite3
import uuid
from collections.abc import Iterable
from datetime import datetime

from app.contracts.models import PositionMode
from app.radar.contracts import ExposureCreateRequest, IncidentImpact, ManualExposure
from app.radar.repositories.database import (
    RadarDatabase,
    decode_decimal,
    decode_timestamp,
    encode_decimal,
    encode_timestamp,
)
from app.radar.repositories.incidents import (
    IMPACT_COLUMNS,
    impact_from_row,
    impact_values,
)


class ExposureRepository:
    """Manually entered lender and borrower exposures and their incident impacts."""

    def __init__(self, database: RadarDatabase) -> None:
        self._db = database

    def create(
        self,
        request: ExposureCreateRequest,
        *,
        created_at: datetime,
        exposure_id: str | None = None,
    ) -> ManualExposure:
        exposure = ManualExposure(
            **request.model_dump(),
            exposure_id=exposure_id or f"exposure-{uuid.uuid4().hex}",
            created_at=created_at,
        )
        try:
            with self._db.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO manual_exposures (
                        exposure_id, entity_id, mode, amount, numeraire,
                        collateral_amount, debt_amount, ltv, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        exposure.exposure_id,
                        exposure.entity_id,
                        exposure.mode.value,
                        encode_decimal(exposure.amount),
                        exposure.numeraire,
                        encode_decimal(exposure.collateral_amount),
                        encode_decimal(exposure.debt_amount),
                        encode_decimal(exposure.ltv),
                        encode_timestamp(exposure.created_at),
                    ),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"exposure id {exposure.exposure_id} already exists") from None
        return exposure

    def get(self, exposure_id: str) -> ManualExposure | None:
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM manual_exposures WHERE exposure_id = ?", (exposure_id,)
            ).fetchone()
        return None if row is None else _exposure(row)

    def list_exposures(self) -> tuple[ManualExposure, ...]:
        with self._db.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM manual_exposures ORDER BY created_at, exposure_id"
            ).fetchall()
        return tuple(_exposure(row) for row in rows)

    def list_exposures_for_incident(
        self, incident_id: str
    ) -> tuple[ManualExposure, ...]:
        """Exposures with at least one stored impact of `incident_id`, in one query."""
        with self._db.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM manual_exposures WHERE exposure_id IN (
                    SELECT exposure_id FROM exposure_impacts WHERE incident_id = ?
                )
                ORDER BY created_at, exposure_id
                """,
                (incident_id,),
            ).fetchall()
        return tuple(_exposure(row) for row in rows)

    def replace_impacts(
        self, exposure_id: str, impacts: Iterable[IncidentImpact]
    ) -> int:
        values = [(exposure_id, *impact_values(impact)) for impact in impacts]
        with self._db.transaction() as connection:
            connection.execute(
                "DELETE FROM exposure_impacts WHERE exposure_id = ?", (exposure_id,)
            )
            connection.executemany(
                f"INSERT INTO exposure_impacts (exposure_id, {IMPACT_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        return len(values)

    def impacts(self, exposure_id: str) -> tuple[IncidentImpact, ...]:
        with self._db.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM exposure_impacts WHERE exposure_id = ?
                ORDER BY incident_id, distance, target_entity_id
                """,
                (exposure_id,),
            ).fetchall()
        return tuple(impact_from_row(row) for row in rows)


def _exposure(row: sqlite3.Row) -> ManualExposure:
    return ManualExposure(
        exposure_id=row["exposure_id"],
        entity_id=row["entity_id"],
        mode=PositionMode(row["mode"]),
        amount=decode_decimal(row["amount"]),
        numeraire=row["numeraire"],
        collateral_amount=decode_decimal(row["collateral_amount"]),
        debt_amount=decode_decimal(row["debt_amount"]),
        ltv=decode_decimal(row["ltv"]),
        created_at=decode_timestamp(row["created_at"]),
    )
