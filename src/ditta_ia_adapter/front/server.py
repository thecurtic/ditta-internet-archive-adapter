"""A small threaded HTTP server around Node.

Standard library only. It listens on a local address; TLS belongs to a
reverse proxy in front of it, which the spec requires for any link the
operator does not control.
"""

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .node import Node, Reply

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024


def make_server(node: Node, host: str, port: int, path_prefix: str = "") -> ThreadingHTTPServer:
    """A server that answers DITTA paths, optionally under a path prefix."""
    prefix = path_prefix.rstrip("/")

    class Handler(BaseHTTPRequestHandler):
        server_version = "ditta-ia-adapter"
        sys_version = ""

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if prefix and path.startswith(prefix + "/"):
                path = path[len(prefix):]
            length = self.headers.get("Content-Length")
            if length is None or not length.isdigit():
                self._send(node.error(411, "malformed", None, "Content-Length is required"))
                return
            if int(length) > MAX_BODY_BYTES:
                # A clean rejection, never a crash (spec, Security considerations).
                self._send(node.error(413, "malformed", None, "request body too large"))
                self.close_connection = True
                return
            self._send(node.handle(path, self.rfile.read(int(length))))

        def do_GET(self) -> None:
            self._send(node.error(405, "rejected", None, "requests are POSTed"))

        def _send(self, reply: Reply) -> None:
            self.send_response(reply.status)
            for name, value in reply.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            self.wfile.write(reply.body)

        def log_message(self, format: str, *args) -> None:
            # Request line and status only: no bodies (so no queries) and no
            # client addresses ever reach the logs.
            log.info(format, *args)

    return ThreadingHTTPServer((host, port), Handler)
