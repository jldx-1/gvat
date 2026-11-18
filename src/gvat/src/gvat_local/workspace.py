"""Rectangle validation, atomic annotation writes, and session review feedback."""

from __future__ import annotations

import hashlib
import html
import io
import json
import math
import os
import stat
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from PIL import Image

Image.init()
IMAGE_EXTENSIONS = {extension for extension, format_name in Image.registered_extensions().items() if format_name in Image.OPEN}


class WorkspaceError(ValueError):
    pass


class Conflict(WorkspaceError):
    """A file changed since it was loaded; the caller must reload before saving."""


def digest(data: bytes | None) -> str:
    return "missing" if data is None else hashlib.sha256(data).hexdigest()


def read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def contained(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise WorkspaceError("Expected a nonempty, relative POSIX path")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise WorkspaceError("Paths must stay inside the selected directory")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise WorkspaceError(f"Path escapes the selected directory: {relative}")
    return resolved


def atomic_write(path: Path, data: bytes, expected: str) -> None:
    """Write a sibling temporary file, then replace; retain an existing file's mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.gvat-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        if digest(read_optional(path)) != expected:
            raise Conflict(f"{path.name} changed on disk. Reload before saving.")
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_classes(path: Path) -> tuple[dict[int, str], str]:
    raw = path.read_bytes()
    if path.suffix.lower() == ".xml":
        from .cvat_xml import CvatDocument

        document = CvatDocument(raw, native=True)
        return document.classes, document.classes_revision
    try:
        if path.suffix.lower() in {".yaml", ".yml", ".json"}:
            document = yaml.safe_load(raw)
            names = document.get("names") if isinstance(document, dict) else document
        else:
            names = raw.decode("utf-8-sig").splitlines()
        if isinstance(names, list):
            pairs = list(enumerate(names))
        elif isinstance(names, dict):
            pairs = list(names.items())
        else:
            raise WorkspaceError("Class file needs a 'names' list/mapping or one name per line")
        classes: dict[int, str] = {}
        for key, name in pairs:
            if isinstance(key, bool) or str(key) != str(int(key)) or int(key) < 0:
                raise WorkspaceError(f"Invalid class ID: {key}")
            if not isinstance(name, str) or not name.strip() or int(key) in classes:
                raise WorkspaceError("Classes need unique nonnegative IDs and nonempty names")
            classes[int(key)] = name.strip()
        if not classes or len(set(classes.values())) != len(classes):
            raise WorkspaceError("Provide at least one class; class names must be unique")
        return dict(sorted(classes.items())), digest(raw)
    except (yaml.YAMLError, UnicodeError, TypeError, OverflowError, ValueError) as error:
        raise WorkspaceError(f"Invalid class file {path}: {error}") from error


def validate_box(box: object, classes: dict[int, str]) -> dict:
    if not isinstance(box, dict):
        raise WorkspaceError("Each annotation must be an object")
    class_id = box.get("class_id")
    if type(class_id) is not int or class_id not in classes:
        raise WorkspaceError(f"Unknown class ID {class_id}; update the class file first")
    coordinates = []
    for key in ("cx", "cy", "width", "height"):
        value = box.get(key)
        if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1:
            raise WorkspaceError(f"{key} must be a finite normalized number between 0 and 1")
        coordinates.append(float(value))
    cx, cy, width, height = coordinates
    if width <= 0 or height <= 0:
        raise WorkspaceError("Bounding boxes must have positive width and height")
    if min(cx - width / 2, cy - height / 2) < -1e-6 or max(cx + width / 2, cy + height / 2) > 1 + 1e-6:
        raise WorkspaceError("Bounding boxes must stay inside the image")
    return dict(zip(("class_id", "cx", "cy", "width", "height"), (class_id, *coordinates)))


def parse_yolo(raw: bytes | None, classes: dict[int, str], name: str) -> list[dict]:
    annotations = []
    try:
        for number, line in enumerate((raw or b"").decode("utf-8-sig").splitlines(), 1):
            fields = line.split()
            if not fields:
                continue
            try:
                if len(fields) != 5:
                    raise WorkspaceError("Expected 5 fields; only YOLO detection boxes are supported")
                class_id = int(fields[0])
                box = dict(zip(("class_id", "cx", "cy", "width", "height"), (class_id, *map(float, fields[1:]))))
                annotations.append(validate_box(box, classes))
            except (ValueError, OverflowError) as error:
                raise WorkspaceError(f"{name}:{number}: {error}") from error
    except UnicodeError as error:
        raise WorkspaceError(f"{name} must be UTF-8 text") from error
    return annotations


def same_boxes(left: list[dict], right: list[dict]) -> bool:
    return len(left) == len(right) and all(
        a["class_id"] == b["class_id"] and all(
            math.isclose(a[key], b[key], rel_tol=0, abs_tol=1e-10)
            for key in ("cx", "cy", "width", "height")
        ) for a, b in zip(left, right)
    )


class Workspace:
    def __init__(self, data: Path, labels: Path, classes: Path, mode: str = "annotate"):
        self.data = data.resolve(strict=True)
        self.labels = labels.resolve(strict=True)
        self.class_file = classes.resolve(strict=True)
        self._review = {"issues": [], "revision": "missing"}
        self.mode = mode
        self.lock = threading.RLock()
        if not self.data.is_dir() or not self.labels.is_dir():
            raise WorkspaceError("Data and labels must be directories")
        load_classes(self.class_file)
        self.scan()

    def scan(self) -> list[str]:
        images = sorted(
            item.relative_to(self.data).as_posix() for item in self.data.rglob("*")
            if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS
            and not any(part.startswith(".") for part in item.relative_to(self.data).parts)
            and item.resolve().is_relative_to(self.data)
        )
        targets: dict[str, str] = {}
        for image in images:
            target = Path(image).with_suffix(".txt").as_posix()
            if target in targets:
                raise WorkspaceError(f"{targets[target]} and {image} map to the same label file {target}")
            targets[target] = image
        if not images:
            raise WorkspaceError(f"No supported images found in {self.data}")
        return images

    def paths(self, image: str) -> tuple[Path, Path]:
        source = contained(self.data, image)
        if source.suffix.lower() not in IMAGE_EXTENSIONS or not source.is_file():
            raise WorkspaceError(f"Image does not exist: {image}. Refresh the sample.")
        target = contained(self.labels, Path(image).with_suffix(".txt").as_posix())
        if target == self.class_file:
            raise WorkspaceError("A label path collides with the class file")
        return source, target

    def manifest(self) -> dict:
        with self.lock:
            classes, revision = load_classes(self.class_file)
            images = [{"path": name, "labeled": self.paths(name)[1].is_file()} for name in self.scan()]
            return {
                "name": self.data.name, "data_path": str(self.data), "labels_path": str(self.labels),
                "classes": [{"id": key, "name": value} for key, value in classes.items()],
                "classes_revision": revision, "images": images, "mode": self.mode,
                "feedback_storage": "session",
            }

    def frame(self, image: str) -> dict:
        with self.lock:
            source, target = self.paths(image)
            image_bytes = source.read_bytes()
            with Image.open(io.BytesIO(image_bytes)) as opened:
                width, height = opened.size
            classes, classes_revision = load_classes(self.class_file)
            raw = read_optional(target)
            return {
                "path": image, "label_path": target.relative_to(self.labels).as_posix(),
                "width": width, "height": height, "image_revision": digest(image_bytes),
                "revision": digest(raw), "classes_revision": classes_revision,
                "annotations": parse_yolo(raw, classes, str(target)),
            }

    def image_bytes(self, image: str, expected: str | None = None) -> tuple[bytes, str]:
        source, _ = self.paths(image)
        raw = source.read_bytes()
        if expected is not None and digest(raw) != expected:
            raise Conflict("The image changed while opening. Reload from disk.")
        # Browsers rotate EXIF JPEGs implicitly. Strip orientation without rotating pixels,
        # so normalized YOLO coordinates and Pillow's dimensions describe the same image.
        with Image.open(io.BytesIO(raw)) as opened:
            if opened.getexif().get(274, 1) != 1:
                output = io.BytesIO()
                opened.convert("RGB").save(output, format="PNG")
                return output.getvalue(), "image/png"
            return raw, Image.MIME.get(opened.format, "application/octet-stream")

    def save(self, image: str, payload: dict) -> dict:
        with self.lock:
            source, target = self.paths(image)
            classes, classes_revision = load_classes(self.class_file)
            raw = read_optional(target)
            if digest(raw) != payload.get("revision"):
                raise Conflict("Labels changed on disk. Reload before saving; your edits have been kept in the browser.")
            if digest(source.read_bytes()) != payload.get("image_revision"):
                raise Conflict("The image changed on disk. Reload before saving.")
            if classes_revision != payload.get("classes_revision"):
                raise Conflict("Class definitions changed on disk. Reload the sample before saving.")
            boxes = payload.get("annotations")
            if not isinstance(boxes, list) or len(boxes) > 100000:
                raise WorkspaceError("annotations must be a list of at most 100000 boxes")
            boxes = [validate_box(box, classes) for box in boxes]
            previous = parse_yolo(raw, classes, str(target))
            if raw is not None and same_boxes(previous, boxes):
                return {"revision": digest(raw), "changed": False}
            output = "".join(
                f"{box['class_id']} " + " ".join(format(box[key], ".12g") for key in ("cx", "cy", "width", "height")) + "\n"
                for box in boxes
            ).encode()
            atomic_write(target, output, payload["revision"])
            return {"revision": digest(output), "changed": True}

    def review(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self._review))

    def _validate_issues(self, issues: object) -> list[dict]:
        if not isinstance(issues, list) or len(issues) > 100000:
            raise WorkspaceError("issues must be a list of at most 100000 findings")
        result, ids = [], set()
        for issue in issues:
            if not isinstance(issue, dict):
                raise WorkspaceError("Each finding must be an object")
            if not isinstance(issue.get("id"), str):
                raise WorkspaceError("Each finding needs a UUID string")
            identifier = str(uuid.UUID(issue["id"]))
            if identifier in ids:
                raise WorkspaceError("Finding IDs must be unique")
            ids.add(identifier)
            image = issue.get("image")
            contained(self.data, image)  # Retain findings for images since removed from disk.
            comment = issue.get("comment")
            if not isinstance(comment, str) or not comment.strip() or len(comment) > 20000:
                raise WorkspaceError("Each finding needs a comment of 1 to 20000 characters")
            if issue.get("status") not in ("open", "resolved"):
                raise WorkspaceError("Finding status must be open or resolved")
            region = issue.get("region")
            if region is not None and (
                not isinstance(region, list) or len(region) != 4
                or any(type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1 for value in region)
                or region[2] <= region[0] or region[3] <= region[1]
            ):
                raise WorkspaceError("Finding regions need normalized [left, top, right, bottom] coordinates")
            result.append({
                "id": identifier, "image": image, "comment": comment.strip(),
                "status": issue["status"], "region": region,
                "created_at": str(issue.get("created_at", datetime.now(timezone.utc).isoformat())),
            })
        return result

    def save_review(self, payload: dict) -> dict:
        with self.lock:
            if payload.get("revision") != self._review["revision"]:
                raise Conflict("Review findings changed in this session. Reload before updating them.")
            issues = self._validate_issues(payload.get("issues"))
            self._review = {"issues": issues, "revision": digest(json.dumps(issues).encode())}
            return self.review()

    def markdown(self, include_resolved: bool = False) -> str:
        issues = [issue for issue in self.review()["issues"] if include_resolved or issue["status"] == "open"]
        lines = ["## Annotation review", "", f"{len(issues)} {'finding' if len(issues) == 1 else 'findings'}", ""]
        for issue in issues:
            # An HTML-escaped code tag safely renders filenames containing Markdown syntax.
            lines.append(f"- **{issue['status'].title()}** — <code>{html.escape(issue['image'])}</code>")
            if issue["region"]:
                lines.append("  - Region (normalized x1, y1, x2, y2): " + ", ".join(f"{value:.4f}" for value in issue["region"]))
            lines.extend(f"  > {line}" for line in issue["comment"].splitlines())
            lines.append("")
        return "\n".join(lines)
