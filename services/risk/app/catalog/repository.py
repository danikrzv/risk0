from pathlib import Path

from app.contracts.models import CatalogDocument, CatalogEntry


class CatalogRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    def list_entries(self) -> tuple[CatalogEntry, ...]:
        catalog = CatalogDocument.model_validate_json(self._path.read_text())
        return catalog.entries
