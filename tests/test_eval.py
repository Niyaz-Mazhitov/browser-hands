"""Стенд надёжности `scripts/eval.py` офлайн: сервер страниц, проверки задач, прогон на FakeCore (без Chrome и сети)."""

import importlib.util
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

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


@pytest.mark.parametrize("name", ["search", "search-spinner", "boot"])
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


def test_delay_flag_changes_only_pages_with_a_search_delay():
    assert stand.page_for(stand.TASKS["search"], delay=900) == "app.html?delay=900"
    assert stand.page_for(stand.TASKS["boot"], delay=900) == "app.html?boot=4000&delay=900"
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
    assert len(core.agents) == 8 and all("run=" in agent["url"] for agent in core.agents)
    assert {agent["run"].max_steps for agent in core.agents} == {12}
    (trace,) = tmp_path.glob("eval-*.jsonl")
    lines = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    runs, summary = lines[:-1], lines[-1]["summary"]
    assert len(runs) == 8 and all(row["verified"] is False for row in runs)
    assert all("run=" in row["start_url"] and row["label"] == "t" for row in runs)
    # по кругу: прогон 1 всех задач, потом 2; wiki (сеть) по умолчанию не входит
    assert [row["task"] for row in runs[:4]] == stand.DEFAULT_TASKS == ["search", "search-spinner", "boot", "form"]
    assert runs[0]["problem"] == "нет отчёта со страницы"
    assert all(row["mode"] == "goal" and "scenario" not in row for row in runs)
    assert all(agent["goal"] and agent["steps"] is None for agent in core.agents)  # режим цели — как раньше
    assert summary["runs"] == 8 and summary["verified"] == 0 and summary["mode"] == "goal"
    assert [s["task"] for s in summary["tasks"]] == stand.DEFAULT_TASKS
    assert summary["tasks"][0]["done"] == 2 and summary["tasks"][0]["failures"] == {"done: нет отчёта со страницы": 2}
    out = capsys.readouterr().out
    assert "verified 0/8" in out and "trace:" in out


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

    assert [agent["goal"] for agent in core.agents] == [""] * 4
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
    assert "итого (scenario): verified 0/4" in out
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
