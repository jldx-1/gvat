"""Native frame indexing and interruptible, lossless FFmpeg batch decoding."""

from __future__ import annotations

import bisect
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .media import ffmpeg_input
from .workspace import WorkspaceError


class DecodeCancelled(Exception):
    """A newer scrub position replaced this request."""


class DecodeTask:
    def __init__(self):
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.process = None

    def check(self):
        if self.cancelled.is_set():
            raise DecodeCancelled()

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass

    def run(self, command, log, timeout=120):
        self.check()
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log)
        with self.lock:
            self.process = process
        started = time.monotonic()
        try:
            while True:
                self.check()
                try:
                    result = process.wait(timeout=.05)
                    self.check()
                    if result:
                        log.seek(0)
                        raise WorkspaceError("FFmpeg could not decode media: " + log.read().decode(errors="replace")[-2000:])
                    return
                except subprocess.TimeoutExpired:
                    if timeout is not None and time.monotonic() - started > timeout:
                        raise WorkspaceError("Video decoding timed out")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            with self.lock:
                self.process = None


def index_video(path: Path) -> dict:
    """Record presentation order and integer timestamps in the counting pass."""
    command = ffmpeg_input(path) + ["-v", "info", "-copyts", "-map", "0:v:0", "-an", "-sn",
                                      "-filter_threads", "1", "-vf", "showinfo", "-vsync", "0", "-f", "null", "-"]
    points, keyframes, time_base = [], [], None
    with tempfile.TemporaryFile() as log:
        DecodeTask().run(command, log, timeout=None)
        log.seek(0)
        for raw in log:
            line = raw.decode(errors="replace")
            base = re.search(r"config in time_base:\s*(\d+)/(\d+)", line)
            if base:
                time_base = [int(base[1]), int(base[2])]
            frame = re.search(r"\bn:\s*(\d+)\s+pts:\s*(\S+).*?\biskey:\s*([01])", line)
            if frame:
                points.append(int(frame[2]) if re.fullmatch(r"-?\d+", frame[2]) else None)
                if frame[3] == "1":
                    keyframes.append(len(points) - 1)
    if not points:
        raise WorkspaceError(f"Source video has no readable frames: {path.name}")
    return {"version": 3, "frames": len(points), "pts": points, "keyframes": keyframes, "time_base": time_base}


def valid_index(value):
    return (isinstance(value, dict) and value.get("version") == 3 and type(value.get("frames")) is int
            and value["frames"] > 0 and isinstance(value.get("pts"), list) and len(value["pts"]) == value["frames"]
            and all(point is None or type(point) is int for point in value["pts"])
            and isinstance(value.get("keyframes"), list)
            and all(type(frame) is int and 0 <= frame < value["frames"] for frame in value["keyframes"]))


def decode_batch(source, numbers: list[int], index: dict, directory: Path, task: DecodeTask):
    """Seek to an indexed keyframe and select exact original presentation frames."""
    points, base = index["pts"], index.get("time_base")
    if "seekable" not in index:
        index["seekable"] = (isinstance(base, list) and len(base) == 2 and all(type(value) is int and value > 0 for value in base)
                             and all(point is not None for point in points)
                             and all(a < b for a, b in zip(points, points[1:])))
    seekable = index["seekable"]
    command = ffmpeg_input(source.path)
    if seekable:
        keys = sorted(index["keyframes"])
        anchor = keys[max(0, bisect.bisect_right(keys, numbers[0]) - 1)] if keys else 0
        if anchor:
            # Start just before the keyframe to avoid decimal seek rounding.
            timestamp = points[anchor] * base[0] / base[1] - .001
            position = command.index("-i")
            command[position:position] = ["-seek_timestamp", "1", "-ss", format(timestamp, ".9f"), "-noaccurate_seek"]
        command += ["-copyts"]
        selected = "+".join(f"eq(pts\\,{points[number]})" for number in numbers)
    else:
        # Missing/duplicate timestamps need presentation-order selection. Still
        # decode the whole batch once; never estimate frame numbers from FPS.
        selected = "+".join(f"eq(n\\,{number})" for number in numbers)
    command += ["-map", "0:v:0", "-an", "-sn", "-filter_threads", "1", "-vf", f"select={selected}",
                "-vsync", "0", "-frames:v", str(len(numbers)), "-vcodec", "png", "-threads", "1",
                "-start_number", "0", str(directory / "%08d.png")]
    with tempfile.TemporaryFile() as log:
        task.run(command, log)
    files = sorted(directory.glob("*.png"))
    if len(files) != len(numbers):
        raise WorkspaceError(f"Video batch returned {len(files)} frames; expected {len(numbers)}")
    return files
