"""A local HTTP(S) server standing in for users' webhook endpoints: it records what it receives
and answers as a test scripts it."""

import http.server
import socketserver
import ssl
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Reply:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    # Seconds to wait before answering.
    delay: float = 0
    # Answer only once this is set (or the receiver stops).
    hang: threading.Event | None = None
    # Seconds between the bytes of the status line and headers.
    trickle: float | None = None
    # A body this long, streamed until the client stops reading.
    body_bytes: int = 0


@dataclass(frozen=True)
class Received:
    method: str
    path: str
    # Names in lowercase.
    headers: dict[str, str]
    body: bytes
    arrived: float


class Receiver:
    """Answers with `default`, or with the replies in `replies` first, in order, or with the one
    in `per_event` for an event's webhook-id."""

    def __init__(self, ssl_context: ssl.SSLContext | None = None, host: str = "127.0.0.1"):
        self.requests: list[Received] = []
        self.replies: list[Reply] = []
        self.per_event: dict[str, Reply] = {}
        self.default = Reply()
        self.connections = 0
        self.in_flight = 0
        self.peak_in_flight = 0
        self.first_request = threading.Event()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._host = host
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.receiver = self
        self._server.ssl_context = ssl_context
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def url(self, path: str = "/hook") -> str:
        scheme = "https" if self._server.ssl_context else "http"
        return f"{scheme}://{self._host}:{self.port}{path}"

    def respond(self, *replies: Reply) -> None:
        self.replies.extend(replies)

    def stop(self) -> None:
        self._stopped.set()
        for reply in [self.default, *self.replies, *self.per_event.values()]:
            if reply.hang is not None:
                reply.hang.set()
        self._server.shutdown()
        self._server.server_close()

    def _received(self, request: Received) -> Reply:
        with self._lock:
            self.requests.append(request)
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            reply = self.per_event.get(request.headers.get("webhook-id", ""))
            if reply is None:
                reply = self.replies.pop(0) if self.replies else self.default
        self.first_request.set()
        return reply

    def _answered(self) -> None:
        with self._lock:
            self.in_flight -= 1

    def _connected(self) -> None:
        with self._lock:
            self.connections += 1


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    receiver: Receiver
    ssl_context: ssl.SSLContext | None

    def process_request_thread(self, request, client_address) -> None:
        self.receiver._connected()
        if self.ssl_context is not None:
            try:
                request = self.ssl_context.wrap_socket(request, server_side=True)
            except (OSError, ssl.SSLError):
                request.close()
                return
        super().process_request_thread(request, client_address)

    def handle_error(self, request, client_address) -> None:
        pass  # Clients hanging up early are expected.


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, format, *args) -> None:
        pass

    def do_POST(self) -> None:
        receiver = self.server.receiver
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        reply = receiver._received(
            Received(
                method=self.command,
                path=self.path,
                headers={name.lower(): value for name, value in self.headers.items()},
                body=body,
                arrived=time.monotonic(),
            )
        )
        try:
            self._answer(reply)
        except OSError:
            pass  # The client gave up.
        finally:
            receiver._answered()
            self.close_connection = True

    def _answer(self, reply: Reply) -> None:
        stopped = self.server.receiver._stopped
        if reply.delay:
            stopped.wait(reply.delay)
        if reply.hang is not None:
            reply.hang.wait(30)
        headers = {"Content-Length": str(reply.body_bytes), "Connection": "close", **reply.headers}
        head = f"HTTP/1.1 {reply.status} Status\r\n" + "".join(
            f"{name}: {value}\r\n" for name, value in headers.items()
        )
        head = (head + "\r\n").encode()
        if reply.trickle is None:
            self.wfile.write(head)
        else:
            for byte in head:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                if stopped.wait(reply.trickle):
                    return
        chunk = b"x" * 65536
        sent = 0
        while sent < reply.body_bytes and not stopped.is_set():
            self.wfile.write(chunk[: reply.body_bytes - sent])
            sent += len(chunk)
        self.wfile.flush()
