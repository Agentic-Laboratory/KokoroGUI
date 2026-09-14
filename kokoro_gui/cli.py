"""Headless Kokoro synthesis for scripts and automation.

This module intentionally targets Kokoro only. The backend protocol exposes
metadata and capabilities, but does not yet define a common generation API.
"""
import argparse
import asyncio
import contextlib
import json
import re
import sys
import time
from pathlib import Path

KokoroEngine = None


LANGUAGE_ALIASES = {
    "a": "a", "en-us": "a", "american-english": "a",
    "b": "b", "en-gb": "b", "british-english": "b",
    "e": "e", "es": "e", "spanish": "e",
    "f": "f", "fr": "f", "french": "f",
    "i": "i", "it": "i", "italian": "i",
    "p": "p", "pt": "p", "portuguese": "p",
    "j": "j", "ja": "j", "japanese": "j",
    "z": "z", "zh": "z", "chinese": "z",
}

DEFAULT_CONFIG = {
    "lang_code": "a",
    "voice": "af_heart",
    "speed": 1.0,
    "split_pattern": r"\n+",
    "filename": "output",
    "format": "wav",
    "out_dir": "audio_output",
    "separate": True,
    "combine": True,
    "export_subtitles": False,
    "caching": False,
    "num_threads": 1,
    "volume": 1.0,
    "pitch": 0.0,
    "normalize": False,
    "trim_silence": False,
    "apply_fx": True,
    "lexicon": {},
}

CONFIG_KEYS = frozenset(DEFAULT_CONFIG) | {"device", "time_id"}
BOOLEAN_CONFIG_KEYS = frozenset({
    "separate", "combine", "export_subtitles", "caching", "normalize",
    "trim_silence", "apply_fx",
})


def _engine_class():
    global KokoroEngine
    if KokoroEngine is None:
        from kokoro_engine import KokoroEngine as engine_class

        KokoroEngine = engine_class
    return KokoroEngine


def _boolean_argument(parser, flag, destination, help_text):
    parser.add_argument(
        flag,
        dest=destination,
        action=argparse.BooleanOptionalAction,
        default=None,
        help=help_text,
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m kokoro_gui.cli",
        description="Generate Kokoro TTS audio without starting the GUI.",
    )
    subparsers = parser.add_subparsers(dest="command")
    synthesize = subparsers.add_parser("synthesize", help="Generate audio from text, standard input, or a document.")
    source = synthesize.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="Text to synthesize.")
    source.add_argument("--input", type=Path, help="TXT, PDF, or EPUB input file.")
    source.add_argument("--stdin", action="store_true", help="Read text from standard input.")
    synthesize.add_argument("--config", type=Path, help="JSON synthesis configuration. Explicit flags take precedence.")
    synthesize.add_argument("--preset", help="Base speaker preset from presets/<name>.json.")
    synthesize.add_argument("--fx-preset", help="Base FX preset from presets/fx/<name>.json.")
    synthesize.add_argument("--language", dest="lang_code", help="Language code or alias, such as a, en-us, or ja.")
    synthesize.add_argument("--voice", help="Kokoro or custom voice name.")
    synthesize.add_argument("--speed", type=float, help="Speech speed multiplier.")
    synthesize.add_argument("--device", choices=["auto", "cpu", "cuda"], help="Inference device. Defaults to auto.")
    synthesize.add_argument("--threads", dest="num_threads", type=int, help="Parallel generation workers, from 1 to 32.")
    synthesize.add_argument("--split-pattern", help="Regular expression passed to Kokoro for segment splitting.")
    synthesize.add_argument("--name", dest="filename", help="Output filename prefix.")
    synthesize.add_argument("--out-dir", type=Path, help="Directory for generated files.")
    synthesize.add_argument("--format", choices=["wav", "flac", "mp3", "ogg"], help="Audio container format.")
    synthesize.add_argument("--time-id", help="Output timestamp suffix. Defaults to the current local time.")
    synthesize.add_argument(
        "--replace",
        action="append",
        metavar="SOURCE=REPLACEMENT",
        help="Case-insensitive lexicon replacement. Repeat for multiple rules.",
    )
    _boolean_argument(synthesize, "--combine", "combine", "Write a combined output file.")
    _boolean_argument(synthesize, "--separate", "separate", "Keep generated part files after combining.")
    _boolean_argument(synthesize, "--subtitles", "export_subtitles", "Write an SRT subtitle file.")
    _boolean_argument(synthesize, "--cache", "caching", "Read and write raw segment cache files.")
    _boolean_argument(synthesize, "--normalize", "normalize", "Normalize output peak level.")
    _boolean_argument(synthesize, "--trim-silence", "trim_silence", "Trim leading and trailing silence.")
    _boolean_argument(synthesize, "--apply-fx", "apply_fx", "Enable the Pedalboard effects chain.")
    return parser


