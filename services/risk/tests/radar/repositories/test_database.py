import sqlite3
import threading
from datetime import timedelta

import pytest

from app.config import CachePolicy
from app.infrastructure.cache import SQLiteCache
from tests.radar.repositories.builders import NOW


RADAR_TABLES = {
    "entities",
    "relationships",
    "state_snapshots",
    "state_hourly_aggregates",
    "health_states",
    "latest_health_states",
    "similarities",
    "incidents",
    "incident_impacts",
    "notifications",
    "manual_exposures",
    "exposure_impacts",
    "scheduler_runs",
}


def table_names(path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def test_migrate_creates_every_radar_table_on_an_empty_database(tmp_path):
    from app.radar.repositories import RadarDatabase

    path = tmp_path / "empty.sqlite3"
    database = RadarDatabase(path)

    database.migrate()

    assert RADAR_TABLES <= table_names(path)
    assert database.applied_migrations() == ("001_radar", "002_latest_health", "003_health_derivation")


def test_migrate_is_idempotent(tmp_path):
    from app.radar.repositories import RadarDatabase

    database = RadarDatabase(tmp_path / "radar.sqlite3")

    database.migrate()
    database.migrate()
    RadarDatabase(tmp_path / "radar.sqlite3").migrate()

    assert database.applied_migrations() == ("001_radar", "002_latest_health", "003_health_derivation")


def test_migrate_keeps_an_existing_cache_only_database_intact(tmp_path):
    from app.radar.repositories import RadarDatabase

    path = tmp_path / "risk0.sqlite3"
    cache = SQLiteCache(path)
    policy = CachePolicy(ttl=timedelta(minutes=1), retention=timedelta(hours=1))
    cache.set("morpho", "market:1:0xabc", b'{"value": 1}', NOW, policy)
    connection = sqlite3.connect(path)
    cache_schema = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'cache_entries'"
    ).fetchone()
    connection.close()

    RadarDatabase(path).migrate()

    assert RADAR_TABLES | {"cache_entries"} <= table_names(path)
    connection = sqlite3.connect(path)
    assert (
        connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'cache_entries'"
        ).fetchone()
        == cache_schema
    )
    connection.close()
    cached = cache.get("morpho", "market:1:0xabc", NOW)
    assert cached is not None and cached.payload == b'{"value": 1}'


def journal_mode(path) -> str:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()


INSERT_RUN = (
    "INSERT INTO scheduler_runs (run_id, loop, status, started_at, "
    "processed_count, error_count, message) "
    "VALUES (?, 'fast', 'running', '2026-10-10T12:00:00.000000+00:00', 0, 0, '')"
)


def test_migrate_switches_new_and_existing_files_to_wal(tmp_path):
    from app.radar.repositories import RadarDatabase

    fresh = tmp_path / "fresh.sqlite3"
    RadarDatabase(fresh).migrate()
    assert journal_mode(fresh) == "wal"

    existing = tmp_path / "existing.sqlite3"
    database = RadarDatabase(existing)
    database.migrate()
    with database.transaction() as connection:
        connection.execute(INSERT_RUN, ("kept",))
    connection = sqlite3.connect(existing)
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.close()
    assert journal_mode(existing) == "delete"

    RadarDatabase(existing).migrate()

    assert journal_mode(existing) == "wal"
    with database.read() as connection:
        rows = connection.execute("SELECT run_id FROM scheduler_runs").fetchall()
    assert [row[0] for row in rows] == ["kept"]


def test_read_does_not_wait_for_an_open_write_transaction(database):
    from app.radar.repositories import RadarDatabase

    # The default 30 s busy timeout would fail the test long before it expired.
    reader = RadarDatabase(database.path, timeout_seconds=0.05)
    writing = threading.Event()
    release = threading.Event()

    def write():
        with database.transaction() as connection:
            connection.execute(INSERT_RUN, ("uncommitted",))
            writing.set()
            release.wait(5)

    writer = threading.Thread(target=write)
    writer.start()
    try:
        assert writing.wait(5)
        with reader.read() as connection:
            count = connection.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
        # A write transaction, by contrast, has to wait for the writer.
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with reader.transaction():
                pass
    finally:
        release.set()
        writer.join()

    assert count == 0


def test_read_rejects_writes(database):
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        with database.read() as connection:
            connection.execute(INSERT_RUN, ("rejected",))


def test_transaction_rolls_back_every_statement_on_error(database):
    with pytest.raises(RuntimeError):
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO scheduler_runs (run_id, loop, status, started_at, "
                "processed_count, error_count, message) "
                "VALUES ('run-1', 'fast', 'running', '2026-10-10T12:00:00.000000+00:00', 0, 0, '')"
            )
            raise RuntimeError("abort")

    with database.transaction() as connection:
        count = connection.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
    assert count == 0


def test_nested_transactions_join_the_outer_transaction(database):
    with pytest.raises(RuntimeError):
        with database.transaction() as outer:
            with database.transaction() as inner:
                assert inner is outer
                inner.execute(
                    "INSERT INTO scheduler_runs (run_id, loop, status, started_at, "
                    "processed_count, error_count, message) "
                    "VALUES ('run-1', 'fast', 'running', "
                    "'2026-10-10T12:00:00.000000+00:00', 0, 0, '')"
                )
            raise RuntimeError("abort after the inner block")

    with database.transaction() as connection:
        count = connection.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
    assert count == 0


