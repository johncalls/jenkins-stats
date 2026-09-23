from __future__ import annotations

import re
from io import StringIO
from typing import Final

from jenkins_stats.cli_progress import ProgressDisplay
from jenkins_stats.collection import CollectionPhase, CollectionProgress

_ANSI_PATTERN: Final = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


def _last_rendered_frame(output: str) -> str:
    return _ANSI_PATTERN.sub("", output).split("\r")[-1]


def test_auto_progress_is_silent_when_stderr_is_not_a_terminal() -> None:
    # Given redirected stderr and automatic progress mode.
    stream = StringIO()
    display = ProgressDisplay("auto", stream=stream)

    # When discovery progress is reported.
    with display:
        display(CollectionProgress(CollectionPhase.DISCOVERING_JOBS))
        display(CollectionProgress(CollectionPhase.JOBS_DISCOVERED, jobs_found=3))

    # Then redirected output stays quiet.
    assert stream.getvalue() == ""


def test_plain_progress_reports_discovery_and_per_job_builds() -> None:
    # Given plain progress mode and a redirected stderr stream.
    stream = StringIO()
    display = ProgressDisplay("plain", stream=stream)

    # When discovery and build collection events are reported.
    with display:
        display(CollectionProgress(CollectionPhase.DISCOVERING_JOBS))
        display(CollectionProgress(CollectionPhase.JOBS_DISCOVERED, jobs_found=2))
        display(
            CollectionProgress(
                CollectionPhase.COLLECTING_JOB_BUILDS,
                job_count=2,
                job_index=1,
                job_full_name="folder/example",
            )
        )
        display(
            CollectionProgress(
                CollectionPhase.BUILD_COLLECTED,
                job_full_name="folder/example",
                builds_collected=1,
            )
        )
        display(
            CollectionProgress(
                CollectionPhase.JOB_BUILDS_COLLECTED,
                job_count=2,
                job_index=1,
                job_full_name="folder/example",
                builds_collected=1,
            )
        )

    # Then plain status lines describe the actual collection phases.
    assert stream.getvalue().splitlines() == [
        "Discovering Jenkins jobs...",
        "Discovered 2 jobs.",
        "[1/2] Collecting builds for folder/example...",
        "  1 builds collected for folder/example",
        "[1/2] Stored 1 builds for folder/example.",
    ]


def test_rich_progress_keeps_overall_collecting_builds_status_simple() -> None:
    # Given rich progress mode collecting builds for one job.
    stream = TtyStringIO()
    display = ProgressDisplay("auto", stream=stream)

    # When a job is collected and rendered by the live progress display.
    with display:
        display(CollectionProgress(CollectionPhase.JOBS_DISCOVERED, jobs_found=1))
        display(
            CollectionProgress(
                CollectionPhase.COLLECTING_JOB_BUILDS,
                job_count=1,
                job_index=1,
                job_full_name="folder/example",
            )
        )
        display(
            CollectionProgress(
                CollectionPhase.BUILD_COLLECTED,
                job_full_name="folder/example",
                builds_collected=1,
            )
        )
        display(
            CollectionProgress(
                CollectionPhase.JOB_BUILDS_COLLECTED,
                job_count=1,
                job_index=1,
                job_full_name="folder/example",
                builds_collected=1,
            )
        )

    # Then job progress appears under a simple overall task text instead of a
    # leading complete/total column or repeated job count on every frame.
    output = _ANSI_PATTERN.sub("", stream.getvalue())
    assert "Collecting builds" in output
    assert "jobs processed" not in output
    assert "[1/1] folder/example: 1 builds" in output
    assert "[1/1] folder/example: Stored 1 builds" in output
    assert re.search(r"(?:^|\r)\d+/\d+\s+0:00:00", output) is None


def test_rich_progress_keeps_collecting_builds_status_still() -> None:
    # Given rich progress mode after job discovery.
    stream = TtyStringIO()
    display = ProgressDisplay("auto", stream=stream)

    # When build collection starts.
    with display:
        display(CollectionProgress(CollectionPhase.JOBS_DISCOVERED, jobs_found=1))

    # Then the overall collection line does not include an animated spinner.
    collecting_lines = [
        line
        for line in _last_rendered_frame(stream.getvalue()).splitlines()
        if "Collecting builds" in line
    ]
    assert collecting_lines
    assert all(
        re.match(r"^\d+:\d\d:\d\d\s+Collecting builds", line)
        for line in collecting_lines
    )


def test_rich_progress_keeps_only_last_five_job_rows() -> None:
    # Given rich progress mode with more job rows than fit in the display window.
    stream = TtyStringIO()
    display = ProgressDisplay("auto", stream=stream)

    # When six jobs have started and the sixth job is still collecting builds.
    with display:
        display(CollectionProgress(CollectionPhase.JOBS_DISCOVERED, jobs_found=6))
        for job_index in range(1, 7):
            job_full_name = f"folder/job-{job_index}"
            display(
                CollectionProgress(
                    CollectionPhase.COLLECTING_JOB_BUILDS,
                    job_count=6,
                    job_index=job_index,
                    job_full_name=job_full_name,
                )
            )
            display(
                CollectionProgress(
                    CollectionPhase.BUILD_COLLECTED,
                    job_full_name=job_full_name,
                    builds_collected=1,
                )
            )
            if job_index < 6:
                display(
                    CollectionProgress(
                        CollectionPhase.JOB_BUILDS_COLLECTED,
                        job_count=6,
                        job_index=job_index,
                        job_full_name=job_full_name,
                        builds_collected=1,
                    )
                )

    # Then the final live frame shows only the last five job rows, with the
    # active job at the bottom of the window.
    output = _last_rendered_frame(stream.getvalue())
    assert "[1/6] folder/job-1" not in output
    job_lines = [
        line.strip() for line in output.splitlines() if "] folder/job-" in line
    ]
    assert len(job_lines) == 5
    assert "[2/6] folder/job-2: Stored 1 builds" in job_lines[0]
    assert "[5/6] folder/job-5: Stored 1 builds" in job_lines[3]
    assert "[6/6] folder/job-6: 1 builds" in job_lines[4]


def test_rich_progress_renders_skipped_jobs_in_overall_numbering() -> None:
    # Given rich progress mode with one completed build job and one skipped job.
    stream = TtyStringIO()
    display = ProgressDisplay("auto", stream=stream)

    # When the skipped job is reported without a matching collection-start event.
    with display:
        display(CollectionProgress(CollectionPhase.JOBS_DISCOVERED, jobs_found=2))
        display(
            CollectionProgress(
                CollectionPhase.COLLECTING_JOB_BUILDS,
                job_count=2,
                job_index=1,
                job_full_name="folder/example",
            )
        )
        display(
            CollectionProgress(
                CollectionPhase.JOB_BUILDS_COLLECTED,
                job_count=2,
                job_index=1,
                job_full_name="folder/example",
                builds_collected=1,
            )
        )
        display(
            CollectionProgress(
                CollectionPhase.JOB_SKIPPED,
                job_count=2,
                job_index=2,
                job_full_name="legacy/freestyle",
                reason="unsupported class",
            )
        )

    # Then the rendered rows include the skipped job that advances the overall bar
    # and styles the skipped job less prominently than collected jobs.
    output = stream.getvalue()
    assert "[1/2] folder/example: Stored 1 builds" in output
    assert "[2/2] legacy/freestyle — Skipped: unsupported class" in output
    assert re.search(
        r"\x1b\[38;5;247m\[2/2] legacy/freestyle — Skipped: "
        r"unsupported class\s*\x1b\[0m",
        output,
    )
