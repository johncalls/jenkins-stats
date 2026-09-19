"""Typed top-level CLI commands and their production runners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from jenkins_stats.collection import (
    BuildCollectionOptions,
    CollectionResult,
    collect,
    collect_job_builds,
    collect_jobs,
)
from jenkins_stats.jenkins import HttpJsonTransport, JenkinsClient
from jenkins_stats.sqlite_store import SqliteStore

if TYPE_CHECKING:
    from datetime import datetime, timedelta
    from pathlib import Path

    from pydantic import HttpUrl

__all__ = [
    "CliCommand",
    "CollectAllCommand",
    "CollectJobBuildsCommand",
    "CollectJobsCommand",
    "run_cli_command",
]


@dataclass(frozen=True)
class _ConnectionCommand:
    """Connection and destination settings shared by collection commands."""

    jenkins_url: HttpUrl
    database: Path
    username: str
    api_token: str
    timeout: float


@dataclass(frozen=True)
class _BuildCollectionCommand(_ConnectionCommand):
    """Build collection settings shared by commands that retrieve builds."""

    page_size: int
    since: datetime | None
    lookback: timedelta | None


@dataclass(frozen=True)
class CollectAllCommand(_BuildCollectionCommand):
    """Collect all visible jobs and their builds."""


@dataclass(frozen=True)
class CollectJobsCommand(_ConnectionCommand):
    """Collect all visible jobs without retrieving builds."""


@dataclass(frozen=True)
class CollectJobBuildsCommand(_BuildCollectionCommand):
    """Collect builds for one job already stored in the destination database."""

    job_full_name: str


type CliCommand = CollectAllCommand | CollectJobsCommand | CollectJobBuildsCommand


def run_cli_command(command: CliCommand) -> CollectionResult:
    """Run a typed top-level CLI command using production components."""
    with (
        HttpJsonTransport(
            command.jenkins_url,
            username=command.username,
            api_token=command.api_token,
            timeout=command.timeout,
        ) as transport,
        SqliteStore.open_migrated(command.database) as store,
    ):
        jenkins_client = JenkinsClient(command.jenkins_url, transport)
        match command:
            case CollectAllCommand():
                result = collect(
                    jenkins_client,
                    store,
                    page_size=command.page_size,
                    since=command.since,
                    lookback=command.lookback,
                )
            case CollectJobsCommand():
                result = collect_jobs(jenkins_client, store)
            case CollectJobBuildsCommand():
                result = collect_job_builds(
                    jenkins_client,
                    store,
                    command.job_full_name,
                    BuildCollectionOptions(
                        page_size=command.page_size,
                        since=command.since,
                        lookback=command.lookback,
                    ),
                )
        return result.with_destination(command.database)
