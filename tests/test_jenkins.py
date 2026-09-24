from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import HttpUrl

if TYPE_CHECKING:
    from collections.abc import Callable

from jenkins_stats import (
    Build,
    BuildStatus,
    HttpJsonTransport,
    JenkinsClient,
    Job,
    JsonTransportError,
    JsonValue,
    StartTimeUnavailable,
)
from jenkins_stats.models import joined_url

BASE_URL = HttpUrl("https://jenkins.example/")
FOLDER_URL = HttpUrl(f"{BASE_URL}job/folder/")
JOB_URL = HttpUrl(f"{FOLDER_URL}job/example/")
BUILD_1_URL = HttpUrl(f"{JOB_URL}1/")
BUILD_2_URL = HttpUrl(f"{JOB_URL}2/")
BUILD_3_URL = HttpUrl(f"{JOB_URL}3/")
CONTEXT_BASE_URL = HttpUrl("https://jenkins.example/jenkins/")
EMPTY_FOLDER_URL = HttpUrl(f"{CONTEXT_BASE_URL}job/empty/")
REPO_URL = HttpUrl(f"{CONTEXT_BASE_URL}job/repo/")
ENCODED_BRANCH_URL = HttpUrl(f"{REPO_URL}job/feature%2Fencoded/")


def _epoch_ms(value: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)


def _example_job() -> Job:
    return Job(
        full_name="folder/example",
        url=JOB_URL,
        jenkins_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
    )


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class RecordingClock(FixedClock):
    def __init__(self, value: datetime) -> None:
        super().__init__(value)
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return super().__call__()


class MissingApiTransport:
    def get_json(self, url: HttpUrl, *, tree: str | None = None) -> JsonValue:
        _ = tree
        raise JsonTransportError(
            url,
            "HTTP request failed",
            status_code=HTTPStatus.NOT_FOUND,
        )


def build_response(
    *,
    number: int,
    url: HttpUrl,
    timestamp: int,
    duration: int,
    result: str | None = "SUCCESS",
    building: bool = False,
    in_progress: bool = False,
    jenkins_class: str = "org.jenkinsci.plugins.workflow.job.WorkflowRun",
) -> dict[str, JsonValue]:
    return {
        "_class": jenkins_class,
        "number": number,
        "url": str(url),
        "timestamp": timestamp,
        "duration": duration,
        "building": building,
        "inProgress": in_progress,
        "result": result,
    }


def pipeline_build_response(
    *,
    number: int,
    start_ms: int,
    duration_ms: int,
    result: str | None = "SUCCESS",
    building: bool = False,
    in_progress: bool = False,
) -> dict[str, JsonValue]:
    return build_response(
        number=number,
        url=HttpUrl(f"{JOB_URL}{number}/"),
        timestamp=max(start_ms - 500, 0),
        duration=duration_ms,
        result=result,
        building=building,
        in_progress=in_progress,
    )


class FakeJenkinsTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[HttpUrl, str | None]] = []

    def get_json(self, url: HttpUrl, *, tree: str | None = None) -> JsonValue:
        self.calls.append((url, tree))

        if url == joined_url(BASE_URL, "api/json"):
            return {"jobs": [{"name": "folder", "url": str(FOLDER_URL)}]}

        if url == joined_url(FOLDER_URL, "api/json"):
            return {"jobs": [{"name": "example", "url": str(JOB_URL)}]}

        if url == joined_url(JOB_URL, "api/json") and tree is not None:
            if tree.startswith("_class,fullName,"):
                return {
                    "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
                    "fullName": "folder/example",
                    "displayName": "Example",
                    "url": str(JOB_URL),
                    "builds": [],
                }
            if tree.endswith("{0,2}"):
                return {
                    "allBuilds": [
                        build_response(
                            number=3,
                            url=BUILD_3_URL,
                            timestamp=1_000,
                            duration=2_500,
                            result="SUCCESS",
                        ),
                        build_response(
                            number=2,
                            url=BUILD_2_URL,
                            timestamp=1_500,
                            duration=0,
                            result=None,
                            building=True,
                            in_progress=True,
                        ),
                    ]
                }
            if tree.endswith("{2,4}"):
                return {
                    "allBuilds": [
                        build_response(
                            number=1,
                            url=BUILD_1_URL,
                            timestamp=500,
                            duration=100,
                            result="FAILURE",
                        )
                    ]
                }

        if url == joined_url(BUILD_3_URL, "wfapi/describe"):
            return {"startTimeMillis": 2_000}
        if url == joined_url(BUILD_1_URL, "wfapi/describe"):
            return {"startTimeMillis": 500}

        raise AssertionError(f"Unexpected request: url={url!r}, tree={tree!r}")


class ContainerGraphTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[HttpUrl, str | None]] = []

    def get_json(self, url: HttpUrl, *, tree: str | None = None) -> JsonValue:
        self.calls.append((url, tree))

        if url == joined_url(CONTEXT_BASE_URL, "api/json"):
            return {
                "jobs": [
                    {"name": "empty", "url": str(EMPTY_FOLDER_URL)},
                    {"name": "repo", "url": str(REPO_URL)},
                ]
            }
        if url == joined_url(EMPTY_FOLDER_URL, "api/json"):
            return {"jobs": []}
        if url == joined_url(REPO_URL, "api/json"):
            return {
                "jobs": [
                    {"name": "feature%2Fencoded", "url": str(ENCODED_BRANCH_URL)},
                    {"name": "root-cycle", "url": str(CONTEXT_BASE_URL)},
                ]
            }
        if url == joined_url(ENCODED_BRANCH_URL, "api/json"):
            return {
                "fullName": "repo/feature%2Fencoded",
                "displayName": "feature/encoded",
                "url": str(ENCODED_BRANCH_URL),
                "builds": [],
            }

        raise AssertionError(f"Unexpected request: url={url!r}, tree={tree!r}")


class DisabledJobTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[HttpUrl, str | None]] = []

    def get_json(self, url: HttpUrl, *, tree: str | None = None) -> JsonValue:
        self.calls.append((url, tree))
        if url == joined_url(BASE_URL, "api/json"):
            return {
                "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
                "fullName": "disabled-pipeline",
                "displayName": "Disabled Pipeline",
                "url": str(JOB_URL),
                "builds": [],
                "disabled": True,
            }
        raise AssertionError(f"Unexpected request: url={url!r}, tree={tree!r}")


class PaginatedBuildTransport:
    def __init__(
        self,
        pages: list[list[JsonValue]],
        start_times_ms: dict[int, int],
    ) -> None:
        self.pages = pages
        self.start_times_ms = start_times_ms
        self.calls: list[tuple[HttpUrl, str | None]] = []
        self.page_count = 0

    def get_json(self, url: HttpUrl, *, tree: str | None = None) -> JsonValue:
        self.calls.append((url, tree))
        if url == joined_url(JOB_URL, "api/json") and tree is not None:
            assert tree.startswith("allBuilds[")
            try:
                return {"allBuilds": self.pages[self.page_count]}
            finally:
                self.page_count += 1
        for number, start_time_ms in self.start_times_ms.items():
            if url == joined_url(HttpUrl(f"{JOB_URL}{number}/"), "wfapi/describe"):
                return {"startTimeMillis": start_time_ms}
        raise AssertionError(f"Unexpected request: url={url!r}, tree={tree!r}")

    def timing_calls_for(self, number: int) -> int:
        return self.calls.count(
            (joined_url(HttpUrl(f"{JOB_URL}{number}/"), "wfapi/describe"), None)
        )


class SingleJobBuildTransport:
    def __init__(self, build: dict[str, JsonValue]) -> None:
        self.build = build
        self.calls: list[tuple[HttpUrl, str | None]] = []

    def get_json(self, url: HttpUrl, *, tree: str | None = None) -> JsonValue:
        self.calls.append((url, tree))
        if (
            url == joined_url(JOB_URL, "api/json")
            and tree is not None
            and tree.startswith("allBuilds[")
        ):
            return {"allBuilds": [self.build]}
        raise AssertionError(f"Unexpected request: url={url!r}, tree={tree!r}")


class PipelineTiming404Transport(SingleJobBuildTransport):
    def get_json(self, url: HttpUrl, *, tree: str | None = None) -> JsonValue:
        if url == joined_url(BUILD_3_URL, "wfapi/describe"):
            raise JsonTransportError(url, "HTTP request failed", status_code=404)
        return super().get_json(url, tree=tree)


