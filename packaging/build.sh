#!/usr/bin/env bash
# The one PyInstaller command, used by release.yml and for local builds, so the two never drift.
# Usage: packaging/build.sh [--onedir|--onefile]   (default --onedir)
set -euo pipefail
cd "$(dirname "$0")/.."
uv run pyinstaller --noconfirm --windowed "${1:---onedir}" --name StockBars --paths . \
  --add-data "app/sp500.csv:app" --collect-submodules keyring.backends \
  --additional-hooks-dir packaging/hooks app/main.py
