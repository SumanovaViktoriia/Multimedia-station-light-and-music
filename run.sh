#!/bin/sh
# Запуск на Linux. Первый раз сам ставит библиотеки в .venv
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt || exit 1
fi
exec .venv/bin/python summary.py "$@"
