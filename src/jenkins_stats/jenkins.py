"""Read-only Jenkins client and transport implementations."""

import math
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Protocol, Self, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType

from pydantic import BaseModel, Field, HttpUrl

from . import models as _models
from .models import joined_url

__all__ = [
    "Clock",
    "HttpJsonTransport",
    "JenkinsClient",
    "JsonTransport",
    "JsonTransportError",
    "JsonValue",
    "StartTimeUnavailable",
]


class Clock(Protocol):
    """Return the current timezone-aware time."""

    def __call__(self) -> datetime:
        """Return the current timezone-aware time."""
        ...


class _ItemReference(BaseModel):
    name: str
    url: HttpUrl


class _BuildReference(BaseModel):
    number: int


class _ItemResponse(BaseModel):
    jenkins_class: str | None = Field(default=None, alias="_class")
    full_name: str | None = Field(default=None, alias="fullName")
    display_name: str | None = Field(default=None, alias="displayName")
    url: HttpUrl | None = None
    disabled: bool = False
    jobs: list[_ItemReference] | None = None
    builds: list[_BuildReference] | None = None


class _BuildResponse(BaseModel):
    jenkins_class: str = Field(alias="_class")
    number: int = Field(gt=0)
    url: HttpUrl
    timestamp: int
    duration: int = Field(ge=0)
    building: bool
    in_progress: bool = Field(alias="inProgress")
    result: _models.BuildStatus | None


class _BuildPage(BaseModel):
    builds: list[_BuildResponse] = Field(alias="allBuilds")


class _PipelineTimingResponse(BaseModel):
    start_time_ms: int = Field(alias="startTimeMillis", gt=0)


class StartTimeUnavailable(RuntimeError):
    """No supported source of actual execution start time was available."""


type JsonValue = (
    str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
)


class JsonTransportError(RuntimeError):
    """Transport failure; status_code is set only for HTTP status errors."""

    def __init__(
        self, url: HttpUrl, message: str, *, status_code: int | None = None
    ) -> None:
        """Initialize the error with the failed URL and optional status code."""
        self.url: str = str(url)
        self.status_code: int | None = status_code
        suffix = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"{message}{suffix}: {url}")


def _same_origin(a: HttpUrl, b: HttpUrl) -> bool:
    return a.scheme == b.scheme and a.host == b.host and a.port == b.port


class JsonTransport(Protocol):
    """Return decoded JSON; raise JsonTransportError on transport failures.

    Jenkins response validation belongs to JenkinsClient, not the transport.
    """

    def get_json(self, url: HttpUrl, *, tree: str | None = None) -> JsonValue:
        """Fetch and decode JSON from `url`, optionally selecting a Jenkins tree."""
        ...