def install_mock_httpx_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    original_client = httpx.Client

    def fake_client(*args: Any, **kwargs: Any) -> httpx.Client:  # noqa: ANN401
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr("jenkins_stats.jenkins.httpx.Client", fake_client)


def test_http_json_transport_rejects_insecure_base_url_by_default() -> None:
    # Given an HTTP Jenkins base URL without explicit insecure opt-in.
    # When the transport is created, then configuration validation rejects it.
    with pytest.raises(ValueError, match="HTTPS"):
        HttpJsonTransport(
            HttpUrl("http://jenkins.example/"),
            username="user",
            api_token="token",
        )


def test_http_json_transport_allows_insecure_base_url_when_explicit() -> None:
    # Given an HTTP Jenkins base URL with explicit insecure opt-in.
    # When the transport is created.
    transport = HttpJsonTransport(
        HttpUrl("http://jenkins.example/"),
        username="user",
        api_token="token",
        allow_insecure=True,
    )

    # Then the HTTP base URL is accepted as configured.
    assert str(transport.base_url) == "http://jenkins.example/"


@pytest.mark.parametrize(
    ("base_url", "message"),
    [
        pytest.param(
            "https://user:pass@jenkins.example/",
            "userinfo",
            id="userinfo",
        ),
        pytest.param(
            "https://jenkins.example/?depth=1",
            "query or fragment",
            id="query",
        ),
        pytest.param(
            "https://jenkins.example/#jobs",
            "query or fragment",
            id="fragment",
        ),
    ],
)
def test_http_json_transport_rejects_base_url_components_outside_controller_root(
    base_url: str,
    message: str,
) -> None:
    # Given a Pydantic-validated HTTP URL with unsupported base-URL components.
    # When the transport is created, then Jenkins transport policy rejects it.
    with pytest.raises(ValueError, match=message):
        HttpJsonTransport(
            HttpUrl(base_url),
            username="user",
            api_token="token",
        )


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_http_json_transport_rejects_invalid_timeouts(timeout: float) -> None:
    # Given a non-positive or non-finite library timeout.
    # When the transport is created, then configuration validation rejects it.
    with pytest.raises(ValueError, match="finite positive"):
        HttpJsonTransport(
            BASE_URL,
            username="user",
            api_token="token",
            timeout=timeout,
        )


def test_http_json_transport_context_manager_closes_httpx_client() -> None:
    # Given an HTTP transport that owns an HTTPX connection pool.
    transport = HttpJsonTransport(
        BASE_URL,
        username="user",
        api_token="token",
    )

    # When the transport context exits.
    with transport as managed_transport:
        assert managed_transport is transport

    # Then the underlying HTTPX client is closed and refuses further requests.
    with pytest.raises(RuntimeError, match="closed"):
        transport.get_json(HttpUrl("https://jenkins.example/api/json"))


def test_http_json_transport_accepts_equivalent_pydantic_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given Pydantic-normalized URL forms for the same HTTPS origin.
    opened_urls: list[str] = []
    opened_headers: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        opened_urls.append(str(request.url))
        opened_headers.append(
            (request.headers["authorization"], request.headers["accept"])
        )
        return httpx.Response(HTTPStatus.OK, json={"ok": True})

    install_mock_httpx_transport(monkeypatch, handler)
    transport = HttpJsonTransport(
        HttpUrl("https://JENKINS.example:443/"),
        username="user",
        api_token="token",
    )

    # When Jenkins returns the normalized spelling of that origin.
    response = transport.get_json(HttpUrl("https://jenkins.example/api/json"))

    # Then the request is allowed rather than rejected as cross-origin.
    assert response == {"ok": True}
    assert opened_urls == ["https://jenkins.example/api/json"]
    assert opened_headers == [("Basic dXNlcjp0b2tlbg==", "application/json")]


def test_http_json_transport_blocks_different_canonical_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a configured Jenkins origin and a fake transport that must not be reached.
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("cross-origin request was opened")

    install_mock_httpx_transport(monkeypatch, handler)
    transport = HttpJsonTransport(
        BASE_URL,
        username="user",
        api_token="token",
    )

    # When a returned URL uses a different effective port, then it is blocked
    # before I/O or credentials can leave the configured origin.
    url = HttpUrl("https://jenkins.example:444/api/json")
    with pytest.raises(JsonTransportError, match="outside the configured origin"):
        transport.get_json(url)


