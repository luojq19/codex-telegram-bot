#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
exec "${PYTHON:-python3}" src/telegram_codex_relay.py
