"""Вкладка агента: атомарный снимок, проверка свежести и исполнение по наблюдаемому узлу.

Перенос `jev_ultrafast/browser.py` (MIT, Browser Use): `Browser` + `browser_operation` → `Tab` поверх прямого
CDP-клиента. Модель никогда не выдаёт селекторы, координаты или JS: цель — id узла из снимка (`snapshot.js`),
геометрия считается заново и проверяется на перекрытие прямо перед вводом.

Вкладка бывает своей (`owned=True`: создали, эмулируем viewport 1120×780, закрываем) и чужой — открытой вкладкой
пользователя (`owned=False`): её не закрываем, не переводим и не меняем ей размер; в конце только `release()`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sys
import threading
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

# Успокоение (после действия, WAIT, загрузки, перед вторым шансом): DOM молчит `quiet` мс, не раньше двух кадров
# и `floor` мс, не дольше `ceiling` мс. Значимая мутация — childList, characterData и атрибуты из `attributes`, если
# значение изменилось; не внутри script/style/template/noscript. `style` не в списке: JS-анимации пишут его каждый кадр.
SETTLE_FLOOR_MS = 50
SETTLE_QUIET_MS = 200
SETTLE_CEILING_MS = 1500
SETTLE_DEADLINE_MARGIN_S = 0.5  # потолок ≤ остаток дедлайна минус это: ответ успевает до дедлайна CDP-вызова
SETTLE_ATTRIBUTES = (
    "class",
    "hidden",
    "open",
    "disabled",
    "aria-disabled",
    "aria-hidden",
    "aria-expanded",
    "aria-busy",
    "aria-selected",
    "aria-checked",
)

# Только чтение; выполняется после того, как действие записано, даже если его прерывает навигация. Кадры — rAF и
# performance.now(); setTimeout — потолок и запасной путь для фоновой вкладки без кадров.
# Итог — {reason, ms, mutations}: quiet — тишина; options — видимые подсказки комбобокса после ввода; ceiling — потолок;
# frames — тишина по таймеру, кадров анимации не было. На выходе observer отключён, таймеры сняты.
SETTLE = """(p => new Promise(resolve => {
  const start=performance.now(), action=p.action||{};
  const field=window.__jevFast?.nodes.get(action.node);
  const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
  let last=start, frames=0, mutations=0, done=false, observer=null, cap=0, backup=0;
  const finish=reason=>{
    if (done) return;
    done=true;
    if (observer) observer.disconnect();
    clearTimeout(cap);
    clearTimeout(backup);
    resolve({reason, ms:Math.round(performance.now()-start), mutations});
  };
  const significant=r=>{
    const e=r.target.nodeType===1 ? r.target : r.target.parentElement;
    if (!e || e.closest('script,style,template,noscript')) return false;
    return r.type!=='attributes' || e.getAttribute(r.attributeName)!==r.oldValue;
  };
  const options=()=>{
    const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
      .split(/\\s+/).filter(Boolean);
    const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
    return roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]).some(e=>{
      const r=e.getBoundingClientRect();
      return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
        e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
    });
  };
  const quiet=now=>now-start>=p.floor && now-last>=p.quiet;
  const tick=()=>{
    if (done) return;
    const now=performance.now();
    if (++frames>=2 && autocomplete && options()) return finish('options');
    if (frames>=2 && quiet(now)) return finish('quiet');
    if (now-start>=p.ceiling) return finish('ceiling');
    requestAnimationFrame(tick);
  };
  const check=()=>{
    if (done) return;
    const now=performance.now();
    if (quiet(now)) return finish(frames>=2 ? 'quiet' : 'frames');
    backup=setTimeout(check, Math.max(16, p.quiet-(now-last), p.floor-(now-start)));
  };
  try {
    observer=new MutationObserver(records=>{
      if (done) return;
      const n=records.filter(significant).length;
      if (n) { mutations+=n; last=performance.now(); }
    });
    if (document.body) observer.observe(document.body, {subtree:true, childList:true, characterData:true,
      attributes:true, attributeFilter:p.attributes, attributeOldValue:true});
  } catch (e) { observer=null; }
  cap=setTimeout(()=>finish('ceiling'), p.ceiling);
  backup=setTimeout(check, p.quiet+p.floor);
  requestAnimationFrame(tick);
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


def _snapshot_part(first: str, last: str) -> str:
    """Кусок `snapshot.js` от `first` до `last` включительно: общие функции без второй копии (подпись поля — та же)."""
    start = READ_STATE.index(first)
    return READ_STATE[start : READ_STATE.index(last, start) + len(last)]


# Только чтение (docs/plan-waits.md §4.2): на каждый {node, label} — значение поля по узлу из кэша снимка, если узел ещё
# в документе; иначе первого видимого редактируемого поля с той же подписью (сайт перерисовал поле — узел новый); иначе
# null. safe/visible/name/role — из snapshot.js, «редактируемое» и значение — как там же (fill-действие и его value).
FIELD_VALUES = (
    "(specs => {\n"
    + _snapshot_part("  const safe = e =>", "    return null;\n  };\n")
    + """  const nodes=window.__jevFast?.nodes;
  const editable=e=>{
    const rname=role(e);
    return !e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
      (['textbox','searchbox','spinbutton'].includes(rname) ||
        (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
  };
  const value=e=>'value' in e ? String(e.value) :
    e.isContentEditable || role(e)==='combobox' ? e.innerText.trim() : '';
  let fields=null;
  const labelled=label=>(fields??=[...document.querySelectorAll(selector)].filter(e=>safe(e) && visible(e) &&
    !e.matches(':disabled') && !e.closest('[aria-disabled="true"]') && editable(e)))
    .find(e=>(name(e)||role(e))===label);
  return specs.map(({node,label})=>{
    const e=node==null ? null : nodes?.get(node);
    if (e?.isConnected && safe(e)) return value(e);
    const same=label==null ? null : labelled(label);
    return same ? value(same) : null;
  });
})("""
)

NAVIGATE_TIMEOUT_S = 15.0
STALE_RETRIES = 10
SCREENSHOT_TIMEOUT_S = 5.0
MEASURE = "[innerWidth, innerHeight, devicePixelRatio]"
MEASURE_TIMEOUT_S = 1.0
LOCATION_TIMEOUT_S = 2.0  # Target.getTargetInfo: url/title вкладки, когда снимок не удался (сценарий, последний шаг)
OWNER = "window.__bhOwner"  # метка-мьютекс вкладки пользователя: uuid сервера browser-hands, который в ней работает
CLAIM_TIMEOUT_S = 3.0
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
    """Одна вкладка (flatten-сессия): своя (`owned`) или вкладка пользователя. Замеры: CDP → `browser_ms`,
    ожидания и загрузка → `wait_ms`."""

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
        owned: bool = True,
    ) -> None:
        self.client = client
        self.session_id = session_id
        self.target_id = target_id
        self.owned = owned  # False — вкладка пользователя: только release(), без closeTarget/navigate/metrics
        self.screenshot_quality = screenshot_quality
        self.screenshot_scale = screenshot_scale
        self.screenshot_width = viewport[0]  # ширина кадра вкладки пользователя (её viewport не трогаем)
        self.viewport = viewport  # у вкладки пользователя — её innerWidth/innerHeight (замер и каждый снимок)
        self.dpr = 1.0  # devicePixelRatio вкладки пользователя; своя эмулируется с DPR 1
        self.deadline: float | None = None  # monotonic; ограничивает таймауты CDP-вызовов агента и потолок успокоения
        self.cancel: threading.Event | None = None  # отмена прогона: выставлена — успокоение не начинается
        self.after_input: dict[str, Any] | None = None  # исполненное действие: следующий observe() сначала успокоит
        self.last_settle: dict[str, Any] | None = None  # итог последнего успокоения {reason, ms, mutations} или None
        self.closed = False
        self._on_release = on_release
        self._focus_emulated = False
        self._owner: str | None = None  # своя метка __bhOwner (claim); release() удаляет её, только если она наша
        self._foreign = False  # метка чужая: release() страницу не трогает
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

    def pause(self, seconds: float) -> None:
        """Подождать (агент ждёт появления элементов); время идёт в `wait_ms`."""
        self._sleep(seconds)

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
        """Своя вкладка: viewport 1120×780 с DPR 1. Любая: focus emulation (снимает `release()`).

        Вкладке пользователя размер не меняем: `setDeviceMetricsOverride` только при `owned`."""
        if self.owned:
            width, height = self.viewport
            self.call(
                "Emulation.setDeviceMetricsOverride",
                {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
            )
        # Держит rAF и меню в фоновой вкладке, не активируя видимую вкладку пользователя. Флаг — до вызова: при
        # таймауте Chrome мог его включить, и release() выключит.
        self._focus_emulated = True
        self.call("Emulation.setFocusEmulationEnabled", {"enabled": True})

    def claim(self, owner: str, *, timeout: float = CLAIM_TIMEOUT_S) -> bool:
        """Мьютекс вкладки пользователя: один `Runtime.evaluate` `window.__bhOwner ??= owner`. True — метка наша или её
        не поставить (документ сменяется: навигация стирает и чужую, от второго клиента тогда защищает `attached`);
        False — чужая, `release()` не тронет ни её, ни кэш снимка. Молчание дольше `timeout` — `CDPTimeout`."""
        expression = f"{OWNER} ??= {json.dumps(owner)}"
        self._owner = owner
        try:
            response = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, timeout=timeout)
        except CDPError as exc:
            log.debug("Метка вкладки не поставлена: %s", exc)
            return True
        value = response.get("result", {}).get("value")
        if response.get("exceptionDetails") or value is None or value == owner:
            return True
        self._owner, self._foreign = None, True
        return False

    def measure(self, *, timeout: float = MEASURE_TIMEOUT_S) -> bool:
        """Вкладка пользователя: её `innerWidth`/`innerHeight`/`devicePixelRatio` → `viewport`, `dpr`. Один
        `Runtime.evaluate`; страница грузится, молчит или ответ странный — прежние значения и False. Закрытая вкладка
        и обрыв соединения — исключением."""
        try:
            response = self.call("Runtime.evaluate", {"expression": MEASURE, "returnByValue": True}, timeout=timeout)
        except (CDPError, CDPTimeout) as exc:
            log.debug("Размер вкладки не снят: %s", exc)
            return False
        value = response.get("result", {}).get("value")
        if response.get("exceptionDetails") or not isinstance(value, list) or len(value) != 3:
            return False
        width, height, dpr = value
        if not all(isinstance(v, int | float) and v > 0 for v in value):
            return False
        self.viewport, self.dpr = (round(width), round(height)), float(dpr)
        return True

    def navigate(self, url: str, *, timeout: float = NAVIGATE_TIMEOUT_S) -> None:
        """`Page.navigate` и опрос `document.readyState` до `complete` (не дольше `timeout`); всё — `wait_ms`.

        Только своя вкладка: во вкладке пользователя агент начинает с того, что открыто."""
        if not self.owned:
            raise RuntimeError("Page.navigate во вкладке пользователя запрещён")
        response = self.call("Page.navigate", {"url": url}, wait=True)
        if response.get("errorText"):
            raise NavigationFailed(f"Navigation to {url_host(url)} failed: {response['errorText']}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                ready = self.evaluate("document.readyState", wait=True) == "complete"
            except (StalePage, CDPError):
                ready = False  # контекст пересоздаётся во время загрузки
            if ready:
                self.settle({"kind": "load"})  # JS страницы достраивает интерфейс и после load
                return
            self._sleep(0.02)

    def close(self) -> None:
        """Закрыть свою вкладку; повторный вызов и обрыв соединения не бросают исключений.

        Вкладку пользователя не закрывает никогда: для неё это `release()`."""
        if not self.owned:
            self.release()
            return
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

    def release(self, *, timeout: float = SCREENSHOT_TIMEOUT_S) -> None:
        """Оставить вкладку открытой (keep_open, вкладка пользователя): во вкладке пользователя одним вызовом удалить
        кэш снимка и свою метку `__bhOwner` (чужую — нет; при чужой метке страницу не трогать), выключить focus
        emulation, если включали, и отсоединить сессию (Chrome снимает эмуляцию), забыть target. `timeout` — на все
        вызовы вместе; ошибки (вкладка закрыта, обрыв, Chrome молчит) — в DEBUG. Никогда не закрывает вкладку."""
        if self.closed:
            return
        self.closed = True
        deadline = time.monotonic() + timeout
        # (метод, параметры, сессия, потолок доли бюджета): уборка кэша — не больше трети, detach важнее
        calls: list[tuple[str, dict[str, Any], str | None, float]] = []
        if not self.owned and not self._foreign:  # не оставлять в странице пользователя кэш снимка и свою метку
            cleanup = "delete window.__jevFast"
            if self._owner is not None:
                cleanup += f"; if ({OWNER} === {json.dumps(self._owner)}) delete {OWNER}"
            calls.append(("Runtime.evaluate", {"expression": cleanup}, self.session_id, timeout / 3))
        if self._focus_emulated:
            calls.append(("Emulation.setFocusEmulationEnabled", {"enabled": False}, self.session_id, timeout))
        calls.append(("Target.detachFromTarget", {"sessionId": self.session_id}, None, timeout))
        for method, params, session_id, cap in calls:
            try:
                left = max(0.0, deadline - time.monotonic())
                self.client.call(method, params, session_id=session_id, timeout=min(cap, left))
            except (CDPError, CDPTimeout, ChromeDisconnected, TabGone) as exc:
                log.debug("%s %s: %s", method, self.session_id, exc)
        self._focus_emulated = False
        if self._on_release:
            self._on_release(self.target_id)

    # --- наблюдение --------------------------------------------------------------------------------------------

    def settle(self, action: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Дождаться тишины DOM (`SETTLE`): один `Runtime.evaluate` с `awaitPromise`, только чтение, время — `wait_ms`.

        Потолок — `SETTLE_CEILING_MS`, но не дальше `deadline − SETTLE_DEADLINE_MARGIN_S` (иначе CDPTimeout уронил бы
        прогон раньше дедлайна); места нет или выставлена отмена — не ждём. Прерван навигацией (`CDPError`,
        `exceptionDetails`) — None. Итог — в `last_settle` и строкой DEBUG."""
        self.last_settle = None
        if self.cancel is not None and self.cancel.is_set():
            log.debug("settle пропущен: отмена")
            return None
        ceiling = SETTLE_CEILING_MS
        if self.deadline is not None:
            ceiling = min(ceiling, int((self.deadline - time.monotonic() - SETTLE_DEADLINE_MARGIN_S) * 1000))
            if ceiling <= 0:
                log.debug("settle пропущен: до дедлайна меньше %g с", SETTLE_DEADLINE_MARGIN_S)
                return None
        action = action or {}
        params = {
            "action": {k: action[k] for k in ("kind", "node") if k in action},
            "floor": SETTLE_FLOOR_MS,
            "quiet": SETTLE_QUIET_MS,
            "ceiling": ceiling,
            "attributes": list(SETTLE_ATTRIBUTES),
        }
        expression = SETTLE + json.dumps(params) + ")"
        try:
            response = self.call(
                "Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True}, wait=True
            )
        except CDPError as exc:
            log.debug("settle прерван: %s", exc)
            return None
        value = response.get("result", {}).get("value")
        if response.get("exceptionDetails") or not isinstance(value, dict):
            log.debug("settle прерван: документ сменился")
            return None
        self.last_settle = value
        log.debug("settle %s %s мс, мутаций %s", value.get("reason"), value.get("ms"), value.get("mutations"))
        return value

    def await_ready(self, action: dict[str, Any] | None = None) -> dict[str, Any]:
        """Страница готова к решению после действия (docs/plan-waits.md §2). Контракт §4.2: пока — `settle(action)`.

        Итог — `{reason, ms, mutations}`; пусто — не ждали (отмена, до дедлайна не осталось места) или ожидание прервала
        навигация."""
        return self.settle(action) or {}

    def await_change(self) -> dict[str, Any]:
        """Дождаться следующего изменения страницы, затем `await_ready` (WAIT Jev, WAIT в проверке, второй взгляд; §2).

        Контракт §4.2: пока — пауза 0,1 с, как у WAIT в `act`, и `settle`; время — `wait_ms`, итог — как у
        `await_ready`."""
        self._sleep(0.1)
        return self.await_ready({"kind": "wait"})

    def field_values(self, specs: list[dict[str, Any]]) -> list[str | None]:
        """Что сейчас в полях, куда печатали (`[{node, label}]` из снимка): один `Runtime.evaluate` (`FIELD_VALUES`),
        только чтение, время — `browser_ms`. На каждое — значение поля по узлу из кэша снимка, если узел ещё в
        документе; иначе первого видимого редактируемого поля с той же подписью; иначе None. Значение — как `value` в
        снимке; password/file/hidden не читаются, напечатанный текст в страницу не уходит (сравнивает вызывающий).
        Документ сменяется — StalePage."""
        if not specs:
            return []
        payload = [
            {
                "node": spec.get("node") if type(spec.get("node")) is int else None,
                "label": spec.get("label") if isinstance(spec.get("label"), str) else None,
            }
            for spec in specs
        ]
        values = self.evaluate(FIELD_VALUES + json.dumps(payload) + ")")
        if not isinstance(values, list) or len(values) != len(specs):
            raise StalePage("Document is navigating")
        return [value if isinstance(value, str) else None for value in values]

    def observe(self, screenshot: bool = False) -> dict[str, Any]:
        if self.after_input:
            action, self.after_input = self.after_input, None
            self.settle(action)
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
        if not self.owned and info.get("w") and info.get("h"):
            self.viewport = (round(info["w"]), round(info["h"]))  # окно пользователя могли изменить
        info["fingerprint"] = fingerprint(info)
        if screenshot:
            info["screenshot"] = self.call(
                "Page.captureScreenshot", {"format": "jpeg", "quality": self.screenshot_quality}
            )["data"]
        return info

    def location(self) -> tuple[str, str] | None:
        """url и title вкладки из `Target.getTargetInfo` — вызов уровня браузера, без JS страницы: отвечает и пока
        документ грузится. Потолок `LOCATION_TIMEOUT_S`; ошибка, таймаут или пустой url — None."""
        budget = self._budget()
        timeout = LOCATION_TIMEOUT_S if budget is None else min(LOCATION_TIMEOUT_S, budget)
        try:
            with self._timed(wait=False):
                response = self.client.call("Target.getTargetInfo", {"targetId": self.target_id}, timeout=timeout)
        except (CDPError, CDPTimeout) as exc:
            log.debug("getTargetInfo %s: %s", self.target_id, exc)
            return None
        info = response.get("targetInfo") if isinstance(response, dict) else None
        url = info.get("url") if isinstance(info, dict) else None
        if not isinstance(url, str) or not url:
            return None
        title = info.get("title") if isinstance(info, dict) else None
        return url, title if isinstance(title, str) else ""

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
        self.after_input = action  # и после WAIT: снимок через 100 мс на грузящейся странице был бы тем же
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
        """JPEG текущего вида вкладки; None, если вкладка погибла или Chrome не ответил.

        Вкладка пользователя: размер ей не меняем, кадр ужимаем `clip.scale` до ~`screenshot_width` px по ширине.
        Картинка = `clip.width × scale × devicePixelRatio` (замер: 1728 CSS px @ DPR 2, scale 1120/3456 → 1120 px)."""
        if self.closed:
            return None
        quality = self.screenshot_quality if quality is None else quality
        scale = self.screenshot_scale if scale is None else scale
        deadline = time.monotonic() + timeout
        params: dict[str, Any] = {"format": "jpeg", "quality": quality}
        try:
            if not self.owned:
                self.measure(timeout=min(MEASURE_TIMEOUT_S, timeout))  # DPR мог смениться (монитор, масштаб)
            if scale != 1.0 or not self.owned:
                metrics = self.call("Page.getLayoutMetrics", timeout=max(0.1, deadline - time.monotonic()))
                view = metrics.get("cssVisualViewport") or {}
                width, height = self.viewport
                width, height = view.get("clientWidth") or width, view.get("clientHeight") or height
                if not self.owned:
                    scale = min(scale, self.screenshot_width / (width * self.dpr))
                if scale != 1.0:
                    params["clip"] = {
                        "x": view.get("pageX", 0),
                        "y": view.get("pageY", 0),
                        "width": width,
                        "height": height,
                        "scale": scale,
                    }
            data = self.call("Page.captureScreenshot", params, timeout=max(0.1, deadline - time.monotonic()))["data"]
        except (TabGone, ChromeDisconnected):
            return None
        except (CDPError, CDPTimeout) as exc:
            log.warning("Скриншот не снят: %s", exc)
            return None
        return base64.b64decode(data)
