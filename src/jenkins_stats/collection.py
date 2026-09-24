"""Application workflow for collecting Jenkins build statistics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from os import PathLike, fspath
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import timedelta

    from jenkins_stats.models import Build, Job

__all__ = [
    "BuildCollectionOptions",
    "CollectionResult",
    "JenkinsBuildClient",
    "SkippedJob",
    "Store",
    "collect",
    "collect_job_builds",
    "collect_jobs",
    "collect_stored_job_builds",
]


class JenkinsBuildClient(Protocol):
    """Client operations required by Jenkins collection workflows."""

    def iter_jobs(self) -> Iterable[Job]:
        """Yield visible Jenkins jobs."""
        ...

    def iter_builds(
        self,
        job: Job,
        *,
        page_size: int = 100,
        since: datetime | None = None,
        lookback: timedelta | None = None,
    ) -> Iterable[Build]:
        """Yield builds for a Jenkins job."""
        ...


class Store(Protocol):
    """Persistence operations required by Jenkins collection workflows."""

    def upsert_jobs(self, jobs: Iterable[Job]) -> None:
        """Insert or update discovered Jenkins jobs."""
        ...

    def get_job(self, full_name: str) -> Job | None:
        """Return a stored Jenkins job by full name, if present."""
        ...

    def iter_jobs(self) -> Iterable[Job]:
        """Yield active stored Jenkins jobs."""
        ...

    def upsert_builds(self, builds: Iterable[Build]) -> None:
        """Insert or update collected Jenkins builds."""
        ...


@dataclass(frozen=True)
class BuildCollectionOptions:
    """Completion-time and pagination options for one job's builds."""

    page_size: int = 100
    since: datetime | None = None
    lookback: timedelta | None = None


_DEFAULT_BUILD_COLLECTION_OPTIONS = BuildCollectionOptions()


@dataclass(frozen=True)
class SkippedJob:
    """A job whose builds were not collected, with the reason for skipping."""

    job_full_name: str
    jenkins_class: str | None
    reason: str


@dataclass(frozen=True)
class CollectionResult:
    """Summary of a collection run."""

    job_count: int
    build_count: int
    destination: str | None = None
    skipped_jobs: tuple[SkippedJob, ...] = ()
    completion_filter: str | None = None
    deleted_job_count: int = 0

    def with_destination(self, destination: str | PathLike[str]) -> CollectionResult:
        """Return this result annotated with where data was stored."""
        return CollectionResult(
            job_count=self.job_count,
            build_count=self.build_count,
            destination=fspath(destination),
            skipped_jobs=self.skipped_jobs,
            completion_filter=self.completion_filter,
            deleted_job_count=self.deleted_job_count,
        )

    def output_lines(self) -> tuple[str, ...]:
        """Return the console summary lines for this collection run."""
        destination = f" in {self.destination}" if self.destination is not None else ""
        summary = (
            f"Stored {self.build_count} builds for {self.job_count} jobs{destination}"
        )
        deleted_line = (
            (f"Marked {self.deleted_job_count} job as deleted",)
            if self.deleted_job_count == 1
            else (
                (f"Marked {self.deleted_job_count} jobs as deleted",)
                if self.deleted_job_count > 1
                else ()
            )
        )
        filter_line = (
            (f"Completion filter: {self.completion_filter}",)
            if self.completion_filter is not None
            else ()
        )
        warnings = tuple(
            f"WARNING: skipped builds for {job.job_full_name}: {job.reason}"
            for job in self.skipped_jobs
        )
        return (summary, *deleted_line, *filter_line, *warnings)


def collect(
    client: JenkinsBuildClient,
    store: Store,
    *,
    page_size: int = 100,
    since: datetime | None = None,
    lookback: timedelta | None = None,
) -> CollectionResult:
    """Collect visible Jenkins jobs and their builds into a store."""
    options = _build_collection_options(page_size, since, lookback)

    jobs = list(client.iter_jobs())
    deleted_job_count = _sync_visible_jobs(store, jobs)
    build_count = 0
    skipped_jobs: list[SkippedJob] = []
    for job in jobs:
        skipped = _skip_reason(job)
        if skipped is not None:
            skipped_jobs.append(skipped)
            continue
        build_count += _collect_job_builds(client, store, job, options)
    return CollectionResult(
        job_count=len(jobs),
        build_count=build_count,
        skipped_jobs=tuple(skipped_jobs),
        completion_filter=_completion_filter_description(options),
        deleted_job_count=deleted_job_count,
    )


