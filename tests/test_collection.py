from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import HttpUrl

from jenkins_stats.collection import (
    DEFAULT_LOOKBACK,
    BuildCollectionOptions,
    CollectionResult,
    collect,
    collect_job_builds,
    collect_jobs,
)
from jenkins_stats.models import Build, BuildStatus, Job

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


BASE_URL = "https://jenkins.example/"


def _job(full_name: str) -> Job:
    return Job(
        full_name=full_name,
        display_name=full_name,
        url=HttpUrl(f"{BASE_URL}job/{full_name}/"),
    )


def _build(job_full_name: str, number: int) -> Build:
    start_time = datetime(2024, 1, number, tzinfo=UTC)
    return Build(
        job_full_name=job_full_name,
        number=number,
        scheduled_time=start_time - timedelta(minutes=1),
        start_time=start_time,
        duration=timedelta(minutes=5),
        status=BuildStatus.SUCCESS,
        url=HttpUrl(f"{BASE_URL}job/{job_full_name}/{number}/"),
    )


class RecordingClient:
    def __init__(
        self,
        jobs: list[Job],
        builds_by_job: dict[str, list[Build]],
        *,
        events: list[str] | None = None,
    ) -> None:
        self.jobs = jobs
        self.builds_by_job = builds_by_job
        self.events = events
        self.build_calls: list[tuple[str, int, datetime | None, timedelta | None]] = []

    def iter_jobs(self) -> Iterator[Job]:
        for job in self.jobs:
            if self.events is not None:
                self.events.append(f"yield_job:{job.full_name}")
            yield job

    def iter_builds(
        self,
        job: Job,
        *,
        page_size: int = 100,
        since: datetime | None = None,
        lookback: timedelta | None = None,
    ) -> Iterator[Build]:
        self.build_calls.append((job.full_name, page_size, since, lookback))
        if self.events is not None:
            self.events.append(f"iter_builds:{job.full_name}")
        yield from self.builds_by_job[job.full_name]


class RecordingStore:
    def __init__(self, *, events: list[str] | None = None) -> None:
        self.jobs: list[Job] = []
        self.builds: list[Build] = []
        self.events = events

    def upsert_job(self, job: Job) -> None:
        self.jobs.append(job)

    def upsert_jobs(self, jobs: Iterable[Job]) -> None:
        job_list = list(jobs)
        self.jobs.extend(job_list)
        if self.events is not None:
            full_names = ",".join(job.full_name for job in job_list)
            self.events.append(f"upsert_jobs:{full_names}")

    def get_job(self, full_name: str) -> Job | None:
        return next((job for job in self.jobs if job.full_name == full_name), None)

    def upsert_builds(self, builds: Iterable[Build]) -> None:
        build_list = list(builds)
        self.builds.extend(build_list)
        if self.events is not None:
            build_keys = ",".join(
                f"{build.job_full_name}#{build.number}" for build in build_list
            )
            self.events.append(f"upsert_builds:{build_keys}")


def test_collect_jobs_upserts_visible_jobs_without_retrieving_builds() -> None:
    # Given visible Jenkins jobs and a client that records build retrieval.
    frontend = _job("frontend")
    backend = _job("backend")
    client = RecordingClient([frontend, backend], {"frontend": [], "backend": []})
    store = RecordingStore()

    # When jobs-only collection runs.
    result = collect_jobs(client, store)

    # Then it stores the visible jobs without retrieving any builds.
    assert result == CollectionResult(job_count=2, build_count=0)
    assert store.jobs == [frontend, backend]
    assert store.builds == []
    assert client.build_calls == []


def test_collect_job_builds_uses_stored_job_without_discovering_all_jobs() -> None:
    # Given a database containing one target job and a client with other jobs.
    target = _job("frontend")
    target_build = _build("frontend", 1)
    other = _job("backend")
    client = RecordingClient(
        [target, other],
        {"frontend": [target_build], "backend": []},
    )
    store = RecordingStore()
    store.upsert_jobs([target])

    # When builds are collected for the stored target job.
    result = collect_job_builds(
        client,
        store,
        "frontend",
        BuildCollectionOptions(lookback=timedelta(hours=6)),
    )

    # Then only its builds are retrieved and stored.
    assert result == CollectionResult(job_count=1, build_count=1)
    assert store.builds == [target_build]
    assert client.build_calls == [("frontend", 100, None, timedelta(hours=6))]


