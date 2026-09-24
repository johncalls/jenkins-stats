"""Domain models for Jenkins build statistics."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, ClassVar, cast

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    PlainSerializer,
    computed_field,
    field_validator,
    model_validator,
)

__all__ = ["Build", "BuildStatus", "Job", "joined_url"]


class BuildStatus(StrEnum):
    """Terminal result reported by Jenkins for a completed build."""

    SUCCESS = "SUCCESS"
    UNSTABLE = "UNSTABLE"
    FAILURE = "FAILURE"
    ABORTED = "ABORTED"
    NOT_BUILT = "NOT_BUILT"


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)


def joined_url(url: HttpUrl, suffix: str) -> HttpUrl:
    """Append a path suffix to an HTTP URL."""
    return HttpUrl(f"{str(url).rstrip('/')}/{suffix.lstrip('/')}")


def _require_millisecond_datetime(value: datetime) -> datetime:
    """Normalize an aware timestamp to UTC and require millisecond precision."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    normalized = value.astimezone(UTC)
    if normalized.microsecond % 1_000 != 0:
        raise ValueError("datetime must be representable as whole milliseconds")
    return normalized


def _require_millisecond_duration(value: timedelta) -> timedelta:
    """Require a duration to be representable as whole milliseconds."""
    if value % _MILLISECOND != timedelta(0):
        raise ValueError("duration must be representable as whole milliseconds")
    return value


def _epoch_ms_value_to_datetime(value: object) -> object:
    if isinstance(value, int) and not isinstance(value, bool):
        return _EPOCH + timedelta(milliseconds=value)
    return value


def _duration_ms_value_to_timedelta(value: object) -> object:
    if isinstance(value, int) and not isinstance(value, bool):
        return timedelta(milliseconds=value)
    return value


def _datetime_to_epoch_ms(value: datetime) -> int:
    return (_require_millisecond_datetime(value) - _EPOCH) // _MILLISECOND


def _duration_to_ms(value: timedelta) -> int:
    return _require_millisecond_duration(value) // _MILLISECOND


_MillisecondAwareDatetime = Annotated[
    datetime,
    AwareDatetime,
    PlainSerializer(_datetime_to_epoch_ms, return_type=int, when_used="json"),
]
_MillisecondDuration = Annotated[
    timedelta,
    PlainSerializer(_duration_to_ms, return_type=int, when_used="json"),
]


class Job(BaseModel):
    """A Jenkins job discovered on a controller."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    full_name: str = Field(min_length=1)
    url: HttpUrl
    display_name: str | None = None
    jenkins_class: str | None = None
    deleted_at: _MillisecondAwareDatetime | None = None
    disabled: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def supports_build_collection(self) -> bool:
        """Whether this job class has a supported build timing source."""
        return self.jenkins_class == "org.jenkinsci.plugins.workflow.job.WorkflowJob"

    @field_validator("deleted_at")
    @classmethod
    def normalize_deleted_at(cls, value: datetime | None) -> datetime | None:
        """Normalize deletion timestamps to UTC at millisecond precision."""
        if value is None:
            return None
        return _require_millisecond_datetime(value)

    @property
    def name(self) -> str:
        """Return the job's final path component."""
        return self.full_name.rsplit("/", 1)[-1]

    @property
    def parent_full_name(self) -> str | None:
        """Return the containing folder or multibranch project name, if any."""
        # Parent may be a folder or a multibranch project.
        return self.full_name.rpartition("/")[0] or None


class Build(BaseModel):
    """Immutable metadata for a completed Jenkins build."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    job_full_name: str = Field(min_length=1)
    number: int = Field(gt=0)
    scheduled_time: _MillisecondAwareDatetime = Field(
        validation_alias=AliasChoices("scheduled_time", "scheduled_time_ms"),
        serialization_alias="scheduled_time_ms",
    )
    start_time: _MillisecondAwareDatetime = Field(
        validation_alias=AliasChoices("start_time", "start_time_ms"),
        serialization_alias="start_time_ms",
    )
    duration: _MillisecondDuration = Field(
        ge=timedelta(0),
        validation_alias=AliasChoices("duration", "duration_ms"),
        serialization_alias="duration_ms",
    )
    status: BuildStatus
    url: HttpUrl
    jenkins_class: str | None = None

    @model_validator(mode="before")
    @classmethod
    def parse_millisecond_aliases(cls, value: object) -> object:
        """Accept persisted Jenkins millisecond fields without changing API fields."""
        if not isinstance(value, Mapping):
            return value

        source = cast("Mapping[object, object]", value)
        data: dict[object, object] = dict(source)
        if "scheduled_time" not in data and "scheduled_time_ms" in data:
            data["scheduled_time"] = _epoch_ms_value_to_datetime(
                data["scheduled_time_ms"]
            )
        if "start_time" not in data and "start_time_ms" in data:
            data["start_time"] = _epoch_ms_value_to_datetime(data["start_time_ms"])
        if "duration" not in data and "duration_ms" in data:
            data["duration"] = _duration_ms_value_to_timedelta(data["duration_ms"])
        return data

    @field_validator("scheduled_time", "start_time")
    @classmethod
    def normalize_to_utc(cls, value: datetime) -> datetime:
        """Normalize an aware timestamp to UTC at millisecond precision."""
        return _require_millisecond_datetime(value)

    @field_validator("duration")
    @classmethod
    def require_millisecond_duration(cls, value: timedelta) -> timedelta:
        """Require duration to be stored without sub-millisecond precision."""
        return _require_millisecond_duration(value)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def end_time(self) -> datetime:
        """Return the build's completion time."""
        return self.start_time + self.duration

    @property
    def key(self) -> tuple[str, int]:
        """Return the stable job-name/build-number identifier."""
        return self.job_full_name, self.number