def test_foreign_keys_are_enforced(database):
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO relationships (source_entity_id, target_entity_id, "
                "relationship_type, metadata, first_seen_at, last_seen_at) "
                "VALUES ('vault:1:0xmissing', 'market:1:0xmissing', 'allocated_to', "
                "'{}', '2026-10-10T12:00:00.000000+00:00', "
                "'2026-10-10T12:00:00.000000+00:00')"
            )


def test_scheduler_runs_are_recorded_and_updated(database):
    from app.radar.contracts import SchedulerRun
    from app.radar.repositories import SchedulerRunRepository

    runs = SchedulerRunRepository(database)
    started = SchedulerRun(
        run_id="fast-1",
        loop="fast",
        status="running",
        started_at=NOW,
        finished_at=None,
        processed_count=0,
        error_count=0,
        message="",
    )
    runs.record(started)
    runs.record(
        started.model_copy(
            update={
                "status": "partial",
                "finished_at": NOW + timedelta(seconds=30),
                "processed_count": 12,
                "error_count": 1,
                "message": "chain 8453 unavailable",
            }
        )
    )
    runs.record(
        SchedulerRun(
            run_id="fast-2",
            loop="fast",
            status="skipped",
            started_at=NOW + timedelta(minutes=5),
            finished_at=NOW + timedelta(minutes=5),
            processed_count=0,
            error_count=0,
            message="previous fast cycle still running",
        )
    )

    latest = runs.latest(loop="fast", limit=10)

    assert [run.run_id for run in latest] == ["fast-2", "fast-1"]
    assert latest[1].status == "partial"
    assert latest[1].error_count == 1
    assert latest[0].status == "skipped"


def test_derivation_migration_adds_nullable_columns_and_keeps_existing_health(
    tmp_path, monkeypatch
):
    import shutil

    from app.radar.repositories import RadarDatabase, StateRepository
    from app.radar.repositories import database as database_module

    original = database_module.MIGRATIONS_DIRECTORY
    before = tmp_path / "migrations"
    before.mkdir()
    for name in ("001_radar.sql", "002_latest_health.sql"):
        shutil.copy(original / name, before / name)
    path = tmp_path / "radar.sqlite3"
    monkeypatch.setattr(database_module, "MIGRATIONS_DIRECTORY", before)
    RadarDatabase(path).migrate()
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO entities (entity_id, entity_type, chain_id, address_or_key, label, "
        "source, catalog, visible, metadata, first_seen_at, last_seen_at) VALUES "
        "('vault:1:old', 'vault', 1, 'old', 'Old', 'fixture', 0, 1, '{}', "
        "'2026-10-10T12:00:00.000000+00:00', '2026-10-10T12:00:00.000000+00:00')"
    )
    for table in ("health_states", "latest_health_states"):
        connection.execute(
            f"INSERT INTO {table} (entity_id, evaluated_at, status, score, reasons, "
            "missing_fields, rule_version) VALUES ('vault:1:old', "
            "'2026-10-10T12:00:00.000000+00:00', 'HEALTHY', '1', '[\"old\"]', '[]', 'v0')"
        )
    connection.commit()
    connection.close()

    monkeypatch.setattr(database_module, "MIGRATIONS_DIRECTORY", original)
    database = RadarDatabase(path)
    database.migrate()
    database.migrate()

    assert database.applied_migrations()[-1] == "003_health_derivation"
    old = StateRepository(database).latest_health("vault:1:old")
    assert old is not None and old.reasons == ("old",) and old.derivation is None
    connection = sqlite3.connect(path)
    try:
        for table in ("health_states", "latest_health_states"):
            names = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            assert names.count("derivation") == 1
    finally:
        connection.close()


def test_concurrent_migrations_apply_each_script_once(tmp_path):
    from app.radar.repositories import RadarDatabase

    path = tmp_path / "radar.sqlite3"
    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def migrate():
        start.wait()
        try:
            RadarDatabase(path).migrate()
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=migrate) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert RadarDatabase(path).applied_migrations() == (
        "001_radar",
        "002_latest_health",
        "003_health_derivation",
    )


def test_a_failing_migration_rolls_back_and_is_not_recorded(tmp_path, monkeypatch):
    import shutil

    from app.radar.repositories import RadarDatabase
    from app.radar.repositories import database as database_module

    original = database_module.MIGRATIONS_DIRECTORY
    broken = tmp_path / "migrations"
    broken.mkdir()
    for name in ("001_radar.sql", "002_latest_health.sql"):
        shutil.copy(original / name, broken / name)
    (broken / "003_broken.sql").write_text(
        "ALTER TABLE health_states ADD COLUMN derivation TEXT;\n"
        "ALTER TABLE no_such_table ADD COLUMN derivation TEXT;\n"
    )
    monkeypatch.setattr(database_module, "MIGRATIONS_DIRECTORY", broken)
    database = RadarDatabase(tmp_path / "radar.sqlite3")

    with pytest.raises(sqlite3.OperationalError):
        database.migrate()

    assert database.applied_migrations() == ("001_radar", "002_latest_health")
    connection = sqlite3.connect(database.path)
    try:
        names = [row[1] for row in connection.execute("PRAGMA table_info(health_states)")]
    finally:
        connection.close()
    assert "derivation" not in names