def _normalise_language(value):
    try:
        return LANGUAGE_ALIASES[value.lower()]
    except (AttributeError, KeyError):
        valid = ", ".join(sorted(LANGUAGE_ALIASES))
        raise ValueError(f"Unknown language '{value}'. Use one of: {valid}.") from None


def _validate_file_component(value, option):
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"{option} must contain only letters, numbers, underscores, and hyphens.")
    return value


def _read_config(path):
    from kokoro_gui.engine.presets import ALLOWED_FX_PRESET_KEYS

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"Cannot read config {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("The config file must contain a JSON object.")
    if "trim" in data and "trim_silence" not in data:
        data["trim_silence"] = data["trim"]
    unsupported = sorted(set(data) - CONFIG_KEYS - ALLOWED_FX_PRESET_KEYS - {"trim"})
    if unsupported:
        raise ValueError(f"Unsupported config keys: {', '.join(unsupported)}.")
    allowed = CONFIG_KEYS | ALLOWED_FX_PRESET_KEYS
    return {key: value for key, value in data.items() if key in allowed}


def _read_text(args, engine):
    if args.text is not None:
        return args.text
    if args.stdin:
        return sys.stdin.read()
    if not args.input.is_file():
        raise ValueError(f"Input file does not exist: {args.input}")
    return engine.extract_text_from_file(str(args.input))


def _parse_replacements(replacements):
    parsed = {}
    for replacement in replacements or []:
        if "=" not in replacement:
            raise ValueError(f"Invalid --replace value '{replacement}'. Use SOURCE=REPLACEMENT.")
        source, target = replacement.split("=", 1)
        if not source:
            raise ValueError("The source side of --replace cannot be empty.")
        parsed[source] = target
    return parsed


def _load_preset(engine, name, fx=False):
    from kokoro_gui.engine.presets import (
        ALLOWED_FX_PRESET_KEYS,
        ALLOWED_PRESET_KEYS,
        filter_allowed_keys,
    )

    preset = engine.load_fx_preset(name) if fx else engine.load_preset(name)
    if preset is None:
        directory = "presets/fx" if fx else "presets"
        raise ValueError(f"Preset not found: {directory}/{name}.json")
    if not isinstance(preset, dict):
        raise ValueError(f"Preset must contain a JSON object: {name}")
    allowed = ALLOWED_FX_PRESET_KEYS if fx else ALLOWED_PRESET_KEYS
    return filter_allowed_keys(preset, allowed)


def _validate_config(config):
    from kokoro_gui.engine.presets import ALLOWED_FX_PRESET_KEYS

    boolean_keys = BOOLEAN_CONFIG_KEYS | {key for key in ALLOWED_FX_PRESET_KEYS if key.endswith("_enabled")}
    for key in boolean_keys:
        if key in config and not isinstance(config[key], bool):
            raise ValueError(f"{key} must be a boolean.")
    config["lang_code"] = _normalise_language(config["lang_code"])
    if not isinstance(config["voice"], str) or not config["voice"]:
        raise ValueError("voice must be a non-empty string.")
    try:
        config["speed"] = float(config["speed"])
    except (TypeError, ValueError):
        raise ValueError("speed must be a number.") from None
    if config["speed"] <= 0:
        raise ValueError("speed must be greater than zero.")
    if isinstance(config["num_threads"], bool) or not isinstance(config["num_threads"], int) or not 1 <= config["num_threads"] <= 32:
        raise ValueError("threads must be between 1 and 32.")
    if config["format"] not in {"wav", "flac", "mp3", "ogg"}:
        raise ValueError("format must be one of: wav, flac, mp3, ogg.")
    if not isinstance(config["split_pattern"], str):
        raise ValueError("split_pattern must be a string.")
    try:
        re.compile(config["split_pattern"])
    except re.error as error:
        raise ValueError(f"Invalid split_pattern: {error}") from error
    if not isinstance(config["out_dir"], str) or not config["out_dir"]:
        raise ValueError("out_dir must be a non-empty path.")
    if not isinstance(config["lexicon"], dict):
        raise ValueError("lexicon must be an object.")
    if config["device"] not in {"auto", "cpu", "cuda"}:
        raise ValueError("device must be one of: auto, cpu, cuda.")
    config["filename"] = _validate_file_component(str(config["filename"]), "--name")
    config["time_id"] = _validate_file_component(str(config["time_id"]), "--time-id")
    return config


