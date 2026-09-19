from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import HttpUrl

from jenkins_stats import (
    Build,
    BuildStatus,
    JenkinsClient,
    Job,
    JsonTransportError,
    JsonValue,
    StartTimeUnavailable,
)
from jenkins_stats.models import joined_url

FIXTURE_DIR = (
    Path(__file__).parent / "fixtures" / "jenkins_contracts" / "sanitized_lts_pipeline"
)
BASE_URL = HttpUrl("https://jenkins.example/")
PIPELINE_JOB_URL = HttpUrl(f"{BASE_URL}job/folder/job/pipeline-main/")
FREESTYLE_JOB_URL = HttpUrl(f"{BASE_URL}job/folder/job/legacy-freestyle/")
PIPELINE_BUILD_42_URL = HttpUrl(f"{PIPELINE_JOB_URL}42/")
PIPELINE_BUILD_43_URL = HttpUrl(f"{PIPELINE_JOB_URL}43/")


def fixture_json(name: str) -> dict[str, object]:
    data = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return cast("dict[str, object]", data)


def json_object(data: dict[str, object], key: str) -> dict[str, object]:
    value = data[key]
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def json_list(data: dict[str, object], key: str) -> list[object]:
    value = data[key]
    assert isinstance(value, list)
    return cast("list[object]", value)


def json_str(data: dict[str, object], key: str) -> str:
    value = data[key]
    assert isinstance(value, str)
    return value


def json_int(data: dict[str, object], key: str) -> int:
    value = data[key]
    assert isinstance(value, int)
    return value


def epoch_ms(value: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)


def expected_trees() -> dict[str, str]:
    return cast(
        "dict[str, str]", json_object(fixture_json("index.json"), "expected_trees")
    )


class JenkinsContractFixtureTransport:
    def __init__(
        self,
        *,
        missing_plugin_timing: bool = False,
        forbidden_folder: bool = False,
    ) -> None:
        self.forbidden_folder = forbidden_folder
        self.calls: list[tuple[HttpUrl, str | None]] = []
        trees = expected_trees()
        item_tree = trees["item"]
        page_0_2_tree = trees["build_page_0_2"]
        page_2_4_tree = trees["build_page_2_4"]
        page_0_100_tree = trees["build_page_0_100"]
        pipeline_page_fixture = (
            "pipeline_missing_plugin_builds_page_0_100.json"
            if missing_plugin_timing
            else "pipeline_builds_page_0_2.json"
        )
        pipeline_page_tree = page_0_100_tree if missing_plugin_timing else page_0_2_tree
        branch_job_url = HttpUrl(
            f"{BASE_URL}job/team-service/job/feature%252Fcontract-fixture/"
        )
        self._responses: dict[tuple[HttpUrl, str | None], str] = {
            (joined_url(BASE_URL, "api/json"), item_tree): "root_api.json",
            (
                joined_url(HttpUrl(f"{BASE_URL}job/folder/"), "api/json"),
                item_tree,
            ): "folder_api.json",
            (
                joined_url(HttpUrl(f"{BASE_URL}job/team-service/"), "api/json"),
                item_tree,
            ): "multibranch_api.json",
            (
                joined_url(PIPELINE_JOB_URL, "api/json"),
                item_tree,
            ): "pipeline_job_item_api.json",
            (
                joined_url(PIPELINE_JOB_URL, "api/json"),
                pipeline_page_tree,
            ): pipeline_page_fixture,
            (
                joined_url(FREESTYLE_JOB_URL, "api/json"),
                item_tree,
            ): "freestyle_job_item_api.json",
            (
                joined_url(FREESTYLE_JOB_URL, "api/json"),
                page_0_100_tree,
            ): "freestyle_builds_page_0_100.json",
            (
                joined_url(branch_job_url, "api/json"),
                item_tree,
            ): "branch_job_item_api.json",
            (
                joined_url(PIPELINE_BUILD_42_URL, "wfapi/describe"),
                None,
            ): "pipeline_build_42_wfapi_describe.json",
        }
        if not missing_plugin_timing:
            self._responses[
                (joined_url(PIPELINE_JOB_URL, "api/json"), page_2_4_tree)
            ] = "pipeline_builds_page_2_4.json"

    def get_json(self, url: HttpUrl, *, tree: str | None = None) -> JsonValue:
        self.calls.append((url, tree))
        if self.forbidden_folder and url == joined_url(
            HttpUrl(f"{BASE_URL}job/folder/"), "api/json"
        ):
            raise JsonTransportError(url, "HTTP request failed", status_code=403)
        if url == joined_url(PIPELINE_BUILD_43_URL, "wfapi/describe"):
            if tree is not None:
                raise AssertionError(f"Unexpected tree for timing request: {tree!r}")
            raise JsonTransportError(url, "HTTP request failed", status_code=404)
        fixture_name = self._responses.get((url, tree))
        if fixture_name is None:
            raise AssertionError(f"Unexpected request: url={url!r}, tree={tree!r}")
        return cast("JsonValue", fixture_json(fixture_name))


