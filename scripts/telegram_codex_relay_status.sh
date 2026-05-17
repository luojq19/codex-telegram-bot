#!/usr/bin/env bash
set -euo pipefail

pgrep -af "telegram_codex_relay.py" || true
