#!/usr/bin/env bash
# Проверки без сети и без платных вызовов: линт, формат, тесты, синтаксис snapshot.js (когда ядро влито).
set -euo pipefail
cd "$(dirname "$0")/.."

uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
if [[ -f browser_hands/snapshot.js ]]; then
  node --check browser_hands/snapshot.js
else
  echo "browser_hands/snapshot.js нет (ядро не влито) — node --check пропущен"
fi