@pytest.mark.parametrize(
    "location",
    [
        "/redirected",
        "https://other.example/api/json",
        "https://jenkins.example:444/api/json",
        "http://jenkins.example/api/json",
    ],
)
def test_http_json_transport_rejects_redirects_before_target_request(
    location: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given HTTPX is configured not to follow redirects automatically.
    opened_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        opened_urls.append(str(request.url))
        return httpx.Response(
            HTTPStatus.FOUND,
            headers={"Location": location},
            request=request,
        )

    install_mock_httpx_transport(monkeypatch, handler)
    transport = HttpJsonTransport(
        BASE_URL,
        username="user",
        api_token="token",
    )

    # When Jenkins returns any redirect, including same-origin or downgrade forms.
    with pytest.raises(JsonTransportError, match="HTTP redirect rejected") as error:
        transport.get_json(HttpUrl("https://jenkins.example/api/json"))

    # Then only the original URL is opened; the redirect target is never contacted.
    assert error.value.status_code == HTTPStatus.FOUND
    assert opened_urls == ["https://jenkins.example/api/json"]


def test_http_json_transport_merges_tree_with_existing_query_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a fake HTTPX transport that records the outgoing request URL.
    opened_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        opened_urls.append(str(request.url))
        return httpx.Response(HTTPStatus.OK, json={"ok": True})

    install_mock_httpx_transport(monkeypatch, handler)
    transport = HttpJsonTransport(
        HttpUrl("https://jenkins.example/"),
        username="user",
        api_token="token",
    )

    # When a tree projection is added to a URL that already has query params.
    response = transport.get_json(
        HttpUrl("https://jenkins.example/api/json?depth=1"),
        tree="jobs[name]",
    )

    # Then the response is decoded and both query parameters are preserved.
    assert response == {"ok": True}
    assert len(opened_urls) == 1
    opened_url = urlsplit(opened_urls[0])
    assert opened_url.scheme == "https"
    assert opened_url.netloc == "jenkins.example"
    assert opened_url.path == "/api/json"
    assert parse_qs(opened_url.query) == {
        "depth": ["1"],
        "tree": ["jobs[name]"],
    }


def test_http_json_transport_wraps_http_protocol_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given HTTPX reports a protocol failure from the underlying transport.
    failure = httpx.RemoteProtocolError("server disconnected")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise failure

    install_mock_httpx_transport(monkeypatch, handler)
    transport = HttpJsonTransport(
        HttpUrl("https://jenkins.example/"),
        username="user",
        api_token="token",
    )
    url = HttpUrl("https://jenkins.example/api/json")

    # When the request is made, then the public transport boundary wraps it.
    with pytest.raises(JsonTransportError, match="HTTP protocol failed") as error:
        transport.get_json(url)

    assert error.value.url == str(url)
    assert error.value.status_code is None
    assert error.value.__cause__ is failure


def test_iter_jobs_traverses_folders_and_yields_jobs() -> None:
    # Given a Jenkins controller with a folder containing one job.
    transport = FakeJenkinsTransport()
    client = JenkinsClient(BASE_URL, transport)

    # When jobs are collected from the controller root.
    jobs = list(client.iter_jobs())

    # Then the nested job is yielded as a public Job model.
    assert jobs == [
        Job(
            full_name="folder/example",
            display_name="Example",
            url=JOB_URL,
            jenkins_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
        ),
    ]
    assert jobs[0].name == "example"
    assert jobs[0].parent_full_name == "folder"


def test_iter_jobs_reports_missing_root_api_as_non_jenkins() -> None:
    # Given the configured base URL has no Jenkins API endpoint.
    client = JenkinsClient(BASE_URL, MissingApiTransport())

    # When jobs are read, then the base URL is diagnosed as non-Jenkins.
    with pytest.raises(ValueError, match="returned HTTP 404"):
        list(client.iter_jobs())


def test_iter_jobs_preserves_disabled_job_state() -> None:
    # Given Jenkins reports a disabled Pipeline job.
    transport = DisabledJobTransport()
    client = JenkinsClient(BASE_URL, transport)

    # When jobs are discovered.
    jobs = list(client.iter_jobs())

    # Then disabled status is part of the public Job model.
    assert jobs == [
        Job(
            full_name="disabled-pipeline",
            display_name="Disabled Pipeline",
            url=JOB_URL,
            jenkins_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
            disabled=True,
        )
    ]


def test_iter_jobs_handles_empty_and_cyclic_containers_with_encoded_paths() -> None:
    # Given a controller under a context path with an empty folder, a cycle, and
    # a multibranch-style job whose URL contains an encoded slash.
    transport = ContainerGraphTransport()
    client = JenkinsClient(CONTEXT_BASE_URL, transport)

    # When jobs are traversed from the context-rooted controller URL.
    jobs = list(client.iter_jobs())

    # Then traversal terminates, skips the empty container, and preserves paths.
    assert jobs == [
        Job(
            full_name="repo/feature%2Fencoded",
            display_name="feature/encoded",
            url=ENCODED_BRANCH_URL,
        )
    ]
    requested_urls = [url for url, _tree in transport.calls]
    assert requested_urls.count(joined_url(CONTEXT_BASE_URL, "api/json")) == 1
    assert joined_url(EMPTY_FOLDER_URL, "api/json") in requested_urls
    assert joined_url(ENCODED_BRANCH_URL, "api/json") in requested_urls


def test_iter_builds_uses_fake_pipeline_timing_and_filters_by_end_time() -> None:
    # Given a job with paginated retained builds, including one in-progress build.
    transport = FakeJenkinsTransport()
    client = JenkinsClient(BASE_URL, transport)
    job = next(client.iter_jobs())

    # When builds are collected with an end-time cutoff.
    builds = list(
        client.iter_builds(
            job,
            page_size=2,
            since=_epoch_ms(4_000),
        )
    )

    # Then builds use Pipeline REST timing and local end-time filtering.
    assert builds == [
        Build(
            job_full_name="folder/example",
            number=3,
            scheduled_time=_epoch_ms(1_000),
            start_time=_epoch_ms(2_000),
            duration=timedelta(milliseconds=2_500),
            status=BuildStatus.SUCCESS,
            url=BUILD_3_URL,
            jenkins_class="org.jenkinsci.plugins.workflow.job.WorkflowRun",
        )
    ]
    assert builds[0].end_time == _epoch_ms(4_500)
    assert (joined_url(BUILD_2_URL, "wfapi/describe"), None) not in transport.calls


def test_iter_builds_skips_disabled_jobs_before_requesting_build_pages() -> None:
    # Given a disabled Pipeline job and a transport that records requests.
    transport = SingleJobBuildTransport(
        pipeline_build_response(number=1, start_ms=1_000, duration_ms=100)
    )
    client = JenkinsClient(BASE_URL, transport)
    disabled_job = _example_job().model_copy(update={"disabled": True})

    # When builds are requested for the disabled job.
    builds = list(client.iter_builds(disabled_job))

    # Then no build pages or timing endpoints are queried.
    assert builds == []
    assert transport.calls == []


def test_iter_builds_resolves_lookback_once_and_scans_past_older_numbers() -> None:
    # Given build-number order differs from completion order across pages.
    clock = RecordingClock(_epoch_ms(10_000))

    transport = PaginatedBuildTransport(
        pages=[
            [
                pipeline_build_response(number=3, start_ms=6_000, duration_ms=1_499),
                pipeline_build_response(number=2, start_ms=6_500, duration_ms=1_000),
            ],
            [pipeline_build_response(number=1, start_ms=6_500, duration_ms=1_001)],
        ],
        start_times_ms={1: 6_500, 2: 6_500, 3: 6_000},
    )
    client = JenkinsClient(BASE_URL, transport, clock=clock)

    # When a lookback resolves to an inclusive completion cutoff of 7,500 ms.
    builds = list(
        client.iter_builds(
            _example_job(),
            page_size=2,
            lookback=timedelta(milliseconds=2_500),
        )
    )

    # Then the clock is called once, equality is included, and later pages are
    # still inspected even after an older high-numbered build is filtered out.
    assert clock.calls == 1
    assert [build.number for build in builds] == [2, 1]
    assert [build.end_time for build in builds] == [_epoch_ms(7_500), _epoch_ms(7_501)]
    assert transport.page_count == 2


def test_iter_builds_deduplicates_full_pages_and_stops_on_empty_page() -> None:
    # Given paginated retained history with a duplicate on a full second page.
    transport = PaginatedBuildTransport(
        pages=[
            [
                pipeline_build_response(number=3, start_ms=3_000, duration_ms=100),
                pipeline_build_response(number=2, start_ms=2_000, duration_ms=100),
            ],
            [
                pipeline_build_response(number=2, start_ms=2_000, duration_ms=100),
                pipeline_build_response(number=1, start_ms=1_000, duration_ms=100),
            ],
            [],
        ],
        start_times_ms={1: 1_000, 2: 2_000, 3: 3_000},
    )
    client = JenkinsClient(BASE_URL, transport)

    # When every page is consumed.
    builds = list(client.iter_builds(_example_job(), page_size=2))

    # Then duplicate build numbers are yielded and timed once, and the empty page
    # terminates iteration after the preceding full pages.
    assert [build.number for build in builds] == [3, 2, 1]
    assert transport.page_count == 3
    assert transport.timing_calls_for(2) == 1


@pytest.mark.parametrize(
    "build_overrides",
    [
        pytest.param({"building": True}, id="building"),
        pytest.param({"inProgress": True}, id="in-progress"),
        pytest.param({"result": None}, id="missing-result"),
    ],
)
def test_iter_builds_skips_non_terminal_builds_without_resolving_timing(
    build_overrides: dict[str, JsonValue],
) -> None:
    # Given a build blocked by one terminality gate.
    build = pipeline_build_response(number=3, start_ms=3_000, duration_ms=100)
    build.update(build_overrides)
    transport = SingleJobBuildTransport(build)
    client = JenkinsClient(BASE_URL, transport)

    # When builds are collected.
    builds = list(client.iter_builds(_example_job()))

    # Then the non-terminal build is not yielded and no timing request is needed.
    assert builds == []
    assert len(transport.calls) == 1
    page_url, page_tree = transport.calls[0]
    assert page_url == joined_url(JOB_URL, "api/json")
    assert page_tree is not None
    assert page_tree.startswith("allBuilds[")


def test_iter_builds_rejects_non_positive_page_size() -> None:
    # Given a Jenkins client and job.
    client = JenkinsClient(
        BASE_URL,
        SingleJobBuildTransport(
            build_response(
                number=1,
                url=BUILD_1_URL,
                timestamp=1_000,
                duration=1_000,
            )
        ),
    )
    job = _example_job()

    # When build iteration is requested with page_size=0, then it is rejected.
    with pytest.raises(ValueError, match="page_size must be positive"):
        list(client.iter_builds(job, page_size=0))


def test_iter_builds_rejects_since_and_lookback_together() -> None:
    # Given a Jenkins client and job.
    client = JenkinsClient(
        BASE_URL,
        SingleJobBuildTransport(
            build_response(
                number=1,
                url=BUILD_1_URL,
                timestamp=1_000,
                duration=1_000,
            )
        ),
    )
    job = _example_job()

    # When both absolute and relative cutoffs are supplied, then it is rejected.
    with pytest.raises(ValueError, match="Specify either since or lookback"):
        list(
            client.iter_builds(
                job,
                since=_epoch_ms(1_000),
                lookback=timedelta(seconds=1),
            )
        )


def test_iter_builds_rejects_naive_since() -> None:
    # Given a Jenkins client and job.
    client = JenkinsClient(
        BASE_URL,
        SingleJobBuildTransport(
            build_response(
                number=1,
                url=BUILD_1_URL,
                timestamp=1_000,
                duration=1_000,
            )
        ),
    )
    job = _example_job()

    # When the absolute cutoff has no timezone, then it is rejected.
    with pytest.raises(ValueError, match="since must include a timezone"):
        list(client.iter_builds(job, since=datetime(2024, 1, 1)))


def test_iter_builds_rejects_out_of_range_since_before_transport() -> None:
    # Given a cutoff whose timezone conversion would underflow UTC.
    transport = SingleJobBuildTransport(
        build_response(
            number=1,
            url=BUILD_1_URL,
            timestamp=1_000,
            duration=1_000,
        )
    )
    client = JenkinsClient(BASE_URL, transport)
    job = _example_job()
    since = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1)))

    # When build iteration starts, then validation fails before any request.
    with pytest.raises(ValueError, match="since is outside supported datetime range"):
        list(client.iter_builds(job, since=since))

    assert transport.calls == []


