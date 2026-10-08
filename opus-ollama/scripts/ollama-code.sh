#!/usr/bin/env bash
# Thin wrapper: all logic lives in ollama_code.py (Python 3, stdlib only).
exec python3 "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")/ollama_code.py" "$@"
