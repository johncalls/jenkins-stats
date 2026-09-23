from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import HttpUrl, ValidationError

from jenkins_stats.models import Build, BuildStatus, Job


def http_url(value: str) -> HttpUrl:
    return HttpUrl(value)


def build_payload() -> dict[str, object]:
    return {
        "job_full_name": "folder/example",
        "number": 42,
        "scheduled_time": datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        "start_time": datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC),
        "duration": timedelta(seconds=55),
        "status": BuildStatus.SUCCESS,
        "url": http_url("https://jenkins.example/job/folder/job/example/42/"),
    }


def test_build_epoch_millisecond_aliases_round_trip_pre_epoch_values() -> None:
    # Given persisted epoch-millisecond timestamps before and after the Unix epoch.
    payload = {
        "job_full_name": "folder/example",
        "number": 42,
        "scheduled_time_ms": -1,
        "start_time_ms": 1_234,
        "duration_ms": 1_234,
        "status": BuildStatus.SUCCESS,
        "url": http_url("https://jenkins.example/job/folder/job/example/42/"),
    }

    # When the build model validates and serializes the persisted representation.
    build = Build.model_validate(payload)
    row = build.model_dump(
        mode="json",
        by_alias=True,
        include={"scheduled_time", "start_time", "duration"},
    )

    # Then millisecond values preserve the exact instants and duration.
    assert build.scheduled_time == datetime(
        1969, 12, 31, 23, 59, 59, 999_000, tzinfo=UTC
    )
    assert build.start_time == datetime(1970, 1, 1, 0, 0, 1, 234_000, tzinfo=UTC)
    assert build.duration == timedelta(seconds=1, milliseconds=234)
    assert row == {
        "scheduled_time_ms": -1,
        "start_time_ms": 1_234,
        "duration_ms": 1_234,
    }


def test_build_normalizes_non_utc_inputs_at_millisecond_precision() -> None:
    # Given millisecond-precision timestamps in a non-UTC timezone.
    local_timezone = timezone(timedelta(hours=5, minutes=30))

    # When a build model is created.
    build = Build.model_validate(
        {
            **build_payload(),
            "scheduled_time": datetime(
                2026, 1, 1, 17, 30, 0, 123_000, tzinfo=local_timezone
            ),
            "start_time": datetime(
                2026, 1, 1, 17, 30, 5, 456_000, tzinfo=local_timezone
            ),
            "duration": timedelta(milliseconds=789),
        }
    )

    # Then timestamps are canonical UTC values without losing milliseconds.
    assert build.scheduled_time == datetime(2026, 1, 1, 12, 0, 0, 123_000, tzinfo=UTC)
    assert build.start_time == datetime(2026, 1, 1, 12, 0, 5, 456_000, tzinfo=UTC)
    assert build.end_time == datetime(2026, 1, 1, 12, 0, 6, 245_000, tzinfo=UTC)


@pytest.mark.parametrize("status", list(BuildStatus))
def test_build_accepts_every_terminal_status(status: BuildStatus) -> None:
    # Given any terminal Jenkins result represented by the public enum.
    # When the build model is created, then that status is preserved exactly.
    build = Build.model_validate({**build_payload(), "status": status})

    assert build.status is status


def test_job_supports_build_collection_only_for_known_job_classes() -> None:
    # Given supported, unsupported, and unknown Jenkins job classes.
    pipeline = Job(
        full_name="folder/pipeline",
        url=http_url("https://jenkins.example/job/folder/job/pipeline/"),
        jenkins_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
    )
    freestyle = Job(
        full_name="folder/freestyle",
        url=http_url("https://jenkins.example/job/folder/job/freestyle/"),
        jenkins_class="hudson.model.FreeStyleProject",
    )
    unknown = Job(
        full_name="folder/unknown",
        url=http_url("https://jenkins.example/job/folder/job/unknown/"),
    )

    # Then only the explicitly supported class is eligible for build collection.
    assert pipeline.supports_build_collection is True
    assert freestyle.supports_build_collection is False
    assert unknown.supports_build_collection is False


def test_job_and_build_models_are_immutable() -> None:
    # Given public domain models.
    job = Job(
        full_name="folder/example",
        display_name="Example",
        url=http_url("https://jenkins.example/job/folder/job/example/"),
    )
    build = Build.model_validate(build_payload())

    # When callers try to mutate them, then Pydantic rejects the assignment.
    with pytest.raises(ValidationError, match="frozen"):
        job.full_name = "folder/other"
    with pytest.raises(ValidationError, match="frozen"):
        build.number = 43

    # And their derived identifiers remain stable observations.
    assert job.name == "example"
    assert job.parent_full_name == "folder"
    assert build.key == ("folder/example", 42)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        pytest.param(
            "scheduled_time",
            datetime(2026, 1, 1, 12, 0, 0, 123_456, tzinfo=UTC),
            id="scheduled_time",
        ),
        pytest.param(
            "start_time",
            datetime(2026, 1, 1, 12, 0, 5, 456_789, tzinfo=UTC),
            id="start_time",
        ),
        pytest.param("duration", timedelta(microseconds=999), id="duration"),
    ],
)
def test_build_rejects_fractional_millisecond_values(
    field_name: str,
    value: object,
) -> None:
    # Given an otherwise valid build payload with one sub-millisecond value.
    payload = {**build_payload(), field_name: value}

    # When the build model is created, then fractional milliseconds are rejected.
    with pytest.raises(ValidationError, match="whole milliseconds"):
        Build.model_validate(payload)
