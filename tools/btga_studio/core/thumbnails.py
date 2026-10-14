"""Bounded on-disk thumbnail cache and an asynchronous, cancellable thumbnail service.

Cache key: sha256 of (resource content ID, renderer version, camera preset,
width, height).  An entry is ``BTTH`` + u32 format version + u16 width +
u16 height + sha256(payload) + RGBA payload, little-endian and explicitly
sized.  A corrupt or truncated entry is deleted and regenerated.  The cache
evicts least-recently-used entries beyond ``max_bytes`` / ``max_entries``.

The service never blocks: ``request`` returns cached pixels or None (the
caller shows a placeholder) and queues the work; ``pump`` (called once per
frame) keeps at most ``workers`` renders in flight, highest priority first,
and stores finished results.  ``cancel_older_than(generation)`` drops queued
work for an abandoned filter or page.
"""

from __future__ import annotations

import hashlib
import heapq
import os
import struct
import threading
from collections import OrderedDict
from concurrent.futures import Executor, ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

RENDERER_VERSION = "preview-rasteriser-1"
CAMERA_PRESET = "three-quarter-v1"
MAGIC = b"BTTH"
FORMAT_VERSION = 1
HEADER = struct.Struct("<4sIHH32s")


def cache_key(content_id: str, width: int, height: int, renderer: str = RENDERER_VERSION, preset: str = CAMERA_PRESET) -> str:
    text = "\x1f".join((content_id, renderer, preset, str(int(width)), str(int(height))))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ThumbnailCache:
    def __init__(self, directory: Path, max_bytes: int = 64 * 1024 * 1024, max_entries: int = 8192):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, int] = OrderedDict()
        self.bytes = 0
        self.evictions = 0
        self.corrupt = 0
        found = []
        for path in self.directory.glob("*/*.btth"):
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append((stat.st_mtime_ns, path.stem, stat.st_size))
        for _, key, size in sorted(found):
            self._entries[key] = size
            self.bytes += size
        self._evict()

    def path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.btth"

    def get(self, key: str) -> tuple[int, int, bytes] | None:
        with self._lock:
            if key not in self._entries:
                return None
        path = self.path(key)
        try:
            data = path.read_bytes()
            magic, version, width, height, digest = HEADER.unpack_from(data, 0)
            payload = data[HEADER.size :]
            if magic != MAGIC or version != FORMAT_VERSION or len(payload) != width * height * 4 or hashlib.sha256(payload).digest() != digest:
                raise ValueError("corrupt thumbnail")
        except (OSError, struct.error, ValueError):
            self.corrupt += 1
            self._remove(key)
            return None
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
        return width, height, payload

    def put(self, key: str, width: int, height: int, rgba: bytes) -> None:
        if len(rgba) != width * height * 4:
            raise ValueError("thumbnail size mismatch")
        data = HEADER.pack(MAGIC, FORMAT_VERSION, width, height, hashlib.sha256(rgba).digest()) + rgba
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)
        with self._lock:
            self.bytes -= self._entries.pop(key, 0)
            self._entries[key] = len(data)
            self.bytes += len(data)
        self._evict()

    def _remove(self, key: str) -> None:
        with self._lock:
            self.bytes -= self._entries.pop(key, 0)
        try:
            self.path(key).unlink()
        except OSError:
            pass

    def _evict(self) -> None:
        while True:
            with self._lock:
                if not self._entries or (self.bytes <= self.max_bytes and len(self._entries) <= self.max_entries):
                    return
                key, size = self._entries.popitem(last=False)
                self.bytes -= size
                self.evictions += 1
            try:
                self.path(key).unlink()
            except OSError:
                pass

    def __len__(self) -> int:
        return len(self._entries)


# ----------------------------------------------------------------- worker --

_WORKER_LIBRARY = {}


def render_thumbnail(catalog_root: str, model_id: str, size: int) -> tuple[int, int, bytes]:
    """Runs in a worker process: decode one model and rasterise it."""

    from tools.btga_assets.studio import StudioLibrary

    library = _WORKER_LIBRARY.get(catalog_root)
    if library is None:
        library = _WORKER_LIBRARY[catalog_root] = StudioLibrary.open(Path(catalog_root))
    return size, size, library.thumbnail(model_id, size)


