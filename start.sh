#!/usr/bin/env bash
# Start Lighthouse on macOS or Linux:  ./start.sh
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "Setting up Lighthouse for the first time..."
    PY="$(command -v python3 || command -v python || true)"
    if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
        echo "Python 3.11+ is required. Install it from https://www.python.org/downloads/ and try again."
        exit 1
    fi
    "$PY" -m venv .venv
    .venv/bin/python -m pip install --quiet --upgrade pip
fi
.venv/bin/python -m pip install --quiet -r requirements.txt
exec .venv/bin/python -m lighthouse start "$@"
