import io
import os
import subprocess
import socket
import struct
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
import zipfile
import time
import urllib.parse
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from gvat_local.cvat_xml import CvatDocument, CvatXmlWorkspace, serialize_xml
from gvat_local.media import count_video_frames, ffmpeg_input
from gvat_local.native import NativeSession
from gvat_local.server import LocalServer
from gvat_local.video_decode import DecodeCancelled, decode_batch
from gvat_local.workspace import Conflict
from video_fixtures import make_video


class ChunkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        self.source = self.media / 'source.mp4'
        make_video(self.source, [Image.new('RGB', (64, 48), (number * 3 % 256, number * 7 % 256, number * 11 % 256)) for number in range(96)])
        self.xml = self.media / 'annotations.xml'
        self.xml.write_bytes(CvatDocument.create(['object'], (64, 48, 96)))
        self.environment = patch.dict(os.environ, {'XDG_CACHE_HOME': str(self.root / 'cache')})
        self.environment.start()
        self.workspace = CvatXmlWorkspace(self.media, self.xml, native_ui=True)
        self.session = NativeSession(self.workspace)
        self.boot = self.session.snapshot()

    def tearDown(self):
        self.session.chunks.close()
        self.environment.stop()
        self.temporary.cleanup()

    def read(self, chunk, generation=1):
        view = self.session.chunk_view(self.boot['view'])
        return self.session.chunks.read(self.boot['view'], view['keys'], view['chunk_size'], chunk, generation)

    def activate(self, chunk, generation=1):
        self.session.chunks.activate(self.boot['view'], chunk, generation)

    def original_pixels(self, number):
        command = ffmpeg_input(self.source) + ['-map', '0:v:0', '-an', '-sn', '-filter_threads', '1',
            '-vf', f'select=eq(n\\,{number})', '-vsync', '0', '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-threads', '1', 'pipe:1']
        result = subprocess.run(command, check=True, capture_output=True)
        return Image.open(io.BytesIO(result.stdout)).convert('RGB').tobytes()

    def test_chunk_uses_one_decode_and_seeks_to_exact_original_frames(self):
        self.activate(2)
        before = (self.xml.read_bytes(), self.xml.stat().st_mtime_ns, self.source.stat().st_mtime_ns)
        with patch('gvat_local.chunks.decode_batch', wraps=decode_batch) as decoder:
            result = self.read(2)
            self.assertEqual(decoder.call_count, 1)
            self.assertEqual(decoder.call_args.args[1], list(range(64, 96)))
            self.assertEqual(self.read(2), result)
            self.assertEqual(decoder.call_count, 1)
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            self.assertEqual(len(archive.namelist()), 32)
            for offset in (0, 7, 31):
                frame = Image.open(io.BytesIO(archive.read(f'{offset:08d}.png'))).convert('RGB')
                self.assertEqual(frame.tobytes(), self.original_pixels(64 + offset))
        self.assertEqual((self.xml.read_bytes(), self.xml.stat().st_mtime_ns, self.source.stat().st_mtime_ns), before)
        self.assertEqual(sorted(path.name for path in self.media.iterdir()), ['annotations.xml', 'source.mp4'])

    def test_filtered_task_preserves_native_frame_numbers(self):
        root = ET.fromstring(self.xml.read_bytes())
        for name, value in (('start_frame', '7'), ('stop_frame', '91'), ('frame_filter', 'step=3')):
            element = root.find(f'meta/task/{name}')
            if element is None:
                element = ET.SubElement(root.find('meta/task'), name)
            element.text = value
        self.xml.write_bytes(serialize_xml(root))
        self.boot = self.session.snapshot()
        self.activate(0)
        with zipfile.ZipFile(io.BytesIO(self.read(0))) as archive:
            for offset in (0, 10, 28):
                pixels = Image.open(io.BytesIO(archive.read(f'{offset:08d}.png'))).convert('RGB').tobytes()
                self.assertEqual(pixels, self.original_pixels(7 + offset * 3))

    def test_new_scrub_cancels_running_process_and_does_not_hold_xml_lock(self):
        started = threading.Event()
        failures = []
        original = decode_batch

        def slow(source, numbers, index, directory, task):
            if numbers[0] == 0:
                started.set()
                with tempfile.TemporaryFile() as log:
                    task.run([sys.executable, '-c', 'import time; time.sleep(60)'], log)
            return original(source, numbers, index, directory, task)

        def first_request():
            try:
                self.read(0)
            except Exception as error:
                failures.append(error)

        self.activate(0)
        with patch('gvat_local.chunks.decode_batch', side_effect=slow):
            first = threading.Thread(target=first_request)
            first.start()
            self.assertTrue(started.wait(3))
            self.assertTrue(self.workspace.lock.acquire(timeout=1))
            self.workspace.lock.release()
            self.activate(2, 2)
            result = self.read(2, 2)
            first.join(timeout=2)
            self.assertFalse(first.is_alive())
            self.assertEqual(len(zipfile.ZipFile(io.BytesIO(result)).namelist()), 32)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], DecodeCancelled)
        with self.assertRaises(DecodeCancelled):
            self.activate(0, 1)

    def test_browser_disconnect_cancels_decode_and_allows_retry(self):
        self.activate(1)
        view = self.session.chunk_view(self.boot['view'])
        with self.assertRaises(DecodeCancelled):
            self.session.chunks.read(self.boot['view'], view['keys'], view['chunk_size'], 1, 1, disconnected=lambda: True)
        self.activate(1, 2)
        self.assertTrue(self.read(1, 2).startswith(b'PK'))

    def test_real_http_disconnect_cancels_chunk_without_server_traceback(self):
        self.activate(0)
        (self.root / 'index.html').write_text('CVAT')
        server = LocalServer(self.workspace, self.root, 'test-token', 0)
        server.native = self.session
        server.handle_error = Mock()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started, cancelled = threading.Event(), threading.Event()

        def slow_build(keys, task):
            started.set()
            try:
                while True:
                    task.check()
                    time.sleep(.01)
            except DecodeCancelled:
                cancelled.set()
                raise

        try:
            with patch.object(self.session.chunks, '_build', side_effect=slow_build):
                browser = socket.create_connection(('127.0.0.1', server.server_port), timeout=3)
                query = urllib.parse.urlencode({'view': self.boot['view'], 'chunk': 0, 'generation': 1})
                browser.sendall(f'GET /api/cvat/chunks?{query} HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\nAuthorization: Bearer test-token\r\n\r\n'.encode())
                self.assertTrue(started.wait(3))
                browser.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                browser.close()
                self.assertTrue(cancelled.wait(3))
            self.assertFalse(server.handle_error.called)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_cached_chunk_still_checks_source_changes(self):
        self.activate(0)
        self.read(0)
        self.source.write_bytes(self.source.read_bytes() + b'changed')
        with self.assertRaises(Conflict):
            self.read(0)

    def test_variable_timestamps_b_frames_and_nonzero_start_seek_exactly(self):
        self.session.chunks.close()
        colors = [Image.new('RGB', (64, 48), (number * 5 % 256, number * 11 % 256, number * 17 % 256)) for number in range(40)]
        command = [ffmpeg_input(self.source)[0], '-nostdin', '-y', '-v', 'error', '-threads', '1',
            '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '64x48', '-r', '2', '-i', 'pipe:0',
            '-vf', 'setpts=PTS+floor(N/3)/(2*TB)', '-fps_mode', 'vfr', '-an', '-c:v', 'libx264',
            '-g', '6', '-bf', '2', '-threads', '1', '-output_ts_offset', '5', str(self.source)]
        subprocess.run(command, input=b''.join(frame.tobytes() for frame in colors), check=True, capture_output=True)
        frame_count = count_video_frames(self.source)
        self.xml.write_bytes(CvatDocument.create(['object'], (64, 48, frame_count)))
        self.workspace = CvatXmlWorkspace(self.media, self.xml, native_ui=True)
        self.session = NativeSession(self.workspace)
        self.boot = self.session.snapshot()
        self.assertEqual(len(self.boot['frames']), frame_count)
        self.activate(1)
        with zipfile.ZipFile(io.BytesIO(self.read(1))) as archive:
            self.assertEqual(len(archive.namelist()), frame_count - 32)
            for offset in (0, 3, frame_count - 33):
                pixels = Image.open(io.BytesIO(archive.read(f'{offset:08d}.png'))).convert('RGB').tobytes()
                self.assertEqual(pixels, self.original_pixels(32 + offset))


if __name__ == '__main__':
    unittest.main()
