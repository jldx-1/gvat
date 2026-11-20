"""CVAT 1.x XML rectangles, preserving metadata and track identities on edits.

CVAT exports native zero-based video frame numbers. GVAT's public frame names
are one-based. Track edits pin neighboring selected frames so a per-frame edit
does not change the rest of an interpolated track.
"""

from __future__ import annotations

import copy
import io
import json
import math
import re
import subprocess
import xml.etree.ElementTree as ET
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from PIL import Image

from .media import MediaWorkspace, ffmpeg_input, source_files, source_revision, video_index
from .workspace import Conflict, WorkspaceError, atomic_write, contained, digest, same_boxes, validate_box


def integer(value: str | None, name: str, minimum: int = 0) -> int:
    try:
        result = int(value)
        if result < minimum:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise WorkspaceError(f"CVAT XML {name} must be an integer >= {minimum}") from None


def coordinates(element: ET.Element) -> list[float]:
    try:
        values = [float(element.attrib[key]) for key in ("xtl", "ytl", "xbr", "ybr")]
        rotation = float(element.get("rotation", "0"))
    except (KeyError, TypeError, ValueError):
        raise WorkspaceError("CVAT XML box needs numeric xtl, ytl, xbr, ybr coordinates") from None
    if not all(math.isfinite(value) for value in [*values, rotation]):
        raise WorkspaceError("CVAT XML coordinates must be finite")
    if rotation % 360:
        raise WorkspaceError("Rotated CVAT XML boxes are not supported by this rectangle editor")
    if values[2] <= values[0] or values[3] <= values[1]:
        raise WorkspaceError("CVAT XML boxes must have positive width and height")
    return values


def set_coordinates(element: ET.Element, box: dict, width: int, height: int) -> None:
    values = [(box["cx"] - box["width"] / 2) * width, (box["cy"] - box["height"] / 2) * height,
              (box["cx"] + box["width"] / 2) * width, (box["cy"] + box["height"] / 2) * height]
    for key, value in zip(("xtl", "ytl", "xbr", "ybr"), values):
        element.set(key, format(max(0, value), ".12g"))


def serialize_xml(root: ET.Element) -> bytes:
    ET.indent(root, space="  ")
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(
        root, encoding="utf-8", short_empty_elements=False,
    ) + b"\n"


def image_name_index(keys):
    """CVAT ZIP exports omit the outer images/ directory from image names."""
    keys = set(keys)
    suffixes = {}
    for key in keys:
        parts = PurePosixPath(key).parts
        for start in range(1, len(parts)):
            suffixes.setdefault('/'.join(parts[start:]), []).append(key)
    return keys, suffixes


def resolve_image_name(name, index, subset=''):
    relative = PurePosixPath(name)
    if not name or relative.is_absolute() or '..' in relative.parts or '\\' in name:
        raise WorkspaceError('CVAT XML image names must be relative POSIX media paths')
    if subset and (PurePosixPath(subset).is_absolute() or '..' in PurePosixPath(subset).parts or '\\' in subset):
        raise WorkspaceError('CVAT XML subset must be a relative POSIX path')
    keys, suffixes = index
    for candidate in ([f'{subset}/{name}', name] if subset else [name]):
        if candidate in keys:
            return candidate
        matches = suffixes.get(candidate, [])
        if len(matches) > 1:
            raise WorkspaceError(f'Ambiguous CVAT XML image name: {name}. Use its full path relative to the sample folder.')
        if matches:
            return matches[0]
    return None


def annotation_media_type(path, data, xml_version='auto'):
    """Preserve an existing XML's media binding when a folder also has exports."""
    document = CvatDocument(path.read_bytes(), native=True, xml_version=xml_version)
    if document.root.findall('track') or document.root.findtext('meta/task/mode') == 'interpolation':
        return 'video'
    kinds = set()
    from .media import probe_media

    files = source_files(data, (path, data / 'reviews.json', data / 'labels.txt'))
    names = image_name_index(file.relative_to(data).as_posix() for file in files)
    for image in document.root.findall('image'):
        relative = PurePosixPath(image.get('name', ''))
        if resolve_image_name(image.get('name', ''), names, image.get('subset', '')):
            kinds.add('images')
        elif len(relative.parts) > 1:
            for end in range(len(relative.parts) - 1, 0, -1):
                source = contained(data, '/'.join(relative.parts[:end]))
                if source.is_file():
                    candidate = probe_media(source)
                    if candidate:
                        kinds.add('images' if candidate.kind == 'image' else 'video')
                    break
        elif re.fullmatch(r'frame_\d+\.[^.]+', str(relative)):
            kinds.add('video')
    return 'all' if len(kinds) > 1 else next(iter(kinds), None)


