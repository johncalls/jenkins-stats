from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import HttpUrl

from jenkins_stats import cli
from jenkins_stats.cli_command import (
    CliCommand,
    CollectAllCommand,
    CollectJobBuildsCommand,
    CollectJobsCommand,
)
from jenkins_stats.collection import CollectionResult
from jenkins_stats.jenkins import JsonTransportError, StartTimeUnavailable
from jenkins_stats.sqlite_store import SqliteStoreOperationError

BASE_URL = "https://jenkins.example/"


def _cli_command_from_argv(argv: list[str]) -> CliCommand:
    parser = cli.build_parser()
    return cli.cli_command_from_args(parser.parse_args(argv))


def test_collect_all_arguments_default_to_no_completion_filter(
    tmp_path: Path,
) -> None:
    # Given all-collection arguments with explicit connection settings.
    database = tmp_path / "jenkins.sqlite"

    # When the parsed namespace is converted into an application command.
    command = _cli_command_from_argv(
        [
            "collect",
            "--url",
            BASE_URL,
            "--db",
            str(database),
            "--username",
            "api-user",
            "--api-token",
            "api-token",
            "all",
        ],
    )

    # Then it contains typed values without a completion-time cutoff.
    assert command == CollectAllCommand(
        jenkins_url=HttpUrl(BASE_URL),
        database=database,
        username="api-user",
        api_token="api-token",
        timeout=30.0,
        page_size=100,
        since=None,
        lookback=None,
    )


def test_collect_commands_use_their_expected_scope_and_filters(tmp_path: Path) -> None:
    # Given collection commands with an explicit shared connection configuration.
    database = tmp_path / "jenkins.sqlite"
    connection_args = [
        "--url",
        BASE_URL,
        "--db",
        str(database),
        "--username",
        "api-user",
        "--api-token",
        "api-token",
    ]

    # When each collection target is parsed.
    jobs_command = _cli_command_from_argv(["collect", *connection_args, "jobs"])
    builds_command = _cli_command_from_argv(
        [
            "collect",
            *connection_args,
            "builds",
            "folder/example",
            "--since",
            "2024-01-01T12:30:00Z",
        ],
    )
    all_command = _cli_command_from_argv(
        ["collect", *connection_args, "all", "--lookback", "6h"],
    )

    # Then jobs excludes build settings, while build targets retain their filters.
    assert jobs_command == CollectJobsCommand(
        jenkins_url=HttpUrl(BASE_URL),
        database=database,
        username="api-user",
        api_token="api-token",
        timeout=30.0,
    )
    assert builds_command == CollectJobBuildsCommand(
        jenkins_url=HttpUrl(BASE_URL),
        database=database,
        username="api-user",
        api_token="api-token",
        timeout=30.0,
        page_size=100,
        since=datetime(2024, 1, 1, 12, 30, tzinfo=UTC),
        lookback=None,
        job_full_name="folder/example",
    )
    assert isinstance(all_command, CollectAllCommand)
    assert all_command.since is None
    assert all_command.lookback == timedelta(hours=6)


def test_collect_uses_environment_url_credentials_and_default_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given Jenkins settings in environment variables and the current directory.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JENKINS_URL", BASE_URL)
    monkeypatch.setenv("JENKINS_USERNAME", "env-user")
    monkeypatch.setenv("JENKINS_API_TOKEN", "env-token")

    # When a jobs-only command omits connection options.
    command = _cli_command_from_argv(["collect", "jobs"])

    # Then it uses environment connection settings and the local default database.
    assert command == CollectJobsCommand(
        jenkins_url=HttpUrl(BASE_URL),
        database=Path("jenkins.sqlite"),
        username="env-user",
        api_token="env-token",
        timeout=30.0,
    )


def test_main_passes_collect_all_command_to_runner_and_prints_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given an all-collection invocation and a runner that records its command.
    database = tmp_path / "jenkins.sqlite"
    seen_commands: list[CliCommand] = []

    def runner(command: CliCommand) -> CollectionResult:
        seen_commands.append(command)
        return CollectionResult(job_count=2, build_count=3).with_destination(
            command.database,
        )

    # When main runs the command.
    exit_code = cli.main(
        [
            "collect",
            "--url",
            BASE_URL,
            "--db",
            str(database),
            "--username",
            "api-user",
            "--api-token",
            "api-token",
            "all",
        ],
        command_runner=runner,
    )

    # Then main delegates to the runner and renders its result.
    assert exit_code == 0
    assert seen_commands == [
        CollectAllCommand(
            jenkins_url=HttpUrl(BASE_URL),
            database=database,
            username="api-user",
            api_token="api-token",
            timeout=30.0,
            page_size=100,
            since=None,
            lookback=None,
        ),
    ]
    assert capsys.readouterr().out == f"Stored 3 builds for 2 jobs in {database}\n"


