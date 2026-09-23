"""Terminal and plain-text rendering for collection progress."""

from __future__ import annotations

import sys
from collections import deque
from typing import TYPE_CHECKING, Final, Self, TextIO

from rich.console import Console, RenderableType
from rich.progress import (
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskID,
    TimeElapsedColumn,
)
from rich.text import Text

from jenkins_stats.collection import CollectionPhase

if TYPE_CHECKING:
    from jenkins_stats.collection import CollectionProgress


_MAX_RICH_JOB_TASKS: Final = 5
_DESCRIPTION_STYLE_FIELD: Final = "description_style"
_SHOW_SPINNER_FIELD: Final = "show_spinner"
_SKIPPED_JOB_STYLE: Final = "grey62"


class _OptionalSpinnerColumn(ProgressColumn):
    """Render a spinner for tasks that have not opted out."""

    def __init__(self, spinner_name: str) -> None:
        """Configure the delegated Rich spinner column."""
        super().__init__()
        self._spinner_column = SpinnerColumn(spinner_name)

    def render(self, task: Task) -> RenderableType:
        """Return the spinner renderable, or a blank cell for quiet tasks."""
        if task.fields.get(_SHOW_SPINNER_FIELD, True):
            return self._spinner_column.render(task)
        return Text("")


class _TaskDescriptionColumn(ProgressColumn):
    """Render each task description, honoring per-task styles."""

    def render(self, task: Task) -> RenderableType:
        """Return the task description with any requested Rich style."""
        description_style = task.fields.get(_DESCRIPTION_STYLE_FIELD)
        if isinstance(description_style, str) and description_style:
            return Text(task.description, style=description_style)
        return Text(task.description)


