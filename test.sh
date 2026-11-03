#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt; }
exec .venv/bin/python -m pytest "$@"