def test_iter_builds_rejects_negative_lookback() -> None:
    # Given a Jenkins client and job.
    client = JenkinsClient(
        BASE_URL,
        SingleJobBuildTransport(
            build_response(
                number=1,
                url=BUILD_1_URL,
                timestamp=1_000,
                duration=1_000,
            )
        ),
    )
    job = _example_job()

    # When the relative lookback is negative, then it is rejected.
    with pytest.raises(ValueError, match="lookback must not be negative"):
        list(client.iter_builds(job, lookback=-timedelta(seconds=1)))


def test_iter_builds_rejects_naive_clock_for_lookback() -> None:
    # Given a client clock that returns a naive datetime.
    client = JenkinsClient(
        BASE_URL,
        SingleJobBuildTransport(
            build_response(
                number=1,
                url=BUILD_1_URL,
                timestamp=1_000,
                duration=1_000,
            )
        ),
        clock=FixedClock(datetime(2024, 1, 1)),
    )
    job = _example_job()

    # When lookback needs the clock value, then the naive result is rejected.
    with pytest.raises(ValueError, match="clock must return a timezone-aware datetime"):
        list(client.iter_builds(job, lookback=timedelta(seconds=1)))


def test_iter_builds_rejects_boundary_clock_timezone_before_transport() -> None:
    # Given a clock value whose timezone conversion would underflow UTC.
    transport = SingleJobBuildTransport(
        build_response(
            number=1,
            url=BUILD_1_URL,
            timestamp=1_000,
            duration=1_000,
        )
    )
    client = JenkinsClient(
        BASE_URL,
        transport,
        clock=FixedClock(datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1)))),
    )
    job = _example_job()

    # When lookback resolution starts, then validation fails before any request.
    with pytest.raises(
        ValueError,
        match="clock value is outside supported datetime range",
    ):
        list(client.iter_builds(job, lookback=timedelta(seconds=1)))

    assert transport.calls == []


