"""Controllable synchronous polling for persisted local jobs.

The CLI or host process owns signal handling: a SIGINT/SIGTERM handler should
set the supplied ``threading.Event``.  This worker never installs global
signal handlers itself.
"""
from __future__ import annotations

from datetime import timedelta
from threading import Event

from .runner import JobRunner


class JobWorker:
    """Poll one runner until a caller-controlled stop event is set."""

    def __init__(self, runner: JobRunner, *, idle_interval: timedelta = timedelta(seconds=1)) -> None:
        if not isinstance(runner, JobRunner):
            raise TypeError("runner must be a JobRunner")
        if not isinstance(idle_interval, timedelta) or idle_interval <= timedelta(0):
            raise ValueError("idle_interval must be a positive timedelta")
        self._runner = runner
        self._idle_seconds = idle_interval.total_seconds()

    def run_forever(self, stop: Event) -> None:
        """Recover leases, process compatible work, and wait interruptibly idle.

        A stop received during a handler is deliberately observed only after
        that handler's ``run_once`` completes its heartbeat cleanup and final
        ownership-guarded transition.
        """
        if not isinstance(stop, Event):
            raise TypeError("stop must be a threading.Event")
        self._runner.recover_expired()
        while not stop.is_set():
            result = self._runner.run_once(allowed_types=self._runner.handler_types)
            if result is None:
                stop.wait(self._idle_seconds)
