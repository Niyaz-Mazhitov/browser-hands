"""Замер WhatsApp `scripts/observe_whatsapp.py` офлайн: обрезка личного, разбор событий, сводка, уборка на фейке."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from browser_hands import browser as browser_module
from tests.fake_cdp import FakeCDPServer, event, reply

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "observe_whatsapp.py"
SALT = b"test-salt"


def load_script():
    spec = importlib.util.spec_from_file_location("observe_whatsapp", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass с отложенными аннотациями ищет свой модуль в sys.modules
    spec.loader.exec_module(module)
    return module


obs = load_script()


# --- обрезка личного ------------------------------------------------------------------------------------------------


def test_label_keeps_chat_name_service_words_and_time():
    assert obs.scrub_label("Рабочий", "Рабочий", SALT) == "Рабочий"
    assert obs.scrub_label("Type a message to Рабочий", "Рабочий", SALT) == "Type a message to Рабочий"
    assert obs.scrub_label("  Read ", "Рабочий", SALT) == "Read"
    assert obs.scrub_label("12:34 Sent", "Рабочий", SALT) == "HH:MM Sent"
    assert obs.scrub_label("«Рабочий»", "Рабочий", SALT) == "«Рабочий»"
    assert obs.scrub_label("", "Рабочий", SALT) == ""
    assert obs.scrub_label(None, "Рабочий", SALT) is None


def test_label_hides_names_text_and_numbers_with_salted_hash():
    hidden = obs.scrub_label("Message from Кеша: привет, дела", "Рабочий", SALT)
    assert hidden is not None and hidden.startswith("Message from … #")
    assert "Кеша" not in hidden and "привет" not in hidden
    phone = obs.scrub_label("+7 701 123 45 67", "Рабочий", SALT)
    assert phone is not None and not any(ch.isdigit() for ch in phone.split("#")[0])
    assert obs.scrub_label("Кеша", "Рабочий", SALT) == obs.scrub_label("Кеша", "Рабочий", SALT)  # повтор узнаваем
    assert obs.scrub_label("Кеша", "Рабочий", SALT) != obs.scrub_label("Кеша", "Рабочий", b"other")  # соль прогона
    assert obs.scrub_label("Рабочийxyz", "Рабочий", SALT).startswith("… #")  # хвост к названию — не пропускать


def test_tokens_values_and_path():
    assert obs.scrub_token("msg-dblcheck", SALT) == "msg-dblcheck"
    assert obs.scrub_token("x1n2onr6", SALT) == "x1n2onr6"
    assert obs.scrub_token("true_77011234567@c.us", SALT).startswith("#")
    assert obs.scrub_token("Имя Фамилия", SALT).startswith("#")
    assert obs.scrub_value("aria-hidden", "true", "Рабочий", SALT) == "true"
    assert obs.scrub_value("tabindex", "-1", "Рабочий", SALT) == "-1"
    assert obs.scrub_value("aria-label", "Кеша", "Рабочий", SALT).startswith("… #")
    desc = {
        "tag": "span",
        "id": "main",
        "cls": ["x1", "_ak8l"],
        "role": "button",
        "label": " Read ",
        "icon": "msg-check",
    }
    assert (
        obs.render_path(desc, "Рабочий", SALT)
        == 'span#main.x1._ak8l[role=button][aria-label="Read"][data-icon=msg-check]'
    )
    assert obs.render_path({"tag": "#text"}, "Рабочий", SALT) == "#text"
    assert obs.render_path(None, "Рабочий", SALT) is None


def test_short_url_host_and_path_only():
    assert obs.short_url("https://web.whatsapp.com/api/x?token=secret#frag") == "web.whatsapp.com/api/x"
    assert obs.short_url("http://127.0.0.1:8123/report") == "127.0.0.1:8123/report"
    assert obs.short_url("https://mmg.whatsapp.net/v/77011234567_abc.enc?x=1") == "mmg.whatsapp.net/v/<n>_abc.enc"
    assert obs.short_url("data:image/png;base64,AAAA") == "data:image/png"
    assert obs.short_url("blob:https://web.whatsapp.com/1234-abcd") == "blob:web.whatsapp.com"
    assert obs.short_url("about:blank") == "about:"


def test_slim_event_drops_headers_bodies_and_payloads():
    request = {
        "method": "Network.requestWillBeSent",
        "params": {
            "requestId": "1",
            "timestamp": 10.0,
            "wallTime": 1000.0,
            "type": "XHR",
            "request": {
                "url": "https://h.test/p?q=secret",
                "method": "POST",
                "headers": {"Cookie": "c=secret"},
                "postData": "secret",
            },
        },
    }
    slim = obs.slim_event(request, 1000.5)
    assert slim == {
        "recv": 1000.5,
        "method": "requestWillBeSent",
        "ts": 10.0,
        "id": "1",
        "url": "h.test/p",
        "http": "POST",
        "type": "XHR",
        "wall": 1000.0,
        "redirect": False,
    }
    frame = {
        "method": "Network.webSocketFrameReceived",
        "params": {"requestId": "w", "timestamp": 11.0, "response": {"opcode": 2, "payloadData": "c2VjcmV0"}},
    }
    assert obs.slim_event(frame, 1.0) == {
        "recv": 1.0,
        "method": "webSocketFrameReceived",
        "ts": 11.0,
        "id": "w",
        "opcode": 2,
        "size": 8,
    }
    assert obs.slim_event({"method": "Page.loadEventFired", "params": {}}, 1.0) is None


def test_recording_client_hands_network_events_to_sink():
    got: list[tuple[str, float]] = []
    with FakeCDPServer() as server:

        def evaluate(frame, ws):
            event(ws, "Network.loadingFinished", {"requestId": "1", "timestamp": 1.0}, "S-1")
            event(ws, "Target.targetInfoChanged", {"targetInfo": {}})
            reply(ws, frame, {"result": {"value": 1}})

        server.on["Runtime.evaluate"] = evaluate
        client = obs.RecordingClient(server.url, sink=lambda message, recv: got.append((message["method"], recv)))
        try:
            client.call("Runtime.evaluate", {"expression": "1"}, session_id="S-1")
        finally:
            client.close()
    assert [method for method, _ in got] == ["Network.loadingFinished"]
    assert [e["method"] for e in client.events] == ["Network.loadingFinished", "Target.targetInfoChanged"]


@pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")
def test_page_expressions_in_real_js():
    params = json.dumps({"max": 10, "label": 120, "values": [], "composer": obs.COMPOSER_RE, "time": obs.TIME_RE})
    script = f"""
      new Function("return " + {json.dumps(obs.OBSERVER_JS + params + ")")});  // синтаксис наблюдателя
      let stopped = 0;
      globalThis.window = {{__bhObs: {{stop() {{ stopped++; }}}}, __jevFast: 1, __bhOwner: "me"}};
      const left = eval({json.dumps(obs.CLEANUP_JS)});
      const after = eval({json.dumps(obs.GLOBALS_JS)});
      const anchor = eval({json.dumps(obs.ARM_JS)}), time = new RegExp({json.dumps(obs.TIME_RE)}).test("9:05");
      console.log(JSON.stringify([left, stopped, after, anchor.length, time]));
    """
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout) == [[], 1, ["__bhOwner"], 2, True]  # __bhOwner снимает release(), не мы


# --- разбор событий -------------------------------------------------------------------------------------------------


def test_replacements_count_node_changes_after_click_only():
    stand = [
        {"t": -1000, "node": 1, "vis": False},
        {"t": 30, "node": 1, "vis": True},
        {"t": 1630, "node": 3, "vis": True},
    ]
    assert obs.replacements(stand) == [{"t": 1630.0, "from": 1, "to": 3, "gap_ms": 0.0}]
    fresh = [{"t": -500, "node": None}, {"t": 80, "node": 4}, {"t": 1600, "node": None}, {"t": 1650, "node": 7}]
    assert obs.replacements(fresh) == [{"t": 1650.0, "from": 4, "to": 7, "gap_ms": 50.0}]
    earlier = [{"t": -900, "node": 1}, {"t": -800, "node": 2}, {"t": 100, "node": 2}]
    assert obs.replacements(earlier) == []  # до клика — не в счёт
    back = [{"t": -10, "node": 1}, {"t": 5, "node": None}, {"t": 9, "node": 1}]
    assert obs.replacements(back) == []  # тот же узел вернулся


def net(method: str, recv: float, **fields: Any) -> dict[str, Any]:
    return {"recv": recv, "method": method, **fields}


def test_network_report_times_pending_websocket():
    click = 1_000_000.0  # мс эпохи; ts + 999.0 с = эпоха
    events = [
        net("requestWillBeSent", 999.9, id="bg", ts=0.5, wall=999.5, url="h/poll", type="XHR"),
        net("requestWillBeSent", 1000.2, id="a", ts=1.1, wall=1000.1, url="h/a", type="Fetch", http="GET"),
        net("requestWillBeSent", 1000.3, id="b", ts=1.2, wall=1000.2, url="h/b", type="XHR", http="POST"),
        net("responseReceived", 1000.4, id="a", ts=1.3, status=200, mime="application/json", type="Fetch"),
        net("loadingFinished", 1000.5, id="a", ts=1.4, size=10),
        net("loadingFailed", 1000.7, id="b", ts=1.6, error="net::ERR_ABORTED"),
        net("requestWillBeSent", 1000.8, id="es", ts=1.7, wall=1000.7, url="h/stream", type="EventSource"),
        net("webSocketFrameReceived", 999.8, id="w", ts=0.4, size=5),
        net("webSocketFrameReceived", 1000.9, id="w", ts=1.8, size=5),
        net("webSocketFrameSent", 1001.9, id="w", ts=2.8, size=5),
    ]
    report = obs.network_report(events, click, -1, 3)
    assert report["clock"] == "wallTime"
    assert [r["url"] for r in report["requests"]] == ["h/a", "h/b", "h/stream"]
    first = report["requests"][0]
    assert first["t"] == 100.0 and first["end"] == 400.0 and first["ms"] == 300.0 and first["status"] == 200
    assert report["requests"][1]["failed"] == "net::ERR_ABORTED"
    assert report["pending"]["timeline"] == [[100.0, 1], [200.0, 2], [400.0, 1], [600.0, 0]]
    assert report["pending"]["peak"] == 2 and report["pending"]["quiet_at_ms"] == 600.0
    assert report["pending"]["at_end"] == []  # EventSource не «в полёте»
    assert report["background_at_click"] == 1  # poll начат до клика и не закончен
    ws = report["websocket"]
    assert ws["per_second"] == [1, 1, 1, 0] and ws["rate_before"] == 1.0 and ws["peak_per_s"] == 1
    assert report["events_per_second"][0] == 2 and sum(report["events_per_second"]) == len(events)


def test_clock_offset_falls_back_to_fastest_receive():
    events = [net("webSocketFrameReceived", 105.3, ts=5.0), net("webSocketFrameReceived", 106.05, ts=6.0)]
    assert obs.clock_offset(events) == (pytest.approx(100.05), "recv")
    assert obs.clock_offset([net("webSocketFrameSent", 1.0)]) == (None, "none")


def test_chat_rows_exact_first_and_prefix_only():
    page = {
        "actions": [
            {"id": "e1", "kind": "fill", "node": 1, "label": "Рабочий"},
            {"id": "e2", "kind": "click", "node": 2, "label": "Работа  10:00 Созвон"},
            {"id": "e3", "kind": "click", "node": 3, "label": "Рабочий  16:54\nЗавтра в 10"},
            {"id": "e4", "kind": "click", "node": 4, "label": "Рабочий"},
            {"id": "e5", "kind": "click", "node": 5, "label": "Рабочийчат"},
        ]
    }
    assert [a["node"] for a in obs.chat_rows(page, "рабочий")] == [4, 3]


# --- отчёт и сводка -------------------------------------------------------------------------------------------------


def stand_capture() -> Any:
    capture = obs.Capture(anchor=(990.0, 1_700_000_000_000.0), click_at=1000.0, installed=True, network_enabled=True)
    capture.idents = [
        {"t": 0.0, "what": "composer", "node": 1, "vis": False, "tag": "div", "label": "Type a message"},
        {"t": 0.0, "what": "footer", "node": 2, "vis": False, "tag": "footer"},
        {"t": 1030.0, "what": "composer", "node": 1, "vis": True, "tag": "div", "label": "Type a message to Рабочий"},
        {"t": 1030.0, "what": "footer", "node": 2, "vis": True, "tag": "footer"},
        {"t": 2600.0, "what": "composer", "node": 3, "vis": True, "tag": "div", "label": "Type a message to Рабочий"},
    ]
    capture.records = [
        {
            "t": 500.0,
            "type": "attributes",
            "path": {"tag": "div", "label": "Кеша привет"},
            "attr": "class",
            "changed": True,
        },
        {
            "t": 1030.0,
            "type": "childList",
            "path": {"tag": "div", "id": "messages"},
            "na": 2,
            "nr": 0,
            "added": [{"tag": "div", "label": "Message from Кеша"}, {"tag": "#text"}],
            "icons": ["msg-dblcheck"],
        },
        {
            "t": 1030.0,
            "type": "attributes",
            "path": {"tag": "div", "role": "button"},
            "attr": "aria-selected",
            "changed": False,
            "value": "false",
        },
        {
            "t": 2600.0,
            "type": "childList",
            "path": {"tag": "footer"},
            "na": 1,
            "nr": 1,
            "added": [{"tag": "div", "role": "textbox", "label": "Type a message to Рабочий"}],
            "removed": [{"tag": "div", "role": "textbox", "label": "Type a message to Рабочий"}],
        },
        {
            "t": 2700.0,
            "type": "attributes",
            "path": {"tag": "span", "label": "Кеша: привет"},
            "attr": "aria-label",
            "changed": True,
            "value": "Кеша: привет",
            "inert": True,
        },
    ]
    capture.total = len(capture.records)
    capture.scan = {
        "root": "#main",
        "icons": [["span", "msg-dblcheck", " Read "], ["span", "msg-dblcheck", " Read "], ["span", "tail-out", None]],
        "times": [
            {"el": {"tag": "span", "cls": ["meta"]}, "ctl": None, "pointer": False, "marks": [], "visible": True},
            {
                "el": {"tag": "span", "cls": ["meta"], "role": "button"},
                "ctl": {"tag": "span", "cls": ["meta"], "role": "button"},
                "pointer": True,
                "marks": [{"tag": "span", "label": "Sent"}],
                "visible": True,
            },
        ],
    }
    return capture


def build(capture: Any, network: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "mode": "launch",
        "url": "http://127.0.0.1:8000/app.html?remount=1600&sendstatus=1500",
        "chat": "Рабочий",
        "seconds": 5.0,
        "baseline": 1.0,
        "click": {"requested": True, "done": True, "attempts": 1, "matches": 1, "error": None, "role": "button"},
        "leftover": [],
        "error": None,
        "salt": SALT,
        "started": "2026-09-26T10:00:00+0500",
    }
    options.update(overrides)
    return obs.build_report(capture, network or [], **options)


def test_report_has_no_names_or_texts():
    network = [
        net("requestWillBeSent", 1_700_000_000.05, id="1", ts=5.0, wall=1_700_000_000.04, url="h.test/api", type="XHR")
    ]
    report = build(stand_capture(), network)
    text = json.dumps(report, ensure_ascii=False)
    for secret in ("Кеша", "привет", "remount=1600&"):
        assert secret not in text
    assert report["params"] == {"remount": "1600", "sendstatus": "1500"}
    assert report["click"]["mousedown_after_arm_ms"] == 10.0
    assert report["network"]["requests"][0]["t"] == 30.0  # эпоха запроса − эпоха клика
    records = report["mutations"]["records"]
    assert records[0]["t"] == -500.0 and records[0]["path"].startswith('div[aria-label="… #')
    assert records[1]["added"][0].startswith('div[aria-label="Message from … #')
    assert records[3]["removed"] == ['div[role=textbox][aria-label="Type a message to Рабочий"]']
    assert records[4]["value"].startswith("… #") and records[4]["sig"] is False  # inert


def test_report_composer_mutations_status_and_summary():
    report = build(stand_capture())
    composer = report["composer"]
    assert composer["before_click"] == {"t": -1000.0, "node": 1, "vis": False, "tag": "div", "label": "Type a message"}
    assert composer["replaced"] == 1 and composer["replacements"][0]["t"] == 1600.0
    assert report["footer"]["replaced"] == 0
    m = report["mutations"]
    assert (
        m["significant_after_click"] == 2 and m["first_significant_ms"] == 30.0 and m["last_significant_ms"] == 1600.0
    )
    assert m["per_second"]["start_s"] == -1 and m["per_second"]["childList"][:3] == [0, 1, 1]
    status = report["status_markup"]
    assert status["icons"] == {"msg-dblcheck": 2, "tail-out": 1}
    assert status["icon_labels"] == [{"icon": "msg-dblcheck", "label": "Read", "n": 2}]
    assert [(g["el"], g["clickable"]) for g in status["times"]] == [
        ("span.meta", False),
        ("span.meta[role=button]", True),
    ]
    lines = obs.summary_lines(report)
    text = "\n".join(lines)
    assert "клик: да (mousedown через 10 мс после arm)" in lines[0]
    assert "поле сообщения: до клика — узел 1, скрыт" in text
    assert "замен после клика: 1 (+1600 мс 1→3)" in text
    assert "msg-dblcheck «Read»×2" in text
    assert "глобалы после release: нет" in text


def test_summary_reports_failures_and_leftovers():
    capture = stand_capture()
    capture.lost = True
    report = build(
        capture,
        click={"requested": True, "done": False, "error": "строки чата «Рабочий» нет в снимке"},
        leftover=["__bhObs"],
        error="StalePage: Document changed during evaluation",
    )
    text = "\n".join(obs.summary_lines(report))
    assert "клик: не выполнен — строки чата «Рабочий» нет в снимке" in text
    assert "ошибка: StalePage" in text
    assert "__bhObs пропал" in text
    assert "глобалы после release: ОСТАЛИСЬ __bhObs" in text
    assert "не проверено" in "\n".join(obs.summary_lines(build(stand_capture(), leftover=None)))


# --- весь ход на фейковом CDP: один клик, уборка в finally ---------------------------------------------------------

PAGE = {
    "url": "https://web.whatsapp.com/",
    "title": "WhatsApp",
    "w": 1200,
    "h": 800,
    "text": "",
    "scroll": {"y": 0, "height": 800},
    "actions": [
        {"id": "e1", "kind": "fill", "node": 3, "label": "Search input textbox", "role": "textbox", "value": ""},
        {"id": "e2", "kind": "click", "node": 4, "label": "Работа 10:00 Созвон", "role": "gridcell"},
        {"id": "e3", "kind": "click", "node": 5, "label": "Рабочий 16:54 Завтра в 10", "role": "gridcell"},
    ],
    "marker": ["m"],
    "page_key": ["k"],
    "guards": {"4": ["g4"], "5": ["g5"]},
    "omitted_actions": 0,
}


class FakeWhatsApp:
    """Вкладка web.whatsapp.com для фейкового CDP: исполняет выражения скрипта над `server.windows`."""

    def __init__(self, server: FakeCDPServer, *, scan_fails: bool = False) -> None:
        self.server = server
        self.scan_fails = scan_fails
        self.drains = 0
        server.targets = [
            {
                "targetId": "W1",
                "type": "page",
                "url": "https://web.whatsapp.com/",
                "attached": False,
                "browserContextId": "C1",
            }
        ]
        server.on["Runtime.evaluate"] = self.evaluate

    def evaluate(self, frame: dict[str, Any], ws: Any) -> None:
        if self.server.page_js(frame, ws):
            return
        expression = frame["params"]["expression"]
        window = self.server.windows.setdefault(frame.get("sessionId", "").removeprefix("S-"), {})
        session = frame.get("sessionId")

        def value(result: Any) -> None:
            reply(ws, frame, {"result": {"type": "object", "value": result}})

        if expression == browser_module.MEASURE:
            value([1200, 800, 2])
        elif expression.startswith("(p => {"):
            window["__bhObs"] = True
            value({"reinstalled": False, "now": 100.0, "date": 1_700_000_000_000.0})
        elif expression == obs.ARM_JS:
            value([200.0, 1_700_000_000_100.0])
        elif expression == obs.DRAIN_JS:
            self.drains += 1
            if self.drains == 1:
                event(
                    ws,
                    "Network.webSocketFrameReceived",
                    {
                        "requestId": "w",
                        "timestamp": 5.0,
                        "response": {"opcode": 2, "payloadData": "c2VjcmV0LXBheWxvYWQ="},
                    },
                    session,
                )
                event(
                    ws,
                    "Network.requestWillBeSent",
                    {
                        "requestId": "r",
                        "timestamp": 5.1,
                        "wallTime": 1.7e9,
                        "type": "XHR",
                        "request": {
                            "url": "https://web.whatsapp.com/x?secret=1",
                            "method": "GET",
                            "headers": {"Cookie": "secret"},
                        },
                    },
                    session,
                )
            records = [{"t": 250.0, "type": "characterData", "path": {"tag": "span", "label": "Кеша"}}]
            value(
                {
                    "records": records if self.drains == 1 else [],
                    "idents": [],
                    "total": 1 if self.drains == 1 else 0,
                    "dropped": 0,
                    "clickAt": 205.0 if window.get("clicked") else None,
                    "now": 300.0,
                }
            )
        elif expression == obs.SCAN_JS:
            if self.scan_fails:
                reply(ws, frame, {"result": {"type": "object"}, "exceptionDetails": {"text": "boom"}})
            else:
                value({"root": "#main", "icons": [], "times": []})
        elif expression == obs.CLEANUP_JS:
            window.pop("__bhObs", None)
            window.pop("__jevFast", None)
            value([])
        elif expression == obs.GLOBALS_JS:
            value([k for k in obs.GLOBALS if k in window])
        elif expression == browser_module.READ_STATE:
            window["__jevFast"] = True
            value(json.loads(json.dumps(PAGE)))
        elif "c.pageKey()" in expression:
            node = expression.split("c.nodes.get(")[1].split(")")[0]
            value([PAGE["page_key"], PAGE["guards"].get(node)])
        elif expression.startswith(browser_module.RESOLVE_TARGET):
            window["clicked"] = json.loads(expression[len(browser_module.RESOLVE_TARGET) : -1])["node"]
            value({"x": 40, "y": 300})
        else:
            value(0)


def write_port(data_dir: Path, port: int) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/fake")


def run_fake(tmp_path: Path, argv: list[str], **options: Any) -> tuple[int, FakeCDPServer, dict[str, Any], str]:
    out = tmp_path / "report.json"
    with FakeCDPServer() as server:
        page = FakeWhatsApp(server, **options)
        write_port(tmp_path / "profile", server.port)
        env = {"BROWSER_HANDS_CHROME_DATA_DIR": str(tmp_path / "profile")}
        code = obs.main([*argv, "--seconds", "0.15", "--baseline", "0", "--out", str(out)], env=env)
    assert page.drains >= 2
    text = out.read_text(encoding="utf-8")
    return code, server, json.loads(text), text


def test_attach_clicks_chat_row_once_and_cleans_up(tmp_path, capsys):
    code, server, report, text = run_fake(tmp_path, ["--chat", "Рабочий"])
    assert code == 0
    assert server.windows["W1"] == {"clicked": 5}  # клик по «Рабочий»; метки и глобалы убраны
    mouse = server.sent("Input.dispatchMouseEvent")
    assert [m["params"]["type"] for m in mouse] == ["mousePressed", "mouseReleased"]
    assert not [m for m in server.methods() if m in ("Input.dispatchKeyEvent", "Input.insertText", "Page.navigate")]
    methods = server.methods()
    cleanup = methods.index("Network.disable")
    assert methods.index("Network.enable") < cleanup < methods.index("Target.detachFromTarget")
    assert methods.count("Target.attachToTarget") == 2 and methods.count("Target.detachFromTarget") == 2  # + проверка
    assert report["click"]["done"] and report["click"]["role"] == "gridcell"
    assert report["click"]["mousedown_after_arm_ms"] == 5.0
    assert report["cleanup"] == {"globals_checked": True, "globals_left": []}
    assert report["network"]["websocket"]["received"] == 1
    for secret in ("secret", "Кеша", "c2VjcmV0"):
        assert secret not in text
    out = capsys.readouterr().out
    assert "клик: да" in out and "глобалы после release: нет" in out


def test_attach_no_click_error_still_cleans_up(tmp_path, capsys):
    code, server, report, _ = run_fake(tmp_path, ["--no-click"], scan_fails=True)
    assert code == 1
    assert not server.sent("Input.dispatchMouseEvent")
    assert browser_module.READ_STATE not in [f["params"].get("expression") for f in server.sent("Runtime.evaluate")]
    assert "Network.disable" in server.methods()
    assert server.windows["W1"] == {}
    assert report["error"].startswith("StalePage") and report["click"]["requested"] is False
    assert report["cleanup"]["globals_left"] == []
    assert "ошибка: StalePage" in capsys.readouterr().out


def test_attach_without_whatsapp_tab_exits_2(tmp_path, capsys):
    with FakeCDPServer() as server:
        write_port(tmp_path, server.port)
        code = obs.main(["--seconds", "0.1"], env={"BROWSER_HANDS_CHROME_DATA_DIR": str(tmp_path)})
    assert code == 2
    assert "web.whatsapp.com не открыта" in capsys.readouterr().err
    assert not [m for m in server.methods() if m.startswith(("Network.", "Input.", "Runtime."))]


def test_launch_requires_url():
    with pytest.raises(SystemExit):
        obs.parse_args(["--mode", "launch"])