class HttpJsonTransport:
    """Authenticated JSON GET transport restricted to the configured origin."""

    def __init__(
        self,
        base_url: HttpUrl,
        *,
        username: str,
        api_token: str,
        timeout: float = 30.0,
        allow_insecure: bool = False,
    ) -> None:
        """Configure Basic-auth JSON access for a single Jenkins origin."""
        if base_url.scheme != "https" and not allow_insecure:
            raise ValueError("base_url must use HTTPS unless allow_insecure=True")
        if base_url.username is not None or base_url.password is not None:
            raise ValueError("base_url must not include userinfo")
        if base_url.query is not None or base_url.fragment is not None:
            raise ValueError("base_url must not include query or fragment")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        self.base_url: HttpUrl = base_url
        self._client = httpx.Client(
            auth=httpx.BasicAuth(username, api_token),
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
        )
        self.timeout = timeout

    def close(self) -> None:
        """Close the underlying HTTPX client and its connection pool."""
        self._client.close()

    def __enter__(self) -> Self:
        """Return this transport for use as a context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the underlying HTTPX client on context exit."""
        _ = (exc_type, exc, traceback)
        self.close()

    def get_json(self, url: HttpUrl, *, tree: str | None = None) -> JsonValue:
        """Fetch JSON from the configured origin, adding `tree` when supplied."""
        # Avoid sending credentials to a different origin from returned URLs.
        if not _same_origin(self.base_url, url):
            raise JsonTransportError(url, "URL is outside the configured origin")
        if tree is not None:
            target = urlsplit(str(url))
            query = urlencode(
                [
                    *parse_qsl(target.query, keep_blank_values=True),
                    ("tree", tree),
                ]
            )
            request_url = HttpUrl(
                urlunsplit(
                    (
                        target.scheme,
                        target.netloc,
                        target.path,
                        query,
                        target.fragment,
                    )
                )
            )
        else:
            request_url = url
        try:
            response = self._client.get(str(request_url))
        except httpx.ProtocolError as exc:
            raise JsonTransportError(request_url, "HTTP protocol failed") from exc
        except httpx.RequestError as exc:
            raise JsonTransportError(request_url, "HTTP connection failed") from exc

        if HTTPStatus.MULTIPLE_CHOICES <= response.status_code < HTTPStatus.BAD_REQUEST:
            raise JsonTransportError(
                request_url,
                "HTTP redirect rejected",
                status_code=response.status_code,
            )

        try:
            response.raise_for_status()
            return cast("JsonValue", response.json())
        except httpx.HTTPStatusError as exc:
            raise JsonTransportError(
                request_url,
                "HTTP request failed",
                status_code=exc.response.status_code,
            ) from exc
        except (ValueError, UnicodeError) as exc:
            raise JsonTransportError(request_url, "Response is not valid JSON") from exc


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _to_utc_in_range(value: datetime, label: str) -> datetime:
    try:
        return value.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError(f"{label} is outside supported datetime range") from exc


