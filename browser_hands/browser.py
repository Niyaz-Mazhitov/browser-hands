"""Вкладка агента: атомарный снимок, проверка свежести и исполнение по наблюдаемому узлу.

Перенос `jev_ultrafast/browser.py` (MIT, Browser Use): `Browser` + `browser_operation` → `Tab` поверх прямого
CDP-клиента. Модель никогда не выдаёт селекторы, координаты или JS: цель — id узла из снимка (`snapshot.js`),
геометрия считается заново и проверяется на перекрытие прямо перед вводом.

Вкладка бывает своей (`owned=True`: создали, эмулируем viewport 1120×780, закрываем) и чужой — открытой вкладкой
пользователя (`owned=False`): её не закрываем, не переводим и не меняем ей размер; в конце только `release()`.

Ожидания — по событиям и состояниям, не по времени (docs/plan-waits.md §2): `await_ready` (кадры без значимых мутаций,
запросы вкладки после действия, readyState, шрифты, конечные анимации, aria-busy) и `await_change` (следующее изменение,
затем `await_ready`). Единственный потолок одного ожидания — `min(остаток дедлайна − запас, fuse_s)`.
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
from .config import Thresholds
from .types import Timing

log = logging.getLogger(__name__)

# Атомарно читает видимый текст и контролы, сохраняя идентичность реальных DOM-узлов.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

# Готовность страницы к решению — по событиям и состояниям, не по времени (docs/plan-waits.md §2). Значимая мутация —
# childList, characterData и атрибуты из `MUTATION_ATTRIBUTES`, если значение изменилось; не внутри
# script/style/template/noscript. `style` не в списке: JS-анимации пишут его каждый кадр.
MUTATION_ATTRIBUTES = (
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
# Кадров подряд без значимых мутаций, после которых DOM считается успокоившимся (§2 п. 1). Единица — кадр страницы,
# не мс. Обоснование: к первому кадру после действия обработчики и микрозадачи уже отработали, а то, что они поставили
# на rAF/setTimeout(0)/MessageChannel, легло в DOM; второй тихий кадр подряд ловит цепочку «rAF → commit», которой
# фреймворки откладывают отрисовку на следующий кадр. Одного кадра мало (второе звено цепочки не видно), три и больше
# не ловят нового класса работы, а только добавляют кадр задержки: всё, что дольше кадра (таймеры, сеть, переходы),
# закрывают сигналы сети, анимаций и aria-busy, а не счёт кадров.
QUIET_FRAMES = 2
WAIT_DEADLINE_MARGIN_S = 0.5  # предохранитель ≤ остаток дедлайна минус это: ответ успевает до дедлайна CDP-вызова
# Сеть (учёт запросов вкладки, §2 п. 2) во вкладке пользователя — только после замера WhatsApp (§0.2, §8): поток
# WS-кадров и буфер ответов Chrome в чужой вкладке. До него — сеть только в своей вкладке (`owned`), у пользователя —
# DOM-сигналы (кадры, readyState, шрифты, анимации, aria-busy).
USER_TAB_NETWORK = False
# Буферы тел ответов не нужны (читаем только события): нули принимает Chrome 154 headless (зонд 26.09); эффект на
# память вкладки не замерялся.
NETWORK_ENABLE = {"maxTotalBufferSize": 0, "maxResourceBufferSize": 0, "maxPostDataSize": 0}

# Общие куски выражений ожидания: какая мутация значима и где её слушать (body; до body — весь документ).
_SIGNIFICANT = """
  const significant=r=>{
    const e=r.target.nodeType===1 ? r.target : r.target.parentElement;
    if (!e || e.closest('script,style,template,noscript')) return false;
    return r.type!=='attributes' || e.getAttribute(r.attributeName)!==r.oldValue;
  };
  const watch=callback=>{
    try {
      const root=document.body||document.documentElement;
      if (!root) return null;
      const o=new MutationObserver(callback);
      o.observe(root, {subtree:true, childList:true, characterData:true, attributes:true,
        attributeFilter:p.attributes, attributeOldValue:true});
      return o;
    } catch (e) { return null; }
  };
