from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING

import httpx
from pydantic import HttpUrl

from jenkins_stats.cli_command import (
    CollectAllCommand,
    CollectAllStoredJobBuildsCommand,
    run_cli_command,
)
from jenkins_stats.models import Build, BuildStatus, Job
from jenkins_stats.sqlite_store import SqliteStore
from tests.test_jenkins import install_mock_httpx_transport

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


BASE_URL = HttpUrl("https://jenkins.example/")
FOLDER_URL = HttpUrl(f"{BASE_URL}job/folder/")
JOB_URL = HttpUrl(f"{FOLDER_URL}job/example/")
BUILD_URL = HttpUrl(f"{JOB_URL}1/")


def test_run_cli_command_collects_through_production_adapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a typed collect command and mocked HTTP responses at the transport
    # boundary, while SQLite, HttpJsonTransport, JenkinsClient, and collection
    # all remain the real production components.
    database = tmp_path / "jenkins.sqlite"
    opened_urls: list[str] = []
    opened_authorization_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        opened_urls.append(str(request.url))
        opened_authorization_headers.append(request.headers["authorization"])
        tree = request.url.params.get("tree")

        if request.url.path == "/api/json":
            return httpx.Response(
                HTTPStatus.OK,
                json={"jobs": [{"name": "folder", "url": str(FOLDER_URL)}]},
            )
        if request.url.path == "/job/folder/api/json":
            return httpx.Response(
                HTTPStatus.OK,
                json={"jobs": [{"name": "example", "url": str(JOB_URL)}]},
            )
        if request.url.path == "/job/folder/job/example/api/json":
            if tree is not None and tree.startswith("_class,fullName,"):
                return httpx.Response(
                    HTTPStatus.OK,
                    json={
                        "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
                        "fullName": "folder/example",
                        "displayName": "Example",
                        "url": str(JOB_URL),
                        "builds": [],
                    },
                )
            if tree is not None and tree.startswith("allBuilds["):
                return httpx.Response(
                    HTTPStatus.OK,
                    json={
                        "allBuilds": [
                            {
                                "_class": (
                                    "org.jenkinsci.plugins.workflow.job.WorkflowRun"
                                ),
                                "number": 1,
                                "url": str(BUILD_URL),
                                "timestamp": 1_500,
                                "duration": 3_000,
                                "building": False,
                                "inProgress": False,
                                "result": "SUCCESS",
                            },
                        ],
                    },
                )
        if request.url.path == "/job/folder/job/example/1/wfapi/describe":
            return httpx.Response(HTTPStatus.OK, json={"startTimeMillis": 2_000})
        return httpx.Response(HTTPStatus.NOT_FOUND, request=request)

    install_mock_httpx_transport(monkeypatch, handler)
    command = CollectAllCommand(
        jenkins_url=BASE_URL,
        database=database,
        username="user",
        api_token="token",
        page_size=100,
        timeout=5.0,
        since=datetime(1970, 1, 1, tzinfo=UTC),
        lookback=None,
    )

    # When the top-level command dispatcher runs the collect command.
    result = run_cli_command(command)

    # Then Jenkins data is fetched through the HTTP adapter and persisted through
    # the real SQLite adapter.
    assert result.job_count == 1
    assert result.build_count == 1
    assert result.destination == str(database)
    assert opened_authorization_headers == ["Basic dXNlcjp0b2tlbg=="] * len(
        opened_urls,
    )
    assert opened_urls == [
        "https://jenkins.example/api/json?tree=_class%2CfullName%2CdisplayName%2Curl%2Cdisabled%2Cjobs%5Bname%2Curl%5D%2Cbuilds%5Bnumber%5D%7B0%2C0%7D",
        "https://jenkins.example/job/folder/api/json?tree=_class%2CfullName%2CdisplayName%2Curl%2Cdisabled%2Cjobs%5Bname%2Curl%5D%2Cbuilds%5Bnumber%5D%7B0%2C0%7D",
        "https://jenkins.example/job/folder/job/example/api/json?tree=_class%2CfullName%2CdisplayName%2Curl%2Cdisabled%2Cjobs%5Bname%2Curl%5D%2Cbuilds%5Bnumber%5D%7B0%2C0%7D",
        "https://jenkins.example/job/folder/job/example/api/json?tree=allBuilds%5B_class%2Cnumber%2Curl%2Ctimestamp%2Cduration%2Cbuilding%2CinProgress%2Cresult%5D%7B0%2C100%7D",
        "https://jenkins.example/job/folder/job/example/1/wfapi/describe",
    ]

    with SqliteStore.open_migrated(database) as store:
        assert list(store.iter_jobs()) == [
            Job(
                full_name="folder/example",
                display_name="Example",
                url=JOB_URL,
                jenkins_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
            ),
        ]
        assert list(store.iter_builds()) == [
            Build(
                job_full_name="folder/example",
                number=1,
                scheduled_time=datetime(1970, 1, 1, 0, 0, 1, 500_000, tzinfo=UTC),
                start_time=datetime(1970, 1, 1, 0, 0, 2, tzinfo=UTC),
                duration=timedelta(seconds=3),
                status=BuildStatus.SUCCESS,
                url=BUILD_URL,
                jenkins_class="org.jenkinsci.plugins.workflow.job.WorkflowRun",
            ),
        ]


