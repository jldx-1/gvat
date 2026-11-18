import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gvat_local.media import MediaWorkspace
from gvat_local.workspace import Conflict, WorkspaceError
from video_fixtures import make_video


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "videos"
        self.labels = self.root / "annotations"
        self.data.mkdir()
        self.labels.mkdir()
        self.classes = self.root / "data.yaml"
        self.classes.write_text("names: [cat, dog]\n")
        self.environment = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
        self.environment.start()
        self.box = {"class_id": 1, "cx": .5, "cy": .5, "width": .25, "height": .5}

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_mp4_mkv_avi_are_readable_and_map_to_distinct_frame_annotations(self):
        for extension in ("mp4", "mkv", "avi"):
            make_video(self.data / f"source.{extension}")
        workspace = MediaWorkspace(self.data, self.labels, self.classes)
        manifest = workspace.manifest()
        self.assertEqual(len(manifest["videos"]), 3)
        self.assertEqual(len(manifest["images"]), 9)
        for extension in ("mp4", "mkv", "avi"):
            key = f"source.{extension}/frame_000001.png"
            frame = workspace.frame(key)
            self.assertEqual((frame["width"], frame["height"]), (64, 48))
            self.assertEqual(frame["revision"], "missing")
            workspace.save(key, {**frame, "annotations": [self.box]})
            self.assertEqual((self.labels / f"source.{extension}/frame_000001.txt").read_text(), "1 0.5 0.5 0.25 0.5\n")

    def test_frames_are_native_distinct_and_cached_outside_the_source_directory(self):
        video = self.data / "source.mp4"
        make_video(video)
        original = video.read_bytes()
        workspace = MediaWorkspace(self.data, self.labels, self.classes)
        for number, channel in ((1, 0), (3, 2)):
            key = f"source.mp4/frame_{number:06}.png"
            raw, _ = workspace.image_bytes(key)
            with Image.open(io.BytesIO(raw)) as frame:
                self.assertGreater(frame.getpixel((0, 0))[channel], 200)
        self.assertEqual(list(self.data.iterdir()), [video])
        self.assertEqual(list(self.labels.iterdir()), [])
        self.assertEqual(video.read_bytes(), original)
        self.assertTrue(workspace.data.is_relative_to(self.root / "cache"))

    def test_missing_annotations_are_created_only_when_saving_a_frame(self):
        make_video(self.data / "source.mp4")
        workspace = MediaWorkspace(self.data, self.labels, self.classes)
        key = "source.mp4/frame_000002.png"
        frame = workspace.frame(key)
        self.assertFalse((self.labels / "source.mp4/frame_000002.txt").exists())
        workspace.save(key, {**frame, "annotations": []})
        self.assertEqual((self.labels / "source.mp4/frame_000002.txt").read_bytes(), b"")
        reopened = MediaWorkspace(self.data, self.labels, self.classes)
        self.assertNotEqual(reopened.frame(key)["revision"], "missing")

    def test_replaced_source_video_cannot_receive_stale_annotations(self):
        video = self.data / "source.mp4"
        make_video(video)
        workspace = MediaWorkspace(self.data, self.labels, self.classes)
        key = "source.mp4/frame_000001.png"
        frame = workspace.frame(key)
        make_video(video, [Image.new("RGB", (64, 48), "white")])
        with self.assertRaises(Conflict):
            workspace.save(key, {**frame, "annotations": [self.box]})
        self.assertFalse((self.labels / "source.mp4/frame_000001.txt").exists())

    def test_image_only_folders_open_and_corrupt_video_files_fail(self):
        Image.new("RGB", (20, 20)).save(self.data / "image.png")
        workspace = MediaWorkspace(self.data, self.labels, self.classes)
        self.assertEqual(workspace.manifest()["images"][0]["path"], "image.png")
        (self.data / "invalid.mp4").write_bytes(b"not a video")
        with self.assertRaisesRegex(WorkspaceError, "Cannot read source media"):
            MediaWorkspace(self.data, self.labels, self.classes)

    def test_frame_paths_cannot_escape_the_selected_annotation_directory(self):
        make_video(self.data / "source.mp4")
        workspace = MediaWorkspace(self.data, self.labels, self.classes)
        for key in ("../outside.png", "source.mp4/../../outside.png", "unknown/frame_000001.png"):
            with self.subTest(key=key), self.assertRaises(WorkspaceError):
                workspace.frame(key)

    def test_session_feedback_uses_source_frame_paths_without_creating_files(self):
        import uuid

        make_video(self.data / "source.mp4")
        workspace = MediaWorkspace(self.data, self.labels, self.classes)
        workspace.save_review({"revision": "missing", "issues": [{"id": str(uuid.uuid4()), "image": "source.mp4/frame_000001.png", "comment": "Check this frame", "status": "open", "region": None}]})
        self.assertIn("source.mp4/frame_000001.png", workspace.markdown())
        self.assertNotIn(str(self.root), workspace.markdown())
        self.assertEqual(list(self.labels.iterdir()), [])
        reopened = MediaWorkspace(self.data, self.labels, self.classes)
        self.assertEqual(reopened.review()["issues"], [])