class ProgressDisplay:
    """Render collection progress to stderr using Rich or plain status lines."""

    def __init__(self, mode: str, *, stream: TextIO | None = None) -> None:
        """Select Rich, plain, or disabled progress rendering."""
        if mode not in {"auto", "plain", "none"}:
            raise ValueError(f"Unknown progress mode: {mode}")
        self._plain = mode == "plain"
        self._stream = sys.stderr if stream is None else stream
        self._enabled = mode == "plain" or (mode == "auto" and self._stream.isatty())
        self._progress = Progress(
            TimeElapsedColumn(),
            _OptionalSpinnerColumn("dots"),
            _TaskDescriptionColumn(),
            console=Console(file=self._stream),
            transient=False,
            disable=not self._enabled or self._plain,
        )
        self._discovery_task: TaskID | None = None
        self._overall_task: TaskID | None = None
        self._overall_total = 0
        self._overall_completed = 0
        self._job_task: TaskID | None = None
        self._job_tasks: deque[TaskID] = deque()
        self._current_job_label = ""
        self._active = False

    def __enter__(self) -> Self:
        """Start the Rich live display when appropriate."""
        if self._enabled and not self._plain:
            self._progress.start()
            self._active = True
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop the Rich display cleanly, including after command failures."""
        if self._active:
            self._progress.stop()
            self._active = False

    def __call__(self, event: CollectionProgress) -> None:
        """Render one collection progress event."""
        if not self._enabled:
            return
        if self._plain:
            self._render_plain(event)
        else:
            self._render_rich(event)

    def _render_plain(self, event: CollectionProgress) -> None:
        if event.phase is CollectionPhase.DISCOVERING_JOBS:
            self._write("Discovering Jenkins jobs...")
        elif event.phase is CollectionPhase.JOBS_DISCOVERED:
            self._write(f"Discovered {event.jobs_found} jobs.")
        elif event.phase is CollectionPhase.COLLECTING_JOB_BUILDS:
            self._write(
                f"[{event.job_index}/{event.job_count}] Collecting builds for "
                f"{event.job_full_name}..."
            )
        elif event.phase is CollectionPhase.BUILD_COLLECTED:
            self._write(
                f"  {event.builds_collected} builds collected for {event.job_full_name}"
            )
        elif event.phase is CollectionPhase.JOB_BUILDS_COLLECTED:
            self._write(
                f"[{event.job_index}/{event.job_count}] Stored "
                f"{event.builds_collected} builds for {event.job_full_name}."
            )
        elif event.phase is CollectionPhase.JOB_SKIPPED:
            self._write(
                f"[{event.job_index}/{event.job_count}] Skipping "
                f"{event.job_full_name}: {event.reason}"
            )

    def _render_rich(self, event: CollectionProgress) -> None:
        if event.phase is CollectionPhase.DISCOVERING_JOBS:
            self._discovery_task = self._progress.add_task(
                "Discovering jobs", total=None
            )
        elif event.phase is CollectionPhase.JOB_FOUND:
            self._render_rich_job_found(event)
        elif event.phase is CollectionPhase.JOBS_DISCOVERED:
            self._render_rich_jobs_discovered(event)
        elif event.phase is CollectionPhase.COLLECTING_JOB_BUILDS:
            self._start_rich_job(event)
        elif event.phase is CollectionPhase.BUILD_COLLECTED:
            self._render_rich_build_collected(event)
        elif event.phase is CollectionPhase.JOB_BUILDS_COLLECTED:
            self._finish_rich_job(event)
        elif event.phase is CollectionPhase.JOB_SKIPPED:
            self._render_rich_skipped_job(event)

    def _render_rich_job_found(self, event: CollectionProgress) -> None:
        if self._discovery_task is not None:
            self._progress.update(
                self._discovery_task,
                description=f"Discovering jobs ({event.jobs_found} found)",
            )

    def _render_rich_jobs_discovered(self, event: CollectionProgress) -> None:
        if self._discovery_task is not None:
            self._progress.update(
                self._discovery_task,
                description=f"Discovered {event.jobs_found} jobs",
                total=1,
                completed=1,
            )
        self._ensure_overall_task(event.jobs_found)

    def _start_rich_job(self, event: CollectionProgress) -> None:
        self._ensure_overall_task(event.job_count or 1)
        self._current_job_label = self._event_job_label(event)
        self._job_task = self._add_rich_job_task(
            self._current_job_label,
            total=None,
        )

    def _render_rich_build_collected(self, event: CollectionProgress) -> None:
        if self._job_task is not None:
            self._progress.update(
                self._job_task,
                description=(
                    f"{self._current_job_label}: {event.builds_collected} builds"
                ),
                completed=event.builds_collected,
                refresh=True,
            )

    def _finish_rich_job(self, event: CollectionProgress) -> None:
        if self._job_task is not None:
            self._progress.update(
                self._job_task,
                description=(
                    f"{self._current_job_label or event.job_full_name}: "
                    f"Stored {event.builds_collected} builds"
                ),
                total=event.builds_collected,
                completed=event.builds_collected,
            )
            self._job_task = None
        self._advance_overall_task()

    def _render_rich_skipped_job(self, event: CollectionProgress) -> None:
        self._ensure_overall_task(event.job_count or 1)
        if self._job_task is not None:
            self._remove_rich_job_task(self._job_task)
            self._job_task = None
        skipped_description = f"{self._event_job_label(event)} — Skipped"
        if event.reason:
            skipped_description = f"{skipped_description}: {event.reason}"
        self._add_rich_job_task(
            skipped_description,
            total=1,
            completed=1,
            description_style=_SKIPPED_JOB_STYLE,
            show_spinner=False,
        )
        self._advance_overall_task()

    def _add_rich_job_task(
        self,
        description: str,
        *,
        total: float | None,
        completed: int = 0,
        description_style: str | None = None,
        show_spinner: bool = True,
    ) -> TaskID:
        task = self._progress.add_task(
            description,
            total=total,
            completed=completed,
            description_style=description_style,
            show_spinner=show_spinner,
        )
        self._job_tasks.append(task)
        self._trim_rich_job_tasks()
        return task

    def _remove_rich_job_task(self, task: TaskID) -> None:
        self._progress.remove_task(task)
        self._job_tasks.remove(task)

    def _trim_rich_job_tasks(self) -> None:
        while len(self._job_tasks) > _MAX_RICH_JOB_TASKS:
            task = self._job_tasks.popleft()
            if self._job_task == task:
                self._job_task = None
            self._progress.remove_task(task)

    def _event_job_label(self, event: CollectionProgress) -> str:
        if event.job_index is not None and event.job_count is not None:
            return f"[{event.job_index}/{event.job_count}] {event.job_full_name}"
        return str(event.job_full_name)

    def _advance_overall_task(self) -> None:
        if self._overall_task is not None:
            self._overall_completed += 1
            self._progress.update(
                self._overall_task,
                advance=1,
                description=self._overall_description(),
            )

    def _ensure_overall_task(self, total: int) -> None:
        if self._overall_task is None:
            self._overall_total = total
            self._overall_completed = 0
            self._overall_task = self._progress.add_task(
                self._overall_description(), total=total, show_spinner=False
            )

    def _overall_description(self) -> str:
        return "Collecting builds"

    def _write(self, line: str) -> None:
        self._stream.write(f"{line}\n")
        self._stream.flush()