def test_run_cli_command_collects_all_stored_job_builds_without_job_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a database whose stored jobs are the source of truth and an HTTP fake
    # that has no controller-root job discovery response.
    database = tmp_path / "jenkins.sqlite"
    with SqliteStore.open_migrated(database) as store:
        store.upsert_job(
            Job(
                full_name="folder/example",
                display_name="Example",
                url=JOB_URL,
                jenkins_class="org.jenkinsci.plugins.workflow.job.WorkflowJob",
            ),
        )

    opened_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        opened_urls.append(str(request.url))
        tree = request.url.params.get("tree")

        if (
            request.url.path == "/job/folder/job/example/api/json"
            and tree is not None
            and tree.startswith("allBuilds[")
        ):
            return httpx.Response(
                HTTPStatus.OK,
                json={
                    "allBuilds": [
                        {
                            "_class": "org.jenkinsci.plugins.workflow.job.WorkflowRun",
                            "number": 1,
                            "url": str(BUILD_URL),
                            "timestamp": 1_500,
                            "duration": 3_000,
                            "building": False,
                            "inProgress": False,
                            "result": "SUCCESS",
                        },
                    ],
                },
            )
        if request.url.path == "/job/folder/job/example/1/wfapi/describe":
            return httpx.Response(HTTPStatus.OK, json={"startTimeMillis": 2_000})
        return httpx.Response(HTTPStatus.NOT_FOUND, request=request)

    install_mock_httpx_transport(monkeypatch, handler)
    command = CollectAllStoredJobBuildsCommand(
        jenkins_url=BASE_URL,
        database=database,
        username="user",
        api_token="token",
        page_size=100,
        timeout=5.0,
        since=datetime(1970, 1, 1, tzinfo=UTC),
        lookback=None,
    )

    # When the all-jobs build command runs.
    result = run_cli_command(command)

    # Then it collects builds for the stored job without querying Jenkins jobs.
    assert result.job_count == 1
    assert result.build_count == 1
    assert result.destination == str(database)
    assert opened_urls == [
        "https://jenkins.example/job/folder/job/example/api/json?tree=allBuilds%5B_class%2Cnumber%2Curl%2Ctimestamp%2Cduration%2Cbuilding%2CinProgress%2Cresult%5D%7B0%2C100%7D",
        "https://jenkins.example/job/folder/job/example/1/wfapi/describe",
    ]

    with SqliteStore.open_migrated(database) as store:
        assert list(store.iter_builds()) == [
            Build(
                job_full_name="folder/example",
                number=1,
                scheduled_time=datetime(1970, 1, 1, 0, 0, 1, 500_000, tzinfo=UTC),
                start_time=datetime(1970, 1, 1, 0, 0, 2, tzinfo=UTC),
                duration=timedelta(seconds=3),
                status=BuildStatus.SUCCESS,
                url=BUILD_URL,
                jenkins_class="org.jenkinsci.plugins.workflow.job.WorkflowRun",
            ),
        ]
