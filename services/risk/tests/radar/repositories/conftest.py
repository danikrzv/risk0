import pytest


@pytest.fixture
def database(tmp_path):
    from app.radar.repositories import RadarDatabase

    db = RadarDatabase(tmp_path / "radar.sqlite3")
    db.migrate()
    return db
