"""Command-line interface for collecting Jenkins statistics."""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from pydantic import HttpUrl, ValidationError

from jenkins_stats.cli_command import (
    CliCommand,
    CollectAllCommand,
    CollectJobBuildsCommand,
    CollectJobsCommand,
    run_cli_command,
)
from jenkins_stats.collection import DEFAULT_LOOKBACK
from jenkins_stats.jenkins import JsonTransportError, StartTimeUnavailable
from jenkins_stats.sqlite_store import SqliteStoreError

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "CliCommandRunner",
    "CliResult",
    "build_parser",
    "cli_command_from_args",
    "main",
    "parse_duration",
]

_SECONDS_PER_MINUTE: Final = 60
_SECONDS_PER_HOUR: Final = 60 * _SECONDS_PER_MINUTE
_SECONDS_PER_DAY: Final = 24 * _SECONDS_PER_HOUR
_DURATION_TOKEN_RE: Final = re.compile(r"(?P<value>\d+)(?P<unit>ms|s|m|h|d)")
_DURATION_UNITS: Final[dict[str, timedelta]] = {
    "ms": timedelta(milliseconds=1),
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
}
_DEFAULT_DATABASE: Final = "./jenkins.sqlite"


class CliResult(Protocol):
    """Command result that can provide lines for CLI output."""

    def output_lines(self) -> Sequence[str]:
        """Return lines to render to stdout."""
        ...


class CliCommandRunner(Protocol):
    """Run a typed top-level CLI command and return a renderable result."""

    def __call__(self, command: CliCommand, /) -> CliResult:
        """Run the command."""
        ...


def parse_duration(value: str) -> timedelta:
    """Parse a CLI duration such as ``1h``, ``30m``, ``1h30m``, or seconds."""
    stripped = value.strip().lower()
    if stripped == "":
        raise argparse.ArgumentTypeError("duration must not be empty")
    if stripped.isdecimal():
        return _timedelta_from_seconds(int(stripped), value)

    total = timedelta(0)
    position = 0
    for match in _DURATION_TOKEN_RE.finditer(stripped):
        if match.start() != position:
            raise argparse.ArgumentTypeError(f"invalid duration: {value}")
        try:
            total += int(match.group("value")) * _duration_unit(match.group("unit"))
        except OverflowError as exc:
            raise argparse.ArgumentTypeError(f"duration is too large: {value}") from exc
        position = match.end()
    if position != len(stripped) or total < timedelta(0):
        raise argparse.ArgumentTypeError(f"invalid duration: {value}")
    return total


def _timedelta_from_seconds(seconds: int, source: str) -> timedelta:
    try:
        return timedelta(seconds=seconds)
    except OverflowError as exc:
        raise argparse.ArgumentTypeError(f"duration is too large: {source}") from exc


def _duration_unit(unit: str) -> timedelta:
    try:
        return _DURATION_UNITS[unit]
    except KeyError as exc:
        raise AssertionError(f"Unsupported duration unit: {unit}") from exc


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return parsed


def _non_negative_duration(value: str) -> timedelta:
    duration = parse_duration(value)
    if duration < timedelta(0):
        raise argparse.ArgumentTypeError("duration must not be negative")
    return duration


def _http_url(value: str) -> HttpUrl:
    try:
        return HttpUrl(value)
    except ValidationError as exc:
        raise argparse.ArgumentTypeError("must be a valid HTTP(S) URL") from exc


