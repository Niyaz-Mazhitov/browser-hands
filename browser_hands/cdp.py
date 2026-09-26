"""Синхронный CDP-клиент поверх одного websocket к browser-level endpoint Chrome.

Один вызов за раз (лок вокруг send/recv; ожидание лока входит в таймаут вызова), без собственных потоков-читателей.
Сообщения без `id` (события) складываются в ограниченную очередь; отсоединение или падение нашей вкладки помечает её
сессию мёртвой (`TabGone`).
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
import time
from collections import deque
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.protocol import State
from websockets.sync.client import ClientConnection, connect

log = logging.getLogger(__name__)

EVENTS_MAX = 200


class CDPException(Exception):
    """Базовая ошибка CDP."""


class CDPError(CDPException, RuntimeError):
    """Chrome ответил `{"error": ...}` на вызов."""

    def __init__(self, method: str, message: str, code: int | None = None) -> None:
        super().__init__(f"{method}: {message}")
        self.method = method
        self.message = message
        self.code = code


class CDPTimeout(CDPException, TimeoutError):
    """Ответ на вызов не пришёл за отведённое время."""


class ChromeDisconnected(CDPException, ConnectionError):
    """Соединение с Chrome закрыто."""


class TabGone(CDPException):
    """Вкладка (сессия) закрыта, отсоединена или упала."""


class CDPClient:
    """Прямой websocket к `ws://127.0.0.1:<port>/devtools/browser/<id>`; `Target.*` и flatten-сессии."""

    def __init__(
        self,
        ws_url: str,
        *,
        call_timeout: float = 30.0,
        connect_timeout: float = 10.0,
        connection: ClientConnection | None = None,
    ) -> None:
        self.ws_url = ws_url
        self.call_timeout = call_timeout
        self.events: deque[dict[str, Any]] = deque(maxlen=EVENTS_MAX)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._dead_sessions: set[str] = set()
        self._session_targets: dict[str, str] = {}
        self._closed = False
        # Локальное соединение: без прокси из env, без сжатия (снимки до сотен КБ), без keepalive-пингов;
        # живость проверяет Chrome.alive(). max_size=None — снимок страницы может быть больше 1 МиБ.
        self._ws = connection or connect(
            ws_url,
            open_timeout=connect_timeout,
            max_size=None,
            compression=None,
            proxy=None,
            ping_interval=None,
            close_timeout=2,
        )

    @property
    def connected(self) -> bool:
        """Соединение не закрыто нами и websocket открыт (без обращения к Chrome)."""
        return not self._closed and self._ws.state is State.OPEN

    def session_alive(self, session_id: str) -> bool:
        return session_id not in self._dead_sessions

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Отправить команду и дождаться ответа с тем же `id`; события по пути — в `self.events`.

        `timeout` — на весь вызов, включая ожидание лока: вызов из другого потока (закрытие вкладок при выходе)
        не ждёт чужой долгий вызов дольше своего таймаута.
        """
        limit = self.call_timeout if timeout is None else timeout
        deadline = time.monotonic() + limit
        if not self._lock.acquire(timeout=max(0.0, limit)):
            raise CDPTimeout(f"{method}: клиент занят другим вызовом дольше {limit:.1f} с")
        try:
            return self._call_locked(method, params, session_id, limit, deadline)
        finally:
            self._lock.release()

    def _call_locked(
        self, method: str, params: dict[str, Any] | None, session_id: str | None, limit: float, deadline: float
    ) -> dict[str, Any]:
        if self._closed:
            raise ChromeDisconnected(f"{method}: соединение с Chrome закрыто")
        if session_id is not None and session_id in self._dead_sessions:
            raise TabGone(f"{method}: вкладка закрыта или упала")
        msg_id = next(self._ids)
        frame: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id is not None:
            frame["sessionId"] = session_id
        try:
            self._ws.send(json.dumps(frame))
        except ConnectionClosed:
            self._mark_closed()
            raise ChromeDisconnected(f"{method}: соединение с Chrome закрыто") from None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CDPTimeout(f"{method}: нет ответа за {limit:.1f} с")
            try:
                raw = self._ws.recv(timeout=remaining)
            except TimeoutError:
                raise CDPTimeout(f"{method}: нет ответа за {limit:.1f} с") from None
            except ConnectionClosed:
                self._mark_closed()
                raise ChromeDisconnected(f"{method}: соединение с Chrome закрыто") from None
            message = json.loads(raw)
            if "id" not in message:
                self._on_event(message)
                if session_id is not None and session_id in self._dead_sessions:
                    raise TabGone(f"{method}: вкладка закрыта или упала")
                continue
            if message["id"] != msg_id:
                # Запоздалый ответ на вызов, у которого уже истёк таймаут.
                log.debug("CDP: пропущен ответ id=%s (ждём %s)", message["id"], msg_id)
                continue
            if "error" in message:
                error = message["error"] or {}
                text = str(error.get("message", "unknown error"))
                if session_id is not None and "session" in text.lower() and "not found" in text.lower():
                    self._dead_sessions.add(session_id)
                    raise TabGone(f"{method}: {text}")
                raise CDPError(method, text, error.get("code"))
            result = message.get("result") or {}
            if method == "Target.attachToTarget" and params and "sessionId" in result:
                self._session_targets[result["sessionId"]] = params.get("targetId", "")
            return result

    def _on_event(self, message: dict[str, Any]) -> None:
        self.events.append(message)
        method = message.get("method")
        params = message.get("params") or {}
        if method == "Target.detachedFromTarget":
            session = params.get("sessionId")
            if session:
                self._dead_sessions.add(session)
        elif method == "Target.targetCrashed" or method == "Target.targetDestroyed":
            target = params.get("targetId")
            for session, owner in self._session_targets.items():
                if owner == target:
                    self._dead_sessions.add(session)
        elif method in {"Inspector.detached", "Inspector.targetCrashed"} and message.get("sessionId"):
            self._dead_sessions.add(message["sessionId"])

    def _mark_closed(self) -> None:
        self._closed = True

    def close(self) -> None:
        """Закрыть websocket без лока: `recv` вызова в другом потоке получит ChromeDisconnected. Идемпотентно."""
        self._closed = True
        try:
            self._ws.close()
        except Exception as exc:  # закрытие не должно ронять вызывающего
            log.debug("CDP: ошибка при закрытии ws: %s", exc)