def test_parse_duration_supports_compound_units() -> None:
    # Given an ordinary compound duration.
    # When it is parsed, then all components are preserved.
    assert cli.parse_duration("1h30m250ms") == timedelta(
        hours=1,
        minutes=30,
        milliseconds=250,
    )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            [
                "collect",
                "--url",
                BASE_URL,
                "--username",
                "u",
                "--api-token",
                "t",
                "all",
                "--since",
                "2024-01-01T00:00:00Z",
                "--lookback",
                "1h",
            ],
            "Specify either --since or --lookback, not both",
        ),
        (
            [
                "collect",
                "--url",
                BASE_URL,
                "--username",
                "u",
                "--api-token",
                "t",
                "all",
                "--since",
                "2024-01-01T00:00:00",
            ],
            "--since must include a timezone",
        ),
        (
            [
                "collect",
                "--url",
                BASE_URL,
                "--username",
                "u",
                "--api-token",
                "t",
                "all",
                "--lookback",
                "nope",
            ],
            "invalid duration",
        ),
        (
            [
                "collect",
                "--url",
                BASE_URL,
                "--username",
                "u",
                "--api-token",
                "t",
                "all",
                "--lookback",
                "1000000000d",
            ],
            "duration is too large",
        ),
        (
            [
                "collect",
                "--url",
                BASE_URL,
                "--username",
                "u",
                "--api-token",
                "t",
                "all",
                "--since",
                "0001-01-01T00:00:00+01:00",
            ],
            "--since is outside supported datetime range",
        ),
    ],
)
def test_collect_command_rejects_invalid_filter_arguments(
    argv: list[str],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given invalid completion-time filter arguments.
    def runner(_command: CliCommand) -> CollectionResult:
        pytest.fail("runner should not be called")

    # When parsing or command validation runs, then argparse exits clearly.
    with pytest.raises(SystemExit) as exc_info:
        cli.main(argv, command_runner=runner)

    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert message in err
    assert "Traceback" not in err


def test_collect_command_rejects_out_of_range_cutoff_before_running(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given a representable lookback that still underflows today's cutoff.
    database = tmp_path / "jenkins.sqlite"

    def runner(_command: CliCommand) -> CollectionResult:
        pytest.fail("runner should not be called")

    # When collection is requested, then validation fails before the runner.
    exit_code = cli.main(
        [
            "collect",
            "--url",
            BASE_URL,
            "--db",
            str(database),
            "--username",
            "u",
            "--api-token",
            "t",
            "all",
            "--lookback",
            "999999999d",
        ],
        command_runner=runner,
    )

    assert exit_code == 2
    assert "lookback produces cutoff outside supported datetime range" in (
        capsys.readouterr().err
    )
    assert not database.exists()


def test_collect_command_requires_url_and_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given no connection options or environment fallbacks.
    monkeypatch.delenv("JENKINS_URL", raising=False)
    monkeypatch.delenv("JENKINS_USERNAME", raising=False)
    monkeypatch.delenv("JENKINS_API_TOKEN", raising=False)

    def runner(_command: CliCommand) -> CollectionResult:
        pytest.fail("runner should not be called")

    # When collection is requested, then it fails before running the command.
    exit_code = cli.main(["collect", "jobs"], command_runner=runner)

    assert exit_code == 2
    assert "Missing required --url or JENKINS_URL" in capsys.readouterr().err
    assert not (tmp_path / "jenkins.sqlite").exists()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        pytest.param(
            StartTimeUnavailable("Pipeline timing endpoint unavailable"),
            "Pipeline timing endpoint unavailable",
            id="missing-start-time",
        ),
        pytest.param(
            JsonTransportError(
                HttpUrl(BASE_URL),
                "HTTP request failed",
                status_code=403,
            ),
            "HTTP request failed",
            id="transport-error",
        ),
        pytest.param(
            SqliteStoreOperationError("SQLite upsert jobs failed: database is locked"),
            "database is locked",
            id="storage-error",
        ),
    ],
)
def test_main_reports_operational_failures_without_success_summary(
    failure: Exception,
    expected: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given a runner that raises an operational failure.
    def runner(_command: CliCommand) -> CollectionResult:
        raise failure

    # When the CLI command runs.
    exit_code = cli.main(
        [
            "collect",
            "--url",
            BASE_URL,
            "--db",
            str(tmp_path / "jenkins.sqlite"),
            "--username",
            "api-user",
            "--api-token",
            "api-token",
            "all",
        ],
        command_runner=runner,
    )

    # Then it returns a controlled failure without a success summary.
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert expected in captured.err
    assert "fix the problem and rerun" in captured.err
    assert "Traceback" not in captured.err
