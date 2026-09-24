from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
from pydantic import HttpUrl

from jenkins_stats import sqlite_store
from jenkins_stats.models import Build, BuildStatus, Job
from jenkins_stats.sqlite_store import (
    SqliteStore,
    SqliteStoreConnectionError,
    SqliteStoreMigrationError,
    SqliteStoreOperationError,
)

BASE_URL = "https://jenkins.example/"
JOB_URL = f"{BASE_URL}job/folder/job/example/"
BUILD_URL = f"{JOB_URL}42/"


def http_url(value: str) -> HttpUrl:
    return HttpUrl(value)


def example_job(
    *,
    full_name: str = "folder/example",
    display_name: str | None = "Example",
    url: str = JOB_URL,
    jenkins_class: str | None = "org.jenkinsci.plugins.workflow.job.WorkflowJob",
    deleted_at: datetime | None = None,
) -> Job:
    return Job(
        full_name=full_name,
        display_name=display_name,
        url=http_url(url),
        jenkins_class=jenkins_class,
        deleted_at=deleted_at,
    )


def example_build(
    *,
    job_full_name: str = "folder/example",
    number: int = 42,
    status: BuildStatus = BuildStatus.SUCCESS,
    url: str = BUILD_URL,
) -> Build:
    return Build(
        job_full_name=job_full_name,
        number=number,
        scheduled_time=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        start_time=datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC),
        duration=timedelta(seconds=55),
        status=status,
        url=http_url(url),
        jenkins_class="org.jenkinsci.plugins.workflow.job.WorkflowRun",
    )


@pytest.fixture
def migrated_store(tmp_path: Path) -> Iterator[SqliteStore]:
    """Yield a migrated store for tests that do not care about the path."""
    with SqliteStore.open_migrated(tmp_path / "jenkins.sqlite") as store:
        yield store


def foreign_keys_enabled(connection: sqlite3.Connection) -> bool:
    row = connection.execute("PRAGMA foreign_keys").fetchone()
    assert row is not None
    value = row[0]
    assert type(value) is int
    return value == 1


def migration_names(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        'SELECT name FROM "_sqlite_migrations" WHERE migration_set = ?',
        ("jenkins_stats",),
    ).fetchall()
    return [name for (name,) in rows if isinstance(name, str)]


def end_time_column_hidden_flag(store: SqliteStore) -> int:
    row = store.db.conn.execute(
        """
        SELECT hidden
        FROM pragma_table_xinfo('builds')
        WHERE name = 'end_time_ms'
        """,
    ).fetchone()
    assert row is not None
    value = row[0]
    assert type(value) is int
    return value


