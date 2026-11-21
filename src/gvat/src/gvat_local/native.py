"""Local transport for the unmodified CVAT annotation models and workspace."""

from __future__ import annotations

import copy
import html
import math
import re
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone

from PIL import Image

from .cvat_xml import serialize_xml
from .chunks import MediaChunks
from .buffering import buffer_plan
from .exports import LocalExports
from .reviews import ReviewFile
from .workspace import Conflict, WorkspaceError, digest

TYPES = {"box": "rectangle", "polygon": "polygon", "polyline": "polyline", "points": "points", "ellipse": "ellipse", "mask": "mask", "skeleton": "skeleton", "cuboid": "cuboid", "tag": "tag"}
CUBE = ("xtl1", "ytl1", "xbl1", "ybl1", "xtr1", "ytr1", "xbr1", "ybr1", "xtl2", "ytl2", "xbl2", "ybl2", "xtr2", "ytr2", "xbr2", "ybr2")
LOCAL_USER = {"id": 1, "username": "local", "email": "", "first_name": "", "last_name": "", "is_staff": False, "is_superuser": False, "groups": []}


class NativeSession:
    def __init__(self, workspace):
        self.workspace = workspace
        self.views = {}
        self.issues = []
        self.issue_counter = 0
        self.comment_counter = 0
        self.review_file = ReviewFile(workspace)
        self._load_reviews()
        self.chunks = MediaChunks(workspace)
        self.exports = LocalExports(workspace)

    def snapshot(self):
        with self.workspace.lock:
            workspace = self.workspace
            workspace._check_sources()
            document = workspace.document()
            self._load_reviews(document)
            keys = list(document.selected or workspace._frames)
            key_numbers = {key: number for number, key in enumerate(keys)}
            frames = []
            for key in keys:
                source, page = workspace._frames[key]
                if source.kind == "image":
                    if source.dimensions:
                        width, height = source.dimensions[page]
                    else:
                        with Image.open(source.path) as opened:
                            opened.seek(page)
                            width, height = opened.size
                else:
                    width, height = document.dimensions.get(key, (0, 0))
                    if not width and source.dimensions:
                        width, height = source.dimensions[0]
                    if not width:
                        with Image.open(workspace._ensure_frame(key)) as opened:
                            width, height = opened.size
                frames.append({"name": key, "width": width, "height": height, "related_files": 0})
            labels, specs = [], {}
            definitions = document.root.findall("meta/task/labels/label") or document.root.findall("meta/project/labels/label")
            for identifier, definition in enumerate(definitions):
                attributes = []
                for attribute in definition.findall("attributes/attribute"):
                    name = attribute.findtext("name")
                    if not name:
                        legacy = re.fullmatch(r"([@~])(\w+)=([^:]+):(.*)", attribute.text or "")
                        if legacy is None:
                            continue
                        name = legacy[3]
                        attribute = ET.fromstring("<attribute />")
                        for field, value in (("name", name), ("input_type", legacy[2]), ("mutable", str(legacy[1] == "~")), ("values", legacy[4].replace(",", "\n")), ("default_value", legacy[4].split(",")[0])):
                            ET.SubElement(attribute, field).text = value
                    spec = {"id": len(specs), "name": name, "mutable": attribute.findtext("mutable", "False").lower() == "true",
                            "input_type": attribute.findtext("input_type", "text"), "default_value": attribute.findtext("default_value", ""),
                            "values": attribute.findtext("values", "").splitlines()}
                    attributes.append(spec)
                    specs[spec["id"]] = spec
                labels.append({"id": identifier, "name": definition.findtext("name"), "type": definition.findtext("type", "any"), "color": definition.findtext("color", ""), "attributes": attributes})
            label_ids = {label["name"]: label["id"] for label in labels}
            attributes = {(label["id"], spec["name"]): spec["id"] for label in labels for spec in label["attributes"]}
            native_numbers = list(document.selected.values()) if document.selected else list(range(len(keys)))
            number_map = {native: index for index, native in enumerate(native_numbers)}
            step = document.step
            def frame_index(number):
                number = int(number)
                if number == native_numbers[-1] + step:
                    return len(keys)
                if number not in number_map:
                    raise WorkspaceError(f"XML frame {number} is outside the selected task")
                return number_map[number]
            nodes = {}
            counter = 0
            def shape(element, frame, label=None, tracked=False):
                nonlocal counter
                label_id = label_ids[label or element.get("label")]
                node = {"type": TYPES[element.tag], "frame": frame, "label_id": label_id,
                        "group": int(element.get("group_id", "0")), "source": element.get("source", "manual"),
                        "rotation": float(element.get("rotation", "0")), "occluded": element.get("occluded", "0") == "1",
                        "outside": element.get("outside", "0") == "1", "z_order": int(element.get("z_order", "0")),
                        "attributes": [{"spec_id": attributes[(label_id, item.get("name"))], "value": item.text or ""}
                                       for item in element.findall("attribute") if (label_id, item.get("name")) in attributes], "elements": []}
                if element.tag == "box":
                    node["points"] = [float(element.get(key)) for key in ("xtl", "ytl", "xbr", "ybr")]
                elif element.tag in ("polygon", "polyline", "points"):
                    node["points"] = [float(value) for value in element.get("points", "").replace(";", ",").split(",") if value.strip()]
                elif element.tag == "ellipse":
                    cx, cy, rx, ry = [float(element.get(key)) for key in ("cx", "cy", "rx", "ry")]
                    node["points"] = [cx, cy, cx + rx, cy - ry]
                elif element.tag == "cuboid":
                    node["points"] = [float(element.get(key)) for key in CUBE]
                elif element.tag == "mask":
                    left, top, width, height = [int(element.get(key)) for key in ("left", "top", "width", "height")]
                    node["points"] = [int(value) for value in element.get("rle", "").split(",") if value.strip()] + [left, top, left + width - 1, top + height - 1]
                elif element.tag == "skeleton":
                    node["elements"] = [shape(child, frame) for child in element if child.tag in TYPES]
                counter += 1
                node["id"] = counter
                nodes[("track-shape" if tracked else "tags" if element.tag == "tag" else "shapes", f"id:{counter}")] = copy.deepcopy(element)
                return node
            annotations = {"version": 0, "shapes": [], "tracks": [], "tags": []}
            for key, image in document.images.items():
                for element in image:
                    if element.tag in TYPES:
                        item = shape(element, key_numbers[key])
                        annotations["tags" if element.tag == "tag" else "shapes"].append(item)
            for identifier, (track, numbers, elements) in document.tracks.items():
                shapes = [shape(element, frame_index(number), track.get("label"), tracked=True) for number, element in zip(numbers, elements) if element.get("keyframe", "1") != "0"]
                if not shapes:
                    raise WorkspaceError("A CVAT track needs at least one keyframe")
                immutable = [item for item in shapes[0]["attributes"] if not specs[item["spec_id"]]["mutable"]]
                for item in shapes:
                    item["attributes"] = [attribute for attribute in item["attributes"] if specs[attribute["spec_id"]]["mutable"]]
                item = {"id": int(identifier), "label_id": label_ids[track.get("label")], "frame": shapes[0]["frame"],
                        "group": int(track.get("group_id", "0")), "source": track.get("source", "manual"),
                        "attributes": immutable, "shapes": shapes, "elements": []}
                annotations["tracks"].append(item)
                nodes[("tracks", f"id:{identifier}")] = copy.deepcopy(track)
            view_id = str(uuid.uuid4())
            largest_frame = max(frame["width"] * frame["height"] * 4 for frame in frames)
            chunk_size = min(32, max(1, (32 * 1024 * 1024) // largest_frame))
            buffering = buffer_plan(workspace, frames, chunk_size)
            chunk_size = buffering['chunk_size']
            self.views[view_id] = {"revision": digest(document.raw), "keys": keys, "numbers": native_numbers, "step": step,
                                   "labels": labels, "specs": specs, "nodes": nodes, "frames": frames, "shape_tracks": {}, "chunk_size": chunk_size, "buffer": buffering}
            while len(self.views) > 16:
                expired = next(iter(self.views))
                self.chunks.release(expired)
                del self.views[expired]
            task = document.root.find("meta/task")
            mode = task.findtext("mode", "annotation") if task is not None else "annotation"
            return {"view": view_id, "revision": digest(document.raw), "name": workspace.source_root.name, "mode": workspace.mode,
                    "frames": keys, "annotations": annotations, "user": LOCAL_USER, "issues": copy.deepcopy(self.issues), "buffer": buffering,
                    "settings": getattr(workspace, 'settings', {'frame_step': 10, 'pixelated': False, 'dataset_format': 'auto', 'labels_format': 'auto'}),
                    "job": {"id": 1, "task_id": 1, "task_name": workspace.source_root.name, "project_id": None, "guide_id": None,
                            "type": "annotation", "state": "in progress", "stage": "annotation", "dimension": "2d", "media_type": "image",
                            "start_frame": 0, "stop_frame": len(keys) - 1, "frame_count": len(keys), "mode": mode,
                            "data_chunk_size": chunk_size, "data_compressed_chunk_type": "imageset", "labels": labels},
                    "meta": {"chunk_size": chunk_size, "deleted_frames": {}, "included_frames": None, "frame_filter": "", "frames": frames,
                             "image_quality": 100, "size": len(keys), "start_frame": 0, "stop_frame": len(keys) - 1,
                             "chunks_updated_date": "", "storage": "local", "cloud_storage_id": None, "chapters": []}}

    def chunk_view(self, identifier):
        with self.workspace.lock:
            view = self.views.get(identifier)
            if view is None:
                raise WorkspaceError("This frame view expired. Reload CVAT.")
            return view

    def save(self, payload):
        with self.workspace.lock:
            self.workspace._check_sources()
            document = self.workspace.document()
            expected = digest(document.raw)
            if payload.get("revision") != expected:
                raise Conflict("XML changed on disk. Reload CVAT before saving; your edits remain in the browser.")
            view = self.views.get(payload.get("view"))
            if view is None or view["revision"] != expected:
                raise Conflict("This annotation view is stale. Reload CVAT before saving.")
            if payload.get("unchanged"):
                return {"revision": expected, "changed": False}
            collection = payload.get("annotations")
            if not isinstance(collection, dict) or any(not isinstance(collection.get(key), list) for key in ("shapes", "tags", "tracks")):
                raise WorkspaceError("Expected a CVAT annotation collection")
            root = copy.deepcopy(document.root)
            original_images = {key: copy.deepcopy(image) for key, image in document.images.items()}
            for element in list(root):
                if element.tag in ("image", "track"):
                    root.remove(element)
            fresh_nodes = {}
            labels = {label["id"]: label for label in view["labels"]}
            def identity(item):
                return f"id:{item['id']}" if item.get("id") is not None else f"client:{item.get('clientID')}"
            def write_shape(item, kind, label_id=None, inherited=(), prior_label=None):
                label_id = item.get("label_id", label_id)
                if label_id not in labels:
                    raise WorkspaceError("Unknown CVAT label; reload class definitions")
                tag = next((tag for tag, name in TYPES.items() if name == item.get("type")), "tag" if kind == "tags" else None)
                if tag is None:
                    raise WorkspaceError(f"Unsupported CVAT shape: {item.get('type')}")
                old = view["nodes"].get((kind, identity(item)))
                element = copy.deepcopy(old) if old is not None and old.tag == tag else ET.Element(tag)
                label = labels[label_id]
                previous_label = element.get("label", prior_label)
                changed_label = previous_label not in (None, label["name"])
                element.set("label", label["name"])
                if item.get("source") and (item["source"] != "manual" or element.get("source")):
                    element.set("source", item["source"])
                if item.get("group"):
                    element.set("group_id", str(int(item["group"])))
                else:
                    element.attrib.pop("group_id", None)
                if tag != "tag":
                    element.set("occluded", str(int(bool(item.get("occluded")))))
                    element.set("z_order", str(int(item.get("z_order", 0))))
                    rotation = float(item.get("rotation", 0))
                    if not math.isfinite(rotation):
                        raise WorkspaceError("Rotation must be finite")
                    if rotation:
                        element.set("rotation", str(rotation))
                    else:
                        element.attrib.pop("rotation", None)
                    points = item.get("points", [])
                    if not isinstance(points, list) or any(type(value) not in (int, float) or not math.isfinite(value) for value in points):
                        raise WorkspaceError("Shape points must be finite numbers")
                    if tag in ("box", "ellipse") and len(points) != 4:
                        raise WorkspaceError("Boxes and ellipses require four coordinates")
                    if tag == "box" and (points[2] <= points[0] or points[3] <= points[1]):
                        raise WorkspaceError("Boxes need positive width and height")
                    if tag == "ellipse" and (points[2] == points[0] or points[3] == points[1]):
                        raise WorkspaceError("Ellipses need positive radii")
                    if tag == "box":
                        element.attrib.update(dict(zip(("xtl", "ytl", "xbr", "ybr"), map(str, points))))
                    elif tag in ("polygon", "polyline", "points"):
                        if len(points) % 2 or len(points) < {"polygon": 6, "polyline": 4, "points": 2}[tag]:
                            raise WorkspaceError("Invalid points for this shape")
                        element.set("points", ";".join(f"{x},{y}" for x, y in zip(points[::2], points[1::2])))
                    elif tag == "ellipse":
                        element.attrib.update(dict(zip(("cx", "cy", "rx", "ry"), map(str, (points[0], points[1], abs(points[2] - points[0]), abs(points[3] - points[1]))))))
                    elif tag == "cuboid":
                        if len(points) != 16:
                            raise WorkspaceError("A 2D cuboid needs sixteen coordinates")
                        element.attrib.update(dict(zip(CUBE, map(str, points))))
                    elif tag == "mask":
                        if len(points) < 5 or any(int(value) != value for value in points):
                            raise WorkspaceError("Invalid mask encoding")
                        left, top, right, bottom = points[-4:]
                        element.attrib.update({"rle": ", ".join(map(str, points[:-4])), "left": str(left), "top": str(top), "width": str(right - left + 1), "height": str(bottom - top + 1)})
                values = [*inherited, *item.get("attributes", [])]
                known_names = {spec["name"] for spec in label["attributes"]}
                for child in list(element):
                    if child.tag in TYPES or (child.tag == "attribute" and (changed_label or child.get("name") in known_names)):
                        element.remove(child)
                for value in values:
                    spec = view["specs"].get(value.get("spec_id"))
                    if spec is None or spec["name"] not in known_names:
                        raise WorkspaceError("Unknown label attribute")
                    ET.SubElement(element, "attribute", {"name": spec["name"]}).text = str(value.get("value", ""))
                for child in item.get("elements", []):
                    element.append(write_shape(child, "shapes"))
                fresh_nodes[(kind, identity(item))] = copy.deepcopy(element)
                return element
            images = {}
            video_mode = document.root.findtext("meta/task/mode") == "interpolation"
            next_track = max((int(track.get("id")) for track in document.root.findall("track")), default=-1) + 1
            for kind in ("shapes", "tags"):
                for item in collection[kind]:
                    frame = item.get("frame")
                    if type(frame) is not int or not 0 <= frame < len(view["keys"]):
                        raise WorkspaceError("Annotation frame is outside this folder")
                    key = view["keys"][frame]
                    if video_mode and kind == "shapes":
                        shape_key = identity(item)
                        identifier = view["shape_tracks"].get(shape_key)
                        if identifier is None:
                            identifier = next_track
                            next_track += 1
                            view["shape_tracks"][shape_key] = identifier
                        track = ET.Element("track", {"id": str(identifier), "label": labels[item["label_id"]]["name"]})
                        if item.get("group"):
                            track.set("group_id", str(int(item["group"])))
                        element = write_shape(item, kind)
                        element.attrib.pop("label", None)
                        element.set("frame", str(view["numbers"][frame]))
                        element.set("outside", "0")
                        element.set("keyframe", "1")
                        track.append(element)
                        outside = copy.deepcopy(element)
                        outside.set("frame", str(view["numbers"][frame] + view["step"]))
                        outside.set("outside", "1")
                        track.append(outside)
                        root.append(track)
                        continue
                    if frame not in images:
                        image = copy.deepcopy(original_images.get(key)) if key in original_images else ET.Element("image", {"id": str(frame), "name": key})
                        for child in list(image):
                            if child.tag in TYPES:
                                image.remove(child)
                        image.set("width", str(view["frames"][frame]["width"]))
                        image.set("height", str(view["frames"][frame]["height"]))
                        images[frame] = image
                    images[frame].append(write_shape(item, kind))
            # Keep explicit negative image entries as well as newly annotated images.
            for key, old in original_images.items():
                if video_mode:
                    continue
                frame = view["keys"].index(key)
                if frame not in images:
                    for child in list(old):
                        if child.tag in TYPES:
                            old.remove(child)
                    images[frame] = old
            root.extend(images[frame] for frame in sorted(images))
            used = set()
            for item in collection["tracks"]:
                old = view["nodes"].get(("tracks", identity(item)))
                identifier = int(old.get("id")) if old is not None else next_track
                if old is None:
                    next_track += 1
                if identifier in used:
                    raise WorkspaceError("Duplicate track identity")
                used.add(identifier)
                track = copy.deepcopy(old) if old is not None else ET.Element("track")
                track.set("id", str(identifier))
                track.set("label", labels[item["label_id"]]["name"])
                if item.get("source") and (item["source"] != "manual" or track.get("source")):
                    track.set("source", item["source"])
                if item.get("group"):
                    track.set("group_id", str(int(item["group"])))
                else:
                    track.attrib.pop("group_id", None)
                for child in list(track):
                    if child.tag in TYPES:
                        track.remove(child)
                for shape in item["shapes"]:
                    frame = shape["frame"]
                    if type(frame) is not int or not 0 <= frame <= len(view["keys"]):
                        raise WorkspaceError("Track frame is outside this folder")
                    native = view["numbers"][frame] if frame < len(view["numbers"]) else view["numbers"][-1] + view["step"]
                    element = write_shape(shape, "track-shape", item["label_id"], item.get("attributes", []), old.get("label") if old is not None else None)
                    element.attrib.pop("label", None)
                    element.set("frame", str(native))
                    element.set("outside", str(int(bool(shape.get("outside")))))
                    element.set("keyframe", "1")
                    track.append(element)
                fresh_nodes[("tracks", identity(item))] = copy.deepcopy(track)
                root.append(track)
            timestamp = datetime.now(timezone.utc).isoformat(sep=" ")
            for field in ("meta/task/updated", "meta/dumped"):
                element = root.find(field)
                if element is not None:
                    element.text = timestamp
            output = serialize_xml(root)
            self.workspace._check_sources()
            self.workspace.write_annotations(output, expected)
            view.update(revision=digest(output), nodes=fresh_nodes)
            return {"revision": digest(output), "changed": True}

    def _review_keys(self):
        return list(self.workspace.document().selected or self.workspace._frames)

    def _load_reviews(self, document=None):
        if document is None and (self.review_file.path.is_file() or getattr(self.workspace, 'remote', None)):
            document = self.workspace.document()
        keys = list(document.selected or self.workspace._frames) if document else list(self.workspace._frames)
        self.issues, self.issue_counter, self.comment_counter = self.review_file.load(keys)

    @contextmanager
    def _review_change(self):
        self._load_reviews()
        previous = copy.deepcopy(self.issues), self.issue_counter, self.comment_counter
        try:
            yield
            self.review_file.save(self._review_keys(), self.issues, self.issue_counter, self.comment_counter)
        except BaseException:
            self.issues, self.issue_counter, self.comment_counter = previous
            raise

    def issue(self, method, identifier, payload):
        with self.workspace.lock, self._review_change():
            if method == "POST":
                keys = list(self.workspace.document().selected or self.workspace._frames)
                frame = payload.get("frame")
                position = payload.get("position")
                message = payload.get("message")
                if type(frame) is not int or not 0 <= frame < len(keys):
                    raise WorkspaceError("Review frame is outside this folder")
                if not isinstance(position, list) or len(position) < 4 or len(position) % 2 or any(type(value) not in (int, float) or not math.isfinite(value) for value in position):
                    raise WorkspaceError("Review regions need finite coordinate pairs")
                if not isinstance(message, str) or not message.strip() or len(message) > 20000:
                    raise WorkspaceError("Review comments need 1 to 20000 characters")
                self.issue_counter += 1
                self.comment_counter += 1
                issue = {"job": 1, "frame": frame, "position": position, "id": self.issue_counter, "owner": LOCAL_USER, "resolved": False,
                         "created_date": datetime.now(timezone.utc).isoformat(), "comments": [{"id": self.comment_counter, "message": message, "owner": LOCAL_USER}]}
                self.issues.append(issue)
                return copy.deepcopy(issue)
            issue = next((issue for issue in self.issues if issue["id"] == identifier), None)
            if issue is None:
                raise WorkspaceError("Unknown review finding")
            if method == "DELETE":
                self.issues.remove(issue)
                return {}
            issue["resolved"] = bool(payload.get("resolved", issue["resolved"]))
            return copy.deepcopy(issue)

    def feedback(self):
        keys = list(self.workspace.document().selected or self.workspace._frames)
        lines = ["## Annotation review", ""]
        for issue in self.issues:
            if not issue["resolved"]:
                lines.append(f"- <code>{html.escape(keys[issue['frame']])}</code>")
                for comment in issue["comments"]:
                    lines.extend(f"  > {line}" for line in comment["message"].splitlines())
                if issue.get("position"):
                    lines.append("  - Region (pixels): " + ", ".join(str(point) for point in issue["position"]))
                lines.append("")
        return "\n".join(lines)

    def comment(self, payload):
        with self.workspace.lock, self._review_change():
            issue = next((issue for issue in self.issues if issue["id"] == payload.get("issue")), None)
            if issue is None:
                raise WorkspaceError("Unknown review finding")
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip() or len(message) > 20000:
                raise WorkspaceError("Review comments need 1 to 20000 characters")
            self.comment_counter += 1
            comment = {"id": self.comment_counter,
                       "message": str(payload.get("message", "")), "owner": LOCAL_USER,
                       "created_date": datetime.now(timezone.utc).isoformat()}
            issue["comments"].append(comment)
            return copy.deepcopy(comment)
