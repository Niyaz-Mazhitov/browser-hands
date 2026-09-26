"""Стенд надёжности `scripts/eval.py` офлайн: сервер страниц, проверки задач, прогон на FakeCore (без Chrome и сети)."""

import importlib.util
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from browser_hands.scenario import parse_steps
from tests.fakes import FakeCore, make_result

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "scripts" / "eval.py"
FIXTURES = ROOT / "tests" / "fixtures"


def load_eval():
    spec = importlib.util.spec_from_file_location("eval_stand", EVAL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass с отложенными аннотациями ищет свой модуль в sys.modules
    spec.loader.exec_module(module)
    return module


stand = load_eval()

GOOD_CHAT = {"run": "r", "ready": True, "openChat": "Рабочий", "query": "Рабочий", "sent": [stand.MESSAGE]}
GOOD_FORM = {"run": "r", "submitted": dict(stand.FORM_EXPECTED), "fields": {}, "error": None}


def post(url: str, body: bytes) -> int:
    request = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status


def test_server_serves_fixtures_and_keeps_the_latest_report_per_run():
    with stand.FixtureServer() as server:
        assert server.base.startswith("http://127.0.0.1:")
        with urllib.request.urlopen(server.url("app.html?delay=700&run=x"), timeout=5) as response:
            page = response.read().decode()
        assert 'aria-label="Search or start a new chat"' in page

        report = {"run": "r1", "seq": 2, "openChat": "Рабочий", "sent": []}
        assert post(server.url("report"), json.dumps(report).encode()) == 204
        stale = {"run": "r1", "seq": 1, "openChat": None, "sent": []}  # пришёл позже, но старше
        assert post(server.url("report"), json.dumps(stale).encode()) == 204
        post(server.url("report"), json.dumps({"run": "r2", "seq": 1}).encode())

        assert server.report("r1") == report
        assert server.wait_report("r1", quiet=0, timeout=1) == report
        assert server.report("r2") == {"run": "r2", "seq": 1}
        assert server.wait_report("nobody", missing=0) is None

        with pytest.raises(urllib.error.HTTPError) as bad:
            post(server.url("report"), b"{not json")
        assert bad.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as outside:
            urllib.request.urlopen(server.url("../README.md"), timeout=5)
        assert outside.value.code == 404


@pytest.mark.parametrize("name", ["search", "search-spinner", "boot", "search-remount"])
def test_chat_tasks_check_the_open_chat_and_the_exact_text(name):
    task = stand.TASKS[name]
    assert task.check(GOOD_CHAT)
    assert not task.check({**GOOD_CHAT, "sent": ["— " + stand.MESSAGE]})  # лишнее тире из формулировки цели
    assert not task.check({**GOOD_CHAT, "sent": [stand.MESSAGE, stand.MESSAGE]})  # отправлено дважды
    assert not task.check({**GOOD_CHAT, "openChat": "Работа"})
    assert not task.check({**GOOD_CHAT, "openChat": None, "sent": []})
    assert not task.check(None)
    assert task.problem({**GOOD_CHAT, "openChat": None, "sent": []}) == "чат не открыт, в поиске «Рабочий»"
    assert task.problem({**GOOD_CHAT, "sent": []}) == "чат открыт, сообщение не отправлено"


def test_form_task_checks_all_four_fields():
    task = stand.TASKS["form"]
    assert task.check(GOOD_FORM)
    other = {**GOOD_FORM, "submitted": {**stand.FORM_EXPECTED, "country": "Russia"}}
    assert not task.check(other)
    assert task.problem(other) == "отправлено не то: country='Russia'"
    unsent = {**GOOD_FORM, "submitted": None, "fields": {**stand.FORM_EXPECTED, "agree": False}}
    assert task.problem(unsent) == "форма не отправлена (не заполнено: agree)"
    assert not task.check(None)


def test_task_pages_exist_and_carry_the_labels_the_scenarios_use():
    app = (FIXTURES / "app.html").read_text(encoding="utf-8")
    for label in (stand.SEARCH_LABEL, "Clear search", "Type a message", "Send", "Voice message"):
        assert f'aria-label="{label}"' in app
    for chat in ("'Рабочий'", "'Работа'", "'Monday'", "'Кеша'"):
        assert chat in app
    form = (FIXTURES / "form.html").read_text(encoding="utf-8")
    assert "Kazakhstan" in form and "I agree to the terms" in form
    for task in stand.TASKS.values():
        if task.network:
            assert task.url and task.url.startswith("https://") and task.page == ""
        else:
            assert (FIXTURES / task.page.split("?")[0]).is_file()


def test_remount_task_turns_on_the_whatsapp_like_page_parts():
    task = stand.TASKS["search-remount"]
    params = dict(parse_qsl(urlsplit(task.page).query))
    assert params == {"delay": "700", "remount": "1600", "sendstatus": "1500", "net": "1"}
    assert (int(params["remount"]), int(params["sendstatus"])) == (stand.REMOUNT_MS, stand.SEND_STATUS_MS)
    assert task.goal == stand.CHAT_GOAL and task.scenario == stand.CHAT_SCENARIO
    for other in ("search", "search-spinner", "boot"):  # прежние задачи — без пересоздания и статусов
        assert "remount" not in stand.TASKS[other].page and "sendstatus" not in stand.TASKS[other].page
    app = (FIXTURES / "app.html").read_text(encoding="utf-8")
    for part in (
        "params.get('remount')",
        "num('sendstatus', 0)",
        "Type a message to ${chat.name}",
        "'Sending…'",
        "'Sent'",
    ):
        assert part in app
    assert 'aria-label="Message info"' in app
    assert stand.COMPOSER_TO == "Type a message to Рабочий"


def test_status_labels_match_only_message_status_buttons():
    page = {
        "actions": [
            {"kind": "click", "label": "09:42 Sent"},
            {"kind": "click", "label": "10:05 Sending…"},
            {"kind": "click", "label": "Рабочий 12:52 Кто заберёт ключи"},
            {"kind": "click", "label": "Sent"},
            {"kind": "fill", "label": stand.COMPOSER_TO, "value": ""},
        ]
    }
    assert stand.labels(page, stand.STATUS_SENT) == ["09:42 Sent"]
    assert stand.labels(page, stand.STATUS_SENDING) == ["10:05 Sending…"]


def test_delay_flag_changes_only_pages_with_a_search_delay():
    assert stand.page_for(stand.TASKS["search"], delay=900) == "app.html?delay=900&net=1"
    assert stand.page_for(stand.TASKS["boot"], delay=900) == "app.html?boot=4000&delay=900&net=1"
    remount_page = "app.html?delay=900&remount=1600&sendstatus=1500&net=1"
    assert stand.page_for(stand.TASKS["search-remount"], delay=900) == remount_page
    remount = stand.page_for(stand.TASKS["search-remount"], delay=None, remount="800,1600")
    assert remount == "app.html?delay=700&remount=800,1600&sendstatus=1500&net=1"  # запятая списка — как есть
    assert stand.page_for(stand.TASKS["search"], delay=None, remount=1600) == "app.html?delay=700&net=1"  # remount нет
    assert stand.page_for(stand.TASKS["search"], delay=None, net=0, sendstatus=900) == "app.html?delay=700&net=0"
    assert stand.parse_args(["--remount", "1600,800"]).remount == "800,1600" and stand.parse_args([]).remount is None
    assert stand.parse_args(["--net", "0", "--sendstatus", "900"]).net == 0
    assert stand.page_for(stand.TASKS["form"], delay=900) == "form.html"
    assert stand.with_params("form.html", run="x-1") == "form.html?run=x-1"


def test_main_on_fake_core_writes_every_run_unverified(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(stand, "REPORT_MISSING_S", 0.0)
    core = FakeCore()
    env = {"OPENROUTER_API_KEY": "k", "BROWSER_HANDS_CHROME_BINARY": sys.executable}  # launch: бинарь должен быть
    argv = ["--runs", "2", "--label", "t", "--out-dir", str(tmp_path)]

    code = stand.main(argv, env=env, factories=core.factories())

    assert code == 1  # FakeAgent страницу не открывает: отчётов нет
    assert len(core.chromes) == 1 and core.chromes[0].config.mode == "launch" and core.chromes[0].config.headless
    assert len(core.agents) == 10 and all("run=" in agent["url"] for agent in core.agents)
    assert {agent["run"].max_steps for agent in core.agents} == {12}
    (trace,) = tmp_path.glob("eval-*.jsonl")
    lines = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    runs, summary = lines[:-1], lines[-1]["summary"]
    assert len(runs) == 10 and all(row["verified"] is False for row in runs)
    assert all("run=" in row["start_url"] and row["label"] == "t" for row in runs)
    # по кругу: прогон 1 всех задач, потом 2; wiki (сеть) по умолчанию не входит
    assert (
        [row["task"] for row in runs[:5]]
        == stand.DEFAULT_TASKS
        == ["search", "search-spinner", "boot", "search-remount", "form"]
    )
    assert runs[0]["problem"] == "нет отчёта со страницы"
    assert all(row["mode"] == "goal" and "scenario" not in row for row in runs)
    assert all(agent["goal"] and agent["steps"] is None for agent in core.agents)  # режим цели — как раньше
    assert summary["runs"] == 10 and summary["verified"] == 0 and summary["mode"] == "goal"
    assert [s["task"] for s in summary["tasks"]] == stand.DEFAULT_TASKS
    assert summary["tasks"][0]["done"] == 2 and summary["tasks"][0]["failures"] == {"done: нет отчёта со страницы": 2}
    out = capsys.readouterr().out
    assert "verified 0/10" in out and "trace:" in out


def test_main_stops_at_the_cost_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(stand, "REPORT_MISSING_S", 0.0)
    core = FakeCore()  # make_result: 2 шага по $0.0021
    env = {"OPENROUTER_API_KEY": "k", "BROWSER_HANDS_CHROME_BINARY": sys.executable}
    argv = ["--runs", "3", "--tasks", "form", "--max-cost", "0.005", "--out-dir", str(tmp_path)]

    assert stand.main(argv, env=env, factories=core.factories()) == 1
    assert len(core.agents) == 2  # $0.0042 < $0.005 → второй прогон; $0.0084 → стоп
    (trace,) = tmp_path.glob("eval-*.jsonl")
    summary = json.loads(trace.read_text(encoding="utf-8").splitlines()[-1])["summary"]
    assert summary["runs"] == 2 and "бюджет" in summary["stopped"]


def test_main_without_key_is_a_config_error(tmp_path, capsys):
    code = stand.main(["--out-dir", str(tmp_path)], env={"BROWSER_HANDS_CHROME_BINARY": sys.executable})
    assert code == 2 and "нет ключа" in capsys.readouterr().err
    assert not list(tmp_path.glob("eval-*.jsonl"))


def test_unknown_task_is_rejected():
    with pytest.raises(SystemExit):
        stand.parse_args(["--tasks", "search,nope"])


def test_record_decisions_keeps_what_jev_saw_and_restores_choose():
    from browser_hands import agent

    original = agent.choose
    state = {
        "url": "http://127.0.0.1/app.html",
        "text": "Chats\nMonday",
        "actions": [
            {"id": "e1", "kind": "fill", "label": stand.SEARCH_LABEL, "value": "Раб"},
            {"id": "e2", "kind": "click", "label": "Clear search"},
            {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
        ],
    }

    class Decision:
        choice, operation, confidence = "e2", "CLICK", 0.8

    def fake_choose(clients, state, goal, history, *, timeout=None):
        return Decision()

    sink: list = []
    agent.choose = fake_choose  # вместо Jev: record_decisions оборачивает то, что сейчас в модуле
    try:
        with stand.record_decisions(sink):
            assert agent.choose is not fake_choose
            assert agent.choose(None, state, "g", [], timeout=1.0).choice == "e2"
        assert agent.choose is fake_choose
    finally:
        agent.choose = original
    (seen,) = sink
    assert seen["op"] == "CLICK" and seen["target"] == "Clear search" and seen["controls_total"] == 2
    assert seen["controls"] == [f"fill:{stand.SEARCH_LABEL}='Раб'", "click:Clear search"]
    assert seen["step_done"] is None and "scenario_step" not in seen  # режим цели / ядро без step_done
    assert isinstance(seen["t_ms"], int) and seen["t_ms"] >= 0  # когда пришёл ответ, от начала прогона
    assert stand.trail(sink) == "CLICK «Clear search»"


# --- режим сценария и wiki -----------------------------------------------------------------------------------------

ENV = {"OPENROUTER_API_KEY": "k", "BROWSER_HANDS_CHROME_BINARY": sys.executable}


def read_trace(out_dir: Path) -> tuple[list[dict], dict]:
    (trace,) = out_dir.glob("eval-*.jsonl")
    lines = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    return lines[:-1], lines[-1]["summary"]


def test_every_task_has_a_valid_scenario_with_the_goal_texts():
    for task in stand.TASKS.values():
        steps = parse_steps(list(task.scenario))
        assert steps == task.steps() and len(steps) >= 2
        assert all(step.do[0].isascii() and step.do[0].isupper() for step in steps)  # do — по-английски
    chat = stand.TASKS["search"].steps()
    assert [step.text for step in chat] == [stand.CHAT, None, stand.MESSAGE, None]
    assert stand.TASKS["search-spinner"].scenario == stand.TASKS["boot"].scenario == stand.CHAT_SCENARIO
    assert stand.TASKS["search-remount"].scenario == stand.CHAT_SCENARIO
    form = stand.TASKS["form"].steps()
    assert [step.text for step in form[:2]] == [stand.FORM_EXPECTED["name"], stand.FORM_EXPECTED["email"]]
    assert "Kazakhstan" in form[2].do and form[2].text is None
    wiki = stand.TASKS["wiki"].steps()
    assert wiki[0].text == "Gödel's incompleteness theorems" and wiki[1].text is None


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("https://en.wikipedia.org/wiki/G%C3%B6del%27s_incompleteness_theorems", None),
        ("https://en.wikipedia.org/wiki/Gödel's_incompleteness_theorems#First", None),
        ("https://en.wikipedia.org/wiki/Main_Page", "открыт https://en.wikipedia.org/wiki/Main_Page"),
        (
            "https://en.wikipedia.org/w/index.php?search=G%C3%B6del%27s+incompleteness+theorems",
            "открыт https://en.wikipedia.org/w/index.php?search=G%C3%B6del%27s+incompleteness+theorems",
        ),
    ],
)
def test_wiki_is_verified_by_the_final_url(url, problem):
    task = stand.TASKS["wiki"]
    assert task.network and task.url == stand.WIKI_URL
    assert task.verify_result is not None and task.verify_result(make_result(url=url)) == problem
    assert task.verify(make_result(url=url), None) == problem  # отчёта страницы у внешнего сайта нет


def test_scenario_mode_passes_steps_without_goal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(stand, "REPORT_MISSING_S", 0.0)
    core = FakeCore(result=make_result("blocked", steps=3, scenario=(1, 4), jev_calls=3))
    argv = ["--mode", "scenario", "--runs", "1", "--out-dir", str(tmp_path)]

    assert stand.main(argv, env=ENV, factories=core.factories()) == 1

    assert [agent["goal"] for agent in core.agents] == [""] * 5
    for agent, name in zip(core.agents, stand.DEFAULT_TASKS, strict=True):
        assert agent["steps"] == stand.TASKS[name].steps()
    runs, summary = read_trace(tmp_path)
    assert all(row["mode"] == "scenario" for row in runs) and summary["mode"] == "scenario"
    row = runs[0]
    assert (row["scenario_done"], row["scenario_total"]) == (1, 4)
    assert (row["model_calls"], row["jev_calls"], row["text_calls"]) == (3, 3, 0)
    assert row["scenario"][0] == {"do": "Type the chat name into the chat search box", "text": "Рабочий"}
    first = summary["tasks"][0]
    assert (first["jev_calls_median"], first["text_calls_median"], first["text_calls_total"]) == (3, 0, 0)
    assert first["failures"] == {"blocked [сценарий 1/4]: нет отчёта со страницы": 1}
    out = capsys.readouterr().out
    assert "итого (scenario): verified 0/5" in out
    assert "    ×1 blocked [сценарий 1/4]: нет отчёта со страницы" in out
    assert " 1/4 " in out  # колонка scn в строке прогона


def test_text_calls_are_model_calls_minus_jev_calls():
    assert stand.text_calls(make_result(steps=4, jev_calls=3)) == 1
    assert stand.text_calls(make_result(steps=0, jev_calls=0)) == 0
    assert stand.text_calls(make_result(steps=2, jev_calls=0)) is None  # ядро без счёта Jev: не «всё — текст»


def test_goal_mode_summary_without_jev_counts_prints_dashes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(stand, "REPORT_MISSING_S", 0.0)
    core = FakeCore()  # make_result: jev_calls = 0 — как у ядра до сценариев

    stand.main(["--runs", "1", "--tasks", "form", "--out-dir", str(tmp_path)], env=ENV, factories=core.factories())

    runs, summary = read_trace(tmp_path)
    assert runs[0]["text_calls"] is None
    assert summary["tasks"][0]["jev_calls_median"] is None and summary["tasks"][0]["text_calls_median"] is None
    assert summary["tasks"][0]["failures"] == {"done: нет отчёта со страницы": 1}
    out = capsys.readouterr().out
    assert "jev / text — медианы вызовов Jev и текстовой модели (model_calls − jev_calls)" in out


def test_wiki_run_uses_the_site_url_and_the_result_url(tmp_path, monkeypatch):
    monkeypatch.setattr(stand, "REPORT_MISSING_S", 5.0)  # отчёта не ждём вовсе: внешний сайт
    article = "https://en.wikipedia.org/wiki/G%C3%B6del%27s_incompleteness_theorems"
    core = FakeCore(result=make_result(url=article, scenario=(2, 2), jev_calls=3))
    argv = ["--mode", "scenario", "--tasks", "wiki", "--runs", "1", "--out-dir", str(tmp_path)]

    assert stand.main(argv, env=ENV, factories=core.factories()) == 0

    (agent,) = core.agents
    assert agent["url"] == stand.WIKI_URL and agent["steps"] == stand.TASKS["wiki"].steps()
    runs, summary = read_trace(tmp_path)
    assert runs[0]["verified"] is True and runs[0]["report"] is None and runs[0]["start_url"] == stand.WIKI_URL
    assert summary["verified"] == 1


def test_tasks_all_adds_wiki_and_mode_is_checked():
    assert stand.parse_args([]).tasks == stand.DEFAULT_TASKS
    assert "wiki" not in stand.DEFAULT_TASKS
    assert stand.parse_args(["--tasks", "all"]).tasks == list(stand.TASKS)
    assert stand.parse_args(["--tasks", "wiki,form"]).tasks == ["wiki", "form"]
    assert stand.parse_args([]).mode == "goal"
    assert stand.parse_args(["--mode", "scenario"]).mode == "scenario"
    with pytest.raises(SystemExit):
        stand.parse_args(["--mode", "x"])


def test_record_decisions_keeps_the_scenario_step_and_step_done():
    from browser_hands import agent

    original = agent.choose
    state = {
        "url": "http://127.0.0.1/app.html",
        "text": "",
        "actions": [{"id": "e2", "kind": "click", "label": "Send"}],
    }

    class Decision:
        choice, operation, confidence, step_done = "e2", "CLICK", 0.8, 0.8349

    class Step:
        number = 2

    def fake_choose(clients, state, goal, history, *args, step=None, timeout=None):
        return Decision()

    sink: list = []
    agent.choose = fake_choose
    try:
        with stand.record_decisions(sink):
            agent.choose(None, state, "", [], step=Step(), timeout=1.0)
            agent.choose(None, state, "", [], None)  # позиционный хвост тоже доходит до ядра
    finally:
        agent.choose = original
    first, second = sink
    assert (first["scenario_step"], first["step_done"]) == (2, 0.835)
    assert "scenario_step" not in second
    assert stand.trail(sink).startswith("CLICK «Send» (шаг 2, p=0.83) → CLICK «Send» (шаг ?, p=0.83)")


def test_fixture_checks_cover_every_local_task():
    assert set(stand.FIXTURE_CHECKS) == set(stand.DEFAULT_TASKS)
    assert not hasattr(
        stand, "SCENARIOS"
    )  # «сценарий» в стенде — только steps задачи, проверки страниц — FIXTURE_CHECKS


# --- net=1, отчёты со временем, развёртка (docs/plan-waits.md §7) -------------------------------------------------


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.headers["Content-Type"].startswith("application/json")
        return json.loads(response.read())


def test_api_answers_after_the_server_side_pause_and_logs_the_call_per_run():
    with stand.FixtureServer() as server:
        started = time.monotonic()
        assert get_json(server.url("api/search?q=%D0%A0%D0%B0%D0%B1&delay=120&run=r1")) == {
            "ok": True,
            "api": "search",
            "q": "Раб",
        }
        assert time.monotonic() - started >= 0.12  # пауза — на сервере, а не в странице
        assert post(server.url("api/send?delay=0&run=r1"), b'{"text": "x"}') == 200
        get_json(server.url("api/search?q=x&delay=oops&run=r2"))  # не число — без паузы
        search, send = server.calls("r1")
        assert (search["api"], search["q"], search["delay_ms"]) == ("search", "Раб", 120)
        assert search["t1"] - search["t0"] >= 0.12 and send["api"] == "send" and send["delay_ms"] == 0
        assert server.calls("r1", "send") == [send] and server.calls("r2")[0]["delay_ms"] == 0
        timeline = stand.call_timeline(server.calls("r1"), started)
        assert timeline[0]["t_ms"] >= 0 and timeline[0]["ms"] >= 120 and timeline[1]["api"] == "send"
    assert stand.api_delay("250") == 250 and stand.api_delay(None) == 0 and stand.api_delay("-5") == 0
    assert stand.api_delay("inf") == 0 and stand.api_delay(str(10**9)) == stand.API_MAX_DELAY_MS


def test_report_history_keeps_every_report_with_its_arrival_time():
    with stand.FixtureServer() as server:
        started = time.monotonic()
        for seq, composer in ((1, None), (2, "при"), (3, "")):
            report = {"run": "r", "seq": seq, "openChat": "Рабочий", "composer": composer, "params": {"delay": 700}}
            post(server.url("report"), json.dumps(report).encode())
        post(server.url("report"), json.dumps({"run": "r", "seq": 1}).encode())  # устаревший — не в историю
        history = server.history("r")
        assert [report["seq"] for _, report in history] == [1, 2, 3]
        timeline = stand.report_timeline(history, started)
    assert [row["composer"] for row in timeline] == [None, "при", ""]
    assert all(row["t_ms"] >= 0 and "params" not in row and "seq" not in row for row in timeline)
    assert timeline[0] == {"t_ms": timeline[0]["t_ms"], "openChat": "Рабочий", "composer": None}


def test_page_params_mirror_the_page_defaults_and_list_parsing():
    p = stand.PageParams.of("app.html")
    assert (p.delay, p.remount, p.sendstatus, p.senddelay, p.net, p.spinner, p.boot) == (
        700,
        (),
        0,
        300,
        True,
        False,
        0,
    )
    q = stand.PageParams.of("app.html?delay=0&remount=1600,800,0,x&sendstatus=1500&net=0&spinner=1&boot=-3")
    assert (q.delay, q.remount, q.sendstatus, q.net, q.spinner, q.boot) == (0, (800, 1600), 1500, False, True, 0)
    assert q.as_report() == {
        "delay": 0,
        "remount": [800, 1600],
        "sendstatus": 1500,
        "senddelay": 300,
        "net": 0,
        "spinner": 1,
        "boot": 0,
    }
    page = "app.html?delay=500&remount=800,1600&net=1"
    same = {"params": stand.PageParams.of(page).as_report()}
    assert stand.params_problem(page, same) is None
    assert stand.params_problem(page, None) is None and stand.params_problem("form.html", {"run": "r"}) is None
    wrong = stand.params_problem(page, {"params": {**same["params"], "remount": [1600]}})
    assert wrong is not None and wrong.startswith("стенд:") and "remount=[1600], ждали [800, 1600]" in wrong
    app = (FIXTURES / "app.html").read_text(encoding="utf-8")
    for part in ("num('senddelay', 300)", "num('net', 1)", "`/api/${api}?", "composer: open ? textOf(composer)"):
        assert part in app
    assert "params: PARAMS" in app and "remountTimers = REMOUNT.map(" in app


@pytest.mark.parametrize(
    ("text", "values"),
    [
        ("0:3000:500", [0, 500, 1000, 1500, 2000, 2500, 3000]),
        ("0:3000:1000", [0, 1000, 2000, 3000]),
        ("1500,0,1500", [0, 1500]),
        ("700", [700]),
        ("0:0:1", [0]),
    ],
)
def test_parse_values_ranges_and_lists(text, values):
    assert stand.parse_values(text) == values


@pytest.mark.parametrize("text", ["", "a", "0:3000", "3000:0:500", "0:10:0", "-1,5", "1:2:3:4"])
def test_parse_values_rejects_bad_input(text):
    with pytest.raises(ValueError, match="--delay-range"):
        stand.parse_values(text, name="--delay-range")


def test_axes_sweep_has_18_cells_with_the_planned_axes():
    args = stand.parse_args(["--sweep", "axes"])
    cells = stand.sweep_of(args)
    assert len(cells) == 18 and len({cell.id for cell in cells}) == 18
    axes: dict[str, list[int]] = {}
    for cell in cells:
        for axis, value in cell.axes:
            axes.setdefault(axis, []).append(value)
    assert {axis: sorted(values) for axis, values in axes.items()} == {
        "delay net=1": [0, 500, 1500, 3000],
        "delay net=0": [0, 500, 1500, 3000],
        "remount": [0, 500, 1000, 1500, 2000, 2500, 3000],
        "sendstatus remount=0": [0, 1500],
        "sendstatus remount=1600": [0, 1500],
    }
    by_id = {cell.id: cell for cell in cells}
    # remount 0 на remount-оси — та же ячейка, что sendstatus 1500 при remount 0: одна, на двух осях
    assert by_id["d700-r0-s1500-n1"].axes == (("remount", 0), ("sendstatus remount=0", 1500))
    assert dict(by_id["d500-r0-s0-n0"].params) == {"delay": 500, "remount": 0, "sendstatus": 0, "net": 0}
    assert dict(by_id["d700-r2500-s1500-n1"].params) == {"delay": 700, "remount": 2500, "sendstatus": 1500, "net": 1}
    assert by_id["d0-r0-s0-n1"].query() == {"delay": "0", "remount": "0", "sendstatus": "0", "net": "1"}
    grid = stand.sweep_cells("grid", delays=[0, 700], remounts=[0, 1600], sendstatuses=[0, 1500], nets=[1])
    assert len(grid) == 8 and all(len(cell.axes) == 4 for cell in grid)
    with pytest.raises(ValueError):
        stand.sweep_cells("line", delays=[0], remounts=[0], sendstatuses=[0], nets=[1])


def test_sweep_args_defaults_and_errors():
    args = stand.parse_args(["--sweep", "axes", "--mode", "goal,scenario"])
    assert args.tasks == ["search-remount"] and args.modes == ["goal", "scenario"]
    assert (args.delays, args.nets, args.sendstatuses) == ([0, 500, 1500, 3000], [0, 1], [0, 1500])
    assert args.remounts == [0, 500, 1000, 1500, 2000, 2500, 3000] and args.net is None and args.sendstatus is None
    narrowed = stand.parse_args(["--sweep", "axes", "--net", "1", "--delay-range", "0:3000:500"])
    assert len(stand.sweep_of(narrowed)) == 7 + 7 + 3
    for bad in (
        ["--mode", "goal,scenario"],  # несколько режимов — только развёртка
        ["--mode", "goal,x"],
        ["--sweep", "axes", "--tasks", "form"],
        ["--sweep", "axes", "--delay", "900"],
        ["--sweep", "axes", "--net", "0,2"],
        ["--net", "0,1"],  # список — только развёртка
        ["--sweep", "axes", "--remount-range", "0:10"],
    ):
        with pytest.raises(SystemExit):
            stand.parse_args(bad)


def page_reporting_factories(core: FakeCore, *, sent: list[str], params_from_page: bool = True) -> dict:
    """Фабрики FakeCore, чей «агент» перед прогоном отчитывается за страницу, как app.html: три отчёта (поиск,
    чат, отправлено) с разобранными параметрами — стенд проверяет по ним, а не по статусу агента."""
    factories = core.factories()
    make_agent = factories["agent_factory"]

    def agent_factory(chrome, clients, url, goal, run, **kwargs):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        base = f"{parts.scheme}://{parts.netloc}/"
        params = stand.PageParams.of(url).as_report() if params_from_page else {"delay": -1}
        states = [
            {"openChat": None, "query": "Раб", "sent": [], "composer": None},
            {"openChat": "Рабочий", "query": "Раб", "sent": [], "composer": stand.MESSAGE},
            {"openChat": "Рабочий", "query": "Раб", "sent": sent, "composer": ""},
        ]
        for seq, state in enumerate(states, 1):
            report = {"run": query["run"], "seq": seq, "ready": True, "params": params, **state}
            post(base + "report", json.dumps(report).encode())
        urllib.request.urlopen(base + f"api/search?q=x&delay=0&run={query['run']}", timeout=5).read()
        return make_agent(chrome, clients, url, goal, run, **kwargs)

    return {**factories, "agent_factory": agent_factory}


def test_sweep_on_fake_core_writes_cells_round_robin_with_reports(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(stand, "REPORT_QUIET_S", 0.0)
    core = FakeCore(result=make_result(scenario=(4, 4), jev_calls=2))
    factories = page_reporting_factories(core, sent=[stand.MESSAGE])
    argv = ["--sweep", "axes", "--delay-range", "0,1500", "--remount-range", "0,1600", "--net", "1"]
    argv += ["--mode", "goal,scenario", "--runs", "2", "--label", "sw", "--out-dir", str(tmp_path)]

    assert stand.main(argv, env=ENV, factories=factories) == 0

    runs, summary = read_trace(tmp_path)
    cells = stand.sweep_of(stand.parse_args(argv))
    assert len(cells) == 2 + 2 + 2  # delay 0/1500; remount 0/1600; sendstatus: 1600/0 и 0/0 новые
    assert len(runs) == len(cells) * 2 * 2 and all(row["verified"] for row in runs)
    first_round = [(row["cell"], row["mode"]) for row in runs[: len(cells) * 2]]
    assert first_round == [(cell.id, mode) for cell in cells for mode in ("goal", "scenario")]
    assert [row["run"] for row in runs] == [1] * (len(cells) * 2) + [2] * (len(cells) * 2)
    row = runs[0]
    assert row["task"] == "search-remount" and row["params"] == dict(cells[0].params)
    assert row["axes"] == [list(axis) for axis in cells[0].axes]
    query = dict(parse_qsl(urlsplit(row["start_url"]).query))
    assert {key: int(query[key]) for key in ("delay", "remount", "sendstatus", "net")} == row["params"]
    assert [report["composer"] for report in row["reports"]] == [None, stand.MESSAGE, ""]
    assert all(report["t_ms"] >= 0 for report in row["reports"])
    assert row["api_calls"][0]["api"] == "search" and row["api_calls"][0]["t_ms"] >= 0
    assert row["steps"][0]["wait_reason"] == "quiet" and row["steps"][0]["pending_requests"] == 0
    assert summary["sweep"] == "axes" and summary["modes"] == ["goal", "scenario"]
    assert len(summary["cells"]) == len(cells) * 2 and summary["cells"][0]["runs"] == 2
    out = capsys.readouterr().out
    assert "## delay net=1 · goal" in out and "итого (развёртка axes, goal, scenario): ячеек 12" in out
    assert out.splitlines()[0].startswith("cell")


def test_sweep_row_fails_when_the_page_understood_other_params(tmp_path, monkeypatch):
    monkeypatch.setattr(stand, "REPORT_QUIET_S", 0.0)
    core = FakeCore()
    factories = page_reporting_factories(core, sent=[stand.MESSAGE], params_from_page=False)
    argv = ["--sweep", "axes", "--delay-range", "0", "--remount-range", "0", "--sendstatus", "0", "--net", "1"]
    argv += ["--runs", "1", "--out-dir", str(tmp_path)]

    assert stand.main(argv, env=ENV, factories=factories) == 0  # развёртка — замер: сделаны все прогоны
    runs, summary = read_trace(tmp_path)
    assert runs and not any(row["verified"] for row in runs)
    assert runs[0]["problem"].startswith("стенд: страница поняла параметры иначе")
    assert summary["verified"] == 0


def test_sweep_stops_at_the_budget_with_cells_evenly_covered(tmp_path, monkeypatch):
    monkeypatch.setattr(stand, "REPORT_MISSING_S", 0.0)
    core = FakeCore()  # $0.0042 за прогон
    argv = ["--sweep", "axes", "--runs", "5", "--max-cost", "0.1", "--out-dir", str(tmp_path)]

    assert stand.main(argv, env=ENV, factories=core.factories()) == 1  # недобор прогонов
    runs, summary = read_trace(tmp_path)
    assert len(runs) == 24 and "бюджет" in summary["stopped"]  # $0.1 / $0.0042 → 24 прогона
    assert [row["cell"] for row in runs[:18]] == [cell.id for cell in stand.sweep_of(stand.parse_args(argv))]


def test_fixture_pages_cover_variants_overrides_and_sweep_cells():
    default = stand.parse_args(["--fixtures-only"])
    remount = stand.fixture_pages(stand.TASKS["search-remount"], default)
    assert [label for label, _ in remount] == ["", "remount=800,1600", "net=0"]
    assert remount[1][1] == "app.html?delay=700&remount=800,1600&sendstatus=1500&net=1"
    assert stand.fixture_pages(stand.TASKS["form"], default) == [("", "form.html")]
    assert stand.fixture_pages(stand.TASKS["boot"], default)[0][0] == "boot=2000"
    override = stand.parse_args(["--fixtures-only", "--net", "0", "--remount", "800,1600"])
    assert stand.fixture_pages(stand.TASKS["search-remount"], override) == [
        ("remount=800,1600,net=0", "app.html?delay=700&remount=800,1600&sendstatus=1500&net=0")
    ]
    assert stand.fixture_pages(stand.TASKS["search"], override) == [("net=0", "app.html?delay=700&net=0")]
    sweep = stand.fixture_pages(stand.TASKS["search-remount"], stand.parse_args(["--fixtures-only", "--sweep", "axes"]))
    assert len(sweep) == 18 and sweep[0][0] == "d0-r0-s0-n1"
    assert sweep[0][1] == "app.html?delay=0&remount=0&sendstatus=0&net=1"


def test_readme_describes_the_sweep_and_the_remount_default():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    dev = " ".join(readme[readme.index("## Разработка") :].split())
    assert "0,9 с" not in dev
    assert f"{stand.REMOUNT_MS / 1000:g}".replace(".", ",") + " с" in dev  # время пересоздания — как REMOUNT_MS
    for part in ("--sweep axes", "net=1", "net=0", "remount=800,1600", "scripts/sweep_report.py", "--fuse"):
        assert part in dev


# --- sweep_report --------------------------------------------------------------------------------------------------


def load_report():
    spec = importlib.util.spec_from_file_location("sweep_report_stand", ROOT / "scripts" / "sweep_report.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


report_module = load_report()


def sweep_row(axis, value, *, mode="scenario", ok=True, elapsed=6000, wait=900, label="before", **extra):
    params = {"delay": 700, "remount": value if axis == "remount" else 0, "sendstatus": 1500, "net": 1}
    return {
        "task": "search-remount",
        "mode": mode,
        "label": label,
        "verified": ok,
        "elapsed_ms": elapsed,
        "timing": {"wait_ms": wait},
        "cost": 0.001,
        "cell": f"c{value}",
        "params": params,
        "axes": [[axis, value]],
        **extra,
    }


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    lines = [json.dumps(row, ensure_ascii=False) for row in rows] + [json.dumps({"summary": {}})]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_wilson_interval_matches_known_values():
    assert report_module.wilson(0, 0) == (0.0, 1.0)
    lo, hi = report_module.wilson(5, 5)
    assert round(lo, 3) == 0.566 and hi == 1.0
    lo, hi = report_module.wilson(0, 5)
    assert lo == 0.0 and round(hi, 3) == 0.434
    lo, hi = report_module.wilson(1, 15)
    assert round(lo, 3) == 0.012 and round(hi, 3) == 0.298


def test_sweep_report_tables_curves_and_before_after(tmp_path, capsys):
    before = [sweep_row("remount", 0, elapsed=5000 + i * 100) for i in range(5)]
    before += [sweep_row("remount", 1600, ok=i == 0, elapsed=9000) for i in range(5)]
    before += [sweep_row("remount", 0, mode="goal") for _ in range(3)]
    after = [sweep_row("remount", value, label="after") for value in (0, 1600) for _ in range(5)]
    first = write_jsonl(tmp_path / "b.jsonl", before)
    second = write_jsonl(tmp_path / "a.jsonl", after)
    out = tmp_path / "traces" / "sweep.md"

    assert report_module.main([str(first), str(second), "--out", str(out)]) == 0

    text = capsys.readouterr().out
    assert "# Развёртка стенда — before: 13 прогонов | after: 10 прогонов" in text
    assert "## remount · scenario (при delay=700, sendstatus=1500, net=1)" in text
    assert "| remount | before: успех (95 %) |" in text and "after: успех (95 %)" in text
    assert "| 0 | 5/5 (57–100 %) | 5.2 / 5.4 | 0.9 / 0.9 | 5/5 (57–100 %)" in text
    assert "| 1600 | 1/5 (4–62 %) | 9.0 / 9.0 |" in text
    assert "  1600 before ████················ 1/5  медиана 9.0 с" in text
    assert "       after  ████████████████████ 5/5" in text
    assert (
        "## remount · goal" in text
        and "| 0 | 3/3" in text
        and "| 0 | 3/3 (44–100 %) | 6.0 / 6.0 | 0.9 / 0.9 | — | — | — |" in text
    )
    assert "before: ячеек 2, режимы goal, scenario, verified 9/13, cost $0.0130" in text
    assert out.read_text(encoding="utf-8").startswith("# Развёртка стенда")


def test_sweep_report_without_sweep_rows_says_so(tmp_path, capsys):
    plain = write_jsonl(tmp_path / "plain.jsonl", [{"task": "form", "verified": True, "mode": "goal"}])
    assert report_module.main([str(plain)]) == 1
    assert "строк развёртки (`axes`) нет" in capsys.readouterr().out
    assert report_module.main([str(tmp_path / "nope.jsonl")]) == 2


def test_fuse_takes_the_last_page_change_before_the_next_decision(tmp_path, capsys):
    decisions = [
        {"op": "TYPE_TEXT", "t_ms": 1000},
        {"op": "CLICK", "t_ms": 2000},
        {"op": "WAIT", "t_ms": 2500},  # не действие
        {"op": "CLICK", "t_ms": 3000},
        {"op": "DONE", "t_ms": 6000},
    ]
    reports = [{"t_ms": t} for t in (100, 1100, 1400, 2100, 4600)]  # после CLICK 3000 — 4600 (пересоздание)
    row = sweep_row("remount", 1600, decisions=decisions, reports=reports)
    samples, quiet = report_module.fuse_samples([row])
    assert samples == [400, 100, 1600] and quiet == 0
    samples, quiet = report_module.fuse_samples([{**row, "reports": [{"t_ms": 100}]}])
    assert samples == [] and quiet == 3
    wa = tmp_path / "wa-observe-1.json"
    wa.write_text(json.dumps({"mutations": {"last_significant_ms": 1629.9}}), encoding="utf-8")
    path = write_jsonl(tmp_path / "f.jsonl", [row])

    assert report_module.main([str(path), "--fuse", "--wa", str(wa)]) == 0

    text = capsys.readouterr().out
    assert "стенд: n=3 (без изменений после действия: 0); p50 400 мс, p90 1600 мс, p99 1600 мс" in text
    assert "WhatsApp (§8): n=1; last_significant_ms: 1630" in text
    assert "рекомендуемое wait_fuse_s = 1.65 с (p99 1630 + кадр 17 мс" in text  # (1629.9 + 17) мс → вверх до 0,05
