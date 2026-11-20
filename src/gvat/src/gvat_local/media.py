"""Pillow and FFmpeg media exposed as stable frames with local YOLO annotations."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass, replace
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, UnidentifiedImageError

from .workspace import image_extensions, Conflict, Workspace, WorkspaceError, atomic_write, contained, digest, read_optional
from .source_cache import SourceCache, cache_root, source_cache_session

# These extensions only improve errors for broken media. They never limit discovery:
# every candidate is identified by Pillow/FFmpeg, including extensionless files.
VIDEO_HINTS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v", ".mpeg", ".mpg", ".ts", ".mts", ".m2ts", ".wmv", ".asf", ".flv", ".mxf", ".ogv", ".vob", ".h264", ".hevc"}
LOCAL_PROTOCOLS = "file,pipe,crypto,data"


@dataclass(frozen=True)
class MediaCandidate:
    path: Path
    kind: str
    frames: int
    fingerprint: tuple[int, int, int]
    dimensions: tuple[tuple[int, int], ...] = ()
    direct_format: str | None = None
    validated: bool = False


@dataclass(frozen=True)
class MediaSource:
    path: Path
    name: str
    kind: str
    revision: str
    fingerprint: tuple[int, int, int]
    frames: int
    dimensions: tuple[tuple[int, int], ...] = ()
    direct_format: str | None = None
    validated: bool = False


def fingerprint(path: Path) -> tuple[int, int, int]:
    metadata = path.stat()
    return metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns


def file_digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def source_files(root: Path, excluded: tuple[Path, ...] = (), recursive: bool = True) -> list[Path]:
    excluded = tuple(path.resolve() for path in excluded)
    files = []
    directories = [root]
    while directories:
        with os.scandir(directories.pop()) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.name.startswith('.') or path in excluded:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if recursive:
                        directories.append(path)
                elif entry.is_file() and (not entry.is_symlink() or path.resolve().is_relative_to(root)):
                    # DirEntry reuses directory metadata. Resolve only symlinks;
                    # resolving every image's parents makes large folders slow.
                    files.append(path)
    return sorted(files)


def ffmpeg_input(path: Path) -> list[str]:
    return [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-v", "error", "-threads", "1", "-protocol_whitelist", LOCAL_PROTOCOLS, "-i", str(path)]


def probe_media(path: Path, kind: str = 'all') -> MediaCandidate | None:
    cache = SourceCache(path)
    before = cache.fingerprint
    record = cache.read()
    if record is not None and record['kind'] != 'other':
        if record['kind'] == 'none' or kind != 'all' and record['kind'] != kind:
            return None
        return MediaCandidate(path, record['kind'], record['frames'], before,
                              tuple(tuple(size) for size in record.get('dimensions', [])), record.get('direct_format'), record['validated'])
    image_error = None
    try:
        if record and record['kind'] == 'other':
            raise UnidentifiedImageError()
        with Image.open(path) as image:
            if kind == 'video':
                return None
            count = getattr(image, "n_frames", 1)
            dimensions = []
            for page in range(count):
                image.seek(page)
                dimensions.append(image.size)
            image.seek(0)
            if count <= 0:
                raise OSError("Image contains no readable frames")
            if fingerprint(path) != before:
                raise Conflict(f"Source media changed while opening: {path.name}. Run GVAT again.")
            direct = {'JPEG': 'jpg', 'PNG': 'png'}.get(image.format) if count == 1 and image.mode in ('RGB', 'RGBA', 'L', 'LA', 'P') else None
            if image.format == 'JPEG' and image.getexif().get(274, 1) != 1:
                direct = None
            cache.write({'kind': 'image', 'frames': count, 'dimensions': dimensions, 'direct_format': direct, 'validated': False})
            return MediaCandidate(path, "image", count, before, tuple(dimensions), direct)
    except Image.DecompressionBombError as error:
        raise WorkspaceError(f"Cannot read source media {path.name}: {error}") from error
    except UnidentifiedImageError:
        pass
    except (OSError, ValueError, RuntimeError) as error:
        image_error = error

    if kind == 'image':
        if image_error or path.suffix.lower() in image_extensions():
            raise WorkspaceError(f'Cannot read source media {path.name}: {image_error or "No readable image"}')
        cache.write({'kind': 'other', 'frames': 0, 'dimensions': []})
        return None

    video_error = None
    try:
        command = ffmpeg_input(path) + ["-v", "info", "-map", "0:v:0", "-an", "-sn", "-frames:v", "1",
                                        "-filter_threads", "1", "-vf", "showinfo=checksum=0", "-f", "null", "-"]
        probe = subprocess.run(command, capture_output=True, timeout=30)
        if probe.returncode == 0:
            if fingerprint(path) != before:
                raise Conflict(f"Source media changed while opening: {path.name}. Run GVAT again.")
            size = re.search(rb'\[Parsed_showinfo[^\]]*\].*?\bs:(\d+)x(\d+)', probe.stderr)
            dimensions = ((int(size[1]), int(size[2])),) if size else ()
            cache.write({'kind': 'video', 'frames': 0, 'dimensions': dimensions, 'direct_format': None})
            return MediaCandidate(path, "video", 0, before, dimensions)
        video_error = probe.stderr.decode(errors="replace").strip().splitlines()[-1:]
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        video_error = [str(error)]
    if path.suffix.lower() in VIDEO_HINTS or kind == 'all' and (image_error or path.suffix.lower() in image_extensions()):
        detail = str(image_error) if image_error else " ".join(video_error or ["No readable video stream"])
        raise WorkspaceError(f"Cannot read source media {path.name}: {detail}")
    if kind == 'all':
        cache.write({'kind': 'none', 'frames': 0, 'dimensions': []})
    return None


@source_cache_session()
def discover_media(root: Path, excluded: tuple[Path, ...] = (), recursive: bool = True, preferred: str = 'all') -> list[MediaCandidate]:
    files = source_files(root, excluded, recursive)
    candidates = []
    for kind in (('image', 'video') if preferred == 'images' else ('video', 'image') if preferred == 'video' else ('all',)):
        candidates = [candidate for path in files if (candidate := probe_media(path, kind)) is not None]
        if candidates:
            break
    if not candidates:
        raise WorkspaceError(f"No supported images or videos found in {root}. Add media readable by Pillow or FFmpeg. GVAT will not start.")
    return candidates


def source_revision(candidate):
    cache = SourceCache(candidate.path)
    if cache.fingerprint != candidate.fingerprint:
        raise Conflict(f'Source media changed while setting up: {candidate.path.name}. Run GVAT again.')
    record = cache.read() or {'kind': candidate.kind, 'frames': candidate.frames, 'dimensions': candidate.dimensions,
                              'direct_format': candidate.direct_format}
    if candidate.kind == 'image':
        # Image cache identity needs a stable file version, not a full read of
        # every image before the command can open. Pixel reads happen on demand.
        # Retain prior cached identities so an upgrade can reuse its previews.
        return record.get('revision') or digest(json.dumps(['image-v2', *cache.stamp]).encode())
    revision = record.get('revision')
    if not isinstance(revision, str) or len(revision) != 64:
        revision = file_digest(candidate.path)
        cache.write({**record, 'revision': revision})
    return revision


@source_cache_session()
def validate_image(candidate):
    cache = SourceCache(candidate.path)
    if cache.fingerprint != candidate.fingerprint:
        raise Conflict(f'Source media changed while opening: {candidate.path.name}. Run GVAT again.')
    record = cache.read() or {'kind': 'image', 'frames': candidate.frames, 'dimensions': candidate.dimensions}
    if record.get('validated'):
        return replace(candidate, direct_format=record.get('direct_format'), validated=True)
    try:
        with Image.open(candidate.path) as image:
            image.load()
            direct = {'JPEG': 'jpg', 'PNG': 'png'}.get(image.format) if candidate.frames == 1 and image.mode in ('RGB', 'RGBA', 'L', 'LA', 'P') and image.getexif().get(274, 1) == 1 else None
    except (Image.DecompressionBombError, OSError, ValueError, RuntimeError) as error:
        raise WorkspaceError(f'Cannot decode source image {candidate.path.name}: {error}') from error
    cache.write({**record, 'direct_format': direct, 'validated': True})
    return replace(candidate, direct_format=direct, validated=True)


def video_index(path, revision):
    from .video_decode import index_video, valid_index

    target = cache_root() / 'video-index' / f'{revision}.json'
    try:
        metadata = json.loads(target.read_bytes())
    except (OSError, ValueError):
        metadata = None
    if not valid_index(metadata):
        before = fingerprint(path)
        print(f'Indexing video: {path.name}', flush=True)
        metadata = index_video(path)
        if fingerprint(path) != before:
            raise Conflict(f'Source media changed during indexing: {path.name}. Run GVAT again.')
        atomic_write(target, json.dumps(metadata).encode(), digest(read_optional(target)))
    return metadata


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
        preview.save(output, format="PNG", compress_level=1)
        return output.getvalue()


class MediaWorkspace(Workspace):
    @source_cache_session()
    def __init__(self, data: Path, labels: Path, classes: Path, mode: str = "annotate", candidates: list[MediaCandidate] | None = None, recursive: bool = True):
        self.source_root = data.resolve(strict=True)
        self.recursive = recursive
        self._media_excluded = (classes.resolve(), labels.resolve(), self.source_root / 'reviews.json')
        candidates = candidates if candidates is not None else discover_media(self.source_root, self._media_excluded, recursive)
        cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser().resolve() / "gvat"
        self.sources: list[MediaSource] = []
        self.video_indexes = {}
        for candidate in candidates:
            path = candidate.path
            if fingerprint(path) != candidate.fingerprint:
                raise Conflict(f"Source media changed while setting up: {path.name}. Run GVAT again.")
            revision = source_revision(candidate)
            count = candidate.frames
            if candidate.kind == "video":
                metadata = video_index(path, revision)
                count = metadata['frames']
                self.video_indexes[path] = metadata
            if fingerprint(path) != candidate.fingerprint:
                raise Conflict(f"Source media changed during indexing: {path.name}. Run GVAT again.")
            self.sources.append(MediaSource(path, path.relative_to(self.source_root).as_posix(), candidate.kind, revision,
                                            candidate.fingerprint, count, candidate.dimensions, candidate.direct_format, candidate.validated))
        self._validation_lock = threading.Lock()
        self._validated_images = {source.path: source.direct_format for source in self.sources if source.kind == 'image' and source.validated}
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
        self._inventory = {path: fingerprint(path) for path in source_files(self.source_root, self._media_excluded, self.recursive)}
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
        current = {path: fingerprint(path) for path in source_files(self.source_root, self._media_excluded, self.recursive)}
        for source in self.sources:
            if current.get(source.path) != source.fingerprint:
                raise Conflict(f"Source media changed or was removed: {source.name}. Stop GVAT and reopen the matching source before saving.")
        known = {source.path for source in self.sources}
        active = {'images': 'image', 'video': 'video'}.get(getattr(self, 'preferred_media', 'all'), 'all')
        for path, signature in current.items():
            if path not in known and self._inventory.get(path) != signature and probe_media(path, active) is not None:
                raise Conflict("The source media list changed. Stop GVAT and reopen the folder to re-index it.")
        self._inventory = current

    def _check_frame_sources(self, keys):
        remote = getattr(self, 'remote', None)
        if remote:
            remote.check_sources()
        for source in {self._frames[key][0] for key in keys}:
            try:
                actual = fingerprint(source.path)
            except OSError:
                actual = None
            if actual != source.fingerprint:
                raise Conflict(f'Source media changed or was removed: {source.name}. Restart GVAT before continuing.')

    def image_format(self, source):
        with self._validation_lock:
            if source.path not in self._validated_images:
                if not source.direct_format:
                    return None  # Lossless preview generation validates these formats.
                candidate = MediaCandidate(source.path, source.kind, source.frames, source.fingerprint,
                                           source.dimensions, source.direct_format, source.validated)
                self._validated_images[source.path] = validate_image(candidate).direct_format
            return self._validated_images[source.path]

    def _label_path(self, key: str) -> Path:
        if key not in self._frames:
            raise WorkspaceError(f"Unknown media frame: {key}")
        target = contained(self.labels, self._label_keys[key])
        if target == self.class_file:
            raise WorkspaceError("An annotation path collides with the class file")
        return target

    def _ensure_frame(self, key: str, check_sources: bool = True) -> Path:
        if check_sources:
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
            if check_sources:
                self._check_sources()
            try:
                atomic_write(target, decoded, "missing")
            except Conflict:
                if read_optional(target) != decoded:
                    raise
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
            classes, revision = self._load_classes()
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
