#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "[INFO] Starting KokoroGUI..."

VENV_DIR=".venv"

# KokoroGUI needs Python 3.11 or 3.12: kokoro==0.9.4 requires Python >=3.10,<3.13,
# and pyproject.toml sets the floor at 3.11, so 3.13+ is not supported yet.
version_in_range() {
    "$1" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 12) else 1)' >/dev/null 2>&1
}

find_python() {
    local candidate
    for candidate in python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && version_in_range "$candidate"; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

if [ ! -d "$VENV_DIR" ]; then
    echo "[INFO] Creating virtual environment..."
    if ! PYTHON_BIN="$(find_python)"; then
        echo "[ERROR] No suitable Python interpreter found. KokoroGUI requires Python 3.11 or 3.12 (kokoro==0.9.4 caps the ceiling below 3.13)."
        if command -v python3 >/dev/null 2>&1; then
            echo "[ERROR] Found on PATH: python3 $(python3 -c 'import platform; print(platform.python_version())')"
        fi
        echo "[ERROR] Install Python 3.11 or 3.12 and re-run this script."
        exit 1
    fi
    echo "[INFO] Using $PYTHON_BIN ($("$PYTHON_BIN" -c 'import platform; print(platform.python_version())'))."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
else
    echo "[INFO] Virtual environment already exists, skipping creation."
fi

VENV_PYTHON="$VENV_DIR/bin/python"

# Install/Update Requirements
if [ -f "requirements.txt" ]; then
    echo "[INFO] Checking requirements..."
    "$VENV_PYTHON" -m pip install -r requirements.txt --quiet
fi

# The GUI needs a Tk-enabled Python. Homebrew Python on macOS and several Linux
# distributions ship without it, which fails with "No module named '_tkinter'".
if ! "$VENV_PYTHON" -c "import tkinter" >/dev/null 2>&1; then
    VENV_VERSION="$("$VENV_PYTHON" -c 'import platform; print(platform.python_version())')"
    VENV_MINOR="$("$VENV_PYTHON" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
    echo "[ERROR] tkinter is not available for the virtual environment's Python ($VENV_VERSION)."
    echo "[ERROR] The GUI cannot start without it."
    case "$(uname -s)" in
        Darwin)
            echo "[ERROR] Fix: brew install python-tk@${VENV_MINOR}"
            ;;
        Linux)
            echo "[ERROR] Fix: sudo apt install python3-tk"
            ;;
        *)
            echo "[ERROR] Install the tkinter package for your Python distribution."
            ;;
    esac
    exit 1
fi

# Start Application
echo "[INFO] Launching GUI..."
exec "$VENV_PYTHON" main.py