def _aware_datetime(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--since must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--since must include a timezone")
    try:
        return parsed.astimezone(UTC)
    except OverflowError as exc:
        raise argparse.ArgumentTypeError(
            "--since is outside supported datetime range"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="jenkins-stats",
        description="Collect Jenkins jobs and build metadata into SQLite.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect_parser = subparsers.add_parser(
        "collect",
        help="collect Jenkins jobs or build metadata into SQLite",
    )
    _add_connection_options(collect_parser)
    collect_subparsers = collect_parser.add_subparsers(
        dest="collect_target", required=True
    )

    all_parser = collect_subparsers.add_parser(
        "all",
        help="collect visible jobs and builds",
    )
    _add_build_collection_options(all_parser)

    collect_subparsers.add_parser(
        "jobs",
        help="collect visible jobs without builds",
    )
    builds_parser = collect_subparsers.add_parser(
        "builds",
        help="collect builds for one previously stored job",
    )
    builds_parser.add_argument(
        "job_full_name",
        help="full name of a job previously stored in the database",
    )
    _add_build_collection_options(builds_parser)
    return parser


def _add_connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        "--jenkins-url",
        dest="jenkins_url",
        type=_http_url,
        default=os.environ.get("JENKINS_URL"),
        help="Jenkins base URL (or JENKINS_URL)",
    )
    parser.add_argument(
        "--db",
        "--database",
        dest="database",
        default=_DEFAULT_DATABASE,
        help=f"SQLite database path (default: {_DEFAULT_DATABASE})",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("JENKINS_USERNAME"),
        help="Jenkins username (or JENKINS_USERNAME)",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get("JENKINS_API_TOKEN"),
        help="Jenkins API token (or JENKINS_API_TOKEN)",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30)",
    )


