"""Filesystem locations for the application's own state.

Every one of these is anchored to the directory holding the source files, not
to the working directory. A Finder/Dock launch on macOS gives CWD="/", which is
read-only, so creating cache/, presets/ or custom_voices/ there failed outright;
launched from any other writable directory, the app quietly scattered a second
set of them and stopped persisting settings.

This module deliberately imports nothing beyond `os`. `kokoro_cli` resolves
paths through it, and pulling in `kokoro_engine` (and therefore torch) would
add ~3s to listing commands such as `kokoro-tts voices`.

Consumers re-export these names and read their own module-level copy, so tests
patch the module whose code does the reading - see tests/conftest.py. Patching
`paths` itself has no effect, because `from paths import X` binds at import.
"""
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))

CUSTOM_VOICES_DIR = os.path.join(APP_DIR, "custom_voices")
CACHE_DIR = os.path.join(APP_DIR, "cache")
PRESETS_DIR = os.path.join(APP_DIR, "presets")
FX_PRESETS_DIR = os.path.join(PRESETS_DIR, "fx")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
