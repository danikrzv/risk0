from pathlib import Path

from app.contracts.models import CatalogEntry, CatalogResponse


class CatalogRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    def list_entries(self) -> tuple[CatalogEntry, ...]:
        catalog = CatalogResponse.model_validate_json(self._path.read_text())
        return catalog.entries
