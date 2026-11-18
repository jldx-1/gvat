import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

from gvat_local.server import Handler, LocalServer
from gvat_local.workspace import Workspace, WorkspaceError


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ("images", "labels", "assets"):
            (self.root / name).mkdir()
        Image.new("RGB", (20, 20)).save(self.root / "images" / "one.png")
        (self.root / "classes.txt").write_text("cat\n")
        (self.root / "assets" / "index.html").write_text("<title>GVAT</title>")
        self.workspace = Workspace(self.root / "images", self.root / "labels", self.root / "classes.txt")
        self.server = LocalServer(self.workspace, self.root / "assets", "test-secret", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def request(self, path, body=None, headers=None):
        defaults = {"Authorization": "Bearer test-secret"}
        defaults.update(headers or {})
        request = urllib.request.Request(self.server.origin + path, data=json.dumps(body).encode() if body is not None else None, headers=defaults, method="PUT" if body is not None else "GET")
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    def test_full_api_roundtrip_and_stale_save(self):
        status, raw = self.request("/api/frame?path=one.png")
        self.assertEqual(status, 200)
        frame = json.loads(raw)
        payload = {**frame, "annotations": [{"class_id": 0, "cx": .5, "cy": .5, "width": .2, "height": .2}]}
        self.assertEqual(self.request("/api/labels?path=one.png", payload)[0], 200)
        self.assertEqual(self.request("/api/labels?path=one.png", payload)[0], 409)
        self.assertEqual((self.root / "labels" / "one.txt").read_text(), "0 0.5 0.5 0.2 0.2\n")

    def test_session_token_and_origin_and_host_are_checked(self):
        self.assertEqual(self.request("/api/workspace", headers={"Authorization": ""})[0], 401)
        self.assertEqual(self.request("/api/workspace", headers={"Origin": "https://attacker.example"})[0], 403)
        self.assertEqual(self.request("/api/workspace", headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("/api/workspace")[0], 200)
        self.assertEqual(self.request("/")[0], 200)

    def test_invalid_requests_and_traversal_do_not_mutate_files(self):
        self.assertEqual(self.request("/api/frame?path=../classes.txt")[0], 400)
        self.assertEqual(self.request("/../classes.txt")[0], 400)
        self.assertEqual(self.request("/api/labels?path=one.png", {"annotations": []})[0], 409)
        self.assertEqual(self.request("/api/review", {"revision": "missing", "issues": [None]})[0], 400)
        self.assertEqual(list((self.root / "labels").iterdir()), [])

    def test_image_snapshot_changes_are_reported(self):
        self.assertEqual(self.request("/api/image?path=one.png&revision=old")[0], 409)
        self.assertEqual(self.request("/api/image?path=one.png")[0], 200)

    @unittest.skipUnless(sys.platform == "linux", "Linux TCP port reuse regression")
    def test_can_restart_on_same_port_immediately_after_serving_browser_request(self):
        port = self.server.server_port
        # Read through the server's FIN, rather than letting urllib close first
        # at Content-Length, to reproduce a browser connection in TIME_WAIT.
        with socket.create_connection(("127.0.0.1", port), timeout=3) as browser:
            browser.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
            response = b""
            while chunk := browser.recv(4096):
                response += chunk
        self.assertIn(b"200 OK", response)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.server = LocalServer(self.workspace, self.root / "assets", "next-session", port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.assertEqual(self.server.server_port, port)
        self.assertEqual(self.request("/api/workspace")[0], 401)
        self.assertEqual(self.request("/api/workspace", headers={"Authorization": "Bearer next-session"})[0], 200)

    def test_live_listener_is_preserved_and_busy_port_error_explains_alternative(self):
        with self.assertRaisesRegex(WorkspaceError, f"Port {self.server.server_port} is already in use.*--port=0"):
            LocalServer(self.workspace, self.root / "assets", "another-session", self.server.server_port)
        self.assertEqual(self.request("/")[0], 200)

    def test_abandoned_response_does_not_send_another_error_on_closed_socket(self):
        for failure in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            for stage in ('headers', 'body'):
                with self.subTest(failure=failure, stage=stage):
                    handler = object.__new__(Handler)
                    handler.send_response = Mock()
                    handler.send_header = Mock()
                    handler.end_headers = Mock(side_effect=failure() if stage == 'headers' else None)
                    handler.wfile = Mock()
                    if stage == 'body':
                        handler.wfile.write.side_effect = failure()
                    handler._send(200, b'frame', 'image/png')
                    self.assertTrue(handler.close_connection)
                    self.assertEqual(handler.send_response.call_count, 1)


if __name__ == "__main__":
    unittest.main()
