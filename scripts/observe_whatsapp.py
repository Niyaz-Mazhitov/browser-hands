"""Замер вкладки WhatsApp (docs/plan-waits.md §8.1): что меняется в DOM и в сети после клика по строке чата.

    uv run --frozen python scripts/observe_whatsapp.py                          # attach: вкладка web.whatsapp.com
    uv run --frozen python scripts/observe_whatsapp.py --chat "Рабочий" --seconds 15
    uv run --frozen python scripts/observe_whatsapp.py --no-click               # только наблюдение
    uv run --frozen python scripts/observe_whatsapp.py --mode launch \\
        --url "<стенд>/app.html?remount=1600&sendstatus=1500" --chat Рабочий --seconds 5

Стенд для `--mode launch` — страницы `tests/fixtures/` через `FixtureServer` из `scripts/eval.py`, например:
    uv run --frozen python -c "import sys,time; sys.path.insert(0,'scripts'); from eval import FixtureServer; \\
        s=FixtureServer().__enter__(); print(s.url('app.html'), flush=True); time.sleep(3600)"
В launch своя headless-вкладка высотой 1600 (`--viewport`): «Рабочий» стенда ниже края списка, а прокручивать нельзя.

Без моделей и платных вызовов. Единственное действие — клик по строке чата `--chat` из снимка (`snapshot.js`) через
`Tab.act` (свежесть и перекрытие — как у агента); ничего не печатает, не отправляет и не прокручивает. Строка чата
должна быть видна в списке. attach — открытая вкладка пользователя, без перехода (`find_user_tab` → `attach_tab`).

Пишет (время — мс от mousedown клика, с `--no-click` — от начала записи): `MutationObserver` на `document.body` (тип,
путь цели `tag#id.class[role][aria-label][data-icon][data-testid]`, добавлено/удалено); идентичность поля сообщения
(`[contenteditable="true"][role="textbox"]` с подписью «Type a message…» или в `footer`) и `footer`: каждое
появление, исчезновение и смена узла; разметку статусов (`[data-icon]`, элементы «HH:MM» у сообщений — тег, роль,
`aria-label`, кликабельность); сеть (`Network.enable`): запросы после клика — только хост и путь, запросы в полёте по
времени, кадры websocket и события `Network.*` в секунду. `--baseline` с до клика — фон для сравнения.

В конце (`finally`): снять наблюдатель, удалить `window.__bhObs` и `window.__jevFast`, `Network.disable`, `release()`;
затем подключиться заново и напечатать, каких глобалов (`__bhObs`, `__jevFast`, `__bhOwner`) не осталось. JSON —
`traces/wa-observe-<ts>.json`, без текстов сообщений и имён: `aria-label` — только служебные слова, название выбранного
чата и «HH:MM», остальное — «…» и хэш с солью прогона (соль не сохраняется); id/классы/data-* с длинными числами или
«@» — хэш; сеть — без query, заголовков и тел, у websocket только размер кадра. Выход 0 — клик выполнен (или
`--no-click`), глобалов не осталось; 1 — иначе; 2 — не подключиться, нет вкладки, неверные параметры.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import statistics
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from browser_hands.browser import SETTLE_ATTRIBUTES, StalePage, Tab
from browser_hands.cdp import CDPClient, CDPError, CDPException
from browser_hands.chrome import Chrome, site_label
from browser_hands.config import BrowserConfig, ConfigError, Settings

ROOT = Path(__file__).resolve().parent.parent
WA_URL = "https://web.whatsapp.com/"
CHAT = "Рабочий"
SECONDS = 15.0
BASELINE_S = 1.0
STAND_VIEWPORT = "1120x1600"
POLL_S = 0.05  # выборка буфера страницы; события сети читаются во время этих вызовов
CALL_S = 3.0  # потолок одного вызова уборки и проверки
CLICK_ATTEMPTS = 3  # страница изменилась между снимком и кликом — переснять
RECORDS_MAX_JS = 50_000  # записей в буфере страницы между двумя выборками; сверх — счётчик dropped
RECORDS_KEEP = 20_000  # записей мутаций в JSON; сверх — только счётчики
LABEL_JS = 120  # символов атрибута, которые страница отдаёт в Python (дальше — обрезка/хэш)
PATH_MAX = 96  # символов пути url в JSON
GLOBALS = ("__bhObs", "__jevFast", "__bhOwner")
COMPOSER_RE = r"^(type a message|введите сообщение|напишите сообщение)"
TIME_RE = r"^\d{1,2}:\d{2}(\s?[AaPp]\.?[Mm]\.?)?(\s*[✓✔]+)?$"
# у мутаций атрибутов сохраняем новое значение только этих (после обрезки)
VALUE_ATTRS = tuple(
    "role aria-label data-icon data-testid aria-busy aria-hidden aria-selected aria-expanded aria-disabled "
    "aria-checked hidden disabled open contenteditable tabindex".split()
)
SIGNIFICANT_ATTRS = frozenset(SETTLE_ATTRIBUTES)  # «значимая мутация» — как у успокоения агента
UNCOUNTED_TYPES = frozenset({"WebSocket", "EventSource"})  # не «в полёте» (§2 п. 2)

# Служебные слова интерфейса WhatsApp (англ. и рус.): в aria-label остаются только они, «HH:MM» и название чата.
SERVICE_WORDS = frozenset(
    """
    a an the to of in on at for from with by and or is are was you your me my it this that new not no yes ok as
    type message messages send sent sending delivered read pending seen unread voice record attach attachment search
    start chat chats list menu more options open close back reply forward copy delete deleted edited edit star starred
    pin pinned unpin mute muted unmute archive archived info contact contacts group groups community communities
    channel channels status updates call calls settings profile photo photos video image gif sticker stickers emoji
    document audio link location poll react reaction reactions view once disappearing online typing last today
    yesterday loading retry download play pause clear filter all favourites favorites conversation panel details
    compose textbox input button icon check dblcheck tick ticks clock time failed error encrypted end-to-end keyboard
    microphone camera select selected lock locked mark marked cancel done here now down up scroll jump bottom top get
    app web whatsapp business label labels draft drafts broadcast add remove report block exit leave join invite
    members admin tail am pm
    а в во на с со к ко от до из для по о об и или не нет да вы вас вам ваш я мне это новый новое новая новые
    введите напишите сообщение сообщения сообщений отправить отправлено отправка отправляется доставлено прочитано
    ожидание просмотрено непрочитанное непрочитанные непрочитанных голосовое запись прикрепить вложение поиск найти
    начать чат чаты чата список меню ещё еще параметры открыть закрыть назад ответить переслать копировать удалить
    удалено изменено изменить избранное закрепить закреплено открепить без звука архив контакт контакты группа группы
    сообщество сообщества канал каналы статус обновления звонки звонок настройки профиль фото видео изображение
    стикер стикеры эмодзи документ аудио ссылка опрос реакция реакции просмотр один раз исчезающие сети печатает был
    была недавно сегодня вчера загрузка повторить скачать пауза очистить фильтр все всё беседа панель сведения
    подробнее клавиатура микрофон камера выбрать выбрано отметить как отмена готово здесь вниз вверх перейти время
    ошибка зашифровано
    """.split()
)
PUNCTUATION = frozenset("…-—–:·|,.()[]/✓✔")
WORD_EDGE = re.compile(r"^\W+|\W+$")
LONG_DIGITS = re.compile(r"\d{7,}")
SAFE_TOKEN = re.compile(r"^[A-Za-z_][\w:.-]{0,59}$")

# Наблюдатель в странице: единственный глобал `window.__bhObs`; снимается CLEANUP. Только чтение DOM, плюс пассивный
# слушатель mousedown (момент клика) — снимается там же.
OBSERVER_JS = r"""(p => {
  let reinstalled = false;
  if (window.__bhObs) {
    try { window.__bhObs.stop(); } catch (e) {}
    delete window.__bhObs;
    reinstalled = true;
  }
  const ids = new WeakMap();
  let next = 1;
  const nid = e => { if (!ids.has(e)) ids.set(e, next++); return ids.get(e); };
  const cut = (v, n) => v === null || v === undefined ? null : String(v).slice(0, n);
  const ATTRS = [['role', 'role'], ['aria-label', 'label'], ['data-icon', 'icon'], ['data-testid', 'testid']];
  const desc = n => {
    if (!n) return null;
    if (n.nodeType !== 1) return {tag: n.nodeName.toLowerCase()};
    const d = {tag: n.tagName.toLowerCase()};
    if (n.id) d.id = cut(n.id, 60);
    const cls = (n.getAttribute('class') || '').trim();
    if (cls) d.cls = cls.split(/\s+/).slice(0, 2).map(c => c.slice(0, 40));
    for (const [attr, key] of ATTRS) {
      const v = n.getAttribute(attr);
      if (v !== null) d[key] = cut(v, p.label);
    }
    return d;
  };
  const LANDMARK = '[id],[role],[data-testid],footer,header,main,section,aside,nav';
  const INERT = 'script,style,template,noscript';
  const values = new Set(p.values), composerRe = new RegExp(p.composer, 'i'), timeRe = new RegExp(p.time);
  const boxLabel = e => e.getAttribute('aria-label') || e.getAttribute('aria-placeholder') ||
    e.getAttribute('data-placeholder') || e.getAttribute('title') || '';
  const findComposer = () => {
    const boxes = [...document.querySelectorAll('[contenteditable="true"][role="textbox"]')];
    return boxes.find(e => composerRe.test(boxLabel(e).trim())) || boxes.find(e => e.closest('footer')) || null;
  };
  const shown = e => e.isConnected &&
    (!e.checkVisibility || e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}));
  const obs = {records: [], idents: [], total: 0, dropped: 0, armAt: null, clickAt: null};
  const last = {};
  const track = t => {
    const c = findComposer();
    const f = c?.closest('footer') || document.querySelector('#main footer') || document.querySelector('footer');
    for (const [what, e] of [['composer', c], ['footer', f]]) {
      const node = e ? nid(e) : null, vis = e ? shown(e) : false, key = node + ':' + vis;
      if (last[what] === key) continue;
      last[what] = key;
      const ev = {t, what, node, vis};
      if (e) ev.tag = e.tagName.toLowerCase();
      if (e && what === 'composer') ev.label = cut(boxLabel(e), p.label);
      obs.idents.push(ev);
    }
  };
  const record = (m, t) => {
    const target = m.target.nodeType === 1 ? m.target : m.target.parentElement;
    const r = {t, type: m.type, path: desc(target), within: desc(target?.parentElement?.closest(LANDMARK))};
    if (target?.closest(INERT)) r.inert = true;
    if (m.type === 'childList') {
      r.na = m.addedNodes.length;
      r.nr = m.removedNodes.length;
      if (r.na) r.added = [...m.addedNodes].slice(0, 3).map(desc);
      if (r.nr) r.removed = [...m.removedNodes].slice(0, 3).map(desc);
      const icons = new Set();
      for (const n of m.addedNodes) {
        if (n.nodeType !== 1 || icons.size >= 6) continue;
        if (n.hasAttribute('data-icon')) icons.add(cut(n.getAttribute('data-icon'), 60));
        for (const i of n.querySelectorAll('[data-icon]')) {
          icons.add(cut(i.getAttribute('data-icon'), 60));
          if (icons.size >= 6) break;
        }
      }
      if (icons.size) r.icons = [...icons];
    } else if (m.type === 'attributes') {
      const now = m.target.getAttribute(m.attributeName);
      r.attr = m.attributeName;
      r.changed = now !== m.oldValue;
      if (values.has(m.attributeName)) r.value = cut(now, p.label);
    }
    return r;
  };
  const mo = new MutationObserver(list => {
    const t = performance.now();
    obs.total += list.length;
    for (const m of list) {
      if (obs.records.length >= p.max) { obs.dropped++; continue; }
      try { obs.records.push(record(m, t)); } catch (e) { obs.dropped++; }
    }
    track(t);
  });
  const onDown = e => { if (e.isTrusted && obs.armAt !== null && obs.clickAt === null) obs.clickAt = e.timeStamp; };
  mo.observe(document.body,
    {subtree: true, childList: true, attributes: true, attributeOldValue: true, characterData: true});
  addEventListener('mousedown', onDown, {capture: true, passive: true});
  obs.drain = () => {
    const t = performance.now();
    track(t);
    const out = {records: obs.records, idents: obs.idents, total: obs.total, dropped: obs.dropped,
      clickAt: obs.clickAt, now: t};
    obs.records = [];
    obs.idents = [];
    obs.total = 0;
    obs.dropped = 0;
    return out;
  };
  obs.scan = () => {
    const icons = [...document.querySelectorAll('[data-icon]')].slice(0, 3000).map(e =>
      [e.tagName.toLowerCase(), cut(e.getAttribute('data-icon'), 60), cut(e.getAttribute('aria-label'), p.label)]);
    const root = document.querySelector('#main') || document.body;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const times = [];
    for (let n = walker.nextNode(); n && times.length < 300; n = walker.nextNode()) {
      if (!timeRe.test(n.textContent.trim())) continue;
      const e = n.parentElement;
      if (!e || e.closest(INERT)) continue;
      const ctl = e.closest('button,a[href],[role="button"],[role="link"],[tabindex]:not([tabindex="-1"])');
      let box = null;  // значок/подпись статуса рядом со временем — не выше пузыря сообщения и не в списке целиком
      for (let a = e, i = 0; a && i < 3; a = a.parentElement, i++) {
        if (a.matches('[role="list"],[role="grid"],[role="application"],main,body')) break;
        if (a.querySelector('[data-icon],[aria-label]')) { box = a; break; }
        if (a.matches('[role="row"],[role="listitem"],[data-id]')) break;
      }
      const marks = box ? [...box.querySelectorAll('[data-icon],[aria-label]')].slice(0, 4).map(desc) : [];
      times.push({el: desc(e), ctl: ctl ? desc(ctl) : null, pointer: getComputedStyle(ctl || e).cursor === 'pointer',
        marks, visible: shown(e)});
    }
    return {icons, times, root: root === document.body ? 'body' : '#main'};
  };
  obs.stop = () => {
    mo.disconnect();
    removeEventListener('mousedown', onDown, {capture: true});
  };
  window.__bhObs = obs;
  track(performance.now());
  return {reinstalled, now: performance.now(), date: Date.now()};
})("""
DRAIN_JS = "window.__bhObs ? window.__bhObs.drain() : null"
SCAN_JS = "window.__bhObs ? window.__bhObs.scan() : null"
# Якорь времени: performance.now() ↔ Date.now() в момент «сейчас кликну» (timeOrigin давно открытой вкладки уплывает
# от системных часов, а сеть CDP и Python меряют по ним).
ARM_JS = (
    "(() => { const o=window.__bhObs; if (o) { o.armAt=performance.now(); o.clickAt=null; } "
    "return [performance.now(), Date.now()]; })()"
)
CLEANUP_JS = (
    "(() => { try { window.__bhObs?.stop(); } catch (e) {} delete window.__bhObs; delete window.__jevFast; "
    "return ['__bhObs','__jevFast'].filter(k => k in window); })()"
)
GLOBALS_JS = f"{json.dumps(list(GLOBALS))}.filter(k => k in window)"

log = logging.getLogger("observe_whatsapp")


class SetupError(RuntimeError):
    """Замер не начать: нет вкладки, неверные параметры, Chrome не найден."""


# --- обрезка личных данных -----------------------------------------------------------------------------------------


def text_hash(value: str, salt: bytes) -> str:
    return hashlib.sha256(salt + value.encode("utf-8")).hexdigest()[:8]


def scrub_label(value: str | None, chat: str, salt: bytes) -> str | None:
    """aria-label без личного: название чата `chat`, служебные слова (SERVICE_WORDS) и знаки остаются, время →
    «HH:MM», прочие слова (имена, текст, числа) — «…» (подряд — одно); если что-то скрыто — « #<хэш с солью>»."""
    if value is None:
        return None
    text = " ".join(value.split())
    if not text:
        return ""
    name = " ".join(chat.split())
    if name and text.casefold() == name.casefold():
        return text
    marker = "\x00"
    if name:
        text_marked = re.sub(re.escape(name), marker, text, flags=re.IGNORECASE)
    else:
        text_marked = text
    out: list[str] = []
    hidden = False
    for token in text_marked.split(" "):
        core = WORD_EDGE.sub("", token)
        if marker in token and not re.search(r"\w", token.replace(marker, "")):
            kept = token.replace(marker, name)
        elif re.fullmatch(TIME_RE, token):
            kept = "HH:MM"
        elif core and core.casefold() in SERVICE_WORDS and not any(ch.isdigit() for ch in core):
            kept = token
        elif not core and token and all(ch in PUNCTUATION for ch in token):
            kept = token
        else:
            hidden = True
            if not out or out[-1] != "…":
                out.append("…")
            continue
        out.append(kept)
    result = " ".join(out)
    return f"{result} #{text_hash(text, salt)}" if hidden else result


def scrub_token(value: str | None, salt: bytes) -> str | None:
    """id, класс, data-icon, data-testid: короткое слово без длинных чисел и «@» — как есть, иначе хэш."""
    if value is None:
        return None
    if SAFE_TOKEN.fullmatch(value) and not re.search(r"\d{5,}", value) and "@" not in value:
        return value
    return f"#{text_hash(value, salt)}"


def scrub_value(attr: str, value: str | None, chat: str, salt: bytes) -> str | None:
    if value is None:
        return None
    if attr == "aria-label":
        return scrub_label(value, chat, salt)
    if value in ("", "true", "false") or re.fullmatch(r"-?\d{1,2}", value):
        return value
    return scrub_token(value, salt)


def render_path(desc: Mapping[str, Any] | None, chat: str, salt: bytes) -> str | None:
    """Описание узла из страницы → `tag#id.class.class[role=…][aria-label="…"][data-icon=…][data-testid=…]`."""
    if not desc:
        return None
    parts = [str(desc.get("tag") or "?")]
    if desc.get("id"):
        parts.append(f"#{scrub_token(str(desc['id']), salt)}")
    for cls in desc.get("cls") or ():
        parts.append(f".{scrub_token(str(cls), salt)}")
    if desc.get("role") is not None:
        parts.append(f"[role={scrub_token(str(desc['role']), salt)}]")
    if desc.get("label") is not None:
        parts.append(f"[aria-label={json.dumps(scrub_label(str(desc['label']), chat, salt), ensure_ascii=False)}]")
    if desc.get("icon") is not None:
        parts.append(f"[data-icon={scrub_token(str(desc['icon']), salt)}]")
    if desc.get("testid") is not None:
        parts.append(f"[data-testid={scrub_token(str(desc['testid']), salt)}]")
    return "".join(parts)


def short_url(url: str) -> str:
    """Только хост и путь (≤ PATH_MAX, числа от 7 цифр → <n>); data: — тип, blob: — хост; без query и #."""
    if url.startswith("data:"):
        return "data:" + url[5:].split(",", 1)[0].split(";", 1)[0][:40]
    if url.startswith("blob:"):
        return "blob:" + (urlsplit(url[5:]).netloc or "?")
    parts = urlsplit(url)
    if not parts.netloc:
        return (parts.scheme or "?") + ":"
    host = parts.hostname or "?"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None:
        host = f"{host}:{port}"
    return host + LONG_DIGITS.sub("<n>", parts.path or "/")[:PATH_MAX]


# --- сеть ----------------------------------------------------------------------------------------------------------


def slim_event(message: Mapping[str, Any], recv: float) -> dict[str, Any] | None:
    """Событие `Network.*` → только нужные поля (без заголовков, тел, query, содержимого кадров); иначе None."""
    method = str(message.get("method") or "")
    if not method.startswith("Network."):
        return None
    params: Mapping[str, Any] = message.get("params") or {}
    event: dict[str, Any] = {"recv": recv, "method": method.removeprefix("Network.")}
    if isinstance(params.get("timestamp"), int | float):
        event["ts"] = float(params["timestamp"])
    if params.get("requestId") is not None:
        event["id"] = str(params["requestId"])
    name = event["method"]
    if name == "requestWillBeSent":
        request: Mapping[str, Any] = params.get("request") or {}
        event["url"] = short_url(str(request.get("url") or ""))
        event["http"] = request.get("method")
        event["type"] = params.get("type")
        if isinstance(params.get("wallTime"), int | float):
            event["wall"] = float(params["wallTime"])
        event["redirect"] = bool(params.get("redirectResponse"))
    elif name == "responseReceived":
        response: Mapping[str, Any] = params.get("response") or {}
        event["status"] = response.get("status")
        event["mime"] = response.get("mimeType")
        event["type"] = params.get("type")
    elif name == "loadingFinished":
        event["size"] = params.get("encodedDataLength")
    elif name == "loadingFailed":
        event["error"] = params.get("errorText")
        event["canceled"] = bool(params.get("canceled"))
        event["type"] = params.get("type")
    elif name in ("webSocketFrameReceived", "webSocketFrameSent"):
        frame: Mapping[str, Any] = params.get("response") or {}
        event["opcode"] = frame.get("opcode")
        event["size"] = len(str(frame.get("payloadData") or ""))
    elif name == "webSocketCreated":
        event["url"] = short_url(str(params.get("url") or ""))
    return event


class NetworkRecorder:
    """Приёмник событий `Network.*` от RecordingClient: только выжимка (`slim_event`) и время приёма."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, message: Mapping[str, Any], recv: float) -> None:
        event = slim_event(message, recv)
        if event is not None:
            self.events.append(event)

    def client(self, ws_url: str, **kwargs: Any) -> CDPClient:
        """Фабрика для `Chrome(client_factory=…)`."""
        return RecordingClient(ws_url, sink=self, **kwargs)


class RecordingClient(CDPClient):
    """CDPClient, отдающий события `Network.*` в `sink` в момент чтения (`time.time()`); остальное — как обычно."""

    def __init__(self, ws_url: str, *, sink: Callable[[Mapping[str, Any], float], None], **kwargs: Any) -> None:
        self.sink = sink
        super().__init__(ws_url, **kwargs)

    def _on_event(self, message: dict[str, Any]) -> None:
        if str(message.get("method") or "").startswith("Network."):
            self.sink(message, time.time())
        super()._on_event(message)


def clock_offset(events: Sequence[Mapping[str, Any]]) -> tuple[float | None, str]:
    """Сдвиг «монотонные секунды CDP → эпоха»: медиана `wallTime − timestamp` запросов, иначе минимум
    `приём − timestamp` (событие, прочитанное без задержки)."""
    walls = [e["wall"] - e["ts"] for e in events if "wall" in e and "ts" in e]
    if walls:
        return statistics.median(walls), "wallTime"
    recvs = [e["recv"] - e["ts"] for e in events if "ts" in e]
    if recvs:
        return min(recvs), "recv"
    return None, "none"


def per_second(times: Sequence[float], start: int, stop: int) -> list[int]:
    bins = [0] * max(0, stop - start)
    for t in times:
        index = math.floor(t / 1000) - start
        if 0 <= index < len(bins):
            bins[index] += 1
    return bins


def network_report(events: Sequence[Mapping[str, Any]], click_epoch_ms: float, start: int, stop: int) -> dict[str, Any]:
    """Запросы после клика, в полёте по времени, WS-кадры и события Network в секунду; время — мс от клика."""
    offset, source = clock_offset(events)

    def rel(event: Mapping[str, Any]) -> float:
        if offset is not None and "ts" in event:
            return round((event["ts"] + offset) * 1000 - click_epoch_ms, 1)
        return round(event["recv"] * 1000 - click_epoch_ms, 1)

    end_ms = stop * 1000
    requests: dict[str, dict[str, Any]] = {}
    ws_recv: list[float] = []
    ws_sent: list[float] = []
    all_times: list[float] = []
    counts: Counter[str] = Counter()
    sockets: list[str] = []
    for event in events:
        t = rel(event)
        all_times.append(t)
        name = str(event["method"])
        counts[name] += 1
        rid = event.get("id")
        if name == "requestWillBeSent" and rid is not None:
            if rid in requests:  # редирект: тот же запрос продолжается
                requests[rid]["redirects"] += 1
                continue
            requests[rid] = {
                "t": t,
                "type": event.get("type"),
                "method": event.get("http"),
                "url": event.get("url"),
                "status": None,
                "mime": None,
                "end": None,
                "redirects": 0,
            }
        elif name == "webSocketFrameReceived":
            ws_recv.append(t)
        elif name == "webSocketFrameSent":
            ws_sent.append(t)
        elif name == "webSocketCreated":
            sockets.append(str(event.get("url")))
        elif rid is not None and rid in requests:
            request = requests[rid]
            if name == "responseReceived":
                request["status"] = event.get("status")
                request["mime"] = event.get("mime")
                request["type"] = event.get("type") or request["type"]
            elif name in ("loadingFinished", "loadingFailed", "requestServedFromCache"):
                if request["end"] is None:
                    request["end"] = t
                if name == "loadingFailed":
                    request["failed"] = event.get("error") or "failed"
                if name == "requestServedFromCache":
                    request["cache"] = True
                if name == "loadingFinished":
                    request["size"] = event.get("size")

    def counted(request: Mapping[str, Any]) -> bool:
        return request.get("type") not in UNCOUNTED_TYPES and request.get("mime") != "text/event-stream"

    after = [r for r in requests.values() if r["t"] >= 0]
    background = [r for r in requests.values() if r["t"] < 0 and counted(r) and (r["end"] is None or r["end"] >= 0)]
    steps: list[tuple[float, int]] = []
    for request in after:
        if counted(request):
            steps.append((request["t"], 1))
            if request["end"] is not None:
                steps.append((request["end"], -1))
    steps.sort()
    timeline: list[list[float]] = []
    pending = peak = 0
    for t, delta in steps:
        pending += delta
        peak = max(peak, pending)
        timeline.append([t, pending])
    quiet_at = timeline[-1][0] if timeline and pending == 0 else None
    at_end = [
        {"url": r["url"], "type": r["type"], "t": r["t"], "age_ms": round(end_ms - r["t"], 1)}
        for r in after
        if counted(r) and (r["end"] is None or r["end"] > end_ms)
    ]
    for request in after:
        if request["end"] is not None:
            request["ms"] = round(request["end"] - request["t"], 1)
    before_s = max(0, -start)
    ws_before = [t for t in ws_recv + ws_sent if t < 0]
    ws_after = [t for t in ws_recv + ws_sent if t >= 0]
    ws_bins = per_second(ws_recv + ws_sent, start, stop)
    net_bins = per_second(all_times, start, stop)
    after_bins = net_bins[before_s:]
    return {
        "clock": source,
        "event_counts": dict(counts.most_common()),
        "events_per_second": net_bins,
        "events_peak_per_s": max(after_bins, default=0),
        "events_mean_per_s": round(sum(after_bins) / len(after_bins), 1) if after_bins else 0.0,
        "requests": sorted(after, key=lambda r: r["t"]),
        "requests_by_type": dict(Counter(str(r["type"]) for r in after).most_common()),
        "background_at_click": len(background),
        "pending": {"timeline": timeline, "peak": peak, "quiet_at_ms": quiet_at, "at_end": at_end},
        "websocket": {
            "sockets": sockets,
            "received": len(ws_recv),
            "sent": len(ws_sent),
            "per_second": ws_bins,
            "rate_before": round(len(ws_before) / before_s, 1) if before_s else None,
            "rate_after": round(len(ws_after) / stop, 1) if stop > 0 else None,
            "peak_per_s": max(ws_bins[before_s:], default=0),
        },
    }


# --- DOM: мутации, поле сообщения, статусы -------------------------------------------------------------------------


def significant(record: Mapping[str, Any]) -> bool:
    """Как у успокоения агента: childList, characterData и изменившиеся атрибуты из SETTLE_ATTRIBUTES; не внутри
    script/style/template/noscript."""
    if record.get("inert"):
        return False
    kind = record.get("type")
    if kind == "childList":
        return bool(record.get("na") or record.get("nr"))
    if kind == "characterData":
        return True
    return record.get("attr") in SIGNIFICANT_ATTRS and bool(record.get("changed"))


def render_record(record: Mapping[str, Any], click_perf: float, chat: str, salt: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {
        "t": round(float(record["t"]) - click_perf, 1),
        "type": record.get("type"),
        "path": render_path(record.get("path"), chat, salt),
    }
    if record.get("within"):
        out["within"] = render_path(record.get("within"), chat, salt)
    if record.get("type") == "childList":
        out["na"], out["nr"] = record.get("na", 0), record.get("nr", 0)
        if record.get("added"):
            out["added"] = [render_path(d, chat, salt) for d in record["added"]]
        if record.get("removed"):
            out["removed"] = [render_path(d, chat, salt) for d in record["removed"]]
        if record.get("icons"):
            out["icons"] = [scrub_token(str(i), salt) for i in record["icons"]]
    elif record.get("type") == "attributes":
        attr = str(record.get("attr"))
        out["attr"], out["changed"] = attr, bool(record.get("changed"))
        if "value" in record:
            out["value"] = scrub_value(attr, record.get("value"), chat, salt)
    if record.get("inert"):
        out["inert"] = True
    out["sig"] = significant(record)
    return out


def mutation_report(
    records: Sequence[Mapping[str, Any]],
    total: int,
    dropped: int,
    click_perf: float,
    start: int,
    stop: int,
    chat: str,
    salt: bytes,
) -> dict[str, Any]:
    rendered = [render_record(r, click_perf, chat, salt) for r in records]
    after = [r for r in rendered if r["t"] >= 0]
    sig_after = [r["t"] for r in after if r["sig"]]
    by_type = Counter(str(r["type"]) for r in rendered)
    attrs = Counter(str(r.get("attr")) for r in rendered if r["type"] == "attributes")
    bins = {
        kind: per_second([r["t"] for r in rendered if r["type"] == kind], start, stop)
        for kind in ("childList", "attributes", "characterData")
    }
    sig_bins = per_second([r["t"] for r in rendered if r["sig"]], start, stop)
    totals = [sum(values) for values in zip(*bins.values(), strict=True)]
    before_s = max(0, -start)
    return {
        "total": total,
        "received": len(rendered),
        "dropped": dropped,
        "by_type": dict(by_type.most_common()),
        "attributes": dict(attrs.most_common(15)),
        "significant": sum(r["sig"] for r in rendered),
        "significant_after_click": len(sig_after),
        "first_significant_ms": min(sig_after, default=None),
        "last_significant_ms": max(sig_after, default=None),
        "last_any_ms": max((r["t"] for r in after), default=None),
        "per_second": {"start_s": start, **bins, "significant": sig_bins},
        "peak_per_s": max(totals[before_s:], default=0),
        "records_kept": min(len(rendered), RECORDS_KEEP),
        "records": rendered[:RECORDS_KEEP],
    }


def replacements(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Смены узла после клика: прежний узел → другой (в том числе после исчезновения; `gap_ms` — сколько узла не
    было). Первое появление, когда узла не было вовсе, — не замена."""
    out: list[dict[str, Any]] = []
    last: Any = None
    gone: float | None = None
    for event in events:
        node, t = event.get("node"), float(event["t"])
        if node is None:
            if last is not None and gone is None:
                gone = t
            continue
        if node != last:
            if last is not None and t >= 0:
                out.append(
                    {"t": t, "from": last, "to": node, "gap_ms": round(t - gone, 1) if gone is not None else 0.0}
                )
            last = node
        gone = None
    return out


def identity_report(
    idents: Sequence[Mapping[str, Any]], what: str, click_perf: float, chat: str, salt: bytes
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for ident in idents:
        if ident.get("what") != what:
            continue
        event: dict[str, Any] = {"t": round(float(ident["t"]) - click_perf, 1), "node": ident.get("node")}
        event["vis"] = bool(ident.get("vis"))
        if ident.get("tag"):
            event["tag"] = ident["tag"]
        if "label" in ident:
            event["label"] = scrub_label(ident.get("label"), chat, salt)
        events.append(event)
    before = [e for e in events if e["t"] < 0]
    swaps = replacements(events)
    return {
        "before_click": before[-1] if before else None,
        "events": [e for e in events if e["t"] >= 0],
        "replacements": swaps,
        "replaced": len(swaps),
    }


def status_report(scan: Mapping[str, Any] | None, chat: str, salt: bytes) -> dict[str, Any] | None:
    """`[data-icon]` (значения и подписи) и элементы «HH:MM» у сообщений, сгруппированные по разметке."""
    if not scan:
        return None
    icons: Counter[str] = Counter()
    labelled: Counter[tuple[str, str]] = Counter()
    for tag, icon, label in scan.get("icons") or ():
        value = scrub_token(str(icon), salt) or "?"
        key = value if tag == "span" else f"{tag}:{value}"
        icons[key] += 1
        if label is not None:
            labelled[(key, scrub_label(str(label), chat, salt) or "")] += 1
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in scan.get("times") or ():
        el = render_path(item.get("el"), chat, salt)
        ctl = render_path(item.get("ctl"), chat, salt)
        marks = [render_path(m, chat, salt) for m in item.get("marks") or ()]
        pointer = bool(item.get("pointer"))
        clickable = ctl is not None or pointer
        key = (el, ctl, pointer, tuple(marks), bool(item.get("visible")))
        group = groups.setdefault(
            key,
            {
                "el": el,
                "ctl": ctl,
                "clickable": clickable,
                "pointer": pointer,
                "marks": marks,
                "visible": bool(item.get("visible")),
                "n": 0,
            },
        )
        group["n"] += 1
    return {
        "root": scan.get("root"),
        "icons": dict(icons.most_common()),
        "icon_labels": [{"icon": k, "label": v, "n": n} for (k, v), n in labelled.most_common(20)],
        "times": sorted(groups.values(), key=lambda g: -g["n"]),
        "times_total": sum(g["n"] for g in groups.values()),
    }


# --- запись во вкладке ---------------------------------------------------------------------------------------------


@dataclass
class Capture:
    """Сырые данные замера (в памяти; в JSON — только через `build_report`)."""

    records: list[dict[str, Any]] = field(default_factory=list)
    idents: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    dropped: int = 0
    anchor: tuple[float, float] | None = None  # (performance.now, Date.now) в момент arm
    click_at: float | None = None  # performance.now() mousedown клика
    scan: dict[str, Any] | None = None
    installed: bool = False
    reinstalled: bool = False
    lost: bool = False  # `__bhObs` пропал (перезагрузка страницы)
    network_enabled: bool = False
    hiccups: int = 0  # выборки, прерванные сменой документа

    @property
    def click_perf(self) -> float:
        if self.click_at is not None:
            return self.click_at
        return self.anchor[0] if self.anchor else 0.0

    @property
    def click_epoch_ms(self) -> float:
        if self.anchor is None:
            return time.time() * 1000
        perf, epoch = self.anchor
        return epoch + (self.click_perf - perf)


def enable_network(tab: Tab) -> bool:
    """`Network.enable` без тел POST в событиях; параметр не принят — без параметров; не вышло — False."""
    for params in ({"maxPostDataSize": 0}, {}):
        try:
            tab.call("Network.enable", params)
            return True
        except CDPError as exc:
            log.warning("Network.enable %s: %s", params, exc)
    return False


def install(tab: Tab, capture: Capture) -> None:
    params = {
        "max": RECORDS_MAX_JS,
        "label": LABEL_JS,
        "values": list(VALUE_ATTRS),
        "composer": COMPOSER_RE,
        "time": TIME_RE,
    }
    value = tab.evaluate(OBSERVER_JS + json.dumps(params) + ")")
    if not isinstance(value, dict):
        raise SetupError("наблюдатель не поставлен: страница не ответила")
    capture.installed = True
    capture.reinstalled = bool(value.get("reinstalled"))


def drain(tab: Tab, capture: Capture) -> None:
    """Забрать буфер `__bhObs` (заодно прочитать накопившиеся события сети)."""
    try:
        if capture.lost:
            tab.evaluate("0")  # наблюдателя нет, но события сети читать дальше
            return
        value = tab.evaluate(DRAIN_JS)
    except (StalePage, CDPError):
        capture.hiccups += 1
        return
    if not isinstance(value, dict):
        capture.lost = True
        return
    capture.records.extend(value.get("records") or ())
    capture.idents.extend(value.get("idents") or ())
    capture.total += int(value.get("total") or 0)
    capture.dropped += int(value.get("dropped") or 0)
    if value.get("clickAt") is not None:
        capture.click_at = float(value["clickAt"])


def pump(tab: Tab, capture: Capture, seconds: float) -> None:
    until = time.monotonic() + seconds
    while True:
        drain(tab, capture)
        if time.monotonic() >= until:
            return
        time.sleep(min(POLL_S, max(0.0, until - time.monotonic())))


def arm(tab: Tab, capture: Capture) -> None:
    value = tab.evaluate(ARM_JS)
    if isinstance(value, list) and len(value) == 2:
        capture.anchor = (float(value[0]), float(value[1]))
        capture.click_at = None


def normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def chat_rows(page: Mapping[str, Any], chat: str) -> list[dict[str, Any]]:
    """Клик-действия снимка, чья подпись — название чата или начинается с него и пробела; точные — первыми."""
    name = normalized(chat)
    exact: list[dict[str, Any]] = []
    prefix: list[dict[str, Any]] = []
    for action in page.get("actions") or ():
        if action.get("kind") != "click":
            continue
        label = normalized(str(action.get("label") or ""))
        if label == name:
            exact.append(action)
        elif label.startswith(name + " "):
            prefix.append(action)
    return exact + prefix


def click_chat(tab: Tab, chat: str, capture: Capture) -> dict[str, Any]:
    info: dict[str, Any] = {"requested": True, "done": False, "attempts": 0, "matches": 0, "error": None}
    for attempt in range(1, CLICK_ATTEMPTS + 1):
        info["attempts"] = attempt
        try:
            page = tab.observe()
        except StalePage:
            info["error"] = "снимок не снялся: страница грузится"
            continue
        rows = chat_rows(page, chat)
        info["matches"] = len(rows)
        if not rows:
            info["error"] = f"строки чата «{chat}» нет в снимке: чат должен быть виден в списке без прокрутки"
            return info
        action = rows[0]
        info["role"] = action.get("role")
        info["selected_before"] = action.get("selected") == "true"
        arm(tab, capture)
        try:
            tab.act(action, page)
        except StalePage:
            info["error"] = "страница менялась между снимком и кликом"
            continue
        info["done"], info["error"] = True, None
        return info
    return info


def open_tab(chrome: Chrome, mode: str, url: str) -> Tab:
    if mode == "attach":
        target = chrome.find_user_tab(url)
        if target is None:
            raise SetupError(f"вкладка {site_label(url)} не открыта в Chrome: откройте её и повторите")
        return chrome.attach_tab(target)
    tab = chrome.new_tab()
    tab.navigate(url)
    return tab


def leftover_globals(chrome: Chrome, target_id: str) -> list[str] | None:
    """Заново подключиться к вкладке (после `release`) и спросить, каких GLOBALS нет; None — не удалось проверить."""
    try:
        client = chrome.client
        session = client.call("Target.attachToTarget", {"targetId": target_id, "flatten": True}, timeout=CALL_S)
        session_id = str(session["sessionId"])
    except (CDPException, KeyError) as exc:
        log.warning("проверка глобалов: не подключиться (%s)", exc)
        return None
    try:
        response = client.call(
            "Runtime.evaluate", {"expression": GLOBALS_JS, "returnByValue": True}, session_id=session_id, timeout=CALL_S
        )
        value = response.get("result", {}).get("value")
        return [str(v) for v in value] if isinstance(value, list) else None
    except CDPException as exc:
        log.warning("проверка глобалов: %s", exc)
        return None
    finally:
        try:
            client.call("Target.detachFromTarget", {"sessionId": session_id}, timeout=CALL_S)
        except CDPException as exc:
            log.debug("detach проверки: %s", exc)


def finish(chrome: Chrome, tab: Tab | None) -> list[str] | None:
    """Уборка в любом случае: снять наблюдатель и свои глобалы, `Network.disable`, `release()`, проверить, закрыть
    соединение (launch — и свой Chrome)."""
    leftover: list[str] | None = None
    try:
        if tab is not None and not tab.closed:
            for method, params in (
                ("Runtime.evaluate", {"expression": CLEANUP_JS, "returnByValue": True}),
                ("Network.disable", {}),
            ):
                try:
                    tab.call(method, params, timeout=CALL_S)
                except CDPException as exc:
                    log.warning("уборка %s: %s", method, exc)
            target_id = tab.target_id
            tab.release(timeout=CALL_S)
            leftover = leftover_globals(chrome, target_id)
    finally:
        chrome.close()
    return leftover


# --- отчёт ---------------------------------------------------------------------------------------------------------


def build_report(
    capture: Capture,
    network: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    url: str,
    chat: str,
    seconds: float,
    baseline: float,
    click: Mapping[str, Any],
    leftover: list[str] | None,
    error: str | None,
    salt: bytes,
    started: str,
) -> dict[str, Any]:
    start, stop = -math.ceil(baseline), math.ceil(seconds)
    click_perf = capture.click_perf
    parts = urlsplit(url)
    click_info = dict(click)
    if capture.anchor is not None and capture.click_at is not None:
        click_info["mousedown_after_arm_ms"] = round(capture.click_at - capture.anchor[0], 1)
    return {
        "version": 1,
        "started": started,
        "mode": mode,
        "site": site_label(url),
        "path": parts.path or "/",
        "params": dict(parse_qsl(parts.query)) if mode == "launch" else {},  # стенд: remount/sendstatus
        "chat": chat,
        "seconds": seconds,
        "baseline_s": baseline,
        "t0": "mousedown" if capture.click_at is not None else "arm",
        "click": click_info,
        "error": error,
        "observer": {
            "installed": capture.installed,
            "reinstalled": capture.reinstalled,
            "lost": capture.lost,
            "hiccups": capture.hiccups,
        },
        "composer": identity_report(capture.idents, "composer", click_perf, chat, salt),
        "footer": identity_report(capture.idents, "footer", click_perf, chat, salt),
        "mutations": mutation_report(
            capture.records, capture.total, capture.dropped, click_perf, start, stop, chat, salt
        ),
        "status_markup": status_report(capture.scan, chat, salt),
        "network": {"enabled": capture.network_enabled}
        | (network_report(network, capture.click_epoch_ms, start, stop) if capture.network_enabled else {}),
        "cleanup": {"globals_checked": leftover is not None, "globals_left": leftover},
    }


def fmt_ms(value: float | None) -> str:
    return "—" if value is None else f"{value:+.0f} мс"


def identity_line(title: str, part: Mapping[str, Any]) -> str:
    def state(event: Mapping[str, Any]) -> str:
        if event.get("node") is None:
            return "нет"
        return f"узел {event['node']}, {'виден' if event.get('vis') else 'скрыт'}"

    before = part.get("before_click")
    events = list(part.get("events") or ())
    text = f"{title}: до клика — {state(before) if before else 'нет'}"
    if events:
        shown = "; ".join(f"{fmt_ms(e['t'])} {state(e)}" for e in events[:6])
        more = f" (+ ещё {len(events) - 6})" if len(events) > 6 else ""
        text += f"; после: {shown}{more}"
    swaps = list(part.get("replacements") or ())
    text += f"; замен после клика: {len(swaps)}"
    if swaps:
        text += " (" + ", ".join(f"{fmt_ms(s['t'])} {s['from']}→{s['to']}" for s in swaps[:6]) + ")"
    return text


def summary_lines(report: Mapping[str, Any]) -> list[str]:
    click = report.get("click") or {}
    if not click.get("requested"):
        click_text = "нет (--no-click)"
    elif click.get("done"):
        after = click.get("mousedown_after_arm_ms")
        click_text = "да" + (f" (mousedown через {after:.0f} мс после arm)" if after is not None else "")
    else:
        click_text = f"не выполнен — {click.get('error')}"
    lines = [
        f"{report['mode']} · {report['site']} · чат «{report['chat']}» · клик: {click_text} · "
        f"запись {report['seconds']:g} с (t=0 — {report['t0']})"
    ]
    if report.get("error"):
        lines.append(f"ошибка: {report['error']}")
    lines.append(identity_line("поле сообщения", report["composer"]))
    lines.append(identity_line("footer", report["footer"]))
    m = report["mutations"]
    types = ", ".join(f"{k} {v}" for k, v in m["by_type"].items()) or "нет"
    lines.append(
        f"мутации: {m['total']} ({types}), значимых после клика {m['significant_after_click']}; "
        f"первая {fmt_ms(m['first_significant_ms'])}, последняя {fmt_ms(m['last_significant_ms'])}; "
        f"пик {m['peak_per_s']}/с" + (f"; потеряно {m['dropped']}" if m["dropped"] else "")
    )
    status = report.get("status_markup")
    if status:
        icons = ", ".join(f"{k}×{v}" for k, v in list(status["icons"].items())[:8]) or "нет"
        labelled = ", ".join(f"{i['icon']} «{i['label']}»×{i['n']}" for i in status["icon_labels"][:4])
        lines.append(f"data-icon: {icons}" + (f"; подписи: {labelled}" if labelled else ""))
        groups = []
        for group in status["times"][:3]:
            how = "кликабельно" if group["clickable"] else "не кликабельно"
            where = group["ctl"] if group["ctl"] and group["ctl"] != group["el"] else ""
            marks = " ".join(m for m in group["marks"][:2] if m)
            groups.append(
                f"{group['el']}{' в ' + where if where else ''} {how} ×{group['n']}" + (f" [{marks}]" if marks else "")
            )
        lines.append(
            f"«HH:MM» у сообщений ({status['root']}): {status['times_total']}"
            + (" — " + "; ".join(groups) if groups else "")
        )
    net = report["network"]
    if not net.get("enabled"):
        lines.append("сеть: Network.enable не удался")
    else:
        hosts = Counter(str(r.get("url")) for r in net["requests"])
        top = ", ".join(f"{u} ×{n}" for u, n in hosts.most_common(3))
        by_type = ", ".join(f"{k} {v}" for k, v in net["requests_by_type"].items())
        pending = net["pending"]
        lines.append(
            f"сеть: запросов после клика {len(net['requests'])}"
            + (f" ({by_type}): {top}" if top else "")
            + f"; в полёте: пик {pending['peak']}, тихо с {fmt_ms(pending['quiet_at_ms'])}, "
            f"на конце {len(pending['at_end'])}; фоновых на клике {net['background_at_click']}"
        )
        ws = net["websocket"]
        before = "—" if ws["rate_before"] is None else f"{ws['rate_before']:g}/с"
        lines.append(
            f"websocket: кадров {ws['received'] + ws['sent']} (до клика {before}, после {ws['rate_after']:g}/с, "
            f"пик {ws['peak_per_s']}/с); событий Network: пик {net['events_peak_per_s']}/с, "
            f"среднее {net['events_mean_per_s']:g}/с (порог §0.2 — 200/с)"
        )
    observer = report["observer"]
    if observer.get("reinstalled"):
        lines.append("внимание: __bhObs остался от прошлого запуска — снят и поставлен заново")
    if observer.get("lost"):
        lines.append("внимание: __bhObs пропал во время записи (страница перезагрузилась?) — DOM записан не весь")
    if click.get("selected_before"):
        lines.append("внимание: строка чата уже была выбрана — замер открытия чата неточен")
    cleanup = report["cleanup"]
    if not cleanup["globals_checked"]:
        lines.append(f"глобалы после release: не проверено ({', '.join(GLOBALS)})")
    elif cleanup["globals_left"]:
        lines.append(f"глобалы после release: ОСТАЛИСЬ {', '.join(cleanup['globals_left'])}")
    else:
        lines.append(f"глобалы после release: нет ({', '.join(GLOBALS)} — false)")
    return lines


def write_report(report: Mapping[str, Any], out: Path | None) -> Path:
    path = out or ROOT / "traces" / f"wa-observe-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


# --- вход ----------------------------------------------------------------------------------------------------------


def viewport(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{3,4})x(\d{3,4})", value)
    if not match:
        raise argparse.ArgumentTypeError("ожидается ШИРИНАxВЫСОТА, например 1120x1600")
    return int(match[1]), int(match[2])


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "Замер вкладки WhatsApp").splitlines()[0])
    parser.add_argument("--mode", choices=("attach", "launch"), default="attach")
    parser.add_argument("--url", help=f"attach: вкладка (по умолчанию {WA_URL}); launch: страница, обязательно")
    parser.add_argument("--chat", default=CHAT, help="название чата в списке (кликается строка с этим названием)")
    parser.add_argument("--seconds", type=float, default=SECONDS, help="сколько писать после клика")
    parser.add_argument("--baseline", type=float, default=BASELINE_S, help="сколько писать до клика (фон)")
    parser.add_argument("--no-click", action="store_true", help="только наблюдение, без клика")
    parser.add_argument("--headed", action="store_true", help="launch: окно вместо headless")
    parser.add_argument("--viewport", type=viewport, default=viewport(STAND_VIEWPORT), help="launch: размер вкладки")
    parser.add_argument("--out", type=Path, help="куда писать JSON (по умолчанию traces/wa-observe-<ts>.json)")
    args = parser.parse_args(argv)
    if args.mode == "launch" and not args.url:
        parser.error("--mode launch требует --url")
    args.url = args.url or WA_URL
    if not 0 < args.seconds <= 120:
        parser.error("--seconds: от 0 до 120")
    if not 0 <= args.baseline <= 10:
        parser.error("--baseline: от 0 до 10")
    if not args.chat.strip() and not args.no_click:
        parser.error("--chat пустой")
    return args


