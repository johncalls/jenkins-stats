from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import UTC, datetime

    from pydantic import HttpUrl
    from sqlite_utils import Database
    from sqlite_utils.db import Table

    from jenkins_stats.cli import CliCommandRunner, CliResult
    from jenkins_stats.cli_command import run_cli_command
    from jenkins_stats.cli_progress import ProgressDisplay
    from jenkins_stats.collection import (
        CollectionProgress,
        CollectionResult,
        JenkinsBuildClient,
        ProgressCallback,
        Store,
    )
    from jenkins_stats.jenkins import (
        Clock,
        HttpJsonTransport,
        JenkinsClient,
        JsonTransport,
    )
    from jenkins_stats.sqlite_store import (
        SqliteStore,
        _BuildsTable,  # pyright: ignore[reportPrivateUsage]
        _JobsTable,  # pyright: ignore[reportPrivateUsage]
    )
    from tests.test_collection import RecordingClient, RecordingStore
    from tests.test_jenkins import (
        ContainerGraphTransport,
        FakeJenkinsTransport,
        FixedClock,
        MissingApiTransport,
        PaginatedBuildTransport,
        PipelineTiming404Transport,
        RecordingClock,
        SingleJobBuildTransport,
    )
    from tests.test_jenkins_contract_fixtures import JenkinsContractFixtureTransport


if TYPE_CHECKING:
    _fake_jenkins_transport_check: JsonTransport = FakeJenkinsTransport()
    _container_graph_transport_check: JsonTransport = ContainerGraphTransport()
    _paginated_build_transport_check: JsonTransport = PaginatedBuildTransport(
        pages=[],
        start_times_ms={},
    )
    _single_job_build_transport_check: JsonTransport = SingleJobBuildTransport(build={})
    _pipeline_timing_404_transport_check: JsonTransport = PipelineTiming404Transport(
        build={},
    )
    _missing_api_transport_check: JsonTransport = MissingApiTransport()
    _contract_fixture_transport_check: JsonTransport = JenkinsContractFixtureTransport()

    _http_transport_check: JsonTransport = HttpJsonTransport(
        HttpUrl("https://jenkins.example/"),
        username="user",
        api_token="token",
    )

    # The unified collection-client protocol is satisfied by both the test fake
    # and the production Jenkins adapter.
    _collection_client_checks: tuple[JenkinsBuildClient, ...] = (
        RecordingClient(jobs=[], builds_by_job={}),
        JenkinsClient(
            HttpUrl("https://jenkins.example/"),
            FakeJenkinsTransport(),
        ),
    )

    def _utc_now_test_helper() -> datetime:
        return datetime.now(UTC)

    _utc_now_test_helper_check: Clock = _utc_now_test_helper
    _fixed_clock_check: Clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    _recording_clock_check: Clock = RecordingClock(datetime(2026, 1, 1, tzinfo=UTC))

    # The unified store protocol includes job lookup, job iteration, and upserts.
    _collection_store_checks: tuple[Store, ...] = (
        RecordingStore(),
        SqliteStore(Database(memory=True)),
    )

    _sqlite_utils_table_check = Table(Database(memory=True), "protocol_check")
    _sqlite_jobs_table_check: _JobsTable = _sqlite_utils_table_check
    _sqlite_builds_table_check: _BuildsTable = _sqlite_utils_table_check

    def _progress_callback_test_helper(event: CollectionProgress, /) -> None:
        _ = event

    _progress_callback_function_check: ProgressCallback = _progress_callback_test_helper
    _progress_display_callback_check: ProgressCallback = ProgressDisplay("none")

    _collection_result_check: CliResult = CollectionResult(
        job_count=0,
        build_count=0,
    )
    _run_cli_command_check: CliCommandRunner = run_cli_command
