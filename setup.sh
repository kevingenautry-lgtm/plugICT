#!/usr/bin/env bash
set -euo pipefail

echo "========================================"
echo "  PlugICT Knowledge Vault Setup"
echo "========================================"
echo ""

PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON="$cmd"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Python not found! Please install Python 3.10+ from:"
    echo "  https://www.python.org/downloads/"
    echo ""
    exit 1
fi

echo "Using Python: $($PYTHON --version)"
echo ""

$PYTHON install.py
echo ""
echo "Setup complete."
