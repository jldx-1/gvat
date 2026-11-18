"""CVAT image chunks with bounded transfer caching and cancellable decoding."""

from __future__ import annotations

import hashlib
import io
import tempfile
import threading
import zipfile
from collections import OrderedDict
from concurrent.futures import CancelledError, ThreadPoolExecutor, TimeoutError
from itertools import groupby
from pathlib import Path

from PIL import Image

from .video_decode import DecodeCancelled, DecodeTask, decode_batch
from .workspace import WorkspaceError, atomic_write


class MediaChunks:
    def __init__(self, workspace):
        self.workspace = workspace
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gvat-frames")
        self.intent = {}
        self.horizons = {}
        self.tasks = {}
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.closed = False

    def activate(self, view, chunk, generation, lookahead=5):
        if type(chunk) is not int or chunk < 0 or type(generation) is not int or generation < 0:
            raise WorkspaceError("Invalid chunk navigation request")
        with self.lock:
            previous = self.intent.get(view, (-1, -1))
            if generation < previous[1] or (generation == previous[1] and chunk < previous[0]):
                raise DecodeCancelled()
            continuous = generation == previous[1] and chunk == previous[0] + 1
            self.intent[view] = (chunk, generation)
            self.horizons[view] = lookahead
            for (owner, number), (task, future) in list(self.tasks.items()):
                keep = chunk <= number <= chunk + lookahead if continuous else number == chunk
                if owner == view and not keep:
                    task.cancel()
                    future.cancel()

    def release(self, view):
        with self.lock:
            self.intent.pop(view, None)
            self.horizons.pop(view, None)
            for (owner, _), (task, future) in list(self.tasks.items()):
                if owner == view:
                    task.cancel()
                    future.cancel()

    def read(self, view, keys, chunk_size, chunk, generation, prefetch=False, disconnected=lambda: False):
        if type(chunk) is not int or chunk < 0 or chunk * chunk_size >= len(keys):
            raise WorkspaceError("Chunk is outside the annotation folder")
        with self.lock:
            if self.closed:
                raise DecodeCancelled()
            desired, current = self.intent.get(view, (-1, -1))
            if generation != current or (chunk != desired and (not prefetch or not desired < chunk <= desired + self.horizons.get(view, 5))):
                raise DecodeCancelled()
            selected = tuple(keys[chunk * chunk_size:(chunk + 1) * chunk_size])
            if selected in self.cache:
                self.cache.move_to_end(selected)
                cached = self.cache[selected]
            else:
                cached = None
        if cached is not None:
            with self.workspace.lock:
                self.workspace._check_sources()
            return cached
        with self.lock:
            if self.closed or self.intent.get(view) != (desired, generation):
                raise DecodeCancelled()
            if not prefetch:
                for (owner, number), (old_task, old_future) in list(self.tasks.items()):
                    if owner == view and number != chunk:
                        old_task.cancel()
                        old_future.cancel()
            identity = (view, chunk)
            existing = self.tasks.get(identity)
            if existing is None or existing[0].cancelled.is_set():
                task = DecodeTask()
                future = self.executor.submit(self._build, selected, task)
                self.tasks[identity] = (task, future)
                future.add_done_callback(lambda completed: self._forget(identity, task, completed))
            else:
                task, future = existing
        try:
            while True:
                task.check()
                if disconnected():
                    task.cancel()
                    raise DecodeCancelled()
                try:
                    return future.result(timeout=.05)
                except TimeoutError:
                    if future.done():
                        raise
                except CancelledError:
                    raise DecodeCancelled() from None
        finally:
            with self.lock:
                if future.done() and self.tasks.get(identity) == (task, future):
                    self.tasks.pop(identity, None)

    def _forget(self, identity, task, future):
        with self.lock:
            if self.tasks.get(identity) == (task, future):
                self.tasks.pop(identity, None)

    def _build(self, keys, task):
        workspace = self.workspace
        with workspace.lock:
            workspace._check_sources()
        # Expensive decoding never holds the XML/workspace lock. A newer seek
        # can terminate FFmpeg immediately, and annotation saves remain responsive.
        paths = {key: workspace.data / f"{hashlib.sha256(key.encode()).hexdigest()}.png" for key in keys}
        for source, grouped in groupby(keys, lambda key: workspace._frames[key][0]):
            group = list(grouped)
            task.check()
            if all(paths[key].is_file() for key in group):
                continue
            if source.kind == "image":
                for key in group:
                    task.check()
                    with workspace.lock:
                        workspace._ensure_frame(key)
            else:
                numbers = [workspace._frames[key][1] for key in group]
                with tempfile.TemporaryDirectory(prefix=".decode-", dir=workspace.data) as directory:
                    decoded = decode_batch(source, numbers, workspace.video_indexes[source.path], Path(directory), task)
                    with workspace.lock:
                        workspace._check_sources()
                        for key, file in zip(group, decoded):
                            task.check()
                            with Image.open(file) as image:
                                image.verify()
                            if not paths[key].is_file():
                                atomic_write(paths[key], file.read_bytes(), "missing")
        task.check()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
            for number, key in enumerate(keys):
                task.check()
                archive.write(paths[key], f"{number:08d}.png")
        result = output.getvalue()
        with workspace.lock:
            workspace._check_sources()
        with self.lock:
            task.check()
            if len(result) <= 64 * 1024 * 1024:
                previous = self.cache.pop(keys, None)
                if previous is not None:
                    self.cache_bytes -= len(previous)
                self.cache[keys] = result
                self.cache_bytes += len(result)
                while self.cache_bytes > 64 * 1024 * 1024 or len(self.cache) > 8:
                    _, removed = self.cache.popitem(last=False)
                    self.cache_bytes -= len(removed)
        return result

    def close(self):
        with self.lock:
            self.closed = True
            for task, future in list(self.tasks.values()):
                task.cancel()
                future.cancel()
            self.cache.clear()
            self.cache_bytes = 0
        self.executor.shutdown(wait=True, cancel_futures=True)
