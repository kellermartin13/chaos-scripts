#!/usr/bin/env bash
# Set up the chaos.py virtualenv (Python 3.12).
#
# nfl_data_py 0.3.3 over-pins pandas<2/numpy<2 (no py3.12 wheels), so it is
# installed with --no-deps on top of the wheel-backed pins in requirements.txt.
set -euo pipefail

cd "$(dirname "$0")"

python3 -m venv .venv
.venv/bin/pip install --upgrade pip setuptools wheel
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install --no-deps nfl_data_py==0.3.3

echo
echo "Done. Run with:"
echo "  .venv/bin/python chaos.py --season 2025 --week 1"