def test_contract_fixture_states_exercised_jenkins_versions() -> None:
    # Given a committed Jenkins contract fixture set.
    metadata = fixture_json("index.json")

    # When its source metadata is inspected.
    source = json_object(metadata, "source")
    plugins = cast("dict[str, str]", json_object(source, "plugins"))
    configurations = cast("list[str]", json_list(source, "configurations"))

    # Then the fixture explicitly states Jenkins and plugin versions.
    assert source["jenkins_version"] == "2.492.3 LTS"
    assert plugins["workflow-job"] == "1540.v295eccc9778f"
    assert plugins["pipeline-rest-api"] == "2.38"
    assert (
        "Multibranch Pipeline project containing an encoded branch job"
        in configurations
    )


def test_iter_jobs_matches_sanitized_folder_and_multibranch_contract() -> None:
    # Given sanitized Jenkins responses for a folder and a multibranch project.
    transport = JenkinsContractFixtureTransport()
    client = JenkinsClient(BASE_URL, transport)

    # When jobs are discovered using the client's Jenkins tree projection.
    jobs = sorted(client.iter_jobs(), key=lambda job: job.full_name)

    # Then Jenkins folder/multibranch item responses validate into public jobs.
    expected_jobs = [
        Job(
            full_name=json_str(record, "full_name"),
            display_name=json_str(record, "display_name"),
            url=HttpUrl(json_str(record, "url")),
        )
        for record in cast(
            "list[dict[str, object]]",
            json_list(fixture_json("index.json"), "expected_jobs"),
        )
    ]
    assert jobs == sorted(expected_jobs, key=lambda job: job.full_name)
    assert all(tree == expected_trees()["item"] for _url, tree in transport.calls)


def test_iter_builds_matches_pipeline_timing_contract() -> None:
    # Given sanitized core build JSON and Pipeline REST timing for a Pipeline run.
    metadata = fixture_json("index.json")
    expected = json_object(metadata, "expected_pipeline_build")
    timing = fixture_json("pipeline_build_42_wfapi_describe.json")
    transport = JenkinsContractFixtureTransport()
    client = JenkinsClient(BASE_URL, transport)
    job = Job(full_name=json_str(expected, "job_full_name"), url=PIPELINE_JOB_URL)

    # When retained builds are collected.
    builds = list(client.iter_builds(job, page_size=2))

    # Then scheduled, start, duration, and end times keep their Jenkins meanings.
    assert builds == [
        Build(
            job_full_name=json_str(expected, "job_full_name"),
            number=json_int(expected, "number"),
            scheduled_time=epoch_ms(json_int(expected, "scheduled_time_ms")),
            start_time=epoch_ms(json_int(expected, "start_time_ms")),
            duration=timedelta(milliseconds=json_int(expected, "duration_ms")),
            status=BuildStatus(json_str(expected, "status")),
            url=HttpUrl(json_str(expected, "url")),
        )
    ]
    assert builds[0].scheduled_time < builds[0].start_time
    assert builds[0].end_time == epoch_ms(json_int(expected, "end_time_ms"))
    assert json_int(expected, "duration_ms") == json_int(timing, "durationMillis")
    assert json_int(expected, "end_time_ms") == json_int(timing, "endTimeMillis")
    assert (
        joined_url(PIPELINE_BUILD_42_URL, "wfapi/describe"),
        None,
    ) in transport.calls
    assert (
        joined_url(HttpUrl(f"{PIPELINE_JOB_URL}41/"), "wfapi/describe"),
        None,
    ) not in transport.calls


def test_non_pipeline_contract_fails_without_approximating_start_time() -> None:
    # Given a sanitized Freestyle build response with only Jenkins core timing.
    transport = JenkinsContractFixtureTransport()
    client = JenkinsClient(BASE_URL, transport)
    job = Job(full_name="folder/legacy-freestyle", url=FREESTYLE_JOB_URL)

    # When builds are collected, then unsupported timing is not approximated.
    with pytest.raises(
        StartTimeUnavailable, match="only Pipeline builds are supported"
    ):
        list(client.iter_builds(job))
    assert all(
        url.path is None or "wfapi/describe" not in url.path
        for url, _tree in transport.calls
    )


def test_missing_pipeline_rest_api_contract_fails_loudly() -> None:
    # Given a Pipeline run whose Pipeline REST timing endpoint is unavailable.
    transport = JenkinsContractFixtureTransport(missing_plugin_timing=True)
    client = JenkinsClient(BASE_URL, transport)
    job = Job(full_name="folder/pipeline-main", url=PIPELINE_JOB_URL)

    # When builds are collected, then the missing plugin/endpoint is explicit.
    with pytest.raises(
        StartTimeUnavailable, match="Pipeline timing endpoint unavailable"
    ):
        list(client.iter_builds(job))
    assert (
        joined_url(PIPELINE_BUILD_43_URL, "wfapi/describe"),
        None,
    ) in transport.calls


def test_permission_failure_contract_propagates_transport_error() -> None:
    # Given a sanitized traversal where a folder is not readable by the API user.
    transport = JenkinsContractFixtureTransport(forbidden_folder=True)
    client = JenkinsClient(BASE_URL, transport)

    # When jobs are traversed, then the permission failure aborts collection.
    with pytest.raises(JsonTransportError) as exc_info:
        list(client.iter_jobs())
    assert exc_info.value.status_code == 403