@dataclass(order=True)
class _Job:
    priority: int
    sequence: int
    key: str = field(compare=False)
    model_id: str = field(compare=False)
    size: int = field(compare=False)
    generation: int = field(compare=False)


class ThumbnailService:
    def __init__(self, catalog_root, cache: ThumbnailCache, workers: int = 2, executor: Executor | None = None,
                 memory_entries: int = 512, render=render_thumbnail):
        """``catalog_root`` is a catalog directory or a callable mapping a model ID to its catalog directory."""

        self.root_for = catalog_root if callable(catalog_root) else (lambda model_id, root=str(catalog_root): root)
        self.catalog_root = None if callable(catalog_root) else str(catalog_root)
        self.cache = cache
        self.workers = max(1, workers)
        self._executor = executor
        self._render = render
        self._queue: list[_Job] = []
        self._queued: dict[str, _Job] = {}
        self._in_flight: dict[str, object] = {}
        self._memory: OrderedDict[str, tuple[int, int, bytes]] = OrderedDict()
        self.memory_entries = memory_entries
        self.generation = 0
        self._sequence = 0
        self.rendered = 0
        self.cancelled = 0
        self.failed: dict[str, str] = {}

    @property
    def executor(self) -> Executor:
        if self._executor is None:
            self._executor = ProcessPoolExecutor(max_workers=self.workers)
        return self._executor

    def request(self, model_id: str, size: int = 96, priority: int = 100) -> tuple[int, int, bytes] | None:
        """Cached RGBA or None; queues a render when missing.  Lower priority values run first."""

        key = cache_key(model_id, size, size)
        if key in self._memory:
            self._memory.move_to_end(key)
            return self._memory[key]
        found = self.cache.get(key)
        if found is not None:
            self._remember(key, found)
            return found
        if key in self.failed or key in self._in_flight:
            return None
        queued = self._queued.get(key)
        if queued is not None and queued.generation == self.generation and queued.priority <= priority:
            return None
        self._sequence += 1
        job = _Job(priority, self._sequence, key, model_id, size, self.generation)
        self._queued[key] = job
        heapq.heappush(self._queue, job)
        return None

    def cancel_older_than(self, generation: int | None = None) -> int:
        """Drop queued (not started) work from earlier generations; returns how many were dropped."""

        self.generation = self.generation + 1 if generation is None else generation
        kept = [job for job in self._queue if job.generation >= self.generation]
        dropped = len(self._queue) - len(kept)
        self._queue = kept
        heapq.heapify(self._queue)
        self._queued = {job.key: job for job in kept}
        self.cancelled += dropped
        return dropped

    def pump(self) -> int:
        """Collect finished renders and start queued ones; returns renders completed this call."""

        done = 0
        for key, future in list(self._in_flight.items()):
            if not future.done():
                continue
            del self._in_flight[key]
            try:
                width, height, rgba = future.result()
            except Exception as error:  # a broken model must not stop the browser
                self.failed[key] = f"{type(error).__name__}: {error}"
                continue
            self.cache.put(key, width, height, rgba)
            self._remember(key, (width, height, rgba))
            self.rendered += 1
            done += 1
        while self._queue and len(self._in_flight) < self.workers:
            job = heapq.heappop(self._queue)
            if self._queued.get(job.key) is not job:
                continue
            del self._queued[job.key]
            if job.generation < self.generation:
                self.cancelled += 1
                continue
            self._in_flight[job.key] = self.executor.submit(self._render, str(self.root_for(job.model_id)), job.model_id, job.size)
        return done

    @property
    def pending(self) -> int:
        return len(self._queue) + len(self._in_flight)

    def _remember(self, key: str, value) -> None:
        self._memory[key] = value
        self._memory.move_to_end(key)
        while len(self._memory) > self.memory_entries:
            self._memory.popitem(last=False)

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
