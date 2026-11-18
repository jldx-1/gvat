"""Pillow and FFmpeg media exposed as stable frames with local YOLO annotations."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, UnidentifiedImageError

from .workspace import IMAGE_EXTENSIONS, Conflict, Workspace, WorkspaceError, atomic_write, contained, digest, load_classes, read_optional

# These extensions only improve errors for broken media. They never limit discovery:
# every candidate is identified by Pillow/FFmpeg, including extensionless files.
VIDEO_HINTS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v", ".mpeg", ".mpg", ".ts", ".mts", ".m2ts", ".wmv", ".asf", ".flv", ".mxf", ".ogv", ".vob", ".h264", ".hevc"}
LOCAL_PROTOCOLS = "file,pipe,crypto,data"


@dataclass(frozen=True)
class MediaCandidate:
    path: Path
    kind: str
    frames: int
    fingerprint: tuple[int, int]


@dataclass(frozen=True)
class MediaSource:
    path: Path
    name: str
    kind: str
    revision: str
    fingerprint: tuple[int, int]
    frames: int


def fingerprint(path: Path) -> tuple[int, int]:
    metadata = path.stat()
    return metadata.st_size, metadata.st_mtime_ns


def file_digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def source_files(root: Path, excluded: tuple[Path, ...] = (), recursive: bool = True) -> list[Path]:
    files = []
    excluded = tuple(path.resolve() for path in excluded)
    for directory, children, names in os.walk(root, followlinks=False):
        directory = Path(directory)
        children[:] = [name for name in children if not name.startswith(".") and (directory / name).resolve() not in excluded]
        if not recursive:
            children.clear()
        for name in names:
            path = directory / name
            if not name.startswith(".") and path.is_file() and path.resolve().is_relative_to(root):
                files.append(path)
    return sorted(files)


def ffmpeg_input(path: Path) -> list[str]:
    return [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-v", "error", "-threads", "1", "-protocol_whitelist", LOCAL_PROTOCOLS, "-i", str(path)]


def probe_media(path: Path) -> MediaCandidate | None:
    before = fingerprint(path)
    image_error = None
    try:
        with Image.open(path) as image:
            count = getattr(image, "n_frames", 1)
            image.load()  # Identification alone does not prove the decoder is available.
            if count <= 0:
                raise OSError("Image contains no readable frames")
            if fingerprint(path) != before:
                raise Conflict(f"Source media changed while opening: {path.name}. Run GVAT again.")
            return MediaCandidate(path, "image", count, before)
    except Image.DecompressionBombError as error:
        raise WorkspaceError(f"Cannot read source media {path.name}: {error}") from error
    except UnidentifiedImageError:
        pass
    except (OSError, ValueError, RuntimeError) as error:
        image_error = error

    video_error = None
    try:
        command = ffmpeg_input(path) + ["-map", "0:v:0", "-an", "-sn", "-frames:v", "1", "-filter_threads", "1", "-f", "null", "-"]
        probe = subprocess.run(command, capture_output=True, timeout=30)
        if probe.returncode == 0:
            if fingerprint(path) != before:
                raise Conflict(f"Source media changed while opening: {path.name}. Run GVAT again.")
            return MediaCandidate(path, "video", 0, before)
        video_error = probe.stderr.decode(errors="replace").strip().splitlines()[-1:]
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        video_error = [str(error)]
    if image_error or path.suffix.lower() in IMAGE_EXTENSIONS | VIDEO_HINTS:
        detail = str(image_error) if image_error else " ".join(video_error or ["No readable video stream"])
        raise WorkspaceError(f"Cannot read source media {path.name}: {detail}")
    return None


def discover_media(root: Path, excluded: tuple[Path, ...] = (), recursive: bool = True) -> list[MediaCandidate]:
    candidates = [candidate for path in source_files(root, excluded, recursive) if (candidate := probe_media(path)) is not None]
    if not candidates:
        raise WorkspaceError(f"No supported images or videos found in {root}. Add media readable by Pillow or FFmpeg. GVAT will not start.")
    return candidates


def count_video_frames(path: Path) -> int:
    # Count decoded native frames, explicitly disabling frame-rate duplication.
    command = ffmpeg_input(path) + ["-map", "0:v:0", "-an", "-sn", "-filter_threads", "1", "-vsync", "0", "-progress", "pipe:1", "-nostats", "-f", "null", "-"]
    result = subprocess.run(command, capture_output=True, check=True)
    values = [int(line.split("=", 1)[1]) for line in result.stdout.decode().splitlines() if line.startswith("frame=")]
    if not values or values[-1] <= 0:
        raise WorkspaceError(f"Source video has no readable frames: {path.name}")
    return values[-1]


def image_preview(path: Path, number: int) -> bytes:
    with Image.open(path) as image:
        image.seek(number)
        if image.mode in {"I", "F"} or image.mode.startswith("I;16"):
            # Preserve useful contrast when viewing integer/float scientific images.
            low, high = image.getextrema()
            if high > low:
                image = image.convert("F").point(lambda value: (value - low) * 255 / (high - low)).convert("L")
        preview = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
        preview.info.clear()  # Keep source pixel geometry; browsers must not rotate EXIF again.
        output = io.BytesIO()
        preview.save(output, format="PNG")
        return output.getvalue()


class MediaWorkspace(Workspace):
    def __init__(self, data: Path, labels: Path, classes: Path, mode: str = "annotate", candidates: list[MediaCandidate] | None = None, recursive: bool = True):
        self.source_root = data.resolve(strict=True)
        self.recursive = recursive
        candidates = candidates if candidates is not None else discover_media(self.source_root, (labels,), recursive)
        cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser().resolve() / "gvat"
        self.sources: list[MediaSource] = []
        self.video_indexes = {}
        for candidate in candidates:
            path = candidate.path
            if fingerprint(path) != candidate.fingerprint:
                raise Conflict(f"Source media changed while setting up: {path.name}. Run GVAT again.")
            revision = file_digest(path)
            count = candidate.frames
            if candidate.kind == "video":
                from .video_decode import index_video, valid_index

                index = cache / "video-index" / f"{revision}.json"
                metadata = None
                try:
                    metadata = json.loads(index.read_text())
                except (OSError, ValueError):
                    pass
                if valid_index(metadata):
                    count = metadata["frames"]
                else:
                    print(f"Indexing video: {path.relative_to(self.source_root)}", flush=True)
                    try:
                        metadata = index_video(path)
                        count = metadata["frames"]
                    except (OSError, subprocess.SubprocessError) as error:
                        raise WorkspaceError(f"Cannot read source media {path.name}: {error}") from error
                    atomic_write(index, json.dumps(metadata).encode(), digest(read_optional(index)))
                self.video_indexes[path] = metadata
            if fingerprint(path) != candidate.fingerprint:
                raise Conflict(f"Source media changed during indexing: {path.name}. Run GVAT again.")
            self.sources.append(MediaSource(path, path.relative_to(self.source_root).as_posix(), candidate.kind, revision, candidate.fingerprint, count))
        self.videos = [source for source in self.sources if source.kind == "video"]
        identity = str(self.source_root) + json.dumps([(source.name, source.revision, source.kind, source.frames) for source in self.sources])
        frames_root = cache / "frames" / hashlib.sha256(identity.encode()).hexdigest()
        self._frames: dict[str, tuple[MediaSource, int]] = {}
        self._label_keys: dict[str, str] = {}
        occupied: dict[str, str] = {}
        for source in self.sources:
            for number in range(source.frames):
                key = source.name if source.kind == "image" and source.frames == 1 else f"{source.name}/frame_{number + 1:06}.png"
                annotation = self._annotation_key(key)
                if annotation in occupied:
                    raise WorkspaceError(f"{occupied[annotation]} and {key} map to the same annotation file {annotation}")
                occupied[annotation] = key
                self._frames[key] = (source, number)
                self._label_keys[key] = annotation
        self._inventory = {path: fingerprint(path) for path in source_files(self.source_root, (labels,), self.recursive)}
        frames_root.mkdir(parents=True, exist_ok=True)
        super().__init__(frames_root, labels, classes, mode)

    def scan(self) -> list[str]:
        return list(self._frames)

    def _annotation_key(self, key: str) -> str:
        return Path(key).with_suffix(".txt").as_posix()

    def _check_sources(self) -> None:
        remote = getattr(self, 'remote', None)
        if remote:
            remote.check_sources()
        for source in self.sources:
            try:
                changed = fingerprint(source.path) != source.fingerprint
            except OSError:
                changed = True
            if changed:
                raise Conflict(f"Source media changed or was removed: {source.name}. Stop GVAT and reopen the matching source before saving.")
        current = {path: fingerprint(path) for path in source_files(self.source_root, (self.labels,), self.recursive)}
        known = {source.path for source in self.sources}
        for path, signature in current.items():
            if path not in known and self._inventory.get(path) != signature and probe_media(path) is not None:
                raise Conflict("The source media list changed. Stop GVAT and reopen the folder to re-index it.")
        self._inventory = current

    def _label_path(self, key: str) -> Path:
        if key not in self._frames:
            raise WorkspaceError(f"Unknown media frame: {key}")
        target = contained(self.labels, self._label_keys[key])
        if target == self.class_file:
            raise WorkspaceError("An annotation path collides with the class file")
        return target

    def _ensure_frame(self, key: str) -> Path:
        self._check_sources()
        if key not in self._frames:
            raise WorkspaceError(f"Unknown media frame: {key}")
        # A flat hashed cache avoids collisions between a.jpg and a.jpg/... virtual keys.
        target = self.data / f"{hashlib.sha256(key.encode()).hexdigest()}.png"
        if not target.is_file():
            source, number = self._frames[key]
            try:
                if source.kind == "image":
                    decoded = image_preview(source.path, number)
                else:
                    command = ffmpeg_input(source.path) + [
                        "-map", "0:v:0", "-an", "-sn", "-filter_threads", "1", "-vf", f"select=eq(n\\,{number})",
                        "-vsync", "0", "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-threads", "1", "pipe:1",
                    ]
                    decoded = subprocess.run(command, capture_output=True, check=True, timeout=120).stdout
                with Image.open(io.BytesIO(decoded)) as frame:
                    frame.verify()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                raise WorkspaceError(f"Could not decode {source.name}, frame {number + 1}: {error}") from error
            self._check_sources()
            atomic_write(target, decoded, "missing")
        return target

    def paths(self, key: str) -> tuple[Path, Path]:
        return self._ensure_frame(key), self._label_path(key)

    def frame(self, key: str) -> dict:
        result = super().frame(key)
        result["label_path"] = self._label_keys[key]
        return result

    def image_bytes(self, key: str, expected: str | None = None) -> tuple[bytes, str]:
        with self.lock:
            return super().image_bytes(key, expected)

    def manifest(self) -> dict:
        with self.lock:
            self._check_sources()
            classes, revision = load_classes(self.class_file)
            existing = {path.relative_to(self.labels).as_posix() for path in self.labels.rglob("*.txt") if path.is_file()}
            return {
                "name": self.source_root.name, "data_path": str(self.source_root), "labels_path": str(self.labels),
                "classes": [{"id": key, "name": value} for key, value in classes.items()],
                "classes_revision": revision, "kind": "media", "mode": self.mode,
                "videos": [{"path": source.name, "frames": source.frames} for source in self.videos],
                "sources": [{"path": source.name, "kind": source.kind, "frames": source.frames} for source in self.sources],
                "images": [{"path": key, "labeled": self._label_keys[key] in existing} for key in self._frames],
                "feedback_storage": "session",
            }
