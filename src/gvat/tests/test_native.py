import copy
import os
import tempfile
from unittest.mock import patch
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gvat_local.cvat_xml import CvatDocument, CvatXmlWorkspace, serialize_xml
from gvat_local.native import NativeSession
from gvat_local.workspace import Conflict, WorkspaceError
from video_fixtures import make_video


class NativeCVATTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "media"
        self.data.mkdir()
        Image.new("RGB", (64, 48), "green").save(self.data / "one.png")
        self.xml = self.data / "annotations.xml"
        root = ET.fromstring(CvatDocument.create(["dog", "cat"], image_count=1))
        image = ET.SubElement(root, "image", {"id": "0", "name": "one.png", "width": "64", "height": "48"})
        box = ET.SubElement(image, "box", {"label": "dog", "xtl": "8", "ytl": "6", "xbr": "40", "ybr": "30", "occluded": "1", "group_id": "7"})
        ET.SubElement(box, "attribute", {"name": "uneditable-legacy-value"}).text = "keep"
        self.xml.write_bytes(serialize_xml(root))
        self.environment = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
        self.environment.start()
        self.workspace = CvatXmlWorkspace(self.data, self.xml, native_ui=True)
        self.session = NativeSession(self.workspace)

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def payload(self, snapshot):
        return {"view": snapshot["view"], "revision": snapshot["revision"], "annotations": copy.deepcopy(snapshot["annotations"])}

    def test_real_cvat_serialization_round_trip_noop_and_revision_conflicts(self):
        original = self.xml.read_bytes()
        modified = self.xml.stat().st_mtime_ns
        snapshot = self.session.snapshot()
        self.assertEqual(snapshot["annotations"]["shapes"][0]["points"], [8, 6, 40, 30])
        self.assertEqual(snapshot["job"]["dimension"], "2d")
        self.session.save({**self.payload(snapshot), "unchanged": True})
        self.assertEqual(self.xml.read_bytes(), original)
        self.assertEqual(self.xml.stat().st_mtime_ns, modified)
        payload = self.payload(snapshot)
        payload["annotations"]["shapes"][0]["points"][0] = 11
        self.session.save(payload)
        box = ET.parse(self.xml).find("image/box")
        self.assertEqual(float(box.get("xtl")), 11)
        self.assertEqual(box.get("group_id"), "7")
        self.assertEqual(box.findtext("attribute"), "keep")
        with self.assertRaises(Conflict):
            self.session.save(payload)
        self.assertEqual(sorted(path.name for path in self.data.iterdir()), ["annotations.xml", "one.png"])

    def test_native_polygon_points_and_rotation_persist_without_box_conversion(self):
        snapshot = self.session.snapshot()
        payload = self.payload(snapshot)
        payload["annotations"]["shapes"].append({
            "clientID": 99, "type": "polygon", "frame": 0, "label_id": 0, "group": 0,
            "points": [2, 2, 30, 2, 20, 25], "rotation": 0, "occluded": False,
            "outside": False, "z_order": 1, "attributes": [], "elements": [],
        })
        payload["annotations"]["shapes"][0]["rotation"] = 15
        self.session.save(payload)
        root = ET.parse(self.xml).getroot()
        self.assertEqual(root.find("image/polygon").get("points"), "2,2;30,2;20,25")
        self.assertEqual(float(root.find("image/box").get("rotation")), 15)
        opened = self.session.snapshot()["annotations"]["shapes"]
        self.assertEqual([shape["type"] for shape in opened], ["rectangle", "polygon"])
        self.assertEqual(opened[1]["points"], [2, 2, 30, 2, 20, 25])

    def test_invalid_native_geometry_does_not_replace_xml(self):
        snapshot = self.session.snapshot()
        payload = self.payload(snapshot)
        payload["annotations"]["shapes"][0]["points"] = [float("nan"), 0, 20, 20]
        original = self.xml.read_bytes()
        with self.assertRaises(WorkspaceError):
            self.session.save(payload)
        self.assertEqual(self.xml.read_bytes(), original)

    def test_native_reviews_persist_in_sample_and_copy_open_feedback(self):
        original = self.xml.read_bytes()
        issue = self.session.issue("POST", None, {"frame": 0, "job": 1, "position": [8, 6, 40, 30], "message": "Tighten dog box"})
        self.session.comment({"issue": issue["id"], "message": "Keep the paws inside"})
        self.assertIn("one.png", self.session.feedback())
        self.assertIn("Keep the paws inside", self.session.feedback())
        self.assertEqual(len(self.session.snapshot()["issues"]), 1)
        self.session.issue("PUT", issue["id"], {"resolved": True})
        self.assertNotIn("Tighten dog box", self.session.feedback())
        self.assertEqual(self.xml.read_bytes(), original)
        reopened = NativeSession(self.workspace)
        self.assertTrue(reopened.issues[0]['resolved'])
        self.assertEqual(reopened.issues[0]['comments'][1]['message'], 'Keep the paws inside')
        self.assertEqual(reopened.issues[0]['frame'], 0)
        self.assertIn('"image": "one.png"', (self.data / 'reviews.json').read_text())
        self.assertNotIn(str(self.data), (self.data / 'reviews.json').read_text())
        self.assertFalse(any(path.is_dir() for path in self.data.iterdir()))

    def test_invalid_review_file_is_reported_without_overwriting_it(self):
        review = self.data / 'reviews.json'
        review.write_text('{broken JSON')
        with self.assertRaises(WorkspaceError):
            NativeSession(self.workspace)
        self.assertEqual(review.read_text(), '{broken JSON')

    def test_failed_review_save_rolls_back_and_other_sessions_merge_existing_findings(self):
        with patch('gvat_local.reviews.atomic_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.session.issue('POST', None, {'frame': 0, 'position': [1, 2, 20, 30], 'message': 'First'})
        self.assertEqual(self.session.issues, [])
        first = self.session.issue('POST', None, {'frame': 0, 'position': [1, 2, 20, 30], 'message': 'First'})
        other = NativeSession(self.workspace)
        second = other.issue('POST', None, {'frame': 0, 'position': [1, 2, 20, 30], 'message': 'Second'})
        self.assertNotEqual(first['id'], second['id'])
        self.session.comment({'issue': first['id'], 'message': 'A reply'})
        self.assertEqual(len(self.session.snapshot()['issues']), 2)
        self.assertEqual(NativeSession(self.workspace).issues[0]['comments'][1]['message'], 'A reply')

    def test_native_track_identity_and_original_pixel_attributes_are_retained(self):
        (self.data / "one.png").unlink()
        make_video(self.data / "source.mp4")
        root = ET.fromstring(CvatDocument.create(["dog"], (64, 48, 3)))
        track = ET.SubElement(root, "track", {"id": "7", "label": "dog", "group_id": "2"})
        box = ET.SubElement(track, "box", {"frame": "0", "xtl": "8", "ytl": "6", "xbr": "40", "ybr": "30", "outside": "0", "occluded": "0", "keyframe": "1"})
        ET.SubElement(box, "attribute", {"name": "legacy"}).text = "retain"
        self.xml.write_bytes(serialize_xml(root))
        session = NativeSession(CvatXmlWorkspace(self.data, self.xml, native_ui=True))
        snapshot = session.snapshot()
        payload = self.payload(snapshot)
        payload["annotations"]["tracks"][0]["shapes"][0]["points"][0] = 10
        session.save(payload)
        track = ET.parse(self.xml).find("track")
        self.assertEqual(track.get("id"), "7")
        self.assertEqual(track.get("group_id"), "2")
        self.assertEqual(track.findtext("box/attribute"), "retain")
        self.assertEqual(float(track.find("box").get("xtl")), 10)

    def test_video_shapes_use_single_frame_cvat_tracks_with_stable_ids(self):
        (self.data / "one.png").unlink()
        make_video(self.data / "source.mp4")
        self.xml.write_bytes(CvatDocument.create(["dog"], (64, 48, 3)))
        session = NativeSession(CvatXmlWorkspace(self.data, self.xml, native_ui=True))
        snapshot = session.snapshot()
        payload = self.payload(snapshot)
        payload["annotations"]["shapes"].append({
            "clientID": 99, "type": "rectangle", "frame": 2, "label_id": 0, "group": 0,
            "points": [2, 2, 30, 25], "rotation": 0, "occluded": False, "outside": False,
            "z_order": 0, "attributes": [], "elements": [],
        })
        result = session.save(payload)
        root = ET.parse(self.xml).getroot()
        self.assertEqual(root.findall("image"), [])
        track = root.find("track")
        identifier = track.get("id")
        self.assertEqual([(box.get("frame"), box.get("outside")) for box in track], [("2", "0"), ("3", "1")])
        payload["revision"] = result["revision"]
        payload["annotations"]["shapes"][0]["points"][0] = 3
        session.save(payload)
        self.assertEqual(ET.parse(self.xml).find("track").get("id"), identifier)
        self.assertEqual(len(session.snapshot()["annotations"]["tracks"]), 1)


if __name__ == "__main__":
    unittest.main()
