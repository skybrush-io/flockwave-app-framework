"""Instrumentation classes for Trio applications that can be used to monitor and
log task performance, detect slow tasks, and more.
"""

from __future__ import annotations

from collections import Counter
from logging import getLogger, Logger
from time import monotonic_ns
from typing import TYPE_CHECKING

from trio import current_time
from trio.abc import Instrument

if TYPE_CHECKING:
    from trio.lowlevel import Task

log = getLogger(__name__)

__all__ = (
    "SlowTaskDetector",
    "TaskTracer",
    "TaskStatistics",
    "get_enabled_instruments",
)


class SlowTaskDetector(Instrument):
    """Detects slow tasks in Trio by monitoring the time taken for each task."""

    _log: Logger
    _start_time: float

    def __init__(self, threshold: float = 0.01):
        super().__init__()
        self.threshold = threshold
        self._start_time = 0
        self._log = log.getChild("slow_tasks")

    def before_run(self) -> None:
        self._log.info(
            f"Slow task detector started with a threshold of {self.threshold * 1000:.1f} ms"
        )

    def before_task_step(self, task: Task):
        """Called when a task starts."""
        self._start_time = current_time()

    def after_task_step(self, task: Task):
        """Called when a task ends."""
        elapsed_time = current_time() - self._start_time
        if elapsed_time > self.threshold:
            self._log.warning(
                f"{task.name} blocked the main loop for {elapsed_time * 1000:.1f} ms"
            )


class TaskStatistics(Instrument):
    """Collects statistics about the time spent in various tasks in Trio
    applications.
    """

    _log: Logger
    _durations: Counter[str]
    _started_at: int
    _task_start_time: int

    def __init__(self):
        super().__init__()
        self._durations = Counter()
        self._log = log.getChild("task_stats")

    def before_run(self) -> None:
        self._log.info("Task duration statistics collection started.")
        self._started_at = monotonic_ns()

    def after_run(self) -> None:
        elapsed_time = monotonic_ns() - self._started_at
        self._log.info(
            f"Task duration statistics collected over {elapsed_time / 1_000_000:.1f} ms"
        )
        max_task_length = (
            max(len(task) for task in self._durations.keys()) if self._durations else 0
        )

        for task, duration in self._durations.most_common():
            self._log.info(
                f"{task:>{max_task_length}}  {duration / 1_000_000:.1f} ms "
                f"({duration / elapsed_time * 100:.2f}%)"
            )

        name = "TOTAL"
        total_duration = self._durations.total()

        self._log.info(
            f"{name:>{max_task_length}}  {total_duration / 1_000_000:.1f} ms "
            f"({total_duration / elapsed_time * 100:.2f}%)"
        )

    def before_task_step(self, task: Task):
        """Called when a task starts."""
        self._task_start_time = monotonic_ns()

    def after_task_step(self, task: Task):
        """Called when a task ends."""
        self._durations[task.name] += monotonic_ns() - self._task_start_time


class TaskTracer(Instrument):
    """Logs task names in their order of execution."""

    _log: Logger
    _started_at: int
    _task_start_time: int
    _history: list[tuple[str, int, int]]

    filename: str
    """Filename to save the task trace log into."""

    def __init__(self, filename: str = "task_trace.log"):
        super().__init__()
        self._log = log.getChild("task_trace")
        self._history = []
        self.filename = filename

    def before_run(self) -> None:
        self._log.info("Task tracing started.")
        self._log.warning("Task tracing consumes a lot of resources. Use with caution.")
        self._started_at = monotonic_ns()

    def after_run(self) -> None:
        self._log.info(f"Task trace log saved to {self.filename}")
        self._log.info("Use https://speedscope.app/ to visualize the trace.")

        with open(self.filename, "w") as f:
            last = self._started_at
            for task_name, start, end in self._history:
                weight = start - last
                if weight > 0:
                    f.write(f"<idle> {weight}\n")

                weight = end - start + 1
                f.write(f"{task_name} {weight}\n")

                last = end + 1

    def before_task_step(self, task: Task):
        """Called when a task starts."""
        self._task_start_time = monotonic_ns()

    def after_task_step(self, task: Task):
        """Called when a task ends."""
        self._history.append((task.name, self._task_start_time, monotonic_ns()))


def get_enabled_instruments(*, env_var: str) -> list[Instrument]:
    """Returns a list of enabled instruments based on the contents of an
    environment variable.

    Args:
        env_var: The name of the environment variable that contains a comma-separated
            list of instrument names to enable.

    Returns:
        A list of enabled instruments.
    """
    from os import getenv

    result = []
    enabled_instruments = str(getenv(env_var, "")).lower().split(",")
    if "slow_tasks" in enabled_instruments:
        result.append(SlowTaskDetector(threshold=0.01))
    if "task_stats" in enabled_instruments:
        result.append(TaskStatistics())
    if "task_trace" in enabled_instruments:
        result.append(TaskTracer())

    return result
