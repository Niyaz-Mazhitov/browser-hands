"""Своя фоновая вкладка: атомарный снимок, проверка свежести и исполнение по наблюдаемому узлу.

Перенос `jev_ultrafast/browser.py` (MIT, Browser Use): `Browser` + `browser_operation` → `Tab` поверх прямого
CDP-клиента. Модель никогда не выдаёт селекторы, координаты или JS: цель — id узла из снимка (`snapshot.js`),
геометрия считается заново и проверяется на перекрытие прямо перед вводом.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .cdp import CDPClient, CDPError, CDPTimeout, ChromeDisconnected, TabGone
from .types import Timing

log = logging.getLogger(__name__)

# Атомарно читает видимый текст и контролы, сохраняя идентичность реальных DOM-узлов.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

# После ввода: ждём видимые подсказки комбобокса не дольше 200 мс, иначе два кадра или 50 мс.
# Только чтение; выполняется после того, как действие записано, даже если его прерывает навигация.
SETTLE = """(action => new Promise(resolve => {
  const field=window.__jevFast?.nodes.get(action.node);
  const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
  let frames=0, stopped=false;
  const finish=()=>{stopped=true;resolve()};
  setTimeout(finish,autocomplete ? 200 : 50);
  const ready=()=>{
    if (stopped) return;
    const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
      .split(/\\s+/).filter(Boolean);
    const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
    const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
    if (++frames>=2 && (!autocomplete || options.some(e=>{
      const r=e.getBoundingClientRect();
      return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
        e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
    }))) finish();
    else requestAnimationFrame(ready);
  };
  requestAnimationFrame(ready);
}))("""

# Код-владелец id узлов: цель — реальный наблюдаемый элемент, никогда не селектор от модели.
RESOLVE_TARGET = """(action => {
  const e=window.__jevFast?.nodes.get(action.node);
  if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
      !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
  if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
  const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
  if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
  if (!e.contains(document.elementFromPoint(x,y))) return null;
  if (action.kind==='select') {
    if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
        !o.disabled && !o.closest('optgroup[disabled]'))) return null;
    e.value=action.value;
    e.dispatchEvent(new Event('input',{bubbles:true}));
    e.dispatchEvent(new Event('change',{bubbles:true}));
  }
  return {x,y};
})("""

# После клика по полю и до ввода: фокус — на самой цели (или на её contenteditable-потомке) и это не
# password/file/hidden, в том числе внутри открытого shadow root. Иначе текст ушёл бы не в то поле.
FOCUSED = """(node => {
  const e=window.__jevFast?.nodes.get(node), a=document.activeElement;
  if (!e?.isConnected || !a || (a!==e && !(e.contains(a) && a.isContentEditable))) return false;
  let d=a;
  while (d.shadowRoot?.activeElement) d=d.shadowRoot.activeElement;
  return !(d.tagName==='INPUT' && ['password','file','hidden'].includes(d.type));
})("""

NAVIGATE_TIMEOUT_S = 15.0
STALE_RETRIES = 10
SCREENSHOT_TIMEOUT_S = 5.0
SELECT_ALL_MODIFIER = 4 if sys.platform == "darwin" else 2  # Meta на macOS, Ctrl иначе


class StalePage(ValueError):
    """Решение больше не относится к наблюдаемой странице."""


class NavigationFailed(RuntimeError):
    """`Page.navigate` вернул `errorText` (например, net::ERR_NAME_NOT_RESOLVED)."""


def url_host(url: str) -> str:
    """Хост url без пути, запроса и логина (для логов INFO); у about:/data: — схема."""
    parts = urlsplit(url)
    return parts.hostname or parts.scheme or "?"


def fingerprint(state: dict[str, Any]) -> str:
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


class Tab:
    """Одна своя вкладка (flatten-сессия). Замеры: CDP → `browser_ms`, ожидания и загрузка → `wait_ms`."""

    def __init__(
        self,
        client: CDPClient,
        session_id: str,
        target_id: str,
        *,
        screenshot_quality: int = 60,
        screenshot_scale: float = 1.0,
        viewport: tuple[int, int] = (1120, 780),
        on_release: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.session_id = session_id
        self.target_id = target_id
        self.screenshot_quality = screenshot_quality
        self.screenshot_scale = screenshot_scale
        self.viewport = viewport
        self.deadline: float | None = None  # monotonic; ограничивает таймауты CDP-вызовов агента
        self.after_input: dict[str, Any] | None = None
        self.closed = False
        self._on_release = on_release
        self._browser_s = 0.0
        self._wait_s = 0.0

    # --- замеры -------------------------------------------------------------------------------------------------

    def take_timing(self) -> Timing:
        """Накопленные `browser_ms`/`wait_ms` с прошлого вызова; счётчики обнуляются."""
        timing = Timing(browser_ms=round(self._browser_s * 1000), wait_ms=round(self._wait_s * 1000))
        self._browser_s = self._wait_s = 0.0
        return timing

    @contextmanager
    def _timed(self, wait: bool) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            if wait:
                self._wait_s += elapsed
            else:
                self._browser_s += elapsed

    def _as_wait(self, seconds: float) -> None:
        """CDP-вызов, прерванный навигацией, фактически ждал загрузку: переносим его время в `wait_ms`."""
        seconds = max(0.0, min(seconds, self._browser_s))
        self._browser_s -= seconds
        self._wait_s += seconds

    def _sleep(self, seconds: float) -> None:
        with self._timed(wait=True):
            time.sleep(seconds)

    def _budget(self) -> float | None:
        if self.deadline is None:
            return None
        return min(self.client.call_timeout, max(1.0, self.deadline - time.monotonic()))

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None, wait: bool = False
    ) -> dict[str, Any]:
        with self._timed(wait):
            return self.client.call(
                method, params, session_id=self.session_id, timeout=timeout if timeout is not None else self._budget()
            )

    def evaluate(self, expression: str, *, wait: bool = False) -> Any:
        mark = self._browser_s
        response = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, wait=wait)
        if response.get("exceptionDetails"):
            self._as_wait(self._browser_s - mark)
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    # --- жизненный цикл ----------------------------------------------------------------------------------------

    def setup(self) -> None:
        width, height = self.viewport
        self.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )
        # Держит rAF и меню в фоновой вкладке, не активируя видимую вкладку пользователя.
        self.call("Emulation.setFocusEmulationEnabled", {"enabled": True})

    def navigate(self, url: str, *, timeout: float = NAVIGATE_TIMEOUT_S) -> None:
        """`Page.navigate` и опрос `document.readyState` до `complete` (не дольше `timeout`); всё — `wait_ms`."""
        response = self.call("Page.navigate", {"url": url}, wait=True)
        if response.get("errorText"):
            raise NavigationFailed(f"Navigation to {url_host(url)} failed: {response['errorText']}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState", wait=True) == "complete":
                    return
            except (StalePage, CDPError):
                pass  # контекст пересоздаётся во время загрузки
            self._sleep(0.02)

    def close(self) -> None:
        """Закрыть свою вкладку; повторный вызов и обрыв соединения не бросают исключений."""
        if self.closed:
            return
        self.closed = True
        try:
            self.client.call("Target.closeTarget", {"targetId": self.target_id}, timeout=SCREENSHOT_TIMEOUT_S)
        except (ChromeDisconnected, CDPTimeout) as exc:
            log.warning("Вкладка %s могла остаться открытой: %s", self.target_id, exc)
            return  # остаётся в owned_targets: Chrome закроет её при следующем connect()
        except CDPError as exc:
            log.debug("closeTarget %s: %s", self.target_id, exc)  # уже закрыта
        if self._on_release:
            self._on_release(self.target_id)

    def release(self) -> None:
        """Оставить вкладку открытой (keep_open): отсоединить сессию (эмуляция снимается), забыть target."""
        if self.closed:
            return
        self.closed = True
        try:
            self.client.call("Target.detachFromTarget", {"sessionId": self.session_id}, timeout=SCREENSHOT_TIMEOUT_S)
        except (CDPError, CDPTimeout, ChromeDisconnected) as exc:
            log.debug("detachFromTarget %s: %s", self.session_id, exc)
        if self._on_release:
            self._on_release(self.target_id)

    # --- наблюдение --------------------------------------------------------------------------------------------

    def observe(self, screenshot: bool = False) -> dict[str, Any]:
        if self.after_input:
            action, self.after_input = self.after_input, None
            try:
                self.call(
                    "Runtime.evaluate",
                    {"expression": SETTLE + json.dumps(action) + ")", "awaitPromise": True, "returnByValue": True},
                    wait=True,
                )
            except CDPError:
                pass
        for attempt in range(STALE_RETRIES):
            mark = self._browser_s
            try:
                return self._observe(screenshot)
            except StalePage:
                self._as_wait(self._browser_s - mark)  # документ грузится: это ожидание, не работа CDP
                if attempt == STALE_RETRIES - 1:
                    raise
                self._sleep(0.02)
        raise StalePage("Page did not settle")

    def _observe(self, screenshot: bool) -> dict[str, Any]:
        info = self.evaluate(READ_STATE)
        if info is None:
            raise StalePage("Document is navigating")
        info["fingerprint"] = fingerprint(info)
        if screenshot:
            info["screenshot"] = self.call(
                "Page.captureScreenshot", {"format": "jpeg", "quality": self.screenshot_quality}
            )["data"]
        return info

    def fresh(self, page: dict[str, Any], action: dict[str, Any] | None = None) -> bool:
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    # --- исполнение --------------------------------------------------------------------------------------------

    def act(self, action: dict[str, Any], page: dict[str, Any], text: str | None = None) -> dict[str, Any]:
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            self._sleep(0.1)
        result = self._act(action, text)
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def _act(self, action: dict[str, Any], text: str | None) -> dict[str, Any]:
        kind = action["kind"]
        if kind == "scroll":
            width, height = self.viewport
            x, y = round(width * 550 / 1120), round(height * 650 / 780)
            self.call(
                "Input.dispatchMouseEvent",
                {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": action["delta"]},
            )
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            if kind == "fill" and text is None:
                raise ValueError("TYPE_TEXT needs generated text")
            response = self.call(
                "Runtime.evaluate", {"expression": RESOLVE_TARGET + json.dumps(action) + ")", "returnByValue": True}
            )
            if response.get("exceptionDetails"):
                if kind == "select":
                    raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
                raise StalePage("Document changed during evaluation")
            target = response.get("result", {}).get("value")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    self.call(
                        "Input.dispatchMouseEvent",
                        {"type": event, "x": x, "y": y, "button": "left", "clickCount": 1},
                    )
                if kind == "fill":
                    self._require_focus(action["node"])
                    self.call(
                        "Input.dispatchKeyEvent",
                        {
                            "type": "keyDown",
                            "key": "a",
                            "code": "KeyA",
                            "modifiers": SELECT_ALL_MODIFIER,
                            "commands": ["selectAll"],
                        },
                    )
                    self.call(
                        "Input.dispatchKeyEvent",
                        {"type": "keyUp", "key": "a", "code": "KeyA", "modifiers": SELECT_ALL_MODIFIER},
                    )
                    self.call("Input.insertText", {"text": text})
        return {"executed": action["id"]}

    def _require_focus(self, node: int) -> None:
        """Один `Runtime.evaluate`: фокус на проверенном поле, иначе StalePage и ничего не печатаем."""
        expression = FOCUSED + json.dumps(node) + ")"
        response = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if response.get("exceptionDetails") or response.get("result", {}).get("value") is not True:
            raise StalePage("Focus is not on the target field after the click; nothing typed. Observe again.")

    # --- финальный кадр ----------------------------------------------------------------------------------------

    def screenshot(
        self, *, quality: int | None = None, scale: float | None = None, timeout: float = SCREENSHOT_TIMEOUT_S
    ) -> bytes | None:
        """JPEG текущего вида вкладки; None, если вкладка погибла или Chrome не ответил."""
        if self.closed:
            return None
        quality = self.screenshot_quality if quality is None else quality
        scale = self.screenshot_scale if scale is None else scale
        deadline = time.monotonic() + timeout
        params: dict[str, Any] = {"format": "jpeg", "quality": quality}
        try:
            if scale != 1.0:
                metrics = self.call("Page.getLayoutMetrics", timeout=timeout)
                view = metrics.get("cssVisualViewport") or {}
                width, height = self.viewport
                params["clip"] = {
                    "x": view.get("pageX", 0),
                    "y": view.get("pageY", 0),
                    "width": view.get("clientWidth", width),
                    "height": view.get("clientHeight", height),
                    "scale": scale,
                }
            data = self.call("Page.captureScreenshot", params, timeout=max(0.1, deadline - time.monotonic()))["data"]
        except (TabGone, ChromeDisconnected):
            return None
        except (CDPError, CDPTimeout) as exc:
            log.warning("Скриншот не снят: %s", exc)
            return None
        return base64.b64decode(data)
