#!/usr/bin/env bash
set -euo pipefail

# Ubuntu/Debian protects the system Python (PEP 668).  Always use a project
# virtual environment instead of installing packages system-wide.
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

# Browser automation is optional.  Install it only when needed.
if [ "${INSTALL_PLAYWRIGHT:-1}" = "1" ]; then
    pip install playwright
    # This project uses Brave as the Chromium-based browser engine.
    if ! command -v brave-browser >/dev/null 2>&1 && ! command -v brave >/dev/null 2>&1; then
        echo "Brave browser is not installed. Install it with: sudo apt install brave-browser"
        echo "Then set BRAVE_EXECUTABLE_PATH if it is installed in a non-standard location."
    fi
fi

echo "Automation environment is ready. Activate it with: source .venv/bin/activate"