import json
import io
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from PIL import Image

from gvat_local.cli import directory, parser, prepare
from gvat_local.cvat_xml import CvatDocument
from gvat_local.workspace import WorkspaceError
from video_fixtures import make_video


@unittest.skipUnless(sys.platform == "linux", "GVAT CLI currently targets Linux")
class CLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "images").mkdir()
        Image.new("RGB", (20, 20)).save(self.root / "images" / "one.png")
        make_video(self.root / "images" / "source.mp4")
        self.environment = {**os.environ, "XDG_CACHE_HOME": str(self.root / "cache")}
        self.process = None

    def tearDown(self):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            self.process.communicate(timeout=10)
        self.temporary.cleanup()

    def command(self, *arguments):
        return [sys.executable, "-m", "gvat_local", *arguments]

    def test_noninteractive_missing_paths_fail_with_actionable_prompt(self):
        result = subprocess.run(self.command("--data_path", "missing", "--labels", "labels", "--yes"), cwd=self.root, env=self.environment, input="", capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("corrected path", result.stderr)
        self.assertFalse((self.root / "missing").exists())

    def test_noninteractive_missing_classes_are_not_invented(self):
        result = subprocess.run(self.command("--data_path", "images", "--yes"), cwd=self.root, env=self.environment, input="", capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("class definitions", result.stderr)
        self.assertFalse((self.root / "images/annotations.xml").exists())

    def test_empty_directory_answer_does_not_select_the_current_directory(self):
        with patch("gvat_local.cli.ask", side_effect=["", str(self.root / "images")]):
            result = directory(None, "Image")
        self.assertEqual(result, self.root / "images")

    def test_mistyped_labels_file_is_reprompted_without_creating_text_files(self):
        classes = self.root / "chosen.txt"
        classes.write_text("cat\ndog\n")
        args = parser().parse_args(["--data_path", str(self.root / "images"), "--yes", "--mode", "annotate"])
        with patch("gvat_local.cli.ask", side_effect=[str(self.root / "missing.txt"), str(classes)]), patch("sys.stdout", new_callable=io.StringIO), patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")}):
            workspace = prepare(args)
        self.assertEqual(workspace.document().classes, {0: "cat", 1: "dog"})
        self.assertEqual(list(self.root.rglob("*.txt")), [classes])

    def test_folder_without_media_errors_before_creating_annotations_or_starting(self):
        (self.root / "images/source.mp4").unlink()
        (self.root / "images/one.png").unlink()
        (self.root / "images/README.md").write_text("Not media")
        result = subprocess.run(self.command("--data_path", "images", "--labels", "labels", "--class-names", "cat", "--yes", "--no-browser"), cwd=self.root, env=self.environment, input="", capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("No supported images or videos", result.stderr)
        self.assertNotIn("Open http", result.stdout)
        self.assertFalse((self.root / "labels").exists())

    def test_image_only_input_starts_without_a_video(self):
        (self.root / "images/source.mp4").unlink()
        url, _ = self.start()
        parsed = urlsplit(url)
        request = urllib.request.Request(f"{parsed.scheme}://{parsed.netloc}/api/workspace", headers={"Authorization": f"Bearer {parsed.fragment.removeprefix('token=')}"})
        with urllib.request.urlopen(request) as response:
            manifest = json.load(response)
        self.assertEqual(manifest["videos"], [])
        self.assertEqual(manifest["images"], [{"path": "one.png", "labeled": False}])

    def test_shared_labels_file_initializes_xml_without_modifying_or_copying_labels(self):
        labels = self.root / "labels.txt"
        labels.write_text("cat\ndog\n")
        modified = labels.stat().st_mtime_ns
        args = parser().parse_args(["--data_path", str(self.root / "images"), "--labels", str(labels), "--yes"])
        with patch("sys.stdout", new_callable=io.StringIO), patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")}):
            workspace = prepare(args)
        self.assertEqual(workspace.xml_file, self.root / "images/annotations.xml")
        self.assertEqual(workspace.document().classes, {0: "cat", 1: "dog"})
        self.assertEqual(labels.stat().st_mtime_ns, modified)
        self.assertEqual(list(self.root.rglob("*.txt")), [labels])
        self.assertFalse(any(path.is_dir() for path in (self.root / "images").iterdir()))

    def test_nested_media_is_loaded_and_xml_stays_in_parent_folder(self):
        (self.root / "images/source.mp4").unlink()
        (self.root / "images/one.png").unlink()
        nested = self.root / "images/nested"
        nested.mkdir()
        Image.new("RGB", (20, 20)).save(nested / "one.png")
        with patch.dict(os.environ, self.environment), patch('sys.stdout', new_callable=io.StringIO):
            workspace = prepare(parser().parse_args(['--data_path', str(self.root / 'images'), '--class-names=cat', '--yes']))
        self.assertEqual(list(workspace._frames), ['nested/one.png'])
        self.assertEqual(workspace.xml_file, self.root / 'images/annotations.xml')
        frame = workspace.frame('nested/one.png')
        workspace.save('nested/one.png', {**frame, 'annotations': [{'class_id': 0, 'cx': .5, 'cy': .5, 'width': .5, 'height': .5}]})
        self.assertEqual(workspace.document().root.find('image').get('name'), 'nested/one.png')
        self.assertEqual(sorted(path.name for path in nested.iterdir()), ['one.png'])

    def test_multiple_xml_files_need_an_explicit_choice(self):
        data = self.root / "images"
        for name in ("one.xml", "two.xml"):
            (data / name).write_bytes(CvatDocument.create(["cat"]))
        with self.assertRaisesRegex(WorkspaceError, "Multiple XML"):
            with patch("sys.stdin.isatty", return_value=False):
                prepare(parser().parse_args(["--data_path", str(data)]))
        with patch("sys.stdout", new_callable=io.StringIO), patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")}):
            workspace = prepare(parser().parse_args(["--data_path", str(data), "--annotations", str(data / "two.xml")]))
        self.assertEqual(workspace.xml_file.name, "two.xml")

    def start(self, *extra):
        arguments = self.command("--data_path", "images", "--class-names", "cat,dog", "--yes", "--no-browser", "--port", "0", *extra)
        self.process = subprocess.Popen(arguments, cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = []
        for _ in range(10):
            if not select.select([self.process.stdout], [], [], 10)[0]:
                self.fail(f"CLI did not start: {lines}")
            output = os.read(self.process.stdout.fileno(), 8192).decode()
            if not output:
                self.fail(self.process.stderr.read())
            lines.extend(output.splitlines())
            for line in lines:
                if line.startswith("Open http"):
                    return line.strip()[5:], arguments
        self.fail(f"No session URL: {lines}")

    def test_real_cli_uses_relative_paths_and_prevents_second_writer(self):
        url, arguments = self.start()
        parsed = urlsplit(url)
        token = parsed.fragment.removeprefix("token=")
        request = urllib.request.Request(f"{parsed.scheme}://{parsed.netloc}/api/workspace", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request) as response:
            manifest = json.load(response)
        self.assertEqual(manifest["mode"], "annotate")
        self.assertEqual(manifest["data_path"], str(self.root / "images"))
        self.assertEqual(manifest["classes"], [{"id": 0, "name": "cat"}, {"id": 1, "name": "dog"}])
        second = subprocess.run(arguments, cwd=self.root, env=self.environment, input="", capture_output=True, text=True, timeout=10)
        self.assertEqual(second.returncode, 2)
        self.assertIn("already using", second.stderr)
        self.process.send_signal(signal.SIGTERM)
        self.process.communicate(timeout=10)
        self.assertEqual(self.process.returncode, 0)

    def test_ctrl_c_then_review_reuses_port_without_changing_saved_xml(self):
        url, _ = self.start("--mode", "annotate")
        port = urlsplit(url).port
        xml = self.root / "images/annotations.xml"
        saved = (xml.read_bytes(), xml.stat().st_mtime_ns)
        with socket.create_connection(("127.0.0.1", port), timeout=3) as browser:
            browser.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
            while browser.recv(4096):
                pass
        self.process.send_signal(signal.SIGINT)
        self.process.communicate(timeout=10)
        self.assertEqual(self.process.returncode, 0)
        review_url, _ = self.start("--port", str(port), "--mode", "review")
        self.assertEqual(urlsplit(review_url).port, port)
        parsed = urlsplit(review_url)
        request = urllib.request.Request(f"{parsed.scheme}://{parsed.netloc}/api/cvat/session", headers={"Authorization": f"Bearer {parsed.fragment.removeprefix('token=')}"})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(json.load(response)["mode"], "review")
        self.assertEqual((xml.read_bytes(), xml.stat().st_mtime_ns), saved)


if __name__ == "__main__":
    unittest.main()
