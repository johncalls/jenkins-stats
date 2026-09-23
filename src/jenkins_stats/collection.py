"""Application workflow for collecting Jenkins build statistics."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from os import PathLike, fspath
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

    from jenkins_stats.models import Build, Job

__all__ = [
    "DEFAULT_LOOKBACK",
    "BuildCollectionOptions",
    "CollectionResult",
    "JenkinsBuildClient",
    "SkippedJob",
    "Store",
    "collect",
    "collect_job_builds",
    "collect_jobs",
]

DEFAULT_LOOKBACK = timedelta(days=1)


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

    def with_destination(self, destination: str | PathLike[str]) -> CollectionResult:
        """Return this result annotated with where data was stored."""
        return CollectionResult(
            job_count=self.job_count,
            build_count=self.build_count,
            destination=fspath(destination),
            skipped_jobs=self.skipped_jobs,
        )

    def output_lines(self) -> tuple[str, ...]:
        """Return the console summary lines for this collection run."""
        destination = f" in {self.destination}" if self.destination is not None else ""
        summary = (
            f"Stored {self.build_count} builds for {self.job_count} jobs{destination}"
        )
        warnings = tuple(
            f"WARNING: skipped builds for {job.job_full_name}: {job.reason}"
            for job in self.skipped_jobs
        )
        return (summary, *warnings)


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
    store.upsert_jobs(jobs)
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
    )


def collect_jobs(client: JenkinsBuildClient, store: Store) -> CollectionResult:
    """Discover visible Jenkins jobs and store them without retrieving builds."""
    jobs = list(client.iter_jobs())
    store.upsert_jobs(jobs)
    return CollectionResult(job_count=len(jobs), build_count=0)


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
    return CollectionResult(job_count=1, build_count=build_count)


def _skip_reason(job: Job) -> SkippedJob | None:
    if job.supports_build_collection:
        return None
    if job.jenkins_class is None:
        reason = "Jenkins job class is unknown; rediscover jobs to refresh metadata"
    else:
        reason = f"unsupported Jenkins job class {job.jenkins_class}"
    return SkippedJob(job.full_name, job.jenkins_class, reason)


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
    if options.since is None and options.lookback is None:
        return replace(options, lookback=DEFAULT_LOOKBACK)
    return options
