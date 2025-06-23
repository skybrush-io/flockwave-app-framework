"""Instrumentation classes for Trio applications that can be used to monitor and
log task performance, detect slow tasks, and more.
"""

from __future__ import annotations

from logging import getLogger, Logger
from typing import TYPE_CHECKING

from trio import current_time
from trio.abc import Instrument

if TYPE_CHECKING:
    from trio.lowlevel import Task

log = getLogger(__name__)

__all__ = ("SlowTaskDetector",)


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
