import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gvat_local.workspace import Conflict, Workspace, WorkspaceError, load_classes


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "images"
        self.labels = self.root / "labels"
        self.data.mkdir()
        self.labels.mkdir()
        (self.data / "nested").mkdir()
        Image.new("RGB", (640, 480), "white").save(self.data / "nested" / "a space ü.png")
        Image.new("RGB", (200, 100), "blue").save(self.data / "other.png")
        self.classes = self.root / "data.yaml"
        self.classes.write_text("names:\n  0: cat\n  7: dog\n", encoding="utf-8")
        self.workspace = Workspace(self.data, self.labels, self.classes)
        self.box = {"class_id": 7, "cx": .5, "cy": .5, "width": .25, "height": .5}

    def tearDown(self):
        self.temporary.cleanup()

    def payload(self, path="nested/a space ü.png", boxes=None):
        return {**self.workspace.frame(path), "annotations": [self.box] if boxes is None else boxes}

    def test_nested_roundtrip_preserves_ids_and_does_not_touch_images(self):
        source = self.data / "nested" / "a space ü.png"
        original = source.read_bytes()
        result = self.workspace.save("nested/a space ü.png", self.payload())
        self.assertTrue(result["changed"])
        self.assertEqual((self.labels / "nested" / "a space ü.txt").read_text(), "7 0.5 0.5 0.25 0.5\n")
        self.assertEqual(self.workspace.frame("nested/a space ü.png")["annotations"], [self.box])
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(len(self.workspace.manifest()["images"]), 2)

    def test_missing_labels_and_intentionally_empty_labels_are_distinct(self):
        self.assertEqual(self.workspace.frame("other.png")["revision"], "missing")
        self.workspace.save("other.png", self.payload("other.png", []))
        self.assertEqual((self.labels / "other.txt").read_bytes(), b"")
        self.assertNotEqual(self.workspace.frame("other.png")["revision"], "missing")
        self.assertTrue(next(image for image in self.workspace.manifest()["images"] if image["path"] == "other.png")["labeled"])

    def test_noop_save_preserves_original_bytes_and_modification_time(self):
        target = self.labels / "other.txt"
        original = b"\xef\xbb\xbf7\t0.500000  0.5 0.2500000 0.5\r\n\r\n"
        target.write_bytes(original)
        before = target.stat().st_mtime_ns
        payload = self.payload("other.png")
        payload["annotations"][0]["cx"] += 1e-12
        result = self.workspace.save("other.png", payload)
        self.assertFalse(result["changed"])
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_external_label_change_and_second_browser_cannot_be_overwritten(self):
        payload = self.payload("other.png")
        (self.labels / "other.txt").write_text("0 0.5 0.5 0.1 0.1\n")
        with self.assertRaises(Conflict):
            self.workspace.save("other.png", payload)
        fresh = self.payload("other.png")
        self.workspace.save("other.png", fresh)
        with self.assertRaises(Conflict):
            self.workspace.save("other.png", fresh)
        self.assertEqual((self.labels / "other.txt").read_text(), "7 0.5 0.5 0.25 0.5\n")

    def test_external_image_and_class_changes_block_stale_saves(self):
        payload = self.payload("other.png")
        Image.new("RGB", (200, 100), "red").save(self.data / "other.png")
        with self.assertRaisesRegex(Conflict, "image changed"):
            self.workspace.save("other.png", payload)
        payload = self.payload("other.png")
        self.classes.write_text("names: [dog, cat]\n")
        with self.assertRaisesRegex(Conflict, "Class definitions"):
            self.workspace.save("other.png", payload)
        self.assertFalse((self.labels / "other.txt").exists())

    def test_unknown_classes_non_detection_and_nonfinite_labels_are_rejected(self):
        for text in ("5 .5 .5 .2 .2", "0 .1 .2 .3 .4 .5 .6", "0 nan .5 .2 .2", "0 .5 .5 -.1 .2"):
            with self.subTest(text=text):
                (self.labels / "other.txt").write_text(text)
                with self.assertRaises(WorkspaceError):
                    self.workspace.frame("other.png")

    def test_invalid_saves_do_not_replace_labels(self):
        payload = self.payload("other.png")
        for change in ({"class_id": 100}, {"cx": float("nan")}, {"width": 0}, {"cx": 0, "width": .4}):
            with self.subTest(change=change):
                bad = {**payload, "annotations": [{**self.box, **change}]}
                with self.assertRaises(WorkspaceError):
                    self.workspace.save("other.png", bad)
                self.assertFalse((self.labels / "other.txt").exists())

    def test_atomic_replace_failure_retains_original_file(self):
        target = self.labels / "other.txt"
        target.write_text("0 .5 .5 .2 .2\n")
        original = target.read_bytes()
        with patch("gvat_local.workspace.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.workspace.save("other.png", self.payload("other.png"))
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.labels.glob("*.gvat-*")), [])

    def test_duplicate_image_stems_fail_instead_of_sharing_a_label(self):
        Image.new("RGB", (20, 20)).save(self.data / "other.jpg")
        with self.assertRaisesRegex(WorkspaceError, "same label file"):
            self.workspace.manifest()

    def test_paths_cannot_escape_selected_directories(self):
        for path in ("../outside.png", "/etc/passwd", "nested\\a.png"):
            with self.subTest(path=path), self.assertRaises(WorkspaceError):
                self.workspace.frame(path)
        (self.labels / "nested").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(WorkspaceError):
            self.workspace.save("nested/a space ü.png", self.payload())

    def test_review_is_session_only_resolves_and_exports_feedback_without_files(self):
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        issue = {"id": str(uuid.uuid4()), "image": "nested/a space ü.png", "comment": "Wrong class\nTighten this box", "region": [.1, .2, .3, .4], "status": "open"}
        review = self.workspace.save_review({"revision": "missing", "issues": [issue]})
        reopened = Workspace(self.data, self.labels, self.classes)
        self.assertEqual(reopened.review()["issues"], [])
        self.assertEqual(self.workspace.review()["issues"][0]["comment"], issue["comment"])
        markdown = self.workspace.markdown()
        self.assertIn("nested/a space ü.png", markdown)
        self.assertIn("Wrong class", markdown)
        self.assertNotIn(str(self.root), markdown)
        review["issues"][0]["status"] = "resolved"
        self.workspace.save_review(review)
        self.assertNotIn("Wrong class", self.workspace.markdown())
        self.assertIn("Wrong class", self.workspace.markdown(include_resolved=True))
        after = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_review_conflicts_and_returned_snapshots_cannot_mutate_session_state(self):
        first = self.workspace.save_review({"revision": "missing", "issues": []})
        with self.assertRaises(Conflict):
            self.workspace.save_review({"revision": "missing", "issues": []})
        first["issues"].append({"comment": "client mutation"})
        self.assertEqual(self.workspace.review()["issues"], [])
        self.assertFalse((self.labels / ".gvat").exists())

    def test_exif_orientation_is_removed_without_changing_yolo_pixel_geometry(self):
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (200, 100), "red").save(self.data / "rotated.jpg", exif=exif)
        raw, mime = self.workspace.image_bytes("rotated.jpg")
        import io
        with Image.open(io.BytesIO(raw)) as image:
            self.assertEqual(image.size, (200, 100))
            self.assertEqual(image.getexif().get(274, 1), 1)
        self.assertEqual(mime, "image/png")

    def test_class_schemas_preserve_sparse_ids_and_validate_names(self):
        self.assertEqual(load_classes(self.classes)[0], {0: "cat", 7: "dog"})
        for text in ("names: []", "names: [cat, cat]", "names: {a: cat}"):
            self.classes.write_text(text)
            with self.assertRaises(WorkspaceError):
                load_classes(self.classes)


if __name__ == "__main__":
    unittest.main()
