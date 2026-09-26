"""Заглушка HTTP-сервера модели на 127.0.0.1: ответ по сценарию — паузы, пробелы keepalive, тело; HTTP/1.1 и h2c.

Так OpenRouter держит соединение, пока ждёт провайдера (25.09): статус 200 сразу, потом пробелы, потом тело. Каждый
запрос получает следующий ответ из списка (последний повторяется). Без сети наружу и без платных вызовов.
"""

from __future__ import annotations

import json
import select
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import h2.config
import h2.connection
import h2.events


@dataclass
class Reply:
    parts: list[tuple[float, bytes]] = field(default_factory=list)  # (пауза перед куском, байты)
    status: int = 200
    end: bool = True  # False — после кусков молчать, не закрывая ответ


def keepalive(every: float, count: int, then: Any = None) -> Reply:
    """`count` пробелов раз в `every` с; потом JSON `then` и конец ответа, а без `then` — молчание без конца."""
    parts = [(every, b" ")] * count
    if then is not None:
        parts.append((0.0, json.dumps(then).encode()))
    return Reply(parts, end=then is not None)


def answer(payload: Any, status: int = 200) -> Reply:
    return Reply([(0.0, json.dumps(payload).encode())], status=status)


class FakeModelServer:
    """`with FakeModelServer([Reply, …], http2=False) as server:` — `server.url`, `server.requests` (тела JSON)."""

    def __init__(self, replies: list[Reply], *, http2: bool = False) -> None:
        self.replies = list(replies)
        self.http2 = http2
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    def next_reply(self, body: bytes, headers: dict[str, str]) -> Reply:
        with self._lock:
            self.requests.append(json.loads(body or b"{}"))
            self.headers.append(headers)
            return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1"

    def __enter__(self) -> FakeModelServer:
        if self.http2:
            self._sock = socket.create_server(("127.0.0.1", 0))
            self.port = self._sock.getsockname()[1]
            self._spawn(self._accept_h2)
        else:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
            self._httpd.daemon_threads = True
            self.port = self._httpd.server_address[1]
            self._spawn(self._httpd.serve_forever, 0.05)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop.set()
        if self.http2:
            self._sock.close()
        else:
            self._httpd.shutdown()
            self._httpd.server_close()
        for thread in self._threads:
            thread.join(timeout=2)

    def _spawn(self, target: Any, *args: Any) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)

    # --- h2c (prior knowledge): один поток на соединение, потоки HTTP/2 по расписанию ----------------------------

    def _accept_h2(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self._spawn(self._serve_h2, conn)

    def _serve_h2(self, conn: socket.socket) -> None:
        h2c = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False, header_encoding="utf-8"))
        h2c.initiate_connection()
        bodies: dict[int, bytearray] = {}
        headers: dict[int, dict[str, str]] = {}
        schedule: dict[int, tuple[list[tuple[float, bytes]], bool]] = {}  # поток → (куски с моментом отправки, end)
        try:
            conn.sendall(h2c.data_to_send())
            while not self.stop.is_set():
                if select.select([conn], [], [], 0.01)[0]:
                    data = conn.recv(65535)
                    if not data:
                        return
                    for event in h2c.receive_data(data):
                        if isinstance(event, h2.events.RequestReceived):
                            headers[event.stream_id] = {str(k): str(v) for k, v in event.headers or ()}
                            bodies[event.stream_id] = bytearray()
                        elif isinstance(event, h2.events.DataReceived):
                            bodies[event.stream_id] += event.data or b""
                            h2c.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                        elif isinstance(event, h2.events.StreamEnded):
                            sid = event.stream_id
                            reply = self.next_reply(bytes(bodies.pop(sid)), headers.pop(sid))
                            h2c.send_headers(
                                sid, [(":status", str(reply.status)), ("content-type", "application/json")]
                            )
                            at, parts = time.monotonic(), []
                            for delay, chunk in reply.parts:
                                at += delay
                                parts.append((at, chunk))
                            schedule[sid] = (parts, reply.end)
                        elif isinstance(event, h2.events.StreamReset):
                            schedule.pop(event.stream_id, None)
                        elif isinstance(event, h2.events.ConnectionTerminated):
                            return
                now = time.monotonic()
                for sid, (parts, end) in list(schedule.items()):
                    while parts and parts[0][0] <= now:
                        h2c.send_data(sid, parts.pop(0)[1])
                    if not parts:
                        del schedule[sid]
                        if end:
                            h2c.end_stream(sid)
                conn.sendall(h2c.data_to_send())
        except OSError:
            return
        finally:
            conn.close()


def _handler(server: FakeModelServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 — имя задаёт http.server
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            reply = server.next_reply(body, {k.lower(): v for k, v in self.headers.items()})
            self.send_response(reply.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.flush()
            try:
                for delay, chunk in reply.parts:
                    if server.stop.wait(delay):
                        return
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                if reply.end:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                else:
                    server.stop.wait(60)  # молчим, пока тест не закроет сервер
                    self.close_connection = True
            except OSError:  # клиент ушёл по своему лимиту
                self.close_connection = True

        def log_message(self, *_args: Any) -> None:
            pass

    return Handler
