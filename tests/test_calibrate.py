"""scripts/calibrate.py на синтетических трассах: разметка, выбор порога, «мало данных», согласие с config."""

import importlib.util
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from browser_hands.config import Thresholds

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "calibrate.py"


def load():
    spec = importlib.util.spec_from_file_location("calibrate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # датаклассы модуля с отложенными аннотациями ищут его в sys.modules
    spec.loader.exec_module(module)
    return module


cal = load()

CHAT, MESSAGE = "Рабочий", "привет"
SCENARIO = [
    {"do": "Type the chat name", "text": CHAT},
    {"do": "Open the chat", "text": None},
    {"do": "Type the message", "text": MESSAGE},
    {"do": "Send the message", "text": None},
]


def d(step, op, target=None, p=0.0, conf=0.9, t_ms=None):
    out = {"scenario_step": step, "op": op, "target": target, "step_done": p, "confidence": conf}
    if t_ms is not None:
        out["t_ms"] = t_ms
    return out


def s(step, op, target, text=None):
    return {"scenario_step": step, "operation": op, "target": target, "text": text}


def chat_run(run_id="r1", verified=True, report=None, sent=True):
    """Сценарий чата: ввод, лишний клик по полю, клик по чату, DONE, ввод, Send, DONE (или BLOCKED без отправки)."""
    decisions = [
        d(1, "TYPE_TEXT", "Search", p=0.0, conf=0.97),
        d(2, "CLICK", "Open Search", p=0.2, conf=0.25),
        d(2, "CLICK", "Рабочий 12:52", p=0.6, conf=0.9),
        d(2, "DONE", p=0.95, conf=0.8),
        d(3, "TYPE_TEXT", "Type a message", p=0.0, conf=1.0),
        d(4, "CLICK", "Send", p=0.05, conf=0.97),
        d(4, "DONE", p=0.4, conf=0.6) if sent else d(4, "BLOCKED", p=0.0, conf=0.2),
    ]
    steps = [
        s(1, "TYPE_TEXT", "Search", CHAT),
        s(2, "CLICK", "Open Search"),
        s(2, "CLICK", "Рабочий 12:52"),
        s(3, "TYPE_TEXT", "Type a message", MESSAGE),
        s(4, "CLICK", "Send"),
    ]
    return {
        "_key": run_id,
        "run_id": run_id,
        "task": "search-remount",
        "mode": "scenario",
        "verified": verified,
        "status": "done" if verified else "blocked",
        "scenario": SCENARIO,
        "report": report if report is not None else {"openChat": CHAT, "sent": [MESSAGE] if verified else []},
        "decisions": decisions,
        "steps": steps,
    }


def test_align_takes_the_last_of_identical_decisions_as_executed():
    decisions = [d(1, "CLICK", "Go"), d(1, "CLICK", "Go"), d(1, "DONE"), d(2, "CLICK", "Go")]
    steps = [s(1, "CLICK", "Go"), s(2, "CLICK", "Go")]
    assert cal.align(decisions, steps) == {1: 0, 3: 1}  # первое «Go» устарело, исполнено второе


def test_step_done_truth_is_the_outcome_not_the_closure():
    other, done = cal.step_done_pairs([chat_run()])
    # текстовые шаги — «нет» до ввода; шаг 2 — «нет» до клика по чату; Send — «нет» до клика
    assert [(p.score, p.truth) for p in other] == [
        (0.0, False),
        (0.2, False),
        (0.6, False),
        (0.0, False),
        (0.05, False),
    ]
    assert [(p.score, p.truth) for p in done] == [(0.95, True), (0.4, True)]  # DONE после Send с p=0.4 — «да»


def test_unverified_run_uses_the_page_report_and_skips_unknown_steps():
    run = chat_run(verified=False, sent=False)
    other, done = cal.step_done_pairs([run])
    assert done == [cal.Pair(0.95, True, "r1")]  # чат открыт (отчёт), шаг 4 не выполнен: BLOCKED — «нет»
    assert [(p.score, p.truth) for p in other][-1] == (0.0, False)
    run["task"] = "wiki"  # задача без отчёта страницы: нетекстовые шаги не размечаются
    other, done = cal.step_done_pairs([run])
    assert done == [] and all(p.score == 0.0 for p in other)


def test_useful_action_is_the_last_action_of_a_completed_step():
    scenario, goal = cal.action_pairs([chat_run()])
    assert [(p.score, p.truth) for p in scenario] == [
        (0.97, True),
        (0.25, False),
        (0.9, True),
        (1.0, True),
        (0.97, True),
    ]
    assert goal == []


def goal_run(run_id, conf, verified, status="done"):
    return {
        "_key": run_id,
        "task": "search",
        "verified": verified,
        "status": status,
        "decisions": [d(None, "CLICK", "Go", p=None, conf=0.8), d(None, "DONE", p=None, conf=conf)],
        "steps": [s(None, "CLICK", "Go")],
    }


def test_done_pairs_take_the_last_done_of_goal_runs():
    runs = [goal_run("g1", 0.9, True), goal_run("g2", 0.3, False), goal_run("g3", 0.9, True, status="timeout")]
    assert [(p.score, p.truth) for p in cal.done_pairs(runs)] == [(0.9, True), (0.3, False)]
    assert [(p.score, p.truth) for p in cal.action_pairs(runs)[1]] == [(0.8, True), (0.8, False), (0.8, True)]


def pairs(*groups):
    """(оценка, правда, сколько) → пары, каждая из своего прогона."""
    out = []
    for score, truth, n in groups:
        out += [cal.Pair(score, truth, f"{score}-{truth}-{i}") for i in range(n)]
    return out


def test_enough_data_and_current_outside_the_admissible_range_moves_to_theta_star():
    data = pairs((0.62, False, 40), (0.67, True, 40), (0.95, True, 100))
    verdict = cal.decide("step_done_min_p", data, current=0.3)
    assert verdict.best == 0.65 and verdict.recommended == 0.65 and verdict.enough
    assert verdict.admissible == (0.65, 0.65)
    assert "θ*" in verdict.reason


def test_enough_data_and_current_admissible_keeps_it():
    data = pairs((0.62, False, 40), (0.67, True, 40), (0.95, True, 100))
    verdict = cal.decide("step_done_min_p", data + pairs((0.66, True, 1)), current=0.65)
    assert verdict.recommended == 0.65 and verdict.enough


def test_little_data_keeps_the_current_value_and_says_how_much_to_collect():
    data = pairs((0.3, False, 2), (0.9, True, 50))
    verdict = cal.decide("done_min_confidence", data, current=0.5)
    assert verdict.recommended == 0.5 and not verdict.enough
    assert verdict.admissible == (0.35, 0.9) and verdict.near == 0
    assert verdict.need_pairs > 0 and "мало данных" in verdict.reason


def test_little_data_keeps_even_a_current_value_outside_the_admissible_range_but_says_so():
    data = pairs((0.62, False, 5), (0.9, True, 50))
    verdict = cal.decide("step_done_min_p", data, current=0.6)
    assert not verdict.enough and verdict.admissible == (0.65, 0.9) and verdict.recommended == 0.6
    assert "вне допустимых" in verdict.reason


def test_no_pairs_keeps_the_current_value():
    verdict = cal.decide("step_done_min_p", [], current=0.7)
    assert verdict.recommended == 0.7 and verdict.best is None


def test_wilson_interval_and_the_needed_sample():
    low, high = cal.wilson(0, 10)
    assert low == 0 and high == pytest.approx(0.2775, abs=1e-3)
    assert cal.wilson(5, 10) == pytest.approx((0.2366, 0.7634), abs=1e-3)
    assert cal.needed(0, 0) == 93  # доля неизвестна (0,5): ±0,1 — 93 пары
    assert cal.needed(0, 10) < cal.needed(5, 10)


def test_fuse_samples_come_from_reports_and_observe_files(tmp_path):
    run = chat_run()
    for i, decision in enumerate(run["decisions"]):
        decision["t_ms"] = 1000 * (i + 1)
    run["reports"] = [{"t_ms": 1500}, {"t_ms": 1800}, {"t_ms": 3200}]  # после решений 1 и 3 (исполнены)
    observe = tmp_path / "wa-observe-1.json"
    observe.write_text(json.dumps({"click": {"done": True}, "mutations": {"last_significant_ms": 1640.0}}))
    skipped = tmp_path / "wa-observe-2.json"
    skipped.write_text(json.dumps({"click": {"done": False}, "mutations": {"last_significant_ms": None}}))
    samples, sources = cal.settle_samples([run], [observe, skipped])
    assert samples == pytest.approx([0.8, 0.2, 1.64]) and sources == {"reports": 2, "observe": 1}
    assert cal.fuse_verdict(samples, 1.5).recommended == 1.5  # 3 замера < 30
    assert cal.fuse_verdict([0.5] * 29 + [1.2], 1.5).recommended == 1.3  # p99 1,2 + кадр → вверх до 0,1


def test_main_reads_traces_writes_markdown_and_prints_recommendations(tmp_path, capsys):
    traces = tmp_path / "eval-1.jsonl"
    rows = [chat_run("a"), chat_run("b", verified=False, sent=False), goal_run("g", 0.9, True), {"summary": {}}]
    traces.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    out = tmp_path / "calibration.md"
    code = cal.main(["--traces", str(traces), "--observe", str(tmp_path / "none-*.json"), "--out", str(out)])
    assert code == 0
    text = out.read_text(encoding="utf-8")
    printed = capsys.readouterr().out
    for name, value in asdict(Thresholds()).items():
        assert f"recommended: {name} = {value:g}" in printed
        assert f"`recommended: {name} = {value:g}`" in text  # мало данных — везде текущее
    assert "| `" + str(traces) + "` | 3 |" in text
    assert "## §1." in text and "## §4." in text


def test_calibration_doc_matches_the_thresholds_in_config():
    """docs/calibration.md — источник значений `Thresholds`: у каждого порога строка `recommended:` с тем же
    значением, что в config.py, и config.py ссылается на раздел документа."""
    doc = (ROOT / "docs" / "calibration.md").read_text(encoding="utf-8")
    config = (ROOT / "browser_hands" / "config.py").read_text(encoding="utf-8")
    for name, value in asdict(Thresholds()).items():
        assert re.search(rf"`recommended: {name} = {value:g}`", doc), name
        section = re.search(rf"{name}: float = [0-9.]+  # значение — docs/calibration\.md (§\d)", config)
        assert section is not None, name
        assert f"## {section[1]}." in doc
