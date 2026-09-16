#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python pc_app/app.py "$@"
fi
exec python3 pc_app/app.py "$@"
