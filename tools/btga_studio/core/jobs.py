"""Background jobs with progress, cancellation, bounded queueing and retry.

The editor runs long operations (source identification, verification and
search, catalog builds, package export, project loading) as ``Job``s so the
UI thread never blocks.  A job's work function receives a ``JobContext``:

    def work(context):
        for i, item in enumerate(items):
            context.check()                 # raises Cancelled when cancellation was requested
            context.progress("hashing", i, len(items))
        return result

``JobRunner.submit`` refuses new work when ``max_pending`` jobs are already
queued or running (``JobQueueFull``), so the UI cannot pile up unbounded
work.  ``poll()`` (called once per frame on the UI thread) returns jobs
that finished since the last call; ``on_done`` callbacks run there, on the
UI thread, never on a worker.  A failed or cancelled job keeps its
arguments, so ``retry`` resubmits it unchanged.

Thread safety: a job's progress fields are written by its worker and read
by the UI; each field is a single reference assignment.  Ownership: the
runner owns its worker threads; ``shutdown`` cancels queued and running
work.  Platform independent.
"""

from __future__ import annotations

import itertools
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable


class Cancelled(Exception):
    """Raised inside a job when the user cancelled it."""


class JobQueueFull(RuntimeError):
    pass


STATES = ("queued", "running", "done", "failed", "cancelled")


@dataclass
class Job:
    id: int
    label: str
    kind: str
    work: Callable
    on_done: Callable | None = None
    state: str = "queued"
    stage: str = ""
    done: int = 0
    total: int | None = None
    error: str | None = None
    detail: str | None = None  # traceback for logs; never shown as the primary message
    result: object = None
    started: float | None = None
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return max(0.0, min(1.0, self.done / self.total))

    @property
    def finished(self) -> bool:
        return self.state in ("done", "failed", "cancelled")

    def cancel(self) -> None:
        self.cancel_event.set()


class JobContext:
    def __init__(self, job: Job):
        self._job = job

    @property
    def cancelled(self) -> bool:
        return self._job.cancel_event.is_set()

    @property
    def cancel_event(self) -> threading.Event:
        return self._job.cancel_event

    def check(self) -> None:
        if self._job.cancel_event.is_set():
            raise Cancelled()

    def progress(self, stage: str, done: int = 0, total: int | None = None) -> None:
        self._job.stage, self._job.done, self._job.total = stage, int(done), (int(total) if total else None)
        self.check()


class JobRunner:
    def __init__(self, workers: int = 2, max_pending: int = 16, log: Callable | None = None):
        self._executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="studio-job")
        self.max_pending = max_pending
        self.jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._finished: list[Job] = []
        self._lock = threading.Lock()
        self._log = log

    @property
    def active(self) -> list[Job]:
        return [job for job in self.jobs.values() if not job.finished]

    def find(self, kind: str) -> list[Job]:
        return [job for job in self.active if job.kind == kind]

    def submit(self, label: str, work: Callable, kind: str = "task", on_done: Callable | None = None) -> Job:
        if len(self.active) >= self.max_pending:
            raise JobQueueFull(f"{len(self.active)} background tasks are already waiting; let them finish first")
        job = Job(next(self._ids), label, kind, work, on_done)
        self.jobs[job.id] = job
        self._executor.submit(self._run, job)
        return job

    def retry(self, job: Job) -> Job:
        return self.submit(job.label, job.work, job.kind, job.on_done)

    def _run(self, job: Job) -> None:
        if job.cancel_event.is_set():
            job.state = "cancelled"
        else:
            job.state, job.started = "running", time.monotonic()
            try:
                job.result = job.work(JobContext(job))
                job.state = "done"
            except Cancelled:
                job.state = "cancelled"
            except Exception as error:  # reported to the user by the UI thread
                job.state, job.error = "failed", _plain(error)
                job.detail = traceback.format_exc()
        job.finished_at = time.monotonic()
        with self._lock:
            self._finished.append(job)

    def poll(self) -> list[Job]:
        """Jobs finished since the last call; runs their ``on_done`` callbacks on the calling (UI) thread."""

        with self._lock:
            finished, self._finished = self._finished, []
        for job in finished:
            if self._log is not None:
                self._log(job)
            if job.on_done is not None:
                job.on_done(job)
        for job in [j for j in self.jobs.values() if j.finished and j not in finished and time.monotonic() - (j.finished_at or 0) > 600]:
            del self.jobs[job.id]  # keep finished jobs visible for a while, but bounded
        return finished

    def wait(self, timeout: float = 60.0) -> list[Job]:
        """Block until no job is active (tests and shutdown only), then poll."""

        deadline = time.monotonic() + timeout
        while self.active and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.poll()

    def shutdown(self) -> None:
        for job in self.active:
            job.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)


def _plain(error: Exception) -> str:
    text = str(error) or type(error).__name__
    return text if isinstance(error, (ValueError, OSError, RuntimeError)) else f"{type(error).__name__}: {text}"