def test_collect_job_builds_rejects_a_job_missing_from_the_store() -> None:
    # Given a target that has not first been stored by jobs collection.
    client = RecordingClient([], {})

    # When targeted build collection is requested, then it fails before HTTP calls.
    with pytest.raises(ValueError, match="Job is not stored: frontend"):
        collect_job_builds(client, RecordingStore(), "frontend")
    assert client.build_calls == []


def test_collect_upserts_visible_jobs_and_builds_with_default_lookback() -> None:
    # Given visible Jenkins jobs and builds.
    frontend = _job("frontend")
    backend = _job("backend")
    frontend_build = _build("frontend", 1)
    backend_build = _build("backend", 2)
    client = RecordingClient(
        [frontend, backend],
        {"frontend": [frontend_build], "backend": [backend_build]},
    )
    store = RecordingStore()

    # When collection runs without an explicit completion-time filter.
    result = collect(client, store)

    # Then the workflow stores all yielded records using a one-day lookback.
    assert result.job_count == 2
    assert result.build_count == 2
    assert store.jobs == [frontend, backend]
    assert store.builds == [frontend_build, backend_build]
    assert client.build_calls == [
        ("frontend", 100, None, DEFAULT_LOOKBACK),
        ("backend", 100, None, DEFAULT_LOOKBACK),
    ]


def test_collection_result_provides_output_lines_with_destination() -> None:
    # Given a collection result annotated with its storage destination.
    result = CollectionResult(job_count=3, build_count=5).with_destination(
        "jenkins.sqlite"
    )

    # When it is converted to output lines.
    lines = result.output_lines()

    # Then the result owns its presentation-ready summary.
    assert lines == ("Stored 5 builds for 3 jobs in jenkins.sqlite",)


def test_collect_upserts_all_jobs_before_retrieving_builds() -> None:
    # Given multiple visible Jenkins jobs and observable collection operations.
    frontend = _job("frontend")
    backend = _job("backend")
    frontend_build = _build("frontend", 1)
    backend_build = _build("backend", 2)
    events: list[str] = []
    client = RecordingClient(
        [frontend, backend],
        {"frontend": [frontend_build], "backend": [backend_build]},
        events=events,
    )
    store = RecordingStore(events=events)

    # When collection runs.
    result = collect(client, store)

    # Then all jobs are discovered and upserted before matching builds are read.
    assert result.job_count == 2
    assert result.build_count == 2
    assert store.jobs == [frontend, backend]
    assert store.builds == [frontend_build, backend_build]
    assert events == [
        "yield_job:frontend",
        "yield_job:backend",
        "upsert_jobs:frontend,backend",
        "iter_builds:frontend",
        "upsert_builds:frontend#1",
        "iter_builds:backend",
        "upsert_builds:backend#2",
    ]


def test_collect_passes_since_instead_of_default_lookback() -> None:
    # Given an explicit completion-time cutoff.
    job = _job("frontend")
    since = datetime(2024, 1, 1, tzinfo=UTC)
    client = RecordingClient([job], {"frontend": []})

    # When collection runs with --since semantics.
    result = collect(client, RecordingStore(), since=since)

    # Then the default lookback is not also sent to the client.
    assert result.job_count == 1
    assert client.build_calls == [("frontend", 100, since, None)]


@pytest.mark.parametrize("page_size", [0, -1])
def test_collect_rejects_non_positive_page_size(page_size: int) -> None:
    # Given an invalid page size.
    client = RecordingClient([_job("frontend")], {"frontend": []})

    # When collection starts, then it fails before reading jobs.
    with pytest.raises(ValueError, match="page_size must be positive"):
        collect(client, RecordingStore(), page_size=page_size)
    assert client.build_calls == []


def test_collect_rejects_since_and_lookback_together() -> None:
    # Given both mutually exclusive completion-time filters.
    client = RecordingClient([_job("frontend")], {"frontend": []})

    # When collection starts, then it fails before reading jobs.
    with pytest.raises(ValueError, match="Specify either since or lookback, not both"):
        collect(
            client,
            RecordingStore(),
            since=datetime(2024, 1, 1, tzinfo=UTC),
            lookback=timedelta(hours=1),
        )
    assert client.build_calls == []
