"""SQLite persistence for Jenkins jobs and builds."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self, cast

from pydantic import HttpUrl
from sqlite_utils import Database
from sqlite_utils.db import NotFoundError
from sqlite_utils.migrations import Migrations

from jenkins_stats.models import Build, Job

if TYPE_CHECKING:
    from os import PathLike

__all__ = [
    "SqliteStore",
    "SqliteStoreConnectionError",
    "SqliteStoreError",
    "SqliteStoreMigrationError",
    "SqliteStoreOperationError",
]

# Migration constants are historical data, not live domain-model policy. Keep
# existing migration values frozen and introduce future status changes through new
# migrations so a recorded migration name always means the same schema.
_BUILD_STATUS_VALUES = (
    "SUCCESS",
    "UNSTABLE",
    "FAILURE",
    "ABORTED",
    "NOT_BUILT",
)
_BUILD_STATUS_VALUES_SQL = ", ".join(f"'{status}'" for status in _BUILD_STATUS_VALUES)
_MIGRATIONS = Migrations("jenkins_stats")

type RowValue = str | int | None
type Row = Mapping[str, RowValue]


class SqliteStoreError(RuntimeError):
    """Base exception for SQLite store failures."""


class SqliteStoreConnectionError(SqliteStoreError):
    """Raised when the SQLite database cannot be opened safely."""


class SqliteStoreMigrationError(SqliteStoreError):
    """Raised when migrations cannot safely initialize the store schema."""


class SqliteStoreOperationError(SqliteStoreError):
    """Raised when a migrated store read or write operation fails."""


class _JobsTable(Protocol):
    def upsert_all(
        self,
        records: Iterable[dict[str, RowValue]],
        *,
        pk: str,
    ) -> object: ...

    def get(self, pk_values: str) -> Row: ...

    def rows_where(
        self,
        where: str | None = None,
        where_args: Sequence[object] | None = None,
        *,
        order_by: str | None = None,
    ) -> Iterable[Row]: ...

    def upsert(self, record: dict[str, RowValue], *, pk: str) -> object: ...


class _BuildsTable(Protocol):
    def upsert_all(
        self,
        records: Iterable[dict[str, RowValue]],
        *,
        pk: tuple[str, str],
    ) -> object: ...

    def get(self, pk_values: tuple[str, int]) -> Row: ...

    def rows_where(
        self,
        where: str | None = None,
        where_args: Sequence[object] | None = None,
        *,
        order_by: str | None = None,
    ) -> Iterable[Row]: ...

    def upsert(
        self,
        record: dict[str, RowValue],
        *,
        pk: tuple[str, str],
    ) -> object: ...


def _execute(
    db: Database,
    sql: str,
    params: Sequence[object] = (),
) -> sqlite3.Cursor:
    return db.conn.execute(sql, params)


@_MIGRATIONS(name="0001_initial_schema")
def _initial_schema(db: Database) -> None:
    _execute(
        db,
        """
        CREATE TABLE jobs (
            full_name TEXT PRIMARY KEY,
            url TEXT NOT NULL UNIQUE,
            display_name TEXT
        )
        """,
    )
    _create_builds_table(db)
    _create_build_indexes(db)


@_MIGRATIONS(name="0002_jenkins_classes")
def _jenkins_classes_schema(db: Database) -> None:
    _execute(db, "ALTER TABLE jobs ADD COLUMN jenkins_class TEXT")
    _execute(db, "ALTER TABLE builds ADD COLUMN jenkins_class TEXT")


@_MIGRATIONS(name="0003_deleted_jobs")
def _deleted_jobs_schema(db: Database) -> None:
    _execute(db, "ALTER TABLE jobs ADD COLUMN deleted_at TEXT")
    _execute(
        db,
        """
        CREATE TRIGGER jobs_deleted_at_no_reset
        BEFORE UPDATE OF deleted_at ON jobs
        FOR EACH ROW
        WHEN OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL
        BEGIN
            SELECT RAISE(
                ABORT,
                'deleted_at cannot be reset for a previously deleted job'
            );
        END
        """,
    )


def _create_builds_table(db: Database) -> None:
    _execute(
        db,
        f"""
        CREATE TABLE builds (
            job_full_name TEXT NOT NULL,
            number INTEGER NOT NULL,
            scheduled_time_ms INTEGER NOT NULL,
            start_time_ms INTEGER NOT NULL,
            duration_ms INTEGER NOT NULL,
            end_time_ms INTEGER GENERATED ALWAYS AS
                (start_time_ms + duration_ms) VIRTUAL,
            status TEXT NOT NULL CHECK (status IN ({_BUILD_STATUS_VALUES_SQL})),
            url TEXT NOT NULL UNIQUE,

            PRIMARY KEY (job_full_name, number),
            FOREIGN KEY (job_full_name)
                REFERENCES jobs(full_name)
                ON UPDATE CASCADE
                ON DELETE CASCADE
        )
        """,
    )


def _create_build_indexes(db: Database) -> None:
    _execute(db, "CREATE INDEX builds_end_time_ms_idx ON builds(end_time_ms)")
    _execute(
        db,
        """
        CREATE INDEX builds_job_full_name_end_time_ms_idx
            ON builds(job_full_name, end_time_ms)
        """,
    )
    _execute(db, "CREATE INDEX builds_status_idx ON builds(status)")


class SqliteStore:
    """Persist Jenkins domain models to SQLite without using an ORM.

    Use :meth:`open_migrated` for normal path-backed collection. The constructor
    is a narrow dependency-injection seam: ``db`` must be open with SQLite
    foreign-key enforcement enabled and the store schema already migrated; its
    caller owns closing it.
    """

    def __init__(self, db: Database) -> None:
        """Wrap an open foreign-key-enforcing sqlite-utils database.

        The caller retains database lifetime and transaction ownership. The
        database must already contain the migrated store schema. For the normal
        path-backed lifecycle, use :meth:`open_migrated` instead.
        """
        self.db = db

    @classmethod
    @contextmanager
    def open_migrated(
        cls,
        path: str | PathLike[str] | None = None,
        *,
        memory: bool = False,
    ) -> Generator[Self]:
        """Yield a migrated path-backed or in-memory store and close it on exit."""
        if memory == (path is not None):
            raise ValueError("Either specify a database path or pass memory=True")

        target = path if path is None or isinstance(path, str) else Path(path)
        try:
            db = Database(target, memory=memory)
        except (sqlite3.Error, ValueError) as exc:
            raise SqliteStoreConnectionError(
                f"SQLite connection initialization failed: {exc}"
            ) from exc

        try:
            _require_foreign_key_enforcement(db)
        except SqliteStoreError:
            db.close()
            raise
        except sqlite3.Error as exc:
            db.close()
            raise SqliteStoreConnectionError(
                f"SQLite connection configuration failed: {exc}"
            ) from exc

        try:
            store = cls(db)
            store._migrate()
            yield store
        finally:
            db.close()

    def _migrate(self) -> None:
        """Apply pending sqlite-utils migrations."""
        try:
            _MIGRATIONS.apply(self.db)
        except sqlite3.Error as exc:
            raise SqliteStoreMigrationError(f"SQLite migration failed: {exc}") from exc

    def table_names(self) -> list[str]:
        """Return database table names, sorted for stable diagnostics."""
        try:
            return sorted(self.db.table_names())
        except sqlite3.Error as exc:
            raise SqliteStoreOperationError(
                f"SQLite read table names failed: {exc}"
            ) from exc

    def upsert_job(self, job: Job) -> None:
        """Insert or replace mutable fields for a Jenkins job."""
        try:
            with self.db.atomic():
                self._upsert_job(job)
        except sqlite3.Error as exc:
            raise SqliteStoreOperationError(f"SQLite upsert job failed: {exc}") from exc

    def upsert_jobs(self, jobs: Iterable[Job]) -> None:
        """Insert or replace multiple Jenkins jobs in one transaction."""
        try:
            with self.db.atomic():
                self._jobs_table().upsert_all(
                    (_job_row(job) for job in jobs),
                    pk="full_name",
                )
        except sqlite3.Error as exc:
            raise SqliteStoreOperationError(
                f"SQLite upsert jobs failed: {exc}"
            ) from exc

    def get_job(self, full_name: str) -> Job | None:
        """Return a stored job by full name, if present."""
        try:
            row = self._jobs_table().get(full_name)
        except NotFoundError:
            return None
        except sqlite3.Error as exc:
            raise SqliteStoreOperationError(f"SQLite read job failed: {exc}") from exc
        return _job_from_row(row)

    def iter_jobs(self) -> Iterable[Job]:
        """Yield active stored jobs ordered by full name."""
        try:
            for row in self._jobs_table().rows_where(
                "deleted_at IS NULL",
                order_by="full_name",
            ):
                yield _job_from_row(row)
        except sqlite3.Error as exc:
            raise SqliteStoreOperationError(
                f"SQLite iterate jobs failed: {exc}"
            ) from exc

    def upsert_build(self, build: Build) -> None:
        """Insert or replace mutable fields for a build."""
        try:
            with self.db.atomic():
                self._upsert_build(build)
        except sqlite3.Error as exc:
            raise SqliteStoreOperationError(
                f"SQLite upsert build failed: {exc}"
            ) from exc

    def upsert_builds(self, builds: Iterable[Build]) -> None:
        """Insert or replace multiple builds in one transaction."""
        try:
            with self.db.atomic():
                self._builds_table().upsert_all(
                    (_build_row(build) for build in builds),
                    pk=("job_full_name", "number"),
                )
        except sqlite3.Error as exc:
            raise SqliteStoreOperationError(
                f"SQLite upsert builds failed: {exc}"
            ) from exc

    def get_build(self, job_full_name: str, number: int) -> Build | None:
        """Return a stored build by job full name and build number, if present."""
        try:
            row = self._builds_table().get((job_full_name, number))
        except NotFoundError:
            return None
        except sqlite3.Error as exc:
            raise SqliteStoreOperationError(f"SQLite read build failed: {exc}") from exc
        return _build_from_row(row)

    def iter_builds(self, *, job_full_name: str | None = None) -> Iterable[Build]:
        """Yield stored builds ordered by completion time and stable build key."""
        try:
            if job_full_name is None:
                rows = self._builds_table().rows_where(
                    order_by="end_time_ms, job_full_name, number"
                )
            else:
                rows = self._builds_table().rows_where(
                    "job_full_name = ?",
                    [job_full_name],
                    order_by="end_time_ms, job_full_name, number",
                )
            for row in rows:
                yield _build_from_row(row)
        except sqlite3.Error as exc:
            raise SqliteStoreOperationError(
                f"SQLite iterate builds failed: {exc}"
            ) from exc

    def _upsert_job(self, job: Job) -> None:
        self._jobs_table().upsert(_job_row(job), pk="full_name")

    def _upsert_build(self, build: Build) -> None:
        self._builds_table().upsert(_build_row(build), pk=("job_full_name", "number"))

    def _jobs_table(self) -> _JobsTable:
        return cast("_JobsTable", self.db["jobs"])

    def _builds_table(self) -> _BuildsTable:
        return cast("_BuildsTable", self.db["builds"])


def _require_foreign_key_enforcement(db: Database) -> None:
    _execute(db, "PRAGMA foreign_keys = ON")
    row = _execute(db, "PRAGMA foreign_keys").fetchone()
    if row is not None and row[0] == 1:
        return

    raise SqliteStoreConnectionError(
        "SQLite foreign-key enforcement is disabled for this connection and could "
        "not be enabled. Ensure no transaction is active before opening "
        "SqliteStore, or enable PRAGMA foreign_keys = ON before beginning the "
        "transaction."
    )


def _job_row(job: Job) -> dict[str, RowValue]:
    return {
        "full_name": job.full_name,
        "url": str(job.url),
        "display_name": job.display_name,
        "jenkins_class": job.jenkins_class,
        "deleted_at": (
            job.deleted_at.isoformat() if job.deleted_at is not None else None
        ),
    }


def _job_from_row(row: Row) -> Job:
    return Job(
        full_name=cast("str", row["full_name"]),
        url=HttpUrl(cast("str", row["url"])),
        display_name=cast("str | None", row["display_name"]),
        jenkins_class=cast("str | None", row["jenkins_class"]),
        deleted_at=_datetime_from_row_value(row["deleted_at"]),
    )


def _datetime_from_row_value(value: RowValue) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise TypeError(f"expected datetime text or NULL, got {type(value).__name__}")


def _build_row(build: Build) -> dict[str, RowValue]:
    return cast(
        "dict[str, RowValue]",
        build.model_dump(
            mode="json",
            by_alias=True,
            include={
                "job_full_name",
                "number",
                "scheduled_time",
                "start_time",
                "duration",
                "status",
                "url",
                "jenkins_class",
            },
        ),
    )


def _build_from_row(row: Row) -> Build:
    return Build.model_validate(row)