def _build_config(args, engine):
    config = {**DEFAULT_CONFIG, "device": "auto", "time_id": time.strftime("%Y%m%d%H%M%S")}
    if args.config is not None:
        config.update(_read_config(args.config))
    if args.preset:
        preset = _load_preset(engine, args.preset)
        config.update(preset)
        if "trim" in preset:
            config["trim_silence"] = preset["trim"]
        if preset.get("fx_preset"):
            config.update(_load_preset(engine, preset["fx_preset"], fx=True))
    if args.fx_preset:
        config.update(_load_preset(engine, args.fx_preset, fx=True))
        config["apply_fx"] = True
    for key in CONFIG_KEYS:
        value = getattr(args, key, None)
        if value is not None:
            config[key] = str(value) if key == "out_dir" else value
    replacements = _parse_replacements(args.replace)
    if replacements:
        config["lexicon"] = {**config["lexicon"], **replacements}
    return _validate_config(config)


def _status(message, is_error):
    prefix = "error" if is_error else "status"
    print(f"{prefix}: {message}", file=sys.stderr)


def _output_path(config):
    return Path(config["out_dir"]) / f"{config['filename']}_{config['time_id']}_combined.{config['format']}"


async def _run_synthesis(args):
    engine = _engine_class()()
    errors = []
    engine.on_status = lambda message, is_error: (
        errors.append(message) if is_error else None,
        _status(message, is_error),
    )
    try:
        with contextlib.redirect_stdout(sys.stderr):
            config = _build_config(args, engine)
            text = _read_text(args, engine).strip()
            if not text:
                raise ValueError("Input text is empty.")
            expected_output = _output_path(config)
            output_dir = Path(config["out_dir"])
            existing_parts = tuple(output_dir.glob(f"{config['filename']}_{config['time_id']}_part*.{config['format']}"))
            subtitle_path = output_dir / f"{config['filename']}_{config['time_id']}_combined.srt"
            if expected_output.exists() or existing_parts or (config["export_subtitles"] and subtitle_path.exists()):
                raise ValueError(f"Output already exists: {expected_output}")
            if not await engine.init_pipeline_async(config["lang_code"], device=config["device"]):
                return 1
            config["voice"] = engine.resolve_voice_path(config["voice"])
            await engine._process_text_async(text, config)
        if errors or engine.cancel_event.is_set():
            return 1
        if config["combine"]:
            if not expected_output.is_file():
                _status(f"Expected output was not created: {expected_output}", True)
                return 1
            print(expected_output)
            return 0
        generated_parts = Path(config["out_dir"]).glob(
            f"{config['filename']}_{config['time_id']}_part*.{config['format']}"
        )
        if not any(generated_parts):
            _status("No output parts were created.", True)
            return 1
        print(config["out_dir"])
        return 0
    except ValueError as error:
        _status(str(error), True)
        return 2
    except Exception as error:
        _status(f"Synthesis failed: {error}", True)
        return 1
    finally:
        engine.worker.stop()


def main(argv=None):
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in {"synthesize", "-h", "--help"}:
        argv.insert(0, "synthesize")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    return asyncio.run(_run_synthesis(args))


if __name__ == "__main__":
    raise SystemExit(main())
