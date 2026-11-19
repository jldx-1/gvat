import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gvat_local.cli import parser, prepare
from gvat_local.native import NativeSession
from gvat_local.video_decode import index_video
from gvat_local.workspace import WorkspaceError
from video_fixtures import make_video


class MediaStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / 'sample'
        self.data.mkdir()
        self.cache = self.root / 'cache'
        self.environment = patch.dict(os.environ, {'XDG_CACHE_HOME': str(self.cache), 'XDG_CONFIG_HOME': str(self.root / 'config')})
        self.environment.start()
        (self.root / 'labels.txt').write_text('dog\n')
        Image.new('RGB', (80, 40), 'red').save(self.data / 'one.jpg')

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def open(self, *extra):
        with redirect_stdout(io.StringIO()):
            return prepare(parser().parse_args(['--data_path', str(self.data), '--class-names=dog', '--yes', '--mode=annotate', *extra]))

    def chunk(self, workspace):
        native = NativeSession(workspace)
        try:
            boot = native.snapshot()
            view = native.chunk_view(boot['view'])
            native.chunks.activate(boot['view'], 0, 1)
            raw = native.chunks.read(boot['view'], view['keys'], view['chunk_size'], 0, 1)
            return zipfile.ZipFile(io.BytesIO(raw))
        finally:
            native.chunks.close()
            native.exports.close()

    def test_mixed_folder_prefers_images_without_decoding_or_indexing_video(self):
        make_video(self.data / 'source.mp4')
        with patch('gvat_local.media.subprocess.run', side_effect=AssertionError('Unselected video was decoded')):
            workspace = self.open()
        self.assertEqual([source.name for source in workspace.sources], ['one.jpg'])
        self.assertEqual(workspace.manifest()['videos'], [])

    def test_video_preference_indexes_once_and_existing_xml_preserves_that_binding(self):
        make_video(self.data / 'source.mp4')
        with patch('gvat_local.video_decode.index_video', wraps=index_video) as indexer:
            workspace = self.open('--media-type=video')
        self.assertEqual(indexer.call_count, 1)
        self.assertEqual([source.name for source in workspace.sources], ['source.mp4'])
        with patch('gvat_local.media.file_digest', side_effect=AssertionError('Unchanged video was rehashed')):
            reopened = self.open('--media-type=images')
        self.assertEqual([source.name for source in reopened.sources], ['source.mp4'])

    def test_existing_image_xml_keeps_images_and_explicit_all_supports_mixed_sources(self):
        self.open()
        root = ET.parse(self.data / 'annotations.xml').getroot()
        ET.SubElement(root, 'image', id='0', name='one.jpg', width='80', height='40')
        (self.data / 'annotations.xml').write_bytes(ET.tostring(root))
        make_video(self.data / 'source.mp4')
        self.assertEqual([source.kind for source in self.open('--media-type=video').sources], ['image'])
        self.assertEqual({source.kind for source in self.open('--media-type=all').sources}, {'image', 'video'})

    def test_preferred_kind_falls_back_if_it_is_absent(self):
        self.assertEqual(self.open('--media-type=video').sources[0].kind, 'image')
        (self.data / 'annotations.xml').unlink()
        (self.data / 'one.jpg').unlink()
        make_video(self.data / 'source.mp4')
        self.assertEqual(self.open('--media-type=images').sources[0].kind, 'video')

    def test_mixed_task_bootstrap_does_not_extract_every_video_frame_for_dimensions(self):
        make_video(self.data / 'source.mp4')
        workspace = self.open('--media-type=all')
        self.assertEqual(workspace.videos[0].dimensions, ((64, 48),))
        with patch.object(workspace, '_ensure_frame', side_effect=AssertionError('Video frame extracted during startup')):
            native = NativeSession(workspace)
            boot = native.snapshot()
        self.assertEqual(len(boot['frames']), 4)
        native.chunks.close()

    def test_warm_start_reuses_dimensions_validation_and_hashes(self):
        self.open()
        with patch('gvat_local.media.file_digest', side_effect=AssertionError('Source was rehashed')), \
             patch('gvat_local.media.Image.open', side_effect=AssertionError('Image was reopened')):
            workspace = self.open()
            native = NativeSession(workspace)
            self.assertEqual(native.snapshot()['meta']['frames'][0]['width'], 80)
            native.chunks.close()
        self.assertTrue((self.cache / 'gvat/source-metadata/index.sqlite3').is_file())

    def test_cold_images_do_not_require_full_content_hashes(self):
        Image.new('RGB', (80, 40), 'blue').save(self.data / 'two.jpg')
        with patch('gvat_local.media.file_digest', side_effect=AssertionError('Unopened image was hashed')):
            workspace = self.open()
        self.assertEqual(len(workspace.sources), 2)
        with self.chunk(workspace) as archive:
            self.assertEqual(len(archive.namelist()), 2)

    def test_later_corrupt_pixels_are_reported_when_the_chunk_is_requested(self):
        second = self.data / 'two.jpg'
        Image.new('RGB', (80, 40), 'blue').save(second)
        second.write_bytes(second.read_bytes()[:-2])
        workspace = self.open()
        before = (workspace.xml_file.read_bytes(), workspace.xml_file.stat().st_mtime_ns)
        with self.assertRaisesRegex(WorkspaceError, 'Cannot decode source image two.jpg'):
            self.chunk(workspace)
        self.assertEqual((workspace.xml_file.read_bytes(), workspace.xml_file.stat().st_mtime_ns), before)

    def test_first_corrupt_image_is_rejected_before_creating_xml(self):
        first = self.data / 'one.jpg'
        first.write_bytes(first.read_bytes()[:-2])
        with self.assertRaisesRegex(WorkspaceError, 'Cannot decode source image one.jpg'):
            self.open()
        self.assertFalse((self.data / 'annotations.xml').exists())

    def test_png_orientation_is_validated_before_using_original_bytes(self):
        exif = Image.Exif()
        exif[274] = 6
        Image.new('RGB', (80, 40), 'blue').save(self.data / 'two.png', exif=exif)
        workspace = self.open()
        with self.chunk(workspace) as archive, Image.open(io.BytesIO(archive.read('00000001.png'))) as decoded:
            self.assertEqual(decoded.size, (80, 40))
            self.assertEqual(decoded.getexif().get(274, 1), 1)

    def test_cache_invalidates_even_if_size_and_mtime_are_preserved(self):
        (self.data / 'one.jpg').unlink()
        image = self.data / 'one.bmp'
        Image.new('RGB', (80, 40), 'red').save(image)
        first = self.open().sources[0]
        before = image.stat()
        Image.new('RGB', (80, 40), 'blue').save(image)
        os.utime(image, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(image.stat().st_size, before.st_size)
        self.assertNotEqual(self.open().sources[0].revision, first.revision)

    def test_malformed_metadata_is_rebuilt(self):
        self.open()
        for record in ('not JSON', '[]', 'null', '{}'):
            with self.subTest(record=record):
                with sqlite3.connect(self.cache / 'gvat/source-metadata/index.sqlite3') as connection:
                    connection.execute('UPDATE sources SET record = ?', (record,))
                self.assertEqual(self.open().sources[0].dimensions, ((80, 40),))

    def test_busy_metadata_cache_falls_back_without_touching_the_other_writer(self):
        self.open()
        with sqlite3.connect(self.cache / 'gvat/source-metadata/index.sqlite3') as connection:
            connection.execute('DELETE FROM sources')
            connection.commit()
            connection.execute('BEGIN IMMEDIATE')
            self.assertEqual(self.open().sources[0].dimensions, ((80, 40),))
            self.assertTrue(connection.in_transaction)
            connection.rollback()

    def test_jpeg_and_png_chunks_keep_original_bytes_and_skip_whole_folder_scans(self):
        Image.new('RGBA', (80, 40), (0, 0, 255, 128)).save(self.data / 'two.png')
        workspace = self.open()
        original = [(file.read_bytes(), file.stat().st_mtime_ns) for file in (self.data / 'one.jpg', self.data / 'two.png')]
        native = NativeSession(workspace)
        boot = native.snapshot()
        view = native.chunk_view(boot['view'])
        native.chunks.activate(boot['view'], 0, 1)
        try:
            with patch.object(workspace, '_check_sources', side_effect=AssertionError('Whole folder was rescanned')):
                raw = native.chunks.read(boot['view'], view['keys'], view['chunk_size'], 0, 1)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                self.assertEqual(archive.read('00000000.jpg'), original[0][0])
                self.assertEqual(archive.read('00000001.png'), original[1][0])
            self.assertEqual(list(workspace.data.glob('*.png')), [])
            self.assertEqual([(file.read_bytes(), file.stat().st_mtime_ns) for file in (self.data / 'one.jpg', self.data / 'two.png')], original)
        finally:
            native.chunks.close()

    def test_exif_orientation_uses_lossless_fallback_without_rotating_coordinates(self):
        image = Image.new('RGB', (80, 40), 'red')
        exif = Image.Exif()
        exif[274] = 6
        image.save(self.data / 'one.jpg', exif=exif)
        workspace = self.open()
        self.assertIsNone(workspace.sources[0].direct_format)
        with self.chunk(workspace) as archive, Image.open(io.BytesIO(archive.read('00000000.png'))) as decoded:
            self.assertEqual(decoded.size, (80, 40))
            with Image.open(self.data / 'one.jpg') as original:
                self.assertEqual(decoded.convert('RGB').tobytes(), original.convert('RGB').tobytes())

    def test_cli_import_does_not_initialize_all_pillow_plugins(self):
        result = subprocess.run([sys.executable, '-c', "import sys; import gvat_local.cli; assert 'PIL.TiffImagePlugin' not in sys.modules"],
                                capture_output=True, text=True, env=os.environ)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