def _add_build_collection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--since",
        type=_aware_datetime,
        help="inclusive completion-time cutoff as a timezone-aware ISO datetime",
    )
    parser.add_argument(
        "--lookback",
        type=_non_negative_duration,
        help="relative completion-time cutoff (default: 1d)",
    )
    parser.add_argument(
        "--page-size",
        type=_positive_int,
        default=100,
        help="Jenkins build page size (default: 100)",
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    command_runner: CliCommandRunner = run_cli_command,
) -> int:
    """Run the command-line interface."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.collect_target in {"all", "builds"} and (
        args.since is not None and args.lookback is not None
    ):
        parser.error("Specify either --since or --lookback, not both")

    try:
        result = command_runner(cli_command_from_args(args))
    except ValueError as exc:
        _write_error(exc)
        return 2
    except JsonTransportError as exc:
        _write_error(exc)
        _write_partial_failure_note()
        return 1
    except StartTimeUnavailable as exc:
        _write_error(exc)
        _write_partial_failure_note()
        return 1
    except SqliteStoreError as exc:
        _write_error(exc)
        _write_partial_failure_note()
        return 1

    _present_result(result)
    return 0


def cli_command_from_args(args: argparse.Namespace) -> CliCommand:
    """Convert parsed arguments into a typed top-level CLI command."""
    if args.command != "collect":
        raise ValueError(f"unknown command: {args.command}")
    if args.collect_target == "all":
        return _collect_all_command_from_args(args)
    if args.collect_target == "jobs":
        return _collect_jobs_command_from_args(args)
    if args.collect_target == "builds":
        return _collect_job_builds_command_from_args(args)
    raise ValueError(f"unknown collection target: {args.collect_target}")


def _collect_all_command_from_args(args: argparse.Namespace) -> CollectAllCommand:
    jenkins_url, database, username, api_token, timeout = _connection_args(args)
    page_size, since, lookback = _build_collection_args(args)
    return CollectAllCommand(
        jenkins_url=jenkins_url,
        database=database,
        username=username,
        api_token=api_token,
        timeout=timeout,
        page_size=page_size,
        since=since,
        lookback=lookback,
    )


def _collect_jobs_command_from_args(args: argparse.Namespace) -> CollectJobsCommand:
    jenkins_url, database, username, api_token, timeout = _connection_args(args)
    return CollectJobsCommand(
        jenkins_url=jenkins_url,
        database=database,
        username=username,
        api_token=api_token,
        timeout=timeout,
    )


def _collect_job_builds_command_from_args(
    args: argparse.Namespace,
) -> CollectJobBuildsCommand:
    jenkins_url, database, username, api_token, timeout = _connection_args(args)
    page_size, since, lookback = _build_collection_args(args)
    return CollectJobBuildsCommand(
        jenkins_url=jenkins_url,
        database=database,
        username=username,
        api_token=api_token,
        timeout=timeout,
        page_size=page_size,
        since=since,
        lookback=lookback,
        job_full_name=_required_str_arg(args, "job_full_name", "JOB_FULL_NAME"),
    )


def _connection_args(
    args: argparse.Namespace,
) -> tuple[HttpUrl, Path, str, str, float]:
    return (
        _required_http_url_arg(args, "jenkins_url", "--url or JENKINS_URL"),
        Path(_str_arg(args, "database")),
        _required_str_arg(args, "username", "--username or JENKINS_USERNAME"),
        _required_str_arg(args, "api_token", "--api-token or JENKINS_API_TOKEN"),
        _float_arg(args, "timeout"),
    )


def _build_collection_args(
    args: argparse.Namespace,
) -> tuple[int, datetime | None, timedelta | None]:
    since = _optional_datetime_arg(args, "since")
    lookback = _optional_timedelta_arg(args, "lookback")
    if since is not None and lookback is not None:
        raise ValueError("Specify either since or lookback, not both")
    effective_lookback = None if since is not None else lookback or DEFAULT_LOOKBACK
    _validate_completion_cutoff(effective_lookback)
    return _int_arg(args, "page_size"), since, effective_lookback


def _validate_completion_cutoff(lookback: timedelta | None) -> None:
    if lookback is None:
        return
    try:
        _cutoff = datetime.now(UTC) - lookback
    except OverflowError as exc:
        raise ValueError(
            "lookback produces cutoff outside supported datetime range"
        ) from exc


def _write_error(exc: BaseException) -> None:
    sys.stderr.write(f"error: {exc}\n")


def _write_partial_failure_note() -> None:
    sys.stderr.write(
        "Collection failed before the success summary. The database may contain "
        "jobs or builds stored before the failure; fix the problem and rerun the "
        "command to upsert data safely.\n"
    )


def _present_result(result: CliResult) -> None:
    def _render_lines(lines: Sequence[str]) -> str:
        if not lines:
            return ""
        return "\n".join(lines) + "\n"

    sys.stdout.write(_render_lines(result.output_lines()))


def _str_arg(args: argparse.Namespace, name: str) -> str:
    value = getattr(args, name)
    if not isinstance(value, str):
        raise TypeError(f"Expected {name} to be a string")
    return value


def _required_http_url_arg(
    args: argparse.Namespace,
    name: str,
    source: str,
) -> HttpUrl:
    value = getattr(args, name)
    if value is None:
        raise ValueError(f"Missing required {source}")
    if not isinstance(value, HttpUrl):
        raise TypeError(f"Expected {name} to be an HTTP URL")
    return value


def _required_str_arg(args: argparse.Namespace, name: str, source: str) -> str:
    value = getattr(args, name)
    if value is None or value == "":
        raise ValueError(f"Missing required {source}")
    if not isinstance(value, str):
        raise TypeError(f"Expected {name} to be a string")
    return value


def _optional_datetime_arg(args: argparse.Namespace, name: str) -> datetime | None:
    value = getattr(args, name)
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"Expected {name} to be a datetime or None")
    return value


def _optional_timedelta_arg(args: argparse.Namespace, name: str) -> timedelta | None:
    value = getattr(args, name)
    if value is None:
        return None
    if not isinstance(value, timedelta):
        raise TypeError(f"Expected {name} to be a timedelta or None")
    return value


def _int_arg(args: argparse.Namespace, name: str) -> int:
    value = getattr(args, name)
    if type(value) is not int:
        raise TypeError(f"Expected {name} to be an integer")
    return value


def _float_arg(args: argparse.Namespace, name: str) -> float:
    value = getattr(args, name)
    if type(value) not in {float, int}:
        raise TypeError(f"Expected {name} to be a float")
    return float(value)


if __name__ == "__main__":
    raise SystemExit(main())
