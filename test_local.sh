#!/usr/bin/env bash
# Quick setup + test runner for local Kudan testing before ClawCloud deployment

set -euo pipefail

VENV_PATH="${1:-.venv}"
PYTHON="${VENV_PATH}/Scripts/python.exe"

echo "=== Kudan Local Test Environment Setup ==="
echo

if [ ! -d "$VENV_PATH" ]; then
  echo "[ERROR] Virtual environment not found at $VENV_PATH"
  exit 1
fi

echo "[1] Verifying Python environment..."
"$PYTHON" --version

echo
echo "[2] Running import smoke test..."
"$PYTHON" -c "
import aiohttp, fastapi, uvicorn, web3
import aiosqlite, telegram
print('✓ All core imports successful')
"

echo
echo "[3] Loading config with test .env..."
if [ ! -f ".env" ]; then
  echo "[WARN] No .env file found. Copy and edit .env.example first:"
  echo "    cp .env.example .env"
  echo "    Edit .env with your Polymarket keys, RPC, and Telegram token"
  exit 0
fi

"$PYTHON" -c "
from config import load_settings
try:
  s = load_settings()
  print('✓ Config loaded')
  print(f'  DB: {s.db_path}')
  print(f'  DRY_RUN: {s.dry_run}')
except Exception as e:
  print(f'✗ Config error: {e}')
  exit(1)
"

echo
echo "[4] Running syntax check on main modules..."
"$PYTHON" -m py_compile main.py config/settings.py data/models.py data/price_feed.py

echo
echo "[5] Test summary:"
echo "✓ Environment ready for local testing"
echo "✓ All dependencies installed"
echo "✓ Config parsing OK"
echo
echo "Next steps:"
echo "  1. Set DRY_RUN=true in .env to start in dry-run mode"
echo "  2. Run: ./$PYTHON main.py"
echo "  3. Health check: curl http://127.0.0.1:8080/health"
echo
echo "Ready to deploy to ClawCloud!"
