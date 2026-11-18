"""Loopback-only HTTP interface for one explicitly selected workspace."""

from __future__ import annotations

import errno
import hmac
import json
import mimetypes
import select
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .workspace import Conflict, Workspace, WorkspaceError, contained
from .video_decode import DecodeCancelled


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    # Linux keeps closed browser connections in TIME_WAIT after Ctrl+C.
    # SO_REUSEADDR permits an immediate restart, while a live listener still wins.
    allow_reuse_address = True

    def __init__(self, workspace: Workspace, assets: Path, token: str, port: int = 8080):
        self.workspace = workspace
        self.assets = assets.resolve()
        self.token = token
        self.native = None
        try:
            super().__init__(("127.0.0.1", port), Handler)
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                raise WorkspaceError(f"Port {port} is already in use. Stop the other instance or use --port=0 to choose a free port.") from error
            raise
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def server_close(self):
        if self.native is not None:
            self.native.chunks.close()
            self.native.exports.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: LocalServer

    def log_message(self, format: str, *args: object) -> None:
        # Avoid logging sample paths or authentication material.
        if args and isinstance(args[0], str) and " /api/" in args[0]:
            return
        super().log_message(format, *args)

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; worker-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Scrubbing/reloading can abandon a preview. The socket is closed;
            # sending an HTTP error here would just produce another traceback.
            self.close_connection = True

    def _disconnected(self):
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            return bool(ready) and not self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        except (ConnectionError, OSError):
            return True

    def _send_export(self, file, filename):
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Length', str(file.stat().st_size))
            safe_name = ''.join(character if character.isascii() and (character.isalnum() or character in '._ -') else '_' for character in filename)
            self.send_header('Content-Disposition', f'attachment; filename="{safe_name}"')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            with file.open('rb') as source:
                while block := source.read(256 * 1024):
                    self.wfile.write(block)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _json(self, status: int, data: object) -> None:
        self._send(status, json.dumps(data, allow_nan=False).encode(), "application/json; charset=utf-8")

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 16 * 1024 * 1024:
            raise WorkspaceError("Request body must be between 1 byte and 16 MiB")
        body = json.loads(self.rfile.read(size))
        if not isinstance(body, dict):
            raise WorkspaceError("Expected a JSON object")
        return body

    def _dispatch(self, method: str) -> None:
        try:
            if self.headers.get("Host") != self.server.origin.removeprefix("http://"):
                self._json(403, {"error": "Use the localhost address printed by GVAT"})
                return
            origin = self.headers.get("Origin")
            if origin and origin != self.server.origin:
                self._json(403, {"error": "Cross-origin requests are not permitted"})
                return
            url = urlsplit(self.path)
            if not url.path.startswith("/api/"):
                if method != "GET":
                    self._json(405, {"error": "Method not allowed"})
                    return
                relative = "index.html" if url.path in ("/", "/tasks/1/jobs/1", "/tasks/1/jobs/1/") else url.path.lstrip("/")
                file = contained(self.server.assets, relative)
                if not file.is_file():
                    self._json(404, {"error": "Asset not found. Run ./install.sh to build the editor."})
                    return
                self._send(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or "application/octet-stream")
                return
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {self.server.token}"):
                self._json(401, {"error": "Open the session URL printed by GVAT to connect"})
                return
            query = parse_qs(url.query)
            image = query.get("path", [""])[0]
            workspace = self.server.workspace
            if url.path.startswith("/api/cvat/"):
                from .native import NativeSession

                with workspace.lock:
                    if self.server.native is None:
                        self.server.native = NativeSession(workspace)
                native = self.server.native
                endpoint = url.path.removeprefix("/api/cvat/")
                if method == "GET" and endpoint == "session":
                    self._json(200, native.snapshot())
                elif method == "GET" and endpoint == "formats":
                    from .exports import public_formats

                    self._json(200, public_formats())
                elif method == "POST" and endpoint == "exports":
                    body = self._body()
                    self._json(200, native.exports.begin(native.chunk_view(body.get('view')), body))
                elif endpoint.startswith('exports/'):
                    identifier = endpoint.split('/')[1]
                    if method == 'DELETE':
                        native.exports.release(identifier)
                        self._json(200, {})
                    elif method == 'PUT':
                        native.exports.get(identifier).append(self._body())
                        self._json(200, {})
                    elif method == 'GET':
                        export = native.exports.get(identifier)
                        try:
                            try:
                                file = export.finish()
                            except (WorkspaceError, OSError):
                                raise
                            except Exception as error:
                                raise WorkspaceError(f'Could not export {export.format}: {error}') from error
                            self._send_export(file, export.filename)
                        finally:
                            native.exports.release(identifier)
                    else:
                        self._json(405, {'error': 'Method not allowed'})
                elif method == "POST" and endpoint == "chunks/activate":
                    body = self._body()
                    view = native.chunk_view(body.get("view"))
                    chunk = body.get("chunk")
                    if type(chunk) is not int or not 0 <= chunk * view["chunk_size"] < len(view["keys"]):
                        raise WorkspaceError("Chunk is outside the annotation folder")
                    native.chunks.activate(body["view"], chunk, body.get("generation"), view["buffer"]["prefetch_chunks"])
                    self._json(200, {})
                elif method == "GET" and endpoint == "chunks":
                    identifier = query.get("view", [""])[0]
                    view = native.chunk_view(identifier)
                    data = native.chunks.read(identifier, view["keys"], view["chunk_size"],
                                              int(query.get("chunk", ["-1"])[0]), int(query.get("generation", ["-1"])[0]),
                                              query.get("prefetch", ["0"])[0] == "1", self._disconnected)
                    self._send(200, data, "application/zip")
                elif method == "GET" and endpoint == "revision":
                    from .workspace import digest

                    self._json(200, {"revision": digest(workspace.document().raw)})
                elif method == "PUT" and endpoint == "annotations":
                    self._json(200, native.save(self._body()))
                elif method == "GET" and endpoint == "feedback":
                    self._json(200, {"markdown": native.feedback()})
                elif endpoint == "issues" and method == "POST":
                    self._json(200, native.issue(method, None, self._body()))
                elif endpoint.startswith("issues/") and method in ("PUT", "DELETE"):
                    self._json(200, native.issue(method, int(endpoint.split("/")[1]), self._body() if method == "PUT" else {}))
                elif endpoint == "comments" and method == "POST":
                    self._json(200, native.comment(self._body()))
                else:
                    self._json(404, {"error": "Unknown local annotation operation"})
            elif method == "GET" and url.path == "/api/workspace":
                self._json(200, workspace.manifest())
            elif method == "GET" and url.path == "/api/frame":
                self._json(200, workspace.frame(image))
            elif method == "GET" and url.path == "/api/image":
                data, content_type = workspace.image_bytes(image, query.get("revision", [None])[0])
                self._send(200, data, content_type)
            elif method == "PUT" and url.path == "/api/labels":
                self._json(200, workspace.save(image, self._body()))
            elif method == "GET" and url.path == "/api/review":
                self._json(200, workspace.review())
            elif method == "PUT" and url.path == "/api/review":
                self._json(200, workspace.save_review(self._body()))
            elif method == "GET" and url.path == "/api/feedback":
                self._json(200, {"markdown": workspace.markdown(query.get("resolved") == ["1"])})
            else:
                self._json(404, {"error": "Unknown endpoint"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except DecodeCancelled:
            self._json(409, {"cancelled": True})
        except Conflict as error:
            self._json(409, {"error": str(error)})
        except (WorkspaceError, ValueError, TypeError, KeyError, UnicodeError) as error:
            self._json(400, {"error": str(error)})
        except OSError as error:
            self._json(422, {"error": str(error)})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")
