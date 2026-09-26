import importlib.util
import json
from pathlib import Path

import pytest

from browser_hands.types import RunResult, Status
from tests.fakes import FakeCore, make_result

BENCH = Path(__file__).resolve().parent.parent / "scripts" / "bench.py"


def load_bench():
    spec = importlib.util.spec_from_file_location("bench", BENCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = load_bench()


def result_with(elapsed_ms: int, model_ms: int, cost: float | None = 0.001, status: Status = "done") -> RunResult:
    result = make_result(status, steps=1, cost=cost)
    result.elapsed_ms = elapsed_ms
    result.timing.model_ms = model_ms
    return result


@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [
        ([5, 1, 4, 2, 3], 95, 5),  # 5 значений: p95 = максимум
        ([5, 1, 4, 2, 3], 50, 3),
        (list(range(1, 21)), 95, 19),
        (list(range(1, 101)), 95, 95),
        ([7], 95, 7),
    ],
)
def test_percentile_nearest_rank(values, p, expected):
    assert bench.percentile(values, p) == expected


def test_percentile_needs_values():
    with pytest.raises(ValueError):
        bench.percentile([], 95)


def test_summarize_median_p95_and_cost():
    results = [
        result_with(3000, 2000),
        result_with(5000, 4000, status="blocked"),
        result_with(4000, 3000, cost=None),
        result_with(3500, 2500),
    ]

    summary = bench.summarize(results)

    assert summary["runs"] == 4 and summary["done"] == 3
    assert summary["stats"]["elapsed_ms"] == {"median": 3750, "p95": 5000}
    assert summary["stats"]["model_ms"] == {"median": 2750, "p95": 4000}
    assert summary["cost_total"] == pytest.approx(0.003)
    assert summary["cost_runs"] == 3
    assert "done 3/4" in bench.format_summary(summary)


def test_summarize_without_cost():
    assert bench.summarize([result_with(1, 1, cost=None)])["cost_total"] is None


def test_bench_reuses_one_chrome_and_writes_jsonl(tmp_path, capsys):
    core = FakeCore()
    argv = ["--runs", "3", "--url", "https://a.test", "--goal", "g", "--out-dir", str(tmp_path)]

    code = bench.main(argv, env={"OPENROUTER_API_KEY": "k"}, factories=core.factories())

    assert code == 0
    assert len(core.chromes) == 1 and core.chromes[0].connect_calls == 3  # тёплый Chrome на все прогоны
    assert len(core.clients) == 1 and len(core.agents) == 3
    (trace,) = tmp_path.glob("bench-*.jsonl")
    lines = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [line.get("run") for line in lines[:3]] == [1, 2, 3]
    assert "screenshot_jpeg" not in lines[0]
    assert lines[3]["summary"]["runs"] == 3
    assert "median" in capsys.readouterr().out
