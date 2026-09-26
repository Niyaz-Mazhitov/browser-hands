"""Фейковый CDP-сервер для офлайн-тестов: websockets.sync.server на 127.0.0.1:0, поведение по методу."""

from __future__ import annotations

import itertools
import json
import re
import threading
from collections.abc import Callable
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import Server, ServerConnection, serve

Handler = Callable[[dict[str, Any], ServerConnection], None]

# Выражения меток вкладки пользователя (browser.Tab.claim / release) — фейк исполняет их над `windows`, как JS страницы.
CLAIM = re.compile(r'window\.__bhOwner \?\?= (".*")')
CLEANUP = re.compile(r'delete window\.__jevFast(?:; if \(window\.__bhOwner === (".*")\) delete window\.__bhOwner)?')


def reply(ws: ServerConnection, frame: dict[str, Any], result: dict[str, Any] | None = None) -> None:
    message: dict[str, Any] = {"id": frame["id"], "result": result or {}}
    if "sessionId" in frame:
        message["sessionId"] = frame["sessionId"]
    ws.send(json.dumps(message))


def error(ws: ServerConnection, frame: dict[str, Any], text: str, code: int = -32000) -> None:
    ws.send(json.dumps({"id": frame["id"], "error": {"code": code, "message": text}}))


def event(ws: ServerConnection, method: str, params: dict[str, Any], session_id: str | None = None) -> None:
    message: dict[str, Any] = {"method": method, "params": params}
    if session_id is not None:
        message["sessionId"] = session_id
    ws.send(json.dumps(message))


class FakeCDPServer:
    """Отвечает как browser-level endpoint Chrome; `on[method]` переопределяет поведение метода.

    `targets` — что вернёт `Target.getTargets` (TargetInfo: вкладки пользователя в attach); по умолчанию пусто.
    `windows` — глобальные переменные страниц по targetId (метка `__bhOwner`, кэш `__jevFast`), см. `page_js`.
    """

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.on: dict[str, Handler] = {}
        self.targets: list[dict[str, Any]] = []
        self.windows: dict[str, dict[str, Any]] = {}
        self.connections = 0
        self._open: list[ServerConnection] = []
        self._targets = itertools.count(1)
        self._server: Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.socket.getsockname()[1]

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/devtools/browser/fake"

    def methods(self) -> list[str]:
        return [f["method"] for f in self.frames]

    def sent(self, method: str) -> list[dict[str, Any]]:
        """Кадры одного метода (с `params` и `sessionId`), по порядку."""
        return [f for f in self.frames if f["method"] == method]

    def start(self) -> FakeCDPServer:
        self._server = serve(self._handle, "127.0.0.1", 0, compression=None, ping_interval=None)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Как выход Chrome: закрыть слушающий сокет и все открытые соединения."""
        if self._server is not None:
            self._server.shutdown()
        for ws in self._open:
            ws.close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __enter__(self) -> FakeCDPServer:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _handle(self, ws: ServerConnection) -> None:
        self.connections += 1
        self._open.append(ws)
        try:
            for raw in ws:
                frame = json.loads(raw)
                self.frames.append(frame)
                handler = self.on.get(frame["method"], self._default)
                handler(frame, ws)
        except ConnectionClosed:
            pass

    def page_js(self, frame: dict[str, Any], ws: ServerConnection) -> bool:
        """Runtime.evaluate меток вкладки пользователя: `__bhOwner ??= …` и уборка из `release()` — над
        `windows[targetId]` (сессия `S-<targetId>`), как их исполнил бы JS страницы. True — ответили."""
        if frame["method"] != "Runtime.evaluate":
            return False
        expression = frame["params"]["expression"]
        target_id = frame.get("sessionId", "").removeprefix("S-")
        if match := CLAIM.fullmatch(expression):
            window = self.windows.setdefault(target_id, {})
            if window.get("__bhOwner") is None:
                window["__bhOwner"] = json.loads(match[1])
            reply(ws, frame, {"result": {"type": "string", "value": window["__bhOwner"]}})
            return True
        if match := CLEANUP.fullmatch(expression):
            window = self.windows.setdefault(target_id, {})
            window.pop("__jevFast", None)
            if match[1] is not None and window.get("__bhOwner") == json.loads(match[1]):
                del window["__bhOwner"]
            reply(ws, frame, {"result": {"type": "boolean", "value": True}})
            return True
        return False

    def _default(self, frame: dict[str, Any], ws: ServerConnection) -> None:
        method = frame["method"]
        if self.page_js(frame, ws):
            return
        if method == "Target.getTargets":
            reply(ws, frame, {"targetInfos": self.targets})
        elif method == "Target.createTarget":
            reply(ws, frame, {"targetId": f"T{next(self._targets)}"})
        elif method == "Target.attachToTarget":
            reply(ws, frame, {"sessionId": "S-" + frame["params"]["targetId"]})
        else:
            reply(ws, frame, {})
