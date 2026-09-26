"""Синхронный CDP-клиент поверх одного websocket к browser-level endpoint Chrome.

Один вызов за раз (лок вокруг send/recv; ожидание лока входит в таймаут вызова), без собственных потоков-читателей.
Сообщения без `id` (события) складываются в ограниченную очередь; отсоединение или падение нашей вкладки помечает её
сессию мёртвой (`TabGone`). События `Network.*` в очередь не идут: по ним ведётся учёт незавершённых запросов сессии
(`pending_since`, docs/plan-waits.md §5.1); читаются они внутри `call` и насосом `wait_events`. Запросы документа,
который сменила навигация главного фрейма, из учёта снимаются: Chrome не присылает им завершения.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.protocol import State
from websockets.sync.client import ClientConnection, connect

log = logging.getLogger(__name__)

EVENTS_MAX = 200
# Незавершённых запросов одной сессии, которые помним (long-poll, запрос, для которого Chrome не прислал завершение):
# сверх — забываем самый старый. Счётчик, не время: с запасом больше, чем запросов у тяжёлой страницы за одно действие
# (сотни), и ограничивает память, если сеть включат во вкладке с бесконечным потоком запросов.
NETWORK_PENDING_MAX = 1000
# Не запросы «страница ждёт ответа»: соединение живёт сколько угодно (docs/plan-waits.md §2 п. 2).
UNTRACKED_TYPES = frozenset({"WebSocket", "EventSource"})
STREAM_MIME = "text/event-stream"
FINISHED = frozenset({"Network.loadingFinished", "Network.loadingFailed", "Network.requestServedFromCache"})


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


@dataclass
class NetworkState:
    """Сеть одной сессии: незавершённые запросы (requestId → `seq` события `requestWillBeSent`), фоновые среди них
    (пережили предохранитель ожидания) и счётчики завершений и WS-кадров — подсказки «что-то пришло» для ожидания
    изменения.

    Смена документа главного фрейма: Chrome не присылает loadingFinished/Failed запросам документа, который сменился
    (зонд 26.09: fetch и уход со страницы — запрос «в полёте» навсегда). Поэтому у запроса помнится `loaderId`
    документа; навигация главного фрейма (запрос `Document` его фрейма) начинает новое поколение, а когда новый документ
    точно на месте (его запрос `Document` завершён или пошёл его подресурс), запросы прежних документов снимаются, и их
    поздние запросы не считаются. Навигация без commit (загрузка файла, 204 — `loadingFailed` запроса документа) —
    прежний документ жив, его запросы остаются."""

    pending: dict[str, int] = field(default_factory=dict)
    background: set[str] = field(default_factory=set)
    finished: int = 0
    ws_frames: int = 0
    loaders: dict[str, str] = field(default_factory=dict)  # requestId → loaderId (у незавершённых, где он есть)
    documents: set[str] = field(default_factory=set)  # loaderId документов нынешнего поколения (главный и его фреймы)
    leaving: set[str] = field(default_factory=set)  # loaderId документов, которые сменяет идущая навигация
    gone: set[str] = field(default_factory=set)  # loaderId сменившихся документов: их запросы не считаем
    navigation: str | None = None  # requestId запроса Document главного фрейма, пока он не завершён
    navigation_loader: str | None = None


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
        self.seq = 0  # принятых сообщений (ответы и события): эпоха ожидания — seq в момент команды действия
        self.network: dict[str, NetworkState] = {}  # по sessionId; заводится первым событием Network.* сессии
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
        """Отправить команду и дождаться ответа с тем же `id`; события по пути — в `self.events` (и учёт сети).

        `timeout` — на весь вызов, включая ожидание лока: вызов из другого потока (закрытие вкладок при выходе)
        не ждёт чужой долгий вызов дольше своего таймаута.
        """
        result = self._call(method, params, session_id, timeout, None)
        assert result is not None  # без `stop` вызов кончается ответом или исключением
        return result

    def call_until(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float | None = None,
        stop: Callable[[], bool],
    ) -> dict[str, Any] | None:
        """Как `call`, но None, как только после очередного события `stop()` истинно: ответ не ждём, он придёт позже
        и будет пропущен (DEBUG). Так ожидание изменения кончается и по сетевому событию, пока промис в странице ждёт
        мутацию."""
        return self._call(method, params, session_id, timeout, stop)

    def _call(
        self,
        method: str,
        params: dict[str, Any] | None,
        session_id: str | None,
        timeout: float | None,
        stop: Callable[[], bool] | None,
    ) -> dict[str, Any] | None:
        limit = self.call_timeout if timeout is None else timeout
        deadline = time.monotonic() + limit
        if not self._lock.acquire(timeout=max(0.0, limit)):
            raise CDPTimeout(f"{method}: клиент занят другим вызовом дольше {limit:.1f} с")
        try:
            return self._call_locked(method, params, session_id, limit, deadline, stop)
        finally:
            self._lock.release()

    def _call_locked(
        self,
        method: str,
        params: dict[str, Any] | None,
        session_id: str | None,
        limit: float,
        deadline: float,
        stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any] | None:
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
            message = self._receive(raw)
            if message is None:
                if session_id is not None and session_id in self._dead_sessions:
                    raise TabGone(f"{method}: вкладка закрыта или упала")
                if stop is not None and stop():
                    return None
                continue
            if message["id"] != msg_id:
                # Запоздалый ответ на вызов, у которого уже истёк таймаут (или прерванный `call_until`).
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

    def wait_events(self, session_id: str, until: Callable[[], bool], timeout: float) -> bool:
        """Насос событий без команды: под локом читать кадры, каждое событие — в учёт (`_on_event`), ответы с чужим
        `id` — DEBUG. True — `until()` истинно (проверяется до чтения и после каждого кадра); False — `timeout` вышел
        или клиент занят дольше него. Сессия умерла — TabGone, соединение закрыто — ChromeDisconnected, как в `call`."""
        deadline = time.monotonic() + timeout
        if not self._lock.acquire(timeout=max(0.0, timeout)):
            return until()
        try:
            while True:
                if self._closed:
                    raise ChromeDisconnected("wait_events: соединение с Chrome закрыто")
                if session_id in self._dead_sessions:
                    raise TabGone("wait_events: вкладка закрыта или упала")
                if until():
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    raw = self._ws.recv(timeout=remaining)
                except TimeoutError:
                    return until()
                except ConnectionClosed:
                    self._mark_closed()
                    raise ChromeDisconnected("wait_events: соединение с Chrome закрыто") from None
                message = self._receive(raw)
                if message is not None:
                    log.debug("CDP: пропущен ответ id=%s (насос событий)", message.get("id"))
        finally:
            self._lock.release()

    def _receive(self, raw: str | bytes) -> dict[str, Any] | None:
        """Принятый кадр: `seq` + 1; событие — в `_on_event` и None; ответ (есть `id`) — наружу."""
        self.seq += 1
        message = json.loads(raw)
        if "id" in message:
            return message
        self._on_event(message)
        return None

    # --- сеть сессии -------------------------------------------------------------------------------------------

    def pending_since(self, session_id: str, seq: int) -> int:
        """Незавершённых нефоновых запросов сессии, начатых после эпохи `seq` (WebSocket, event-stream — не в счёт)."""
        state = self.network.get(session_id)
        if state is None:
            return 0
        return sum(1 for rid, start in state.pending.items() if start > seq and rid not in state.background)

    def in_flight_since(self, session_id: str, seq: int) -> int:
        """Незавершённых запросов сессии, начатых после эпохи `seq`, в том числе фоновых (пережили предохранитель, но
        ответа ещё нет): «страница ещё загружается» для наблюдения Jev и правила «нет изменений» (WebSocket,
        event-stream — не в счёт, как в `pending_since`)."""
        state = self.network.get(session_id)
        if state is None:
            return 0
        return sum(1 for start in state.pending.values() if start > seq)

    def mark_background(self, session_id: str) -> int:
        """Все незавершённые запросы сессии — фоновые (пережили предохранитель): больше не держат ожиданий. Итог —
        сколько их."""
        state = self.network.get(session_id)
        if state is None:
            return 0
        state.background.update(state.pending)
        return len(state.pending)

    def network_marks(self, session_id: str) -> tuple[int, int]:
        """(завершений запросов, WS-кадров) сессии с начала учёта — сравнить «до» и «после»."""
        state = self.network.get(session_id)
        return (0, 0) if state is None else (state.finished, state.ws_frames)

    def forget_network(self, session_id: str) -> None:
        """Сеть сессии выключена или сессия отсоединена: учёт больше не нужен."""
        self.network.pop(session_id, None)

    def _on_network(self, session_id: str | None, method: str, params: dict[str, Any]) -> None:
        if session_id is None or session_id in self._dead_sessions:
            return
        state = self.network.setdefault(session_id, NetworkState())
        if method in {"Network.webSocketFrameReceived", "Network.webSocketFrameSent"}:
            state.ws_frames += 1
            return
        rid = params.get("requestId")
        if not isinstance(rid, str):
            return
        if method == "Network.requestWillBeSent":
            loader = params.get("loaderId") if isinstance(params.get("loaderId"), str) else ""
            if loader in state.gone:
                return  # запрос документа, который уже сменился: завершения Chrome не пришлёт
            main = self._session_targets.get(session_id)  # у вкладки Chrome id главного фрейма = targetId
            if params.get("type") == "Document" and loader and main and params.get("frameId") == main:
                if loader != state.navigation_loader:
                    self._navigation_started(state, rid, loader)
            elif loader and loader == state.navigation_loader:
                self._navigation_committed(state)  # подресурс нового документа: он уже на месте
            # Редирект приходит тем же requestId — это тот же запрос, эпоха прежняя.
            if params.get("type") in UNTRACKED_TYPES or rid in state.pending:
                return
            state.pending[rid] = self.seq
            if loader:
                state.loaders[rid] = loader
                if loader not in state.leaving:
                    state.documents.add(loader)
            if len(state.pending) > NETWORK_PENDING_MAX:
                oldest = next(iter(state.pending))
                del state.pending[oldest]
                state.background.discard(oldest)
                state.loaders.pop(oldest, None)
                log.debug("CDP: сеть %s — больше %d запросов в полёте, забыт старший", session_id, NETWORK_PENDING_MAX)
        elif method in FINISHED:
            self._finish_request(state, rid)
            if rid == state.navigation:
                if method == "Network.loadingFailed":
                    self._navigation_aborted(state)
                else:
                    self._navigation_committed(state)
        elif method == "Network.responseReceived":
            response = params.get("response") or {}
            if params.get("type") in UNTRACKED_TYPES or response.get("mimeType") == STREAM_MIME:
                self._finish_request(state, rid)  # поток событий: ответ не кончится, страница его не «ждёт»

    @staticmethod
    def _navigation_started(state: NetworkState, rid: str, loader: str) -> None:
        """Запрос `Document` главного фрейма: документы нынешнего поколения уходят (до commit они ещё живы)."""
        state.leaving |= state.documents
        state.leaving.discard(loader)
        state.documents = {loader}
        state.navigation, state.navigation_loader = rid, loader

    @staticmethod
    def _navigation_committed(state: NetworkState) -> None:
        """Новый документ на месте: запросы ушедших документов снять (завершения не будет), их поздние — не считать."""
        orphans = [r for r, loader in state.loaders.items() if loader in state.leaving]
        for r in orphans:
            state.pending.pop(r, None)
            state.background.discard(r)
            state.loaders.pop(r, None)
        if orphans:
            log.debug("CDP: смена документа — сняты запросы прежнего: %d", len(orphans))
        state.gone = state.leaving
        state.leaving = set()
        state.navigation = state.navigation_loader = None

    @staticmethod
    def _navigation_aborted(state: NetworkState) -> None:
        """Навигация без commit (загрузка файла, 204): прежний документ жив, его запросы — снова его."""
        state.documents |= state.leaving
        state.leaving = set()
        state.navigation = state.navigation_loader = None

    @staticmethod
    def _finish_request(state: NetworkState, rid: str) -> None:
        if rid in state.pending:
            del state.pending[rid]
            state.background.discard(rid)
            state.loaders.pop(rid, None)
            state.finished += 1

    def _on_event(self, message: dict[str, Any]) -> None:
        method = message.get("method") or ""
        params = message.get("params") or {}
        if method.startswith("Network."):
            self._on_network(message.get("sessionId"), method, params)
            return  # поток сетевых событий (в том числе WS-кадры) не вытесняет из очереди события жизненного цикла
        self.events.append(message)
        if method == "Target.detachedFromTarget":
            session = params.get("sessionId")
            if session:
                self._dead_sessions.add(session)
                self.network.pop(session, None)
        elif method == "Target.targetCrashed" or method == "Target.targetDestroyed":
            target = params.get("targetId")
            for session, owner in self._session_targets.items():
                if owner == target:
                    self._dead_sessions.add(session)
                    self.network.pop(session, None)
        elif method in {"Inspector.detached", "Inspector.targetCrashed"} and message.get("sessionId"):
            self._dead_sessions.add(message["sessionId"])
            self.network.pop(message["sessionId"], None)

    def _mark_closed(self) -> None:
        self._closed = True

    def close(self) -> None:
        """Закрыть websocket без лока: `recv` вызова в другом потоке получит ChromeDisconnected. Идемпотентно."""
        self._closed = True
        try:
            self._ws.close()
        except Exception as exc:  # закрытие не должно ронять вызывающего
            log.debug("CDP: ошибка при закрытии ws: %s", exc)