class JenkinsClient:
    """Read jobs and retained builds from a single controller.

    Inject a JsonTransport for I/O and optionally an aware-datetime clock.
    Uses only GET requests. Follows returned item/build URLs, preserving Jenkins
    context paths and encoded branch names. Views are not consulted.

    Actual start times are read from the Pipeline REST API plugin. Other job
    types are rejected because Jenkins core JSON does not expose executor start
    time.

    HTTP errors and unsupported timing sources are raised, never silently
    converted into an incomplete collection. API permissions limit visibility.
    Build pagination is not a transactional snapshot; avoid concurrent history
    deletion during a full import, and deduplicate by Build.key.
    """

    _ITEM_TREE = (
        "_class,fullName,displayName,url,disabled,jobs[name,url],builds[number]{0,0}"
    )
    _BUILD_FIELDS = "_class,number,url,timestamp,duration,building,inProgress,result"

    def __init__(
        self,
        base_url: HttpUrl,
        transport: JsonTransport,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        """Initialize a read-only client using an injected JSON transport."""
        self.base_url: HttpUrl = base_url
        self.transport = transport
        self.clock = clock

    def _get_item(self, url: HttpUrl) -> _ItemResponse:
        item_url = joined_url(url, "/api/json")
        try:
            raw_item = self.transport.get_json(item_url, tree=self._ITEM_TREE)
        except JsonTransportError as exc:
            if url == self.base_url and exc.status_code == HTTPStatus.NOT_FOUND:
                raise ValueError(
                    "Jenkins base URL is not a Jenkins controller: "
                    f"{item_url} returned HTTP 404"
                ) from exc
            raise
        return _ItemResponse.model_validate(raw_item)

    def iter_jobs(self) -> Iterator[_models.Job]:
        """Traverse containers, including folders and multibranch projects."""
        pending = [self.base_url]
        seen: set[str] = set()
        while pending:
            url = pending.pop()
            url_key = str(url)
            if url_key in seen:
                continue
            seen.add(url_key)
            item = self._get_item(url)
            # Presence of builds identifies a Job, even with no retained runs.
            # Presence of jobs identifies a container, including an empty one.
            if item.builds is not None:
                if item.full_name is None or item.url is None:
                    raise ValueError(f"Job lacks fullName/url: {url}")
                yield _models.Job(
                    full_name=item.full_name,
                    display_name=item.display_name,
                    url=item.url,
                    jenkins_class=item.jenkins_class,
                    disabled=item.disabled,
                )
            if item.jobs is not None:
                pending.extend(child.url for child in item.jobs)
            elif item.builds is None:
                raise ValueError(f"Unsupported item: neither jobs nor builds: {url}")

    def _start_time_ms(self, build: _BuildResponse) -> int:
        if build.jenkins_class != "org.jenkinsci.plugins.workflow.job.WorkflowRun":
            raise StartTimeUnavailable(
                f"Actual start time is not exposed by core JSON for {build.url}; "
                "only Pipeline builds are supported."
            )
        try:
            timing = _PipelineTimingResponse.model_validate(
                self.transport.get_json(joined_url(build.url, "/wfapi/describe"))
            )
        except JsonTransportError as exc:
            if exc.status_code == HTTPStatus.NOT_FOUND:
                raise StartTimeUnavailable(
                    f"Pipeline timing endpoint unavailable for {build.url}; "
                    "check the Pipeline REST API plugin and build availability."
                ) from exc
            raise
        return timing.start_time_ms

    def _resolve_cutoff(
        self,
        since: datetime | None,
        lookback: timedelta | None,
    ) -> datetime | None:
        if since is not None and lookback is not None:
            raise ValueError("Specify either since or lookback, not both")
        if since is not None:
            if since.tzinfo is None or since.utcoffset() is None:
                raise ValueError("since must include a timezone")
            return _to_utc_in_range(since, "since")
        if lookback is None:
            return None
        if lookback < timedelta(0):
            raise ValueError("lookback must not be negative")
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        now_utc = _to_utc_in_range(now, "clock value")
        try:
            return now_utc - lookback
        except OverflowError as exc:
            raise ValueError(
                "lookback produces cutoff outside supported datetime range"
            ) from exc

    def _to_build(
        self,
        job: _models.Job,
        raw_build: _BuildResponse,
    ) -> _models.Build | None:
        if raw_build.building or raw_build.in_progress or raw_build.result is None:
            return None
        return _models.Build.model_validate(
            {
                "job_full_name": job.full_name,
                "number": raw_build.number,
                "scheduled_time_ms": raw_build.timestamp,
                "start_time_ms": self._start_time_ms(raw_build),
                "duration_ms": raw_build.duration,
                "status": raw_build.result,
                "url": raw_build.url,
                "jenkins_class": raw_build.jenkins_class,
            }
        )

    def iter_builds(
        self,
        job: _models.Job,
        *,
        page_size: int = 100,
        since: datetime | None = None,
        lookback: timedelta | None = None,
    ) -> Iterator[_models.Build]:
        """Read retained builds, optionally filtered by completion time.

        since: Inclusive, timezone-aware completion-time cutoff.
        lookback: Relative cutoff, e.g. timedelta(hours=1). Resolved once against
            the injected clock when iteration starts. Mutually exclusive with since.
        Neither: Return all retained builds (the original behavior).

        Filtering uses end_time, not scheduled_time: a run scheduled before a
        previous poll may only finish now. All history pages must be visited,
        because build-number order is not completion order. Filtering is local;
        Jenkins still receives the same paginated queries, including timing
        requests for builds. This is incremental output, not a server-
        side time filter or a guarantee of lower request volume.

        For polling, persist the UTC time captured BEFORE a successful scan as
        the next since value, only after consuming and storing all its results.
        A small overlap helps with delayed visibility and clock differences;
        upsert by Build.key to handle overlap and inclusive boundaries.
        For multiple jobs, share one cutoff and advance it only after all jobs
        succeed. Results are limited to retained, visible builds. Failed polls
        must not advance the checkpoint.
        """
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        cutoff = self._resolve_cutoff(since, lookback)
        if job.disabled or not job.supports_build_collection:
            return
        offset = 0
        seen: set[int] = set()
        while True:
            tree = f"allBuilds[{self._BUILD_FIELDS}]{{{offset},{offset + page_size}}}"
            page = _BuildPage.model_validate(
                self.transport.get_json(joined_url(job.url, "/api/json"), tree=tree)
            )
            for raw_build in page.builds:
                if raw_build.number in seen:
                    continue
                seen.add(raw_build.number)
                build = self._to_build(job, raw_build)
                if build is not None and (cutoff is None or build.end_time >= cutoff):
                    yield build
            if len(page.builds) < page_size:
                break
            offset += page_size