def test_iter_builds_rejects_out_of_range_cutoff_subtraction_before_transport() -> None:
    # Given a lookback that would underflow the datetime range.
    transport = SingleJobBuildTransport(
        build_response(
            number=1,
            url=BUILD_1_URL,
            timestamp=1_000,
            duration=1_000,
        )
    )
    client = JenkinsClient(
        BASE_URL,
        transport,
        clock=FixedClock(datetime(1, 1, 1, tzinfo=UTC)),
    )
    job = _example_job()

    # When lookback resolution starts, then validation fails before any request.
    with pytest.raises(
        ValueError,
        match="lookback produces cutoff outside supported datetime range",
    ):
        list(client.iter_builds(job, lookback=timedelta(milliseconds=1)))

    assert transport.calls == []


def test_pipeline_timing_404_raises_start_time_unavailable() -> None:
    # Given a Pipeline build whose timing endpoint returns 404.
    build = build_response(
        number=3,
        url=BUILD_3_URL,
        timestamp=1_000,
        duration=1_000,
        result="SUCCESS",
    )
    transport = PipelineTiming404Transport(build)
    client = JenkinsClient(BASE_URL, transport)
    job = _example_job()

    # When builds are collected, then timing resolution fails clearly.
    with pytest.raises(
        StartTimeUnavailable,
        match="Pipeline timing endpoint unavailable",
    ):
        list(client.iter_builds(job))


def test_start_time_resolution_rejects_non_pipeline_builds() -> None:
    # Given a non-Pipeline build.
    build = build_response(
        number=8,
        url=BUILD_3_URL,
        timestamp=10_000,
        duration=5_000,
        result="SUCCESS",
        jenkins_class="hudson.model.FreeStyleBuild",
    )
    transport = SingleJobBuildTransport(build)
    client = JenkinsClient(BASE_URL, transport)
    job = _example_job()

    # When builds are collected, then start-time resolution fails clearly.
    with pytest.raises(
        StartTimeUnavailable,
        match="only Pipeline builds are supported",
    ):
        list(client.iter_builds(job))