def assert_connection_closed(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_open_migrated_returns_store_with_complete_schema(tmp_path: Path) -> None:
    # Given no existing database file.
    database_path = tmp_path / "jenkins.sqlite"

    # When the store is opened through the migrated context factory.
    with SqliteStore.open_migrated(str(database_path)) as store:
        # Then the schema is ready for normal constrained writes.
        assert migration_names(store.db.conn) == [
            "0001_initial_schema",
            "0002_jenkins_classes",
            "0003_deleted_jobs",
        ]
        store.upsert_job(example_job())
        with pytest.raises(SqliteStoreOperationError, match="upsert build"):
            store.upsert_build(example_build(job_full_name="folder/missing"))


def test_open_migrated_closes_connection_when_migration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a factory-opened store whose migration will fail before it is yielded.
    migration_connection: sqlite3.Connection | None = None

    def fail_migration(store: SqliteStore) -> None:
        nonlocal migration_connection
        migration_connection = store.db.conn
        raise SqliteStoreMigrationError("boom")

    monkeypatch.setattr(SqliteStore, "_migrate", fail_migration)

    # When the migrated context is entered.
    with (
        pytest.raises(SqliteStoreMigrationError, match="boom"),
        SqliteStore.open_migrated(tmp_path / "jenkins.sqlite"),
    ):
        pytest.fail("store was yielded after failed migration")

    # Then the connection is closed despite no store being yielded.
    assert migration_connection is not None
    assert_connection_closed(migration_connection)


def test_open_migrated_closes_connection_on_normal_exit(tmp_path: Path) -> None:
    # Given a migrated store from the path-backed context factory.
    connection: sqlite3.Connection | None = None

    # When its context exits normally.
    with SqliteStore.open_migrated(tmp_path / "jenkins.sqlite") as store:
        connection = store.db.conn
        store.upsert_job(example_job())

    # Then the factory-owned connection is closed.
    assert connection is not None
    assert_connection_closed(connection)


def test_open_migrated_closes_connection_on_exception(tmp_path: Path) -> None:
    # Given a migrated store from the path-backed context factory.
    connection: sqlite3.Connection | None = None

    def fail_collection() -> None:
        nonlocal connection
        with SqliteStore.open_migrated(tmp_path / "jenkins.sqlite") as store:
            connection = store.db.conn
            raise RuntimeError("boom")

    # When its context exits because collection code fails.
    with pytest.raises(RuntimeError, match="boom"):
        fail_collection()

    # Then the factory-owned connection is still closed.
    assert connection is not None
    assert_connection_closed(connection)


@pytest.mark.parametrize("invalid_target", ["path-and-memory", "neither"])
def test_open_migrated_requires_exactly_one_database_target(
    invalid_target: str,
    tmp_path: Path,
) -> None:
    # Given an invalid factory target selection.
    if invalid_target == "path-and-memory":
        path = tmp_path / "jenkins.sqlite"
        memory = True
    elif invalid_target == "neither":
        path = None
        memory = False
    else:
        raise AssertionError(f"Unsupported invalid target: {invalid_target}")

    # When the migrated factory is entered.
    # Then it rejects the selection before opening a database.
    with (
        pytest.raises(ValueError, match="Either specify a database path"),
        SqliteStore.open_migrated(path, memory=memory),
    ):
        pytest.fail("factory accepted an invalid database target selection")


def test_open_migrated_wraps_invalid_database_path_value() -> None:
    # Given a path value that SQLite cannot open.
    # When the migrated factory creates the sqlite-utils database.
    with (
        pytest.raises(SqliteStoreConnectionError, match="embedded null byte"),
        SqliteStore.open_migrated("\x00"),
    ):
        pytest.fail("factory accepted an invalid SQLite path value")


def test_open_migrated_wraps_connection_configuration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given SQLite fails after opening but before migrations run.
    def fail_configuration(_db: object) -> None:
        raise sqlite3.OperationalError("PRAGMA failed")

    monkeypatch.setattr(
        sqlite_store,
        "_require_foreign_key_enforcement",
        fail_configuration,
    )

    # When the migrated factory configures the connection.
    with (
        pytest.raises(
            SqliteStoreConnectionError,
            match="SQLite connection configuration failed: PRAGMA failed",
        ),
        SqliteStore.open_migrated(tmp_path / "jenkins.sqlite"),
    ):
        pytest.fail("factory accepted an unconfigured SQLite connection")


def test_open_migrated_supports_in_memory_database() -> None:
    # Given the in-memory factory mode.
    connection: sqlite3.Connection | None = None

    # When a store writes within its factory context.
    with SqliteStore.open_migrated(memory=True) as store:
        connection = store.db.conn
        store.upsert_job(example_job())
        assert store.get_job("folder/example") == example_job()

    # Then factory cleanup closes the in-memory connection too.
    assert connection is not None
    assert_connection_closed(connection)


def test_open_migrated_enables_foreign_keys(tmp_path: Path) -> None:
    # Given a path-backed store.
    # When the migrated factory opens it.
    with SqliteStore.open_migrated(tmp_path / "jenkins.sqlite") as store:
        # Then its factory-owned connection has foreign-key enforcement enabled.
        assert foreign_keys_enabled(store.db.conn)


def test_open_migrated_creates_schema_and_records_sqlite_utils_migration(
    tmp_path: Path,
) -> None:
    # Given a new store with no schema yet.
    # When the migrated factory creates it.
    with SqliteStore.open_migrated(tmp_path / "jenkins.sqlite") as store:
        # Then the schema exists and all migrations are recorded once.
        assert migration_names(store.db.conn) == [
            "0001_initial_schema",
            "0002_jenkins_classes",
            "0003_deleted_jobs",
        ]
        assert store.table_names() == ["_sqlite_migrations", "builds", "jobs"]
        assert end_time_column_hidden_flag(store) == 2


def test_upsert_job_is_idempotent(migrated_store: SqliteStore) -> None:
    # Given a migrated store and two versions of the same job.
    original_job = example_job(display_name="Old name")
    updated_job = example_job(display_name="New name")

    # When the job is upserted twice.
    migrated_store.upsert_job(original_job)
    migrated_store.upsert_job(updated_job)

    # Then the row is updated in place instead of duplicated.
    assert migrated_store.get_job("folder/example") == updated_job


def test_deleted_job_timestamp_cannot_be_reset_by_upsert(
    migrated_store: SqliteStore,
) -> None:
    # Given a stored job that was previously marked deleted.
    deleted_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    removed = example_job(
        full_name="folder/removed",
        url=f"{BASE_URL}job/removed/",
        deleted_at=deleted_at,
    )
    migrated_store.upsert_job(removed)

    # When the same full name is upserted as an active job.
    with pytest.raises(
        SqliteStoreOperationError,
        match="deleted_at cannot be reset for a previously deleted job",
    ):
        migrated_store.upsert_job(removed.model_copy(update={"deleted_at": None}))

    # Then the tombstone remains and active job iteration excludes it.
    assert migrated_store.get_job("folder/removed") == removed
    assert list(migrated_store.iter_jobs()) == []


def test_upsert_build_preserves_pre_epoch_millisecond_values_from_non_utc_inputs(
    migrated_store: SqliteStore,
) -> None:
    migrated_store.upsert_job(example_job())

    # Given a build with exact millisecond values before the Unix epoch, supplied
    # in a non-UTC timezone.
    local_timezone = timezone(timedelta(hours=5, minutes=30))
    build = Build(
        job_full_name="folder/example",
        number=42,
        scheduled_time=datetime(1970, 1, 1, 5, 29, 59, 998_000, tzinfo=local_timezone),
        start_time=datetime(1970, 1, 1, 5, 29, 59, 999_000, tzinfo=local_timezone),
        duration=timedelta(milliseconds=2),
        status=BuildStatus.SUCCESS,
        url=http_url(BUILD_URL),
    )

    # When the build is persisted and read back.
    migrated_store.upsert_build(build)
    round_tripped = migrated_store.get_build("folder/example", 42)

    # Then the store preserves the canonical domain model.
    assert round_tripped == build
    assert round_tripped is not None
    assert round_tripped.end_time == datetime(1970, 1, 1, 0, 0, 0, 1_000, tzinfo=UTC)
    assert list(migrated_store.iter_builds()) == [round_tripped]


def test_upsert_build_is_idempotent_and_round_trips_domain_model(
    migrated_store: SqliteStore,
) -> None:
    # Given a migrated store with the build's parent job.
    migrated_store.upsert_job(example_job())
    failed_build = example_build(status=BuildStatus.FAILURE)
    successful_build = example_build(status=BuildStatus.SUCCESS)

    # When the same build key is upserted with new mutable fields.
    migrated_store.upsert_build(failed_build)
    migrated_store.upsert_build(successful_build)

    # Then the latest build round-trips through SQLite as the domain model.
    assert migrated_store.get_build("folder/example", 42) == successful_build


def test_build_requires_existing_job(migrated_store: SqliteStore) -> None:
    # When a build references a missing parent job, then the store rejects it.
    with pytest.raises(SqliteStoreOperationError, match="upsert build"):
        migrated_store.upsert_build(example_build())


def test_upsert_jobs_persists_successful_bulk_batches_and_ignores_empty_iterables(
    migrated_store: SqliteStore,
) -> None:
    # Given enough jobs to force sqlite-utils to process multiple batches.
    jobs = [
        example_job(
            full_name=f"folder/job-{index:03d}",
            display_name=f"Job {index}",
            url=f"{BASE_URL}job/folder/job/job-{index:03d}/",
        )
        for index in range(150)
    ]

    # When the bulk upsert succeeds and an empty iterable is later supplied.
    migrated_store.upsert_jobs(jobs)
    migrated_store.upsert_jobs(())

    # Then every job remains persisted in stable iteration order.
    assert list(migrated_store.iter_jobs()) == jobs


def test_upsert_builds_persists_successful_bulk_batches_and_ignores_empty_iterables(
    migrated_store: SqliteStore,
) -> None:
    # Given a parent job and enough builds to force multiple sqlite-utils batches.
    migrated_store.upsert_job(example_job())
    builds = [
        example_build(
            number=index,
            url=f"{JOB_URL}{index}/",
        )
        for index in range(1, 151)
    ]

    # When the bulk upsert succeeds and an empty iterable is later supplied.
    migrated_store.upsert_builds(builds)
    migrated_store.upsert_builds([])

    # Then every build remains persisted in completion/key iteration order.
    assert list(migrated_store.iter_builds()) == builds


def test_upsert_jobs_rolls_back_inserts_and_updates_when_generator_fails_after_batch(
    migrated_store: SqliteStore,
) -> None:
    # Given an existing job that the generator-backed bulk operation will update.
    original_job = example_job(display_name="Original")
    attempted_update = example_job(display_name="Updated")
    migrated_store.upsert_job(original_job)

    # And a generator that yields more than one sqlite-utils batch before failing.
    # The yielded rows prove that both updates and inserts are rolled back.
    def jobs() -> Iterator[Job]:
        yield attempted_update
        for index in range(1, 150):
            yield example_job(
                full_name=f"folder/job-{index:03d}",
                display_name=f"Job {index}",
                url=f"{BASE_URL}job/folder/job/job-{index:03d}/",
            )
        raise RuntimeError("boom")

    # When input generation fails after sqlite-utils has processed a batch.
    with pytest.raises(RuntimeError, match="boom"):
        migrated_store.upsert_jobs(jobs())

    # Then the attempted update and all successful-looking inserts are gone.
    assert migrated_store.get_job("folder/example") == original_job
    assert list(migrated_store.iter_jobs()) == [original_job]


def test_upsert_builds_rolls_back_inserts_and_updates_when_generator_fails_after_batch(
    migrated_store: SqliteStore,
) -> None:
    # Given an existing build that the generator-backed bulk operation will update.
    migrated_store.upsert_job(example_job())
    original_build = example_build(
        number=1,
        status=BuildStatus.SUCCESS,
        url=f"{JOB_URL}1/",
    )
    attempted_update = example_build(
        number=1,
        status=BuildStatus.FAILURE,
        url=f"{JOB_URL}1/",
    )
    migrated_store.upsert_build(original_build)

    # And a generator that yields more than one sqlite-utils batch before failing.
    # The yielded rows prove that both updates and inserts are rolled back.
    def builds() -> Iterator[Build]:
        yield attempted_update
        for index in range(2, 151):
            yield example_build(
                number=index,
                url=f"{JOB_URL}{index}/",
            )
        raise RuntimeError("boom")

    # When input generation fails after sqlite-utils has processed a batch.
    with pytest.raises(RuntimeError, match="boom"):
        migrated_store.upsert_builds(builds())

    # Then the attempted update and all successful-looking inserts are gone.
    assert migrated_store.get_build("folder/example", 1) == original_build
    assert list(migrated_store.iter_builds()) == [original_build]


def test_upsert_jobs_rolls_back_inserts_and_updates_on_late_constraint_failure(
    migrated_store: SqliteStore,
) -> None:
    # Given an existing job that the bulk operation will try to update.
    original_job = example_job(display_name="Original")
    attempted_update = example_job(display_name="Updated")
    migrated_store.upsert_job(original_job)

    # And another existing job that owns a unique URL.
    duplicate_url = f"{BASE_URL}job/folder/job/duplicate/"
    job_with_existing_url = example_job(
        full_name="folder/duplicate",
        display_name="Duplicate owner",
        url=duplicate_url,
    )
    migrated_store.upsert_job(job_with_existing_url)

    # And a bulk payload whose final row violates the URL uniqueness constraint.
    # The preceding rows prove that both updates and inserts are rolled back.
    jobs = [attempted_update]
    jobs.extend(
        example_job(
            full_name=f"folder/job-{index:03d}",
            display_name=f"Job {index}",
            url=f"{BASE_URL}job/folder/job/job-{index:03d}/",
        )
        for index in range(1, 149)
    )
    jobs.append(
        example_job(
            full_name="folder/conflicting-url",
            display_name="Conflicting URL",
            url=duplicate_url,
        )
    )

    # When the late constraint failure aborts the bulk upsert.
    with pytest.raises(SqliteStoreOperationError, match="upsert jobs"):
        migrated_store.upsert_jobs(jobs)

    # Then the attempted update and all successful-looking inserts are gone.
    assert migrated_store.get_job("folder/example") == original_job
    assert list(migrated_store.iter_jobs()) == [job_with_existing_url, original_job]


def test_upsert_builds_rolls_back_inserts_and_updates_on_late_constraint_failure(
    migrated_store: SqliteStore,
) -> None:
    migrated_store.upsert_job(example_job())

    # Given an existing build that the bulk operation will try to update.
    original_build = example_build(
        number=1,
        status=BuildStatus.SUCCESS,
        url=f"{JOB_URL}1/",
    )
    attempted_update = example_build(
        number=1,
        status=BuildStatus.FAILURE,
        url=f"{JOB_URL}1/",
    )
    migrated_store.upsert_build(original_build)

    # And a bulk payload whose final row violates the job foreign key constraint.
    # The preceding rows prove that both updates and inserts are rolled back.
    builds = [attempted_update]
    builds.extend(
        example_build(
            number=index,
            url=f"{JOB_URL}{index}/",
        )
        for index in range(2, 150)
    )
    builds.append(
        example_build(
            job_full_name="folder/missing",
            number=150,
            url=f"{BASE_URL}job/folder/job/missing/150/",
        )
    )

    # When the late constraint failure aborts the bulk upsert.
    with pytest.raises(SqliteStoreOperationError, match="upsert builds"):
        migrated_store.upsert_builds(builds)

    # Then the attempted update and all successful-looking inserts are gone.
    assert migrated_store.get_build("folder/example", 1) == original_build
    assert list(migrated_store.iter_builds()) == [original_build]


def test_missing_record_reads_return_none(migrated_store: SqliteStore) -> None:
    # Given an empty migrated store.
    # When absent records are read by primary key.
    # Then absence is represented as None rather than a sqlite-utils exception.
    assert migrated_store.get_job("folder/missing") is None
    assert migrated_store.get_build("folder/missing", 1) is None


def test_iter_builds_orders_ties_by_stable_key_and_filters_by_job(
    migrated_store: SqliteStore,
) -> None:
    # Given multiple jobs with builds that share the same completion timestamp.
    first_job = example_job(
        full_name="folder/a",
        display_name="A",
        url=f"{BASE_URL}job/folder/job/a/",
    )
    second_job = example_job(
        full_name="folder/b",
        display_name="B",
        url=f"{BASE_URL}job/folder/job/b/",
    )
    migrated_store.upsert_jobs([second_job, first_job])
    builds = [
        example_build(
            job_full_name="folder/b",
            number=2,
            url=f"{BASE_URL}job/folder/job/b/2/",
        ),
        example_build(
            job_full_name="folder/a",
            number=2,
            url=f"{BASE_URL}job/folder/job/a/2/",
        ),
        example_build(
            job_full_name="folder/b",
            number=1,
            url=f"{BASE_URL}job/folder/job/b/1/",
        ),
        example_build(
            job_full_name="folder/a",
            number=1,
            url=f"{BASE_URL}job/folder/job/a/1/",
        ),
    ]

    # When all builds are persisted out of order.
    migrated_store.upsert_builds(builds)

    # Then global iteration uses completion time plus stable build key, and
    # the job filter keeps the same ordering for that job's builds.
    expected_global = [builds[3], builds[1], builds[2], builds[0]]
    assert list(migrated_store.iter_builds()) == expected_global
    assert list(migrated_store.iter_builds(job_full_name="folder/b")) == [
        builds[2],
        builds[0],
    ]


def test_all_statuses_persist_after_reopening_database(tmp_path: Path) -> None:
    # Given builds covering every terminal status written to a database file.
    database_path = tmp_path / "jenkins.sqlite"
    expected_builds = [
        example_build(
            number=index,
            status=status,
            url=f"{JOB_URL}{index}/",
        )
        for index, status in enumerate(BuildStatus, start=1)
    ]
    with SqliteStore.open_migrated(database_path) as store:
        store.upsert_job(example_job())
        store.upsert_builds(expected_builds)

    # When the file is opened by a new store instance.
    with SqliteStore.open_migrated(database_path) as reopened_store:
        reopened_job = reopened_store.get_job("folder/example")
        reopened_builds = list(reopened_store.iter_builds())

    # Then jobs and every status round-trip beyond the original connection.
    assert reopened_job == example_job()
    assert reopened_builds == expected_builds
    assert [build.status for build in reopened_builds] == list(BuildStatus)