def collect_jobs(client: JenkinsBuildClient, store: Store) -> CollectionResult:
    """Discover visible Jenkins jobs and store them without retrieving builds."""
    jobs = list(client.iter_jobs())
    deleted_job_count = _sync_visible_jobs(store, jobs)
    return CollectionResult(
        job_count=len(jobs),
        build_count=0,
        deleted_job_count=deleted_job_count,
    )


def collect_job_builds(
    client: JenkinsBuildClient,
    store: Store,
    job_full_name: str,
    options: BuildCollectionOptions = _DEFAULT_BUILD_COLLECTION_OPTIONS,
) -> CollectionResult:
    """Collect builds for one job previously stored by :func:`collect_jobs`."""
    effective_options = _effective_build_collection_options(options)
    job = store.get_job(job_full_name)
    if job is None:
        raise ValueError(f"Job is not stored: {job_full_name}")
    if (skipped := _skip_reason(job)) is not None:
        return CollectionResult(job_count=1, build_count=0, skipped_jobs=(skipped,))
    build_count = _collect_job_builds(client, store, job, effective_options)
    return CollectionResult(
        job_count=1,
        build_count=build_count,
        completion_filter=_completion_filter_description(effective_options),
    )


def collect_stored_job_builds(
    client: JenkinsBuildClient,
    store: Store,
    options: BuildCollectionOptions = _DEFAULT_BUILD_COLLECTION_OPTIONS,
) -> CollectionResult:
    """Collect builds for every job already stored without discovering jobs."""
    effective_options = _effective_build_collection_options(options)
    jobs = list(store.iter_jobs())
    build_count = 0
    skipped_jobs: list[SkippedJob] = []
    for job in jobs:
        skipped = _skip_reason(job)
        if skipped is not None:
            skipped_jobs.append(skipped)
            continue
        build_count += _collect_job_builds(client, store, job, effective_options)
    return CollectionResult(
        job_count=len(jobs),
        build_count=build_count,
        skipped_jobs=tuple(skipped_jobs),
        completion_filter=_completion_filter_description(effective_options),
    )


def _sync_visible_jobs(store: Store, jobs: list[Job]) -> int:
    visible_full_names = {job.full_name for job in jobs}
    deleted_at = _utc_now_millisecond()
    deleted_jobs = [
        job.model_copy(update={"deleted_at": deleted_at})
        for job in store.iter_jobs()
        if job.deleted_at is None and job.full_name not in visible_full_names
    ]
    store.upsert_jobs([*jobs, *deleted_jobs])
    return len(deleted_jobs)


def _utc_now_millisecond() -> datetime:
    now = datetime.now(UTC)
    return now.replace(microsecond=(now.microsecond // 1_000) * 1_000)


def _skip_reason(job: Job) -> SkippedJob | None:
    if job.deleted_at is not None:
        return SkippedJob(
            job.full_name,
            job.jenkins_class,
            "job is marked deleted; rediscover jobs to refresh metadata",
        )
    if job.disabled:
        return SkippedJob(job.full_name, job.jenkins_class, "job is disabled")
    if job.supports_build_collection:
        return None
    if job.jenkins_class is None:
        reason = "Jenkins job class is unknown; rediscover jobs to refresh metadata"
    else:
        reason = f"unsupported Jenkins job class {job.jenkins_class}"
    return SkippedJob(job.full_name, job.jenkins_class, reason)


def _completion_filter_description(options: BuildCollectionOptions) -> str:
    if options.since is not None:
        return f"since {options.since.isoformat()}"
    if options.lookback is not None:
        return f"lookback {options.lookback}"
    return "none (all retained builds)"


def _collect_job_builds(
    client: JenkinsBuildClient,
    store: Store,
    job: Job,
    options: BuildCollectionOptions,
) -> int:
    builds = list(
        client.iter_builds(
            job,
            page_size=options.page_size,
            since=options.since,
            lookback=options.lookback,
        )
    )
    store.upsert_builds(builds)
    return len(builds)


def _build_collection_options(
    page_size: int,
    since: datetime | None,
    lookback: timedelta | None,
) -> BuildCollectionOptions:
    return _effective_build_collection_options(
        BuildCollectionOptions(
            page_size=page_size,
            since=since,
            lookback=lookback,
        )
    )


def _effective_build_collection_options(
    options: BuildCollectionOptions,
) -> BuildCollectionOptions:
    if options.since is not None and options.lookback is not None:
        raise ValueError("Specify either since or lookback, not both")
    if options.page_size <= 0:
        raise ValueError("page_size must be positive")
    return options