def browser_config(args: argparse.Namespace, env: Mapping[str, str], profile: str | None) -> BrowserConfig:
    base = Settings.from_env(env).browser
    if args.mode == "attach":
        return replace(base, mode="attach", ws_url=None)
    config = replace(
        base,
        mode="launch",
        ws_url=None,
        headless=not args.headed,
        launch_data_dir=Path(profile or ""),
        viewport=args.viewport,
        connect_timeout_s=min(base.connect_timeout_s, 30.0),
    )
    if not config.chrome_binary.is_file():
        raise SetupError(f"Chrome не найден: {config.chrome_binary}; задайте BROWSER_HANDS_CHROME_BINARY")
    return config


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    salt = secrets.token_bytes(16)
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    profile = tempfile.mkdtemp(prefix="browser-hands-observe-") if args.mode == "launch" else None
    try:
        try:
            config = browser_config(args, os.environ if env is None else env, profile)
        except (ConfigError, SetupError) as exc:
            print(f"observe: {exc}", file=sys.stderr)
            return 2
        recorder = NetworkRecorder()
        chrome = Chrome(config, client_factory=recorder.client)
        capture = Capture()
        click: dict[str, Any] = {"requested": not args.no_click, "done": False, "error": None}
        tab: Tab | None = None
        error: str | None = None
        leftover: list[str] | None = None
        try:
            chrome.connect()
            tab = open_tab(chrome, args.mode, args.url)
            capture.network_enabled = enable_network(tab)
            install(tab, capture)
            if args.baseline > 0:
                pump(tab, capture, args.baseline)
            if args.no_click:
                arm(tab, capture)
            else:
                click = click_chat(tab, args.chat, capture)
                if capture.anchor is None:  # клика не было: время — от этого момента
                    arm(tab, capture)
            pump(tab, capture, args.seconds)
            capture.scan = tab.evaluate(SCAN_JS) if not capture.lost else None
            drain(tab, capture)
        except (Exception, KeyboardInterrupt) as exc:  # замер частичный (или Ctrl+C): убрать и записать, что успели
            error = f"{type(exc).__name__}: {exc}"
        finally:
            leftover = finish(chrome, tab)
        if tab is None:
            print(f"observe: {error}", file=sys.stderr)
            return 2
        report = build_report(
            capture,
            recorder.events,
            mode=args.mode,
            url=args.url,
            chat=args.chat,
            seconds=args.seconds,
            baseline=args.baseline,
            click=click,
            leftover=leftover,
            error=error,
            salt=salt,
            started=started,
        )
        path = write_report(report, args.out)
        for line in summary_lines(report):
            print(line)
        print(f"файл: {path}")
        clicked = args.no_click or bool(click.get("done"))
        return 0 if error is None and clicked and leftover == [] else 1
    finally:
        if profile is not None:
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
