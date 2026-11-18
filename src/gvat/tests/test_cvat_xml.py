import io
import os
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gvat_local.cli import parser, prepare
from gvat_local.cvat_xml import CvatDocument, CvatXmlWorkspace
from gvat_local.workspace import Conflict, WorkspaceError
from video_fixtures import make_video


class CvatXmlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "media"
        self.data.mkdir()
        self.xml = self.data / "annotations.xml"
        self.environment = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def images(self):
        for name in ("one.png", "two.png"):
            Image.new("RGB", (64, 48), "green").save(self.data / name)
        raw = CvatDocument.create(["dog", "cat"], image_count=2).decode()
        self.xml.write_text(raw.replace("</annotations>", '''<!--retain comment-->
<image id="0" name="one.png" width="64" height="48">
  <box label="dog" xtl="8" ytl="6" xbr="40" ybr="30" occluded="1" group_id="7" source="auto">
    <attribute name="pose">running &amp; playing</attribute>
  </box>
</image></annotations>'''))
        return CvatXmlWorkspace(self.data, self.xml)

    def video(self, step=1):
        make_video(self.data / "source.mp4", [Image.new("RGB", (64, 48), "green") for _ in range(5)])
        raw = CvatDocument.create(["dog", "cat"], (64, 48, 5)).decode()
        if step > 1:
            raw = raw.replace("<labels>", f"<frame_filter>step={step}</frame_filter><labels>")
            raw = raw.replace("<size>5</size>", "<size>3</size>")
        self.xml.write_text(raw.replace("</annotations>", '''<track id="7" label="dog" source="auto" group_id="2">
  <box frame="0" xtl="4" ytl="4" xbr="24" ybr="24" outside="0" occluded="1" keyframe="1"><attribute name="pose">run</attribute></box>
  <box frame="4" xtl="12" ytl="4" xbr="32" ybr="24" outside="0" occluded="0" keyframe="1"><attribute name="pose">run</attribute></box>
</track></annotations>'''))
        return CvatXmlWorkspace(self.data, self.xml)

    def key(self, number):
        return f"source.mp4/frame_{number:06}.png"

    def test_image_round_trip_preserves_metadata_attributes_noop_bytes_and_other_frames(self):
        workspace = self.images()
        original = self.xml.read_bytes()
        mtime = self.xml.stat().st_mtime_ns
        frame = workspace.frame("one.png")
        self.assertEqual(frame["annotations"][0]["class_id"], 0)
        self.assertFalse(workspace.save("one.png", frame)["changed"])
        self.assertEqual(self.xml.read_bytes(), original)
        self.assertEqual(self.xml.stat().st_mtime_ns, mtime)
        frame["annotations"][0]["cx"] += .05
        result = workspace.save("one.png", frame)
        self.assertTrue(result["changed"])
        updated = workspace.frame("one.png")
        self.assertEqual(updated["classes_revision"], frame["classes_revision"])
        box = ET.parse(self.xml).find("image/box")
        self.assertEqual(box.get("occluded"), "1")
        self.assertEqual(box.get("group_id"), "7")
        self.assertEqual(box.findtext("attribute"), "running & playing")
        self.assertIn(b"<!--retain comment-->", self.xml.read_bytes())
        self.assertFalse(workspace.frame("two.png")["labeled"])
        self.assertFalse((self.data / "one.txt").exists())

    def test_new_image_and_empty_annotations_save_in_same_xml(self):
        workspace = self.images()
        frame = workspace.frame("two.png")
        self.assertFalse(frame["labeled"])
        workspace.save("two.png", frame)
        self.assertTrue(workspace.frame("two.png")["labeled"])
        self.assertEqual(ET.parse(self.xml).findall("image")[1].get("name"), "two.png")
        frame = workspace.frame("one.png")
        workspace.save("one.png", {**frame, "annotations": []})
        self.assertEqual(workspace.frame("one.png")["annotations"], [])

    def test_xml_and_source_changes_reject_stale_writes(self):
        workspace = self.images()
        frame = workspace.frame("one.png")
        self.xml.write_text(self.xml.read_text().replace("source=\"auto\"", "source=\"manual\""))
        with self.assertRaises(Conflict):
            workspace.save("one.png", frame)
        frame = workspace.frame("one.png")
        Image.new("RGB", (64, 48), "blue").save(self.data / "one.png")
        with self.assertRaises(Conflict):
            workspace.save("one.png", frame)

    def test_missing_media_dimensions_unsupported_shapes_and_entities_fail_without_changes(self):
        self.images()
        original = self.xml.read_text()
        for changed in (original.replace('name="one.png"', 'name="missing.png"'),
                        original.replace('width="64"', 'width="640"'),
                        original.replace('<box label=', '<polygon label=').replace('</box>', '</polygon>'),
                        original.replace('occluded="1"', 'occluded="1" rotation="30"'),
                        '<!DOCTYPE annotations [<!ENTITY bad "value">]>' + original):
            self.xml.write_text(changed)
            with self.assertRaises(WorkspaceError):
                CvatXmlWorkspace(self.data, self.xml).frame("one.png")
            self.assertEqual(self.xml.read_text(), changed)

    def test_interpolated_video_edits_pin_neighbors_and_preserve_track_attributes(self):
        workspace = self.video()
        previous = [workspace.frame(self.key(number))["annotations"] for number in range(1, 6)]
        frame = workspace.frame(self.key(3))
        self.assertAlmostEqual(frame["annotations"][0]["cx"], 18 / 64)
        frame["annotations"][0]["cx"] += .1
        workspace.save(self.key(3), frame)
        self.assertAlmostEqual(workspace.frame(self.key(3))["annotations"][0]["cx"], 18 / 64 + .1)
        for number in (1, 2, 4, 5):
            self.assertEqual(workspace.frame(self.key(number))["annotations"], previous[number - 1])
        track = ET.parse(self.xml).find("track")
        self.assertEqual(track.get("id"), "7")
        self.assertEqual(track.get("group_id"), "2")
        self.assertEqual(track.find("box[@frame='2']/attribute").text, "run")

    def test_track_deletion_reclassification_and_new_box_affect_only_selected_frame(self):
        workspace = self.video()
        frame = workspace.frame(self.key(3))
        workspace.save(self.key(3), {**frame, "annotations": []})
        self.assertEqual(workspace.frame(self.key(3))["annotations"], [])
        self.assertEqual(len(workspace.frame(self.key(2))["annotations"]), 1)
        self.assertEqual(len(workspace.frame(self.key(4))["annotations"]), 1)
        frame = workspace.frame(self.key(4))
        frame["annotations"][0]["class_id"] = 1
        workspace.save(self.key(4), frame)
        self.assertEqual([box["class_id"] for box in workspace.frame(self.key(4))["annotations"]], [1])
        self.assertEqual([box["class_id"] for box in workspace.frame(self.key(5))["annotations"]], [0])
        frame = workspace.frame(self.key(1))
        new = {"class_id": 1, "cx": .7, "cy": .7, "width": .2, "height": .2}
        result = workspace.save(self.key(1), {**frame, "annotations": [*frame["annotations"], new]})
        self.assertEqual(len(result["annotations"]), 2)
        self.assertTrue(all(box.get("xml_id") for box in result["annotations"]))
        self.assertEqual(len(workspace.frame(self.key(2))["annotations"]), 1)

    def test_video_frame_filter_uses_original_native_indices(self):
        workspace = self.video(step=2)
        self.assertEqual([image["path"] for image in workspace.manifest()["images"]], [self.key(n) for n in (1, 3, 5)])
        frame = workspace.frame(self.key(3))
        workspace.save(self.key(3), {**frame, "annotations": []})
        self.assertEqual(len(workspace.frame(self.key(1))["annotations"]), 1)
        self.assertEqual(len(workspace.frame(self.key(5))["annotations"]), 1)
        with self.assertRaises(WorkspaceError):
            workspace.frame(self.key(2))

    def test_image_export_of_video_frames_is_zero_based(self):
        workspace = self.video()
        self.xml.write_bytes(CvatDocument.create(["dog"], image_count=3).replace(b"</annotations>", b'<image id="0" name="frame_000000.png" width="64" height="48"><box label="dog" xtl="4" ytl="4" xbr="24" ybr="24" /></image></annotations>'))
        workspace = CvatXmlWorkspace(self.data, self.xml)
        self.assertEqual(len(workspace.frame(self.key(1))["annotations"]), 1)
        self.assertEqual(workspace.frame(self.key(2))["annotations"], [])
        frame = workspace.frame(self.key(2))
        workspace.save(self.key(2), frame)
        image = ET.parse(self.xml).findall("image")[1]
        self.assertEqual(image.get("name"), "frame_000001.png")
        self.assertEqual(image.get("id"), "1")
        self.assertTrue(workspace.frame(self.key(2))["labeled"])

    def test_cli_detects_existing_xml_and_creates_missing_xml_with_explicit_classes(self):
        self.images()
        with patch("sys.stdout", new_callable=io.StringIO):
            workspace = prepare(parser().parse_args(["--data_path", str(self.data), "--mode", "review"]))
        self.assertIsInstance(workspace, CvatXmlWorkspace)
        self.assertFalse((self.data / "annotations").exists())
        target = self.data / "new.xml"
        with patch("sys.stdout", new_callable=io.StringIO):
            created = prepare(parser().parse_args(["--data_path", str(self.data), "--labels", str(target), "--class-names", "dog,cat", "--yes"]))
        self.assertEqual(created.frame("one.png")["annotations"], [])
        self.assertEqual(created.mode, "annotate")
        self.assertTrue(target.is_file())

    def test_new_video_xml_uses_native_tracks_and_review_never_creates_sidecars(self):
        make_video(self.data / "source.mp4")
        with patch("sys.stdout", new_callable=io.StringIO):
            workspace = prepare(parser().parse_args(["--data_path", str(self.data), "--labels", str(self.xml), "--class-names", "dog", "--yes", "--mode", "annotate"]))
        frame = workspace.frame(self.key(2))
        workspace.save(self.key(2), {**frame, "annotations": [{"class_id": 0, "cx": .5, "cy": .5, "width": .5, "height": .5}]})
        shape = ET.parse(self.xml).find("track/box")
        self.assertEqual(shape.get("frame"), "1")
        self.assertEqual(len(workspace.frame(self.key(1))["annotations"]), 0)
        before = {path.name: path.read_bytes() for path in self.data.iterdir() if path.is_file()}
        workspace.save_review({"revision": "missing", "issues": [{"id": str(uuid.uuid4()), "image": self.key(2), "status": "open", "comment": "Tighten box", "region": None}]})
        self.assertIn("Tighten box", workspace.markdown())
        self.assertEqual(CvatXmlWorkspace(self.data, self.xml).review()["issues"], [])
        self.assertFalse(any(path.is_dir() for path in self.data.iterdir()))
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.data.iterdir() if path.is_file()})


if __name__ == "__main__":
    unittest.main()
