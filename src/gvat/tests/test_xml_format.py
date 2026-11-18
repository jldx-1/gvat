"""Conformance checks for CVAT XML 1.1 and legacy 1.0."""

import io
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gvat_local.cli import parser, prepare
from gvat_local.cvat_xml import CvatDocument, CvatXmlWorkspace, serialize_xml
from video_fixtures import make_video


class XmlFormatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "images"
        self.data.mkdir()
        for name in ("a.png", "b.png", "c.png"):
            Image.new("RGB", (64, 48), "green").save(self.data / name)
        self.labels = self.root / "labels.txt"
        self.labels.write_text("dog\n")
        self.environment = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def new_workspace(self, data):
        with patch("sys.stdout", new_callable=io.StringIO):
            return prepare(parser().parse_args(["--data_path", str(data), "--labels", str(self.labels), "--yes"]))

    def assert_common_metadata(self, raw, mode, count):
        self.assertTrue(raw.startswith(b'<?xml version="1.0" encoding="utf-8"?>\n'))
        root = ET.fromstring(raw)
        self.assertEqual(root.tag, "annotations")
        self.assertEqual(root.findtext("version"), "1.1")
        task = root.find("meta/task")
        required = {"id", "name", "size", "mode", "overlap", "bugtracker", "flipped", "created", "updated", "labels", "segments", "owner"}
        self.assertTrue(required.issubset({item.tag for item in task}))
        self.assertGreaterEqual(int(task.findtext("id")), 0)
        self.assertEqual(int(task.findtext("size")), count)
        self.assertEqual(task.findtext("mode"), mode)
        self.assertEqual(task.findtext("flipped"), "False")
        self.assertEqual(task.findtext("labels/label/name"), "dog")
        self.assertIsNotNone(task.find("labels/label/attributes"))
        self.assertIsNotNone(task.find("owner/username"))
        self.assertIsNotNone(task.find("owner/email"))
        self.assertEqual(int(task.findtext("segments/segment/start")), 0)
        self.assertEqual(int(task.findtext("segments/segment/stop")), count - 1)
        self.assertIsNotNone(task.find("segments/segment/url"))
        for value in (task.findtext("created"), task.findtext("updated"), root.findtext("meta/dumped")):
            self.assertIsNotNone(datetime.fromisoformat(value).tzinfo)
        return root

    def test_new_image_xml_has_complete_11_metadata_and_lexical_image_ids(self):
        workspace = self.new_workspace(self.data)
        root = self.assert_common_metadata(workspace.xml_file.read_bytes(), "annotation", 3)
        self.assertIsNone(root.find("meta/task/original_size"))
        for name in ("c.png", "a.png"):
            frame = workspace.frame(name)
            workspace.save(name, {**frame, "annotations": [{"class_id": 0, "cx": .5, "cy": .5, "width": .5, "height": .5}]})
        root = self.assert_common_metadata(workspace.xml_file.read_bytes(), "annotation", 3)
        self.assertEqual({item.get("name"): item.get("id") for item in root.findall("image")}, {"c.png": "2", "a.png": "0"})
        for image in root.findall("image"):
            self.assertEqual((image.get("width"), image.get("height")), ("64", "48"))
            box = image.find("box")
            self.assertEqual(set(box.attrib), {"label", "xtl", "ytl", "xbr", "ybr", "occluded", "z_order"})
            self.assertEqual([float(box.get(key)) for key in ("xtl", "ytl", "xbr", "ybr")], [16, 12, 48, 36])

    def test_new_video_xml_has_original_size_and_native_track_fields(self):
        data = self.root / "video"
        make_video(data / "source.mp4")
        workspace = self.new_workspace(data)
        root = self.assert_common_metadata(workspace.xml_file.read_bytes(), "interpolation", 3)
        self.assertEqual(root.findtext("meta/task/original_size/width"), "64")
        self.assertEqual(root.findtext("meta/task/original_size/height"), "48")
        key = "source.mp4/frame_000002.png"
        frame = workspace.frame(key)
        workspace.save(key, {**frame, "annotations": [{"class_id": 0, "cx": .5, "cy": .5, "width": .5, "height": .5}]})
        root = self.assert_common_metadata(workspace.xml_file.read_bytes(), "interpolation", 3)
        track = root.find("track")
        self.assertEqual(set(track.attrib), {"id", "label"})
        first, outside = track.findall("box")
        expected = {"frame", "xtl", "ytl", "xbr", "ybr", "outside", "occluded", "keyframe"}
        self.assertEqual(set(first.attrib), expected)
        self.assertEqual((first.get("frame"), first.get("outside"), first.get("keyframe")), ("1", "0", "1"))
        self.assertEqual((outside.get("frame"), outside.get("outside")), ("2", "1"))
        self.assertEqual(sorted(path.name for path in data.iterdir()), ["annotations.xml", "source.mp4"])

    def test_10_images_without_dimensions_retain_legacy_attribute_definitions(self):
        root = ET.fromstring(CvatDocument.create(["dog"], image_count=3))
        root.find("version").text = "1.0"
        task = root.find("meta/task")
        task.remove(task.find("flipped"))
        ET.SubElement(task.find("labels/label/attributes"), "attribute").text = "@select=model:a,b,c"
        image = ET.SubElement(root, "image", {"id": "0", "name": "a.png"})
        box = ET.SubElement(image, "box", {"label": "dog", "xtl": "8", "ytl": "6", "xbr": "40", "ybr": "30", "occluded": "1"})
        ET.SubElement(box, "attribute", {"name": "model"}).text = "a"
        xml = self.data / "annotations.xml"
        xml.write_bytes(serialize_xml(root))
        workspace = CvatXmlWorkspace(self.data, xml)
        frame = workspace.frame("a.png")
        self.assertEqual((frame["width"], frame["height"]), (64, 48))
        frame["annotations"][0]["cx"] += .05
        workspace.save("a.png", frame)
        root = ET.parse(xml).getroot()
        self.assertEqual(root.findtext("version"), "1.0")
        self.assertEqual(root.findtext("meta/task/labels/label/attributes/attribute"), "@select=model:a,b,c")
        self.assertEqual(root.findtext("image/box/attribute"), "a")
        self.assertIsNone(root.find("image").get("width"))

    def test_10_video_without_original_size_reads_dimensions_from_source(self):
        data = self.root / "legacy-video"
        make_video(data / "source.mp4")
        root = ET.fromstring(CvatDocument.create(["dog"], (64, 48, 3)))
        root.find("version").text = "1.0"
        task = root.find("meta/task")
        task.remove(task.find("original_size"))
        task.remove(task.find("flipped"))
        track = ET.SubElement(root, "track", {"id": "4", "label": "dog"})
        ET.SubElement(track, "box", {"frame": "0", "xtl": "8", "ytl": "6", "xbr": "40", "ybr": "30", "outside": "0", "occluded": "0", "keyframe": "1"})
        xml = data / "annotations.xml"
        xml.write_bytes(serialize_xml(root))
        workspace = CvatXmlWorkspace(data, xml)
        frame = workspace.frame("source.mp4/frame_000002.png")
        self.assertEqual((frame["width"], frame["height"]), (64, 48))
        self.assertEqual(frame["annotations"][0]["xml_id"], "track:4")
        frame["annotations"][0]["cx"] += .05
        workspace.save(frame["path"], frame)
        root = ET.parse(xml).getroot()
        self.assertEqual(root.findtext("version"), "1.0")
        self.assertIsNone(root.find("meta/task/original_size"))


if __name__ == "__main__":
    unittest.main()
