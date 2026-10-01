#!/usr/bin/env sh
# HZ Bot - uruchomienie panelu (Linux/macOS). Przy pierwszym starcie instaluje wszystko automatycznie.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Pierwsze uruchomienie - instaluję zależności, to potrwa chwilę..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -e ".[capture]"
  .venv/bin/python -m playwright install chromium
fi
exec .venv/bin/python -m hzbot "$@"
