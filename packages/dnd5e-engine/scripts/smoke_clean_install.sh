#!/usr/bin/env bash
# Compatibility entrypoint; share the portable runner used by root/package Makefiles.
set -euo pipefail
ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec uv run --no-project python "$ENGINE_DIR/../../tools/smoke_clean_install.py"
