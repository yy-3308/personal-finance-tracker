#!/bin/bash

# ── myfinance startup script ──
# Just run: ./start.sh

set -e

PORT=5001
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo ""
echo "  ┌─────────────────────────────┐"
echo "  │   myfinance — starting up   │"
echo "  └─────────────────────────────┘"
echo ""

# Check for Python
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo "  ✗ Python is not installed."
    echo ""
    echo "  Install it from: https://www.python.org/downloads/"
    echo ""
    exit 1
fi

echo "  ✓ Found $($PY --version)"

# Create virtual environment if needed
if [ ! -d "venv" ]; then
    echo "  → Setting up for first time (this takes ~30 seconds)..."
    $PY -m venv venv
    echo "  ✓ Created virtual environment"
fi

# Activate
source venv/bin/activate

# Install dependencies if needed
if [ ! -f "venv/.deps_installed" ]; then
    echo "  → Installing dependencies..."
    pip install -q -r requirements.txt
    pip install -q openpyxl
    touch venv/.deps_installed
    echo "  ✓ Dependencies installed"
fi

# Create import folder
mkdir -p ~/Downloads/spend_tracker

echo "  ✓ Import folder ready: ~/Downloads/spend_tracker/"
echo ""
echo "  Starting server on http://localhost:$PORT"
echo "  Press Ctrl+C to stop"
echo ""

# Open browser after a short delay
(sleep 1.5 && open "http://localhost:$PORT" 2>/dev/null || xdg-open "http://localhost:$PORT" 2>/dev/null || true) &

# Run the app
$PY app.py