"""

# Только чтение; выполняется после того, как действие записано, даже если его прерывает навигация (§2 п. 1, 3–6).
# Готово, когда `p.frames` кадров подряд без значимых мутаций И документ загружен (readyState complete), шрифты
# загружены, нет идущих конечных анимаций (бесконечные — спиннеры — не ждём) и видимых [aria-busy=true]. Комбобокс после
# ввода с видимыми подсказками — готово сразу. Скрытая вкладка (rAF не идёт) — ходы MessageChannel вместо кадров;
# условие не выполнено — ход ждёт мутацию, readystatechange или fonts.ready (без холостого цикла). Единственный таймер —
# предохранитель `p.fuse_ms`. Итог — {reason, ms, mutations, frames}: quiet | options | frames (без кадров) | fuse.
# На выходе observer отключён, таймер и слушатели сняты.
READY = (
    """(p => new Promise(resolve => {
  const start=performance.now(), action=p.action||{};
  const field=window.__jevFast?.nodes.get(action.node);
  const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
  let frames=0, turns=0, quiet=0, dirty=false, mutations=0, done=false, parked=false, observer=null, fuse=0;
  let channel=null;
  const finish=reason=>{
    if (done) return;
    done=true;
    if (observer) observer.disconnect();
    clearTimeout(fuse);
    if (channel) {
      channel.port1.onmessage=null;
      channel.port1.close();
      document.removeEventListener('readystatechange', resume);
    }
    resolve({reason, ms:Math.round(performance.now()-start), mutations, frames});
  };"""
    + _SIGNIFICANT
    + """  const options=()=>{
    const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
      .split(/\\s+/).filter(Boolean);
    const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
    return roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]).some(e=>{
      const r=e.getBoundingClientRect();
      return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
        e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
    });
  };
  const shown=e=>e.checkVisibility ? e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) : true;
  const busy=()=>[...document.querySelectorAll('[aria-busy="true"]')].some(shown);
  const animating=()=>{
    try {
      return (document.getAnimations?.() ?? []).some(a=>a.playState==='running' &&
        Number.isFinite(a.effect?.getComputedTiming?.().endTime));
    } catch (e) { return false; }
  };
  const ready=hidden=>document.readyState==='complete' && document.fonts?.status!=='loading' && !busy() &&
    (hidden || !animating());
  const step=()=>{
    if (dirty) { dirty=false; quiet=0; } else quiet++;
  };
  const tick=()=>{
    if (done) return;
    frames++;
    step();
    if (frames>=2 && autocomplete && options()) return finish('options');
    if (quiet>=p.frames && ready(false)) return finish('quiet');
    requestAnimationFrame(tick);
  };
  const turn=()=>{
    if (done || document.visibilityState!=='hidden') return;
    turns++;
    step();
    if (turns>=2 && autocomplete && options()) return finish('options');
    if (quiet>=p.frames) {
      if (ready(true)) return finish('frames');
      parked=true;
      return;
    }
    channel.port2.postMessage(0);
  };
  const resume=()=>{
    if (!parked || done) return;
    parked=false;
    channel.port2.postMessage(0);
  };
  observer=watch(records=>{
    if (done) return;
    const n=records.filter(significant).length;
    if (n) { mutations+=n; dirty=true; resume(); }
  });
  fuse=setTimeout(()=>finish('fuse'), p.fuse_ms);
  requestAnimationFrame(tick);
  if (document.visibilityState==='hidden') {
    channel=new MessageChannel();
    channel.port1.onmessage=turn;
    document.addEventListener('readystatechange', resume);
    document.fonts?.ready?.then(resume, ()=>{});
    channel.port2.postMessage(0);
  }
}))("""
)

# Ожидание изменения (WAIT Jev, второй взгляд, §2): первая значимая мутация или конец CSS-анимации/перехода; иначе
# предохранитель. Только чтение; итог — {reason: mutation | animation | fuse, ms}; на выходе observer, слушатели и
# таймер сняты.
CHANGE = (
    """(p => new Promise(resolve => {
  const start=performance.now(), ends=['animationend','transitionend','animationcancel','transitioncancel'];
  let done=false, observer=null, fuse=0;
  const finish=reason=>{
    if (done) return;
    done=true;
    if (observer) observer.disconnect();
    for (const type of ends) document.removeEventListener(type, ended, true);
    clearTimeout(fuse);
    resolve({reason, ms:Math.round(performance.now()-start)});
  };
  const ended=()=>finish('animation');"""
    + _SIGNIFICANT
    + """  observer=watch(records=>{ if (!done && records.some(significant)) finish('mutation'); });
  for (const type of ends) document.addEventListener(type, ended, true);
  fuse=setTimeout(()=>finish('fuse'), p.fuse_ms);
}))("""
)

# Своя вкладка после Page.navigate (он отвечает после commit — зонд 26.09): документ загружен — событие
# readystatechange → complete (то же, что load); иначе предохранитель навигации. true — загружен, false — нет.
LOAD = """(p => new Promise(resolve => {
  if (document.readyState==='complete') return resolve(true);
  let cap=0;
  const end=value=>{
    document.removeEventListener('readystatechange', check);
    clearTimeout(cap);
    resolve(value);
  };
  const check=()=>{ if (document.readyState==='complete') end(true); };
  document.addEventListener('readystatechange', check);
  cap=setTimeout(()=>end(false), p.fuse_ms);
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
    .find(e=>label==='' ? !name(e) : (name(e)||role(e))===label);
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
        network: bool | None = None,
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
        self.deadline: float | None = None  # monotonic; ограничивает таймауты CDP-вызовов и предохранитель ожиданий
        self.cancel: threading.Event | None = None  # отмена прогона: выставлена — ожидание не начинается и прерывается
        self.fuse_s = Thresholds().wait_fuse_s  # предохранитель одного ожидания (агент может задать свой Thresholds)
        # учёт запросов вкладки (Network.enable): своя — да, пользователя — по USER_TAB_NETWORK (plan-waits.md §0.2)
        self.network = (owned or USER_TAB_NETWORK) if network is None else network
        self.after_input: dict[str, Any] | None = None  # исполненное действие: следующий observe() сначала подождёт
        self.last_settle: dict[str, Any] | None = None  # итог последнего ожидания после действия (await_*) или None
        self.closed = False
        self._on_release = on_release
        self._focus_emulated = False
        self._network_enabled = False
        self._epoch = 0  # client.seq перед командой последнего действия: запросы, начатые позже, — от действия
        # то же, но только исполненного действия агента (`act`), без навигации: «запросы, начатые последним действием»
        # (`loading`); None — действий ещё не было
        self.action_epoch: int | None = None
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
        """Подождать (агент ждёт появления элементов); время идёт в `wait_ms`. Совместимость: agent.py до пакета
        «сценарий» (docs/plan-waits.md §6.2 — там `await_change` вместо опроса); у вкладки своих пауз больше нет."""
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
        """Своя вкладка: viewport 1120×780 с DPR 1. Любая: focus emulation (снимает `release()`). С учётом сети
        (`network`: своя вкладка, у пользователя — по USER_TAB_NETWORK) — `Network.enable` без буферов тел.

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
        if self.network:
            self._network_enabled = True  # как у focus emulation: при таймауте Chrome мог включить, release() выключит
            self.call("Network.enable", dict(NETWORK_ENABLE))
            self._epoch = self.client.seq

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
        """`Page.navigate` (отвечает после commit), загрузка документа (`LOAD`: readyState → complete, не дольше
        `timeout` и дедлайна), затем `await_ready({"kind": "load"})`; всё — `wait_ms`. Не загрузился за `timeout` —
        дальше без ожидания готовности, как раньше.

        Только своя вкладка: во вкладке пользователя агент начинает с того, что открыто."""
        if not self.owned:
            raise RuntimeError("Page.navigate во вкладке пользователя запрещён")
        self._epoch = self.client.seq
        response = self.call("Page.navigate", {"url": url}, wait=True)
        if response.get("errorText"):
            raise NavigationFailed(f"Navigation to {url_host(url)} failed: {response['errorText']}")
        if self._await_load(timeout):
            self.await_ready({"kind": "load"})  # JS страницы достраивает интерфейс и после load

    def _await_load(self, timeout: float) -> bool:
        """`LOAD` в документе после commit: True — readyState complete. Документ сменился (редирект) — ждать его
        запросы (`_wait_network`) и повторить, не больше STALE_RETRIES раз; отмена, дедлайн и `timeout` — False."""
        limit = timeout
        if self.deadline is not None:
            limit = min(limit, self.deadline - time.monotonic() - WAIT_DEADLINE_MARGIN_S)
        until = time.monotonic() + limit
        with self._timed(wait=True):
            for _ in range(STALE_RETRIES):
                left = until - time.monotonic()
                if self._cancelled() or left <= 0:
                    return False
                expression = LOAD + json.dumps({"fuse_ms": max(1, round(left * 1000))}) + ")"
                try:
                    response = self.client.call(
                        "Runtime.evaluate",
                        {"expression": expression, "awaitPromise": True, "returnByValue": True},
                        session_id=self.session_id,
                        timeout=left + WAIT_DEADLINE_MARGIN_S,
                    )
                except CDPError as exc:
                    response = {"exceptionDetails": {"text": str(exc)}}
                if not response.get("exceptionDetails"):
                    return response.get("result", {}).get("value") is True
                log.debug("загрузка: документ сменился, жду его запросы")
                self._wait_network(until)
        return False

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
        self.client.forget_network(self.session_id)
        if self._on_release:
            self._on_release(self.target_id)

    def release(self, *, timeout: float = SCREENSHOT_TIMEOUT_S) -> None:
        """Оставить вкладку открытой (keep_open, вкладка пользователя): во вкладке пользователя одним вызовом удалить
        кэш снимка и свою метку `__bhOwner` (чужую — нет; при чужой метке страницу не трогать), выключить focus
        emulation и сеть, если включали, и отсоединить сессию (Chrome снимает эмуляцию), забыть target. `timeout` —
        на все вызовы вместе; ошибки (вкладка закрыта, обрыв, Chrome молчит) — в DEBUG. Никогда не закрывает вкладку."""
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
        if self._network_enabled:  # вкладка не остаётся с включённой сетью (и до detach)
            calls.append(("Network.disable", {}, self.session_id, timeout))
        calls.append(("Target.detachFromTarget", {"sessionId": self.session_id}, None, timeout))
        for method, params, session_id, cap in calls:
            try:
                left = max(0.0, deadline - time.monotonic())
                self.client.call(method, params, session_id=session_id, timeout=min(cap, left))
            except (CDPError, CDPTimeout, ChromeDisconnected, TabGone) as exc:
                log.debug("%s %s: %s", method, self.session_id, exc)
        self._focus_emulated = self._network_enabled = False
        self.client.forget_network(self.session_id)
        if self._on_release:
            self._on_release(self.target_id)

    # --- наблюдение --------------------------------------------------------------------------------------------

    def _cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    def _fuse(self) -> float:
        """Потолок одного ожидания: `fuse_s`, но не дальше `deadline − WAIT_DEADLINE_MARGIN_S` (иначе CDPTimeout уронил
        бы прогон раньше дедлайна). ≤ 0 — не ждать."""
        fuse = self.fuse_s
        if self.deadline is not None:
            fuse = min(fuse, self.deadline - time.monotonic() - WAIT_DEADLINE_MARGIN_S)
        return fuse

    def _pending(self) -> int:
        """Запросов вкладки в полёте, начатых после эпохи действия (без учёта сети — 0)."""
        return self.client.pending_since(self.session_id, self._epoch) if self.network else 0

    def in_flight(self, since: int | None) -> int:
        """Запросов вкладки в полёте, начатых после эпохи `since` (`client.seq`), в том числе фоновых: предохранитель
        ожидания их пережил, ответа ещё нет. Без учёта сети или без эпохи — 0. Только учёт в Python, без вызова CDP."""
        if not self.network or since is None:
            return 0
        return self.client.in_flight_since(self.session_id, since)

    def loading(self) -> int:
        """«Страница ещё загружается»: запросов в полёте, начатых последним исполненным действием (`action_epoch`),
        фоновые тоже — факт для наблюдения Jev (только число). До первого действия и без учёта сети — 0."""
        return self.in_flight(self.action_epoch)

    def _wait_network(self, until: float) -> bool:
        """Насос событий, пока запросы после эпохи не завершатся (или отмена), не дольше `until` (monotonic). True —
        завершились; без учёта сети — сразу True."""
        if not self.network:
            return True
        left = until - time.monotonic()
        if left <= 0:
            return self._pending() == 0
        return self.client.wait_events(self.session_id, lambda: self._cancelled() or self._pending() == 0, left)

    def settle(self, action: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Совместимость (agent.py до пакета «сценарий», scripts/eval.py): `await_ready`; None — не ждали."""
        return self.await_ready(action) or None

    def await_ready(self, action: dict[str, Any] | None = None) -> dict[str, Any]:
        """Страница готова к решению после действия (docs/plan-waits.md §2): только чтение, время — `wait_ms`.

        Цикл: промис `READY` (кадры, readyState, шрифты, анимации, aria-busy, подсказки комбобокса) → запросы вкладки,
        начатые после эпохи действия, завершены? нет — насос событий до их завершения → снова `READY` (ответ мог
        изменить DOM) — пока оба условия не выполнятся в одном проходе. Каждый проход кончается событием; единственный
        потолок — предохранитель `_fuse()`: истёк — `fuse`, запросы в полёте — фоновые до конца жизни сессии. Документ
        сменился (навигация) — проход повторяется в новом, не больше STALE_RETRIES раз.

        Итог — `{reason, wait_reason, ms, mutations, frames, passes, pending_requests}` (`wait_reason` = `reason`:
        quiet | options | frames | fuse) и в `last_settle`; пусто — не ждали: отмена, нет места до дедлайна, документ
        так и не дал ответа."""
        result = self._ready(action or {})
        self.last_settle = result or None
        return result

    def _ready(self, action: dict[str, Any]) -> dict[str, Any]:
        self.after_input = None  # ожидание после действия — вот оно
        if self._cancelled():
            log.debug("await_ready пропущен: отмена")
            return {}
        fuse = self._fuse()
        if fuse <= 0:
            log.debug("await_ready пропущен: до дедлайна меньше %g с", WAIT_DEADLINE_MARGIN_S)
            return {}
        started = time.monotonic()
        until = started + fuse
        mutations = frames = passes = interrupted = 0
        reason: str | None = None
        with self._timed(wait=True):
            while reason is None:
                left = until - time.monotonic()
                if self._cancelled():
                    log.debug("await_ready прерван: отмена")
                    return {}
                if left <= 0:
                    reason = "fuse"
                    break
                value = self._ready_pass(action, left)
                passes += 1
                if value is None:
                    interrupted += 1
                    if interrupted >= STALE_RETRIES:
                        log.debug("await_ready: документ сменяется %d раз подряд — снимок решит", interrupted)
                        return {}
                    self._wait_network(until)  # новый документ: его запросы
                    continue
                if not value:
                    log.debug("await_ready: нет итога от страницы")
                    return {}
                mutations += int(value.get("mutations") or 0)
                frames += int(value.get("frames") or 0)
                if value.get("reason") == "fuse" or not isinstance(value.get("reason"), str):
                    reason = "fuse"
                elif self._pending() == 0:
                    reason = value["reason"]
                elif not self._wait_network(until):
                    reason = "fuse"
        pending = self._pending()
        if reason == "fuse" and self.network:
            self.client.mark_background(self.session_id)
        result = {
            "reason": reason,
            "wait_reason": reason,
            "ms": round((time.monotonic() - started) * 1000),
            "mutations": mutations,
            "frames": frames,
            "passes": passes,
            "pending_requests": pending,
        }
        log.debug(
            "await_ready %s %s мс, мутаций %s, кадров %s, проходов %s, запросов в полёте %s",
            reason,
            result["ms"],
            mutations,
            frames,
            passes,
            pending,
        )
        return result

    def _ready_pass(self, action: dict[str, Any], left: float) -> dict[str, Any] | None:
        """Один проход `READY` (не дольше `left` с). None — документ сменился; {} — ответ без итога."""
        params = {
            "action": {k: action[k] for k in ("kind", "node") if k in action},  # метка и значение в страницу не уходят
            "fuse_ms": max(1, round(left * 1000)),
            "frames": QUIET_FRAMES,
            "attributes": list(MUTATION_ATTRIBUTES),
        }
        try:
            response = self.client.call(
                "Runtime.evaluate",
                {"expression": READY + json.dumps(params) + ")", "awaitPromise": True, "returnByValue": True},
                session_id=self.session_id,
                timeout=left + WAIT_DEADLINE_MARGIN_S,
            )
        except CDPError as exc:
            log.debug("await_ready: проход прерван: %s", exc)
            return None
        if response.get("exceptionDetails"):
            log.debug("await_ready: проход прерван: документ сменился")
            return None
        value = response.get("result", {}).get("value")
        return value if isinstance(value, dict) else {}

    def await_change(self) -> dict[str, Any]:
        """Дождаться следующего изменения страницы, затем `await_ready` (WAIT Jev, WAIT в проверке, второй взгляд; §2).

        Изменение — первое из: значимая мутация или конец CSS-анимации/перехода (промис `CHANGE`), с учётом сети — ещё
        завершение любого запроса вкладки или WS-кадр (подсказка «сейчас что-то изменится»), смена документа. Каждое
        из двух ожиданий — не дольше предохранителя; изменения не было — `fuse`, без `await_ready`. Итог — как у
        `await_ready`, плюс `change` (что разбудило) и `ready` (чем кончилась готовность); `wait_reason` — `change`,
        если изменение было и страница затем готова, иначе `fuse`. Пусто — не ждали (отмена, нет места до дедлайна)."""
        self.after_input = None
        self.last_settle = None
        if self._cancelled():
            log.debug("await_change пропущен: отмена")
            return {}
        fuse = self._fuse()
        if fuse <= 0:
            log.debug("await_change пропущен: до дедлайна меньше %g с", WAIT_DEADLINE_MARGIN_S)
            return {}
        started = time.monotonic()
        marks = self.client.network_marks(self.session_id) if self.network else (0, 0)

        def moved() -> bool:
            return self._cancelled() or (self.network and self.client.network_marks(self.session_id) != marks)

        params = {"fuse_ms": max(1, round(fuse * 1000)), "attributes": list(MUTATION_ATTRIBUTES)}
        with self._timed(wait=True):
            try:
                response = self.client.call_until(
                    "Runtime.evaluate",
                    {"expression": CHANGE + json.dumps(params) + ")", "awaitPromise": True, "returnByValue": True},
                    session_id=self.session_id,
                    timeout=fuse + WAIT_DEADLINE_MARGIN_S,
                    stop=moved,
                )
            except CDPError as exc:
                response = {"exceptionDetails": {"text": str(exc)}}
        change: str | None
        if response is None:
            if self._cancelled():
                log.debug("await_change прерван: отмена")
                return {}
            finished, _ws = self.client.network_marks(self.session_id)
            change = "network" if finished != marks[0] else "websocket"
        elif response.get("exceptionDetails"):
            change = "navigation"
        else:
            value = response.get("result", {}).get("value")
            change = value.get("reason") if isinstance(value, dict) else None
            change = change if change in {"mutation", "animation", "fuse"} else None
        if change == "fuse":
            result = {
                "reason": "fuse",
                "wait_reason": "fuse",
                "change": None,
                "ms": round((time.monotonic() - started) * 1000),
                "mutations": 0,
                "frames": 0,
                "passes": 0,
                "pending_requests": self._pending(),
            }
            self.last_settle = result
            log.debug("await_change: изменений нет за %s мс", result["ms"])
            return result
        ready = self._ready({"kind": "wait"})
        if not ready and (change is None or self._cancelled()):
            return {}
        ready_reason = ready.get("reason")
        reason = "change" if change is not None and ready_reason != "fuse" else (ready_reason or "change")
        result = {
            "mutations": 0,
            "frames": 0,
            "passes": 0,
            "pending_requests": self._pending(),
            **ready,
            "reason": reason,
            "wait_reason": reason,
            "change": change,
            "ready": ready_reason,
            "ms": round((time.monotonic() - started) * 1000),
        }
        self.last_settle = result
        log.debug("await_change %s → %s (%s), %s мс", change, reason, ready_reason, result["ms"])
        return result

    def field_values(self, specs: list[dict[str, Any]]) -> list[str | None]:
        """Что сейчас в полях, куда печатали (`[{node, label}]` из снимка): один `Runtime.evaluate` (`FIELD_VALUES`),
        только чтение, время — `browser_ms`. На каждое — значение поля по узлу из кэша снимка, если узел ещё в
        документе; иначе первого видимого редактируемого поля с той же подписью (`label` "" — поле без имени любой
        роли: сайт мог пересоздать его с другой ролью); иначе None. Значение — как `value` в
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
        """Снимок страницы. После действия (`after_input`) — сначала ожидание: WAIT — `await_change`, иначе
        `await_ready`. Документ сменяется (StalePage) — повтор после готовности нового документа (`READY` ждёт
        readyState complete событием), не больше STALE_RETRIES раз; итог этих ожиданий `last_settle` не трогает."""
        action = self.after_input
        if action:
            if action.get("kind") == "wait":
                self.await_change()
            else:
                self.await_ready(action)
        for attempt in range(STALE_RETRIES):
            mark = self._browser_s
            try:
                return self._observe(screenshot)
            except StalePage:
                self._as_wait(self._browser_s - mark)  # документ грузится: это ожидание, не работа CDP
                if attempt == STALE_RETRIES - 1:
                    raise
                self._ready({"kind": "retry"})
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
        self._epoch = self.client.seq  # WAIT и SELECT (его change — внутри evaluate); у ввода — уточняется в _act
        result = self._act(action, text)
        self.action_epoch = self._epoch  # действие исполнено: его запросы — «начатые последним действием»
        self.after_input = action  # следующий observe() подождёт: WAIT — изменения, остальное — готовности
        return result

    def _act(self, action: dict[str, Any], text: str | None) -> dict[str, Any]:
        kind = action["kind"]
        if kind == "scroll":
            width, height = self.viewport
            x, y = round(width * 550 / 1120), round(height * 650 / 780)
            self._epoch = self.client.seq
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
                self._epoch = self.client.seq  # эпоха — перед первой командой ввода
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