class CvatDocument:
    def __init__(self, raw: bytes, native: bool = False, xml_version: str = 'auto'):
        self.native = native
        try:
            text = raw.decode("utf-8-sig")
            if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
                raise WorkspaceError("CVAT XML must not contain DTDs or entity definitions")
            self.root = ET.fromstring(text, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
        except (ET.ParseError, UnicodeError) as error:
            raise WorkspaceError(f"Invalid UTF-8 CVAT XML: {error}") from error
        if self.root.tag != 'annotations':
            raise WorkspaceError('Expected a CVAT XML <annotations> export')
        if xml_version not in ('auto', '1.0', '1.1'):
            raise WorkspaceError('CVAT XML import version must be auto, 1.0 or 1.1')
        declared = self.root.findtext('version', '').strip()
        if declared and declared not in ('1.0', '1.1'):
            raise WorkspaceError(f'Unsupported CVAT XML version {declared!r}; supported versions: 1.0, 1.1')
        if xml_version != 'auto' and declared and declared != xml_version:
            raise WorkspaceError(f'CVAT XML declares version {declared}, but import requires {xml_version}. Use --xml-version=auto or update import.xml_version.')
        if not declared and xml_version == 'auto':
            raise WorkspaceError('CVAT XML version tag is missing. Set --xml-version=1.0 or --xml-version=1.1 (or import.xml_version in config).')
        self.version = declared or xml_version
        if not declared:
            version = self.root.find('version')
            if version is None:
                version = ET.Element('version')
                self.root.insert(0, version)
            version.text = self.version  # Persist only when annotations are saved.
        definitions = self.root.findall("./meta/task/labels/label") or self.root.findall("./meta/project/labels/label")
        names = [element.findtext("name", "").strip() for element in definitions]
        if not names or any(not name for name in names) or len(set(names)) != len(names):
            raise WorkspaceError("CVAT XML needs unique, nonempty label definitions in meta/task/labels or meta/project/labels")
        self.classes = dict(enumerate(names))
        self.class_ids = {name: key for key, name in self.classes.items()}
        self.classes_revision = digest(json.dumps([
            ET.canonicalize(ET.tostring(item, encoding="unicode"), strip_text=True) for item in definitions
        ]).encode())
        for flipped in self.root.findall("./meta//flipped"):
            if (flipped.text or "").strip().lower() in ("true", "1"):
                raise WorkspaceError("Flipped CVAT XML exports are not supported by this rectangle editor")
        for parent in [*self.root.findall("image"), *self.root.findall("track")]:
            for child in parent:
                supported = {"box", "polygon", "polyline", "points", "ellipse", "cuboid", "mask", "skeleton", "tag"} if native else {"box"}
                if isinstance(child.tag, str) and child.tag not in supported:
                    raise WorkspaceError(f"CVAT XML <{child.tag}> annotations are not supported by this rectangle editor")
            for box in parent.findall("box"):
                if not native:
                    coordinates(box)
                for flag in ("outside", "occluded", "keyframe"):
                    if box.get(flag, "0") not in ("0", "1"):
                        raise WorkspaceError(f"CVAT XML {flag} must be 0 or 1")
                if (parent.get("label") if parent.tag == "track" else box.get("label")) not in self.class_ids:
                    raise WorkspaceError("CVAT XML box references an undefined label")
        if not native and self.root.findall("track") and self.root.findall("image"):
            raise WorkspaceError("Mixed CVAT XML image and track exports are not supported")
        self.raw = raw
        self.images: dict[str, ET.Element] = {}
        self.tracks: dict[str, tuple[ET.Element, list[int], list[ET.Element]]] = {}
        self.selected: dict[str, int] = {}
        self.dimensions: dict[str, tuple[int, int]] = {}
        self.video_images: dict[str, int] = {}
        self.image_indices: dict[str, int] = {}
        self.step = 1

    @classmethod
    def create(cls, names: list[str], video: tuple[int, int, int] | None = None,
               image_count: int = 0, name: str = "GVAT local annotations") -> bytes:
        root = ET.Element("annotations")
        ET.SubElement(root, "version").text = "1.1"
        meta = ET.SubElement(root, "meta")
        task = ET.SubElement(meta, "task")
        timestamp = datetime.now(timezone.utc).isoformat(sep=" ")
        count = video[2] if video else image_count
        ET.SubElement(task, "id").text = "0"
        ET.SubElement(task, "name").text = name
        ET.SubElement(task, "size").text = str(count)
        ET.SubElement(task, "mode").text = "interpolation" if video else "annotation"
        ET.SubElement(task, "overlap").text = "0"
        ET.SubElement(task, "bugtracker")
        ET.SubElement(task, "flipped").text = "False"
        ET.SubElement(task, "created").text = timestamp
        ET.SubElement(task, "updated").text = timestamp
        labels = ET.SubElement(task, "labels")
        for name in names:
            label = ET.SubElement(labels, "label")
            ET.SubElement(label, "name").text = name
            ET.SubElement(label, "attributes")
        segments = ET.SubElement(task, "segments")
        if count:
            segment = ET.SubElement(segments, "segment")
            ET.SubElement(segment, "id").text = "0"
            ET.SubElement(segment, "start").text = "0"
            ET.SubElement(segment, "stop").text = str(count - 1)
            ET.SubElement(segment, "url")
        owner = ET.SubElement(task, "owner")
        ET.SubElement(owner, "username").text = "local"
        ET.SubElement(owner, "email")
        if video:
            original = ET.SubElement(task, "original_size")
            ET.SubElement(original, "width").text = str(video[0])
            ET.SubElement(original, "height").text = str(video[1])
        ET.SubElement(meta, "dumped").text = timestamp
        return serialize_xml(root)

    @classmethod
    def for_media(cls, names: list[str], candidates: list) -> bytes:
        video = None
        if len(candidates) == 1 and candidates[0].kind == "video":
            source = candidates[0].path
            command = ffmpeg_input(source) + ["-map", "0:v:0", "-an", "-sn", "-frames:v", "1",
                       "-filter_threads", "1", "-f", "image2pipe", "-vcodec", "png", "-threads", "1", "pipe:1"]
            try:
                if candidates[0].dimensions:
                    width, height = candidates[0].dimensions[0]
                else:
                    raw = subprocess.run(command, capture_output=True, check=True, timeout=120).stdout
                    with Image.open(io.BytesIO(raw)) as image:
                        width, height = image.size
                video = width, height, video_index(source, source_revision(candidates[0]))['frames']
            except (OSError, subprocess.SubprocessError) as error:
                raise WorkspaceError(f"Cannot initialize CVAT XML for {source.name}: {error}") from error
        count = sum(candidate.frames if candidate.kind == "image" else video_index(candidate.path, source_revision(candidate))['frames']
                    for candidate in candidates) if video is None else 0
        return cls.create(names, video, image_count=count, name=candidates[0].path.parent.name)

    def bind(self, workspace: MediaWorkspace) -> None:
        keys = set(workspace._frames)
        names = image_name_index(key for key, (source, _) in workspace._frames.items() if source.kind == 'image')
        self.image_indices = {key: index for index, key in enumerate(sorted(keys))}
        if len(workspace.sources) == 1 and len(workspace.videos) == 1:
            self.video_images = {key: number for key, (_, number) in workspace._frames.items()}
        seen_ids = set()
        for image in self.root.findall("image"):
            name = image.get("name", "")
            key = resolve_image_name(name, names, image.get('subset', '')) or name
            if key not in keys:
                # CVAT for images names video frames frame_000000.png (zero-based).
                match = re.fullmatch(r"frame_(\d+)\.[^.]+", name)
                if match and len(workspace.videos) == 1:
                    key = f"{workspace.videos[0].name}/frame_{int(match[1]) + 1:06}.png"
            if key not in keys:
                raise WorkspaceError(f"CVAT XML image is missing from the selected media folder: {name}")
            identifier = integer(image.get("id"), "image id")
            if key in self.images or identifier in seen_ids:
                raise WorkspaceError("CVAT XML image paths and IDs must be unique")
            seen_ids.add(identifier)
            self.images[key] = image
            if self.version == "1.0" and (image.get("width") is None or image.get("height") is None):
                with Image.open(workspace._ensure_frame(key)) as source:
                    self.dimensions[key] = source.size
            else:
                self.dimensions[key] = (integer(image.get("width"), "image width", 1), integer(image.get("height"), "image height", 1))
            if not self.native:
                self.boxes(key, *self.dimensions[key])
        task = self.root.find("./meta/task")
        is_video = bool(self.root.findall("track")) or (task is not None and task.findtext("mode") == "interpolation")
        if not is_video:
            return
        image_sequence = self.native and not workspace.videos
        if task is None or (not image_sequence and (len(workspace.sources) != 1 or len(workspace.videos) != 1)):
            raise WorkspaceError("CVAT XML video tracks require one video and a single task export")
        source = None if image_sequence else workspace.videos[0]
        frame_count = len(workspace._frames) if image_sequence else source.frames
        start = integer(task.findtext("start_frame", "0"), "start_frame")
        stop = integer(task.findtext("stop_frame", str(frame_count - 1)), "stop_frame")
        frame_filter = task.findtext("frame_filter", "").strip()
        if frame_filter:
            match = re.fullmatch(r"step\s*=\s*(\d+)", frame_filter)
            if not match:
                raise WorkspaceError("CVAT XML frame_filter must be empty or step=N")
            self.step = integer(match[1], "frame step", 1)
        if start > stop or stop >= frame_count:
            raise WorkspaceError("CVAT XML task frame range does not match the source video")
        if (self.version == "1.0" or image_sequence) and task.find("original_size") is None:
            first_key = list(workspace._frames)[start] if image_sequence else f"{source.name}/frame_{start + 1:06}.png"
            with Image.open(workspace._ensure_frame(first_key)) as opened:
                size = opened.size
        else:
            size = (integer(task.findtext("original_size/width"), "original width", 1),
                    integer(task.findtext("original_size/height"), "original height", 1))
        self.selected = {(list(workspace._frames)[number] if image_sequence else f"{source.name}/frame_{number + 1:06}.png"): number
                         for number in range(start, stop + 1, self.step)}
        self.dimensions.update({key: size for key in self.selected})
        for track in self.root.findall("track"):
            identifier = str(integer(track.get("id"), "track id"))
            if identifier in self.tracks:
                raise WorkspaceError("CVAT XML track IDs must be unique")
            shapes = [element for element in track if isinstance(element.tag, str)] if self.native else track.findall("box")
            elements = sorted(shapes, key=lambda box: integer(box.get("frame"), "track frame"))
            numbers = [integer(box.get("frame"), "track frame") for box in elements]
            if not numbers or len(set(numbers)) != len(numbers):
                raise WorkspaceError("CVAT XML tracks need boxes with unique frame numbers")
            for number, box in zip(numbers, elements):
                # CVAT permits one closing outside keyframe immediately after a task.
                if number < start or number > stop + self.step or (number - start) % self.step:
                    raise WorkspaceError("CVAT XML track frame does not match its task range/filter")
                if number > stop and box.get("outside", "0") != "1":
                    raise WorkspaceError("A CVAT XML box past stop_frame must be outside")
                if not self.native:
                    self._box(box, track.get("label"), *size, f"track:{identifier}")
            self.tracks[identifier] = track, numbers, elements

    def _box(self, element: ET.Element, label: str, width: int, height: int, identifier: str, points=None) -> dict:
        left, top, right, bottom = points if points is not None else coordinates(element)
        box = validate_box({"class_id": self.class_ids[label], "cx": (left + right) / (2 * width),
                            "cy": (top + bottom) / (2 * height), "width": (right - left) / width,
                            "height": (bottom - top) / height}, self.classes)
        return {**box, "xml_id": identifier}

    def track_at(self, identifier: str, frame: int) -> tuple[ET.Element, list[float]] | None:
        _, numbers, elements = self.tracks[identifier]
        index = bisect_right(numbers, frame) - 1
        if index < 0 or elements[index].get("outside", "0") == "1":
            return None
        element = elements[index]
        points = coordinates(element)
        if index + 1 < len(numbers) and numbers[index] != frame:
            fraction = (frame - numbers[index]) / (numbers[index + 1] - numbers[index])
            points = [left + fraction * (right - left) for left, right in zip(points, coordinates(elements[index + 1]))]
        return element, points

    def boxes(self, key: str, width: int, height: int) -> list[dict]:
        if key in self.images:
            image = self.images[key]
            return [self._box(box, box.get("label"), width, height, f"image:{image.get('id')}:{index}")
                    for index, box in enumerate(image.findall("box"))]
        if key not in self.selected:
            return []
        result = []
        for identifier, (track, _, _) in self.tracks.items():
            state = self.track_at(identifier, self.selected[key])
            if state is not None:
                result.append(self._box(state[0], track.get("label"), width, height, f"track:{identifier}", state[1]))
        return result

    def update(self, key: str, boxes: list[dict], width: int, height: int) -> bytes:
        previous = self.boxes(key, width, height)
        identifiers = {box["xml_id"] for box in previous}
        supplied = [box["xml_id"] for box in boxes if "xml_id" in box]
        if len(set(supplied)) != len(supplied) or any(identifier not in identifiers for identifier in supplied):
            raise WorkspaceError("Unknown or duplicate CVAT XML box identity; reload before saving")
        if self.selected:
            self._update_tracks(key, boxes, previous, width, height)
        else:
            image = self.images.get(key)
            if image is None:
                identifier = self.image_indices[key]
                name = key
                if key in self.video_images:
                    native = self.video_images[key]
                    name = f"frame_{native:06}.png"
                    if native not in {int(item.get("id")) for item in self.images.values()}:
                        identifier = native
                if identifier in {int(item.get("id")) for item in self.images.values()}:
                    raise WorkspaceError("XML image IDs conflict with the media's lexical order; check the matching source files")
                image = ET.SubElement(self.root, "image", {"id": str(identifier), "name": name, "width": str(width), "height": str(height)})
            old = dict(zip((box["xml_id"] for box in previous), image.findall("box")))
            for element in list(image.findall("box")):
                image.remove(element)
            for box in boxes:
                element = old.get(box.get("xml_id"))
                if element is None:
                    element = ET.Element("box", {"occluded": "0", "z_order": "0"})
                element.set("label", self.classes[box["class_id"]])
                set_coordinates(element, box, width, height)
                image.append(element)
        timestamp = datetime.now(timezone.utc).isoformat(sep=" ")
        for field in ("./meta/task/updated", "./meta/dumped"):
            element = self.root.find(field)
            if element is not None:
                element.text = timestamp
        return serialize_xml(self.root)

    def _update_tracks(self, key: str, boxes: list[dict], previous: list[dict], width: int, height: int) -> None:
        frame = self.selected[key]
        updated = {box.get("xml_id"): box for box in boxes if box.get("xml_id")}
        for old in previous:
            box = updated.get(old["xml_id"])
            if box is not None and same_boxes([old], [box]):
                continue
            identifier = old["xml_id"].split(":", 1)[1]
            track, _, _ = self.tracks[identifier]
            template = self.track_at(identifier, frame)[0]
            # Pin both neighbors using the original, unmodified interpolation.
            for neighbor in (frame - self.step, frame + self.step):
                if neighbor not in self.selected.values():
                    continue
                state = self.track_at(identifier, neighbor)
                if state is not None:
                    element = copy.deepcopy(state[0])
                    element.set("frame", str(neighbor))
                    element.set("keyframe", "1")
                    for coordinate, value in zip(("xtl", "ytl", "xbr", "ybr"), state[1]):
                        element.set(coordinate, format(value, ".12g"))
                    self._put(track, element)
            element = copy.deepcopy(template)
            element.set("frame", str(frame))
            element.set("keyframe", "1")
            if box is None or box["class_id"] != old["class_id"]:
                element.set("outside", "1")
            else:
                element.set("outside", "0")
                set_coordinates(element, box, width, height)
            self._put(track, element)
        next_id = max((int(track.get("id")) for track in self.root.findall("track")), default=-1) + 1
        for box in boxes:
            old = next((item for item in previous if item["xml_id"] == box.get("xml_id")), None)
            if old is not None and old["class_id"] == box["class_id"]:
                continue
            track = ET.SubElement(self.root, "track", {"id": str(next_id), "label": self.classes[box["class_id"]]})
            next_id += 1
            element = ET.SubElement(track, "box", {"frame": str(frame), "keyframe": "1", "outside": "0", "occluded": "0"})
            set_coordinates(element, box, width, height)
            outside = copy.deepcopy(element)
            outside.set("frame", str(frame + self.step))
            outside.set("outside", "1")
            track.append(outside)

    @staticmethod
    def _put(track: ET.Element, element: ET.Element) -> None:
        for existing in track.findall("box"):
            if existing.get("frame") == element.get("frame"):
                track.remove(existing)
        track.append(element)
        shapes = sorted(track.findall("box"), key=lambda shape: int(shape.get("frame")))
        for shape in shapes:
            track.remove(shape)
        track.extend(shapes)


class CvatXmlWorkspace(MediaWorkspace):
    def __init__(self, data: Path, xml: Path, native_ui: bool = False, xml_version: str = 'auto', **kwargs):
        self.native_ui = native_ui
        self.xml_version = xml_version
        self.xml_file = xml.resolve(strict=True)
        kwargs.setdefault("recursive", True)
        super().__init__(data, self.xml_file.parent, self.xml_file, **kwargs)
        self.document()  # Validate every imported box/path before opening the editor.

    def _annotation_key(self, key: str) -> str:
        return key

    def _load_classes(self):
        document = CvatDocument(self.xml_file.read_bytes(), native=self.native_ui, xml_version=self.xml_version)
        return document.classes, document.classes_revision

    def document(self) -> CvatDocument:
        remote = getattr(self, 'remote', None)
        if remote:
            remote.refresh_file(self.xml_file)
        document = CvatDocument(self.xml_file.read_bytes(), native=self.native_ui, xml_version=self.xml_version)
        document.bind(self)
        return document

    def write_annotations(self, data, expected):
        remote = getattr(self, 'remote', None)
        if remote:
            remote.write_file(self.xml_file, data, expected)
        else:
            atomic_write(self.xml_file, data, expected)

    def paths(self, key: str) -> tuple[Path, Path]:
        return self._ensure_frame(key), self.xml_file

    def manifest(self) -> dict:
        with self.lock:
            self._check_sources()
            document = self.document()
            return {
                "name": self.source_root.name, "data_path": str(self.source_root), "labels_path": str(self.xml_file),
                "classes": [{"id": key, "name": value} for key, value in document.classes.items()],
                "classes_revision": document.classes_revision, "kind": "media", "mode": self.mode,
                "sources": [{"path": source.name, "kind": source.kind, "frames": source.frames} for source in self.sources],
                "videos": [{"path": source.name, "frames": source.frames} for source in self.videos],
                "images": [{"path": key, "labeled": key in document.images or key in document.selected}
                           for key in (document.selected or self._frames)], "feedback_storage": "session",
                "has_annotations": bool(document.images or document.tracks),
            }

    def frame(self, key: str) -> dict:
        with self.lock:
            document = self.document()
            if document.selected and key not in document.selected:
                raise WorkspaceError("Frame is outside the CVAT XML task range/filter")
            source = self._ensure_frame(key).read_bytes()
            with Image.open(io.BytesIO(source)) as image:
                width, height = image.size
            if key in document.dimensions and document.dimensions[key] != (width, height):
                raise WorkspaceError(f"CVAT XML dimensions do not match source media: {key}")
            return {
                "path": key, "label_path": self.xml_file.name, "width": width, "height": height,
                "image_revision": digest(source), "revision": digest(document.raw),
                "classes_revision": document.classes_revision, "annotations": document.boxes(key, width, height),
                "labeled": key in document.images or key in document.selected,
            }

    def save(self, key: str, payload: dict) -> dict:
        with self.lock:
            frame = self.frame(key)
            if frame["revision"] != payload.get("revision"):
                raise Conflict("XML annotations changed on disk. Reload before saving; your edits are kept in the browser.")
            if frame["image_revision"] != payload.get("image_revision"):
                raise Conflict("The source image changed on disk. Reload before saving.")
            if frame["classes_revision"] != payload.get("classes_revision"):
                raise Conflict("XML class definitions changed. Reload before saving.")
            document = self.document()
            if digest(document.raw) != frame["revision"]:
                raise Conflict("XML annotations changed while saving. Reload before saving.")
            supplied = payload.get("annotations")
            if not isinstance(supplied, list) or len(supplied) > 100000:
                raise WorkspaceError("annotations must be a list of at most 100000 boxes")
            boxes = []
            for item in supplied:
                box = validate_box(item, document.classes)
                if "xml_id" in item:
                    if not isinstance(item["xml_id"], str):
                        raise WorkspaceError("XML box identity must be a string")
                    box["xml_id"] = item["xml_id"]
                boxes.append(box)
            previous = frame["annotations"]
            if frame["labeled"] and same_boxes(previous, boxes) and [box.get("xml_id") for box in previous] == [box.get("xml_id") for box in boxes]:
                return {"revision": frame["revision"], "changed": False, "annotations": previous}
            output = document.update(key, boxes, frame["width"], frame["height"])
            atomic_write(self.xml_file, output, frame["revision"])
            return {"revision": digest(output), "changed": True, "annotations": self.frame(key)["annotations"]}
