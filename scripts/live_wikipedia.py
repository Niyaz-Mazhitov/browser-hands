"""Живая проверка ядра: headless launch с временным профилем, Wikipedia, платные вызовы Jev (доли цента).

Запуск из клона: uv run --frozen --env-file <путь к клону>/.env python scripts/live_wikipedia.py
Выход 0 только при status == done и открытой статье; скриншот — traces/live-<ts>.jpg. Не запускается из pytest.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from browser_hands.agent import Agent
from browser_hands.chrome import Chrome
from browser_hands.config import BrowserConfig, ModelConfig, RunConfig
from browser_hands.model import ModelClients

URL = "https://en.wikipedia.org/wiki/Main_Page"
GOAL = "Find and open the Wikipedia article about Gödel's incompleteness theorems."
EXPECTED = "incompleteness_theorems"
ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "Живая проверка ядра").splitlines()[0])
    parser.add_argument("--url", default=URL)
    parser.add_argument("--goal", default=GOAL)
    parser.add_argument("--expect", default=EXPECTED, help="подстрока итогового url для успеха")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--verbose", action="store_true", help="usage моделей в stderr")
    args = parser.parse_args()

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.verbose:
        logging.getLogger("browser_hands.model").setLevel(logging.DEBUG)

    jev_key = os.environ.get("BROWSER_HANDS_JEV_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
    text_key = os.environ.get("BROWSER_HANDS_TEXT_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
    if not jev_key:
        print("нет ключа: задайте OPENROUTER_API_KEY (uv run --env-file <proj>/.env ...)", file=sys.stderr)
        return 2

    profile = Path(tempfile.mkdtemp(prefix="browser-hands-live-"))
    browser = BrowserConfig(mode="launch", headless=True, launch_data_dir=profile, connect_timeout_s=30.0)
    chrome = Chrome(browser)
    clients = ModelClients(ModelConfig(jev_api_key=jev_key, text_api_key=text_key))
    try:
        started = time.perf_counter()
        chrome.connect()
        connect_ms = round((time.perf_counter() - started) * 1000)
        warm = threading.Thread(target=clients.warmup, daemon=True)  # как сервер: параллельно с new_tab
        warm.start()
        run = RunConfig(max_steps=args.max_steps, timeout_s=args.timeout)
        agent = Agent(
            chrome,
            clients,
            args.url,
            args.goal,
            run,
            screenshot_quality=browser.screenshot_quality,
            screenshot_scale=browser.screenshot_scale,
        )
        result = agent.run()
    finally:
        chrome.close()
        clients.close()
        shutil.rmtree(profile, ignore_errors=True)

    print(f"chrome launch+connect: {connect_ms} ms (не входит в elapsed)")
    for step in result.steps:
        typed = f' — "{step.text}"' if step.text is not None else ""
        t = step.timing
        cost = f" ${step.cost:.5f}" if step.cost is not None else ""
        print(
            f"{step.index}. {step.operation} {step.target!r}{typed} changed={step.page_changed} "
            f"(model {t.model_ms} / text {t.text_ms} / browser {t.browser_ms} / wait {t.wait_ms} ms){cost}"
        )
    t = result.timing
    print(f"status: {result.status}")
    print(f"url: {result.url}")
    print(f"title: {result.title}")
    print(f"steps: {len(result.steps)}, model_calls: {result.model_calls}")
    print(f"elapsed_ms: {result.elapsed_ms}")
    print(f"timing: model {t.model_ms} / text {t.text_ms} / browser {t.browser_ms} / wait {t.wait_ms} ms")
    print(f"cost: {'$%.5f' % result.cost if result.cost is not None else 'n/a'}")
    if result.error:
        print(f"error: {result.error}")
    if result.screenshot_jpeg:
        traces = ROOT / "traces"
        traces.mkdir(exist_ok=True)
        shot = traces / f"live-{time.strftime('%Y%m%d-%H%M%S')}.jpg"
        shot.write_bytes(result.screenshot_jpeg)
        print(f"screenshot: {shot} ({len(result.screenshot_jpeg)} bytes)")
    ok = result.status == "done" and args.expect in result.url
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
