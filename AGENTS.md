# KokoroGUI Agent Guide

## Start Here

Run commands from the repository root. Install the project into a Python 3.11 or 3.12 virtual environment: `kokoro==0.9.4` requires Python `>=3.10,<3.13`, and `pyproject.toml` sets the floor at 3.11, so 3.13 and newer are not supported yet. Plain `python`/`python3` may resolve to an unsupported version depending on the platform, so name the interpreter explicitly.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install -r requirements-test.txt
```

On Windows, create the virtual environment with `py -3.12 -m venv .venv` (the `py` launcher selects the version) and replace `.venv/bin/python` with `.venv\Scripts\python` in the commands above.

The GUI needs a Tk-enabled Python. Homebrew Python on macOS ships without it (`brew install python-tk@3.12`, matching the interpreter version above), and several Linux distributions split it into a separate package (`sudo apt install python3-tk`). The CLI does not need Tk.

Use `kokoro-tts --help` or `.venv/bin/python -m kokoro_cli --help` to discover the CLI. The full reference is in [docs/cli.md](docs/cli.md).

## Commands

```bash
# Inspect available voices and the selected inference device.
kokoro-tts voices --language en-us
kokoro-tts devices

# Generate audio without starting the Tk GUI.
kokoro-tts --text "Hello from Kokoro." --voice af_heart --out-dir audio_output

# Run the mocked fast suite.
pytest
```

The default test suite mocks the Kokoro model. Real synthesis is opt-in, downloads model weights on first use, and writes audio to `tests/output/`:

```bash
pytest -m integration tests/integration -s
```

On headless Linux, GUI tests need a display:

```bash
xvfb-run -a pytest
```

## Project Boundaries

- `kokoro_cli.py` is the non-GUI command-line adapter. Keep its parser, `--help`, and `docs/cli.md` aligned.
- `gui.py` owns Tk state and UI-only settings. Do not import it from the CLI.
- `kokoro_engine.py` owns synthesis, file extraction, caching, presets, voice mixing, and DSP processing.
- `playback.py` owns optional audio playback. Standard CLI synthesis must work without an audio device.
- Tests use mocked pipelines by default. Add CLI behavior tests without requiring model downloads or real playback.

## Working Rules

- Preserve the GUI and CLI as separate front ends over `KokoroEngine`.
- Keep CLI output machine-readable: generated paths go to stdout; progress and errors go to stderr.
- Add tests and update `docs/cli.md` when a public CLI command, option, default, or output convention changes.
- Do not change `config.json` unless the task explicitly concerns saved GUI settings. The CLI reads a config only when `--config` is supplied and never writes one.
- Do not commit generated `cache/`, `custom_voices/`, `audio_output/`, `tests/output/`, virtual environments, or downloaded model artifacts.
- Use `requirements-rocm.txt` only for Linux hosts with ROCm installed. Standard `pip install -e .` is for CPU, CUDA, Windows, and macOS environments.
