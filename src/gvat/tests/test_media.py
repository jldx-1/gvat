import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gvat_local.media import MediaWorkspace, discover_media
from gvat_local.workspace import Conflict, WorkspaceError
from video_fixtures import make_video


class MediaFormatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "media"
        self.labels = self.root / "annotations"
        self.data.mkdir()
        self.labels.mkdir()
        self.classes = self.root / "classes.txt"
        self.classes.write_text("object\n")
        self.environment = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def workspace(self):
        return MediaWorkspace(self.data, self.labels, self.classes)

    def test_pillow_image_formats_are_decoded_by_content_and_served_as_png(self):
        formats = {"jpg": "JPEG", "png": "PNG", "bmp": "BMP", "gif": "GIF", "ppm": "PPM", "tif": "TIFF", "pcx": "PCX", "tga": "TGA", "webp": "WEBP", "ico": "ICO"}
        image = Image.new("RGB", (64, 48), "red")
        originals = {}
        for extension, format_name in formats.items():
            path = self.data / f"image_{extension}.{extension}"
            image.save(path, format=format_name)
            originals[path] = path.read_bytes()
        workspace = self.workspace()
        self.assertEqual(len(workspace.manifest()["images"]), len(formats))
        for path, original in originals.items():
            key = path.name
            raw, mime = workspace.image_bytes(key)
            self.assertEqual(mime, "image/png")
            with Image.open(io.BytesIO(raw)) as preview:
                self.assertEqual(preview.format, "PNG")
                self.assertGreater(preview.getpixel((0, 0))[0], 200)
            frame = workspace.frame(key)
            workspace.save(key, {**frame, "annotations": []})
            self.assertEqual((self.labels / path.with_suffix(".txt").name).read_bytes(), b"")
            self.assertEqual(path.read_bytes(), original)

    def test_animated_gif_and_multipage_tiff_have_distinct_native_frames(self):
        images = [Image.new("RGB", (64, 48), color) for color in ("red", "blue")]
        for extension, format_name in (("gif", "GIF"), ("tiff", "TIFF")):
            images[0].save(self.data / f"sequence.{extension}", format=format_name, save_all=True, append_images=images[1:], duration=100)
        workspace = self.workspace()
        self.assertEqual(len(workspace.scan()), 4)
        for extension in ("gif", "tiff"):
            for number, channel in ((1, 0), (2, 2)):
                key = f"sequence.{extension}/frame_{number:06}.png"
                raw, _ = workspace.image_bytes(key)
                with Image.open(io.BytesIO(raw)) as preview:
                    self.assertGreater(preview.getpixel((0, 0))[channel], 200)
                frame = workspace.frame(key)
                workspace.save(key, {**frame, "annotations": []})
                self.assertTrue((self.labels / f"sequence.{extension}/frame_{number:06}.txt").is_file())

    def test_mov_webm_and_unlisted_video_extensions_use_ffmpeg_content_detection(self):
        for extension in ("mov", "webm", "mp4"):
            make_video(self.data / f"source.{extension}")
        (self.data / "source.mp4").rename(self.data / "source.unlisted")
        workspace = self.workspace()
        self.assertEqual(len(workspace.videos), 3)
        self.assertEqual(len(workspace.scan()), 9)
        for source in workspace.videos:
            raw, mime = workspace.image_bytes(f"{source.name}/frame_000003.png")
            self.assertEqual(mime, "image/png")
            with Image.open(io.BytesIO(raw)) as image:
                self.assertGreater(image.getpixel((0, 0))[2], 200)

    def test_mixed_folders_ignore_metadata_and_support_extensionless_images(self):
        make_video(self.data / "clip.mp4")
        Image.new("RGB", (64, 48), "red").save(self.data / "photograph", format="PNG")
        (self.data / "data.yaml").write_text("names: [object]\n")
        (self.data / "README.md").write_text("Dataset notes")
        (self.data / "notes.pdf").write_text("not media; PDF has no Pillow reader")
        workspace = self.workspace()
        self.assertEqual(len(workspace.sources), 2)
        self.assertEqual(len(workspace.scan()), 4)
        frame = workspace.frame("photograph")
        workspace.save("photograph", {**frame, "annotations": []})
        self.assertTrue((self.labels / "photograph.txt").exists())

    def test_16_bit_tiff_is_viewable_without_rewriting_the_source(self):
        image = Image.new("I;16", (2, 1))
        image.putpixel((0, 0), 0)
        image.putpixel((1, 0), 65535)
        path = self.data / "scientific.tif"
        image.save(path)
        original = path.read_bytes()
        raw, _ = self.workspace().image_bytes(path.name)
        with Image.open(io.BytesIO(raw)) as preview:
            self.assertEqual(preview.getpixel((0, 0)), (0, 0, 0))
            self.assertEqual(preview.getpixel((1, 0)), (255, 255, 255))
        self.assertEqual(path.read_bytes(), original)

    def test_matching_image_stems_cannot_silently_share_annotations(self):
        Image.new("RGB", (20, 20)).save(self.data / "duplicate.png")
        Image.new("RGB", (20, 20)).save(self.data / "duplicate.tiff")
        with self.assertRaisesRegex(WorkspaceError, "same annotation file"):
            self.workspace()

    def test_changed_or_added_images_require_reindexing_and_never_overwrite_labels(self):
        path = self.data / "one.tiff"
        Image.new("RGB", (20, 20), "red").save(path)
        workspace = self.workspace()
        frame = workspace.frame(path.name)
        Image.new("RGB", (20, 20), "blue").save(path)
        with self.assertRaises(Conflict):
            workspace.save(path.name, {**frame, "annotations": []})
        self.assertFalse((self.labels / "one.txt").exists())
        workspace = self.workspace()
        Image.new("RGB", (20, 20), "green").save(self.data / "two.gif")
        with self.assertRaisesRegex(Conflict, "media list changed"):
            workspace.manifest()

    def test_nonmedia_folder_fails_without_creating_annotations(self):
        (self.data / "notes.txt").write_text("A directory of notes")
        with self.assertRaisesRegex(WorkspaceError, "No supported images or videos"):
            discover_media(self.data)
        self.assertEqual(list(self.labels.iterdir()), [])
