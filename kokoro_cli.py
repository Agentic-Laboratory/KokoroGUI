"""Command-line interface for KokoroGUI synthesis and voice management."""

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

KokoroEngine = None
get_inference_device = None
playback = None


LANGUAGES = {
    "a": "American English",
    "b": "British English",
    "e": "Spanish",
    "f": "French",
    "i": "Italian",
    "p": "Portuguese",
    "j": "Japanese",
    "z": "Chinese",
}

LANGUAGE_ALIASES = {
    "a": "a",
    "en-us": "a",
    "american-english": "a",
    "b": "b",
    "en-gb": "b",
    "british-english": "b",
    "e": "e",
    "es": "e",
    "spanish": "e",
    "f": "f",
    "fr": "f",
    "french": "f",
    "i": "i",
    "it": "i",
    "italian": "i",
    "p": "p",
    "pt": "p",
    "portuguese": "p",
    "j": "j",
    "ja": "j",
    "japanese": "j",
    "z": "z",
    "zh": "z",
    "chinese": "z",
}

VOICE_DB = {
    "a": ["af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa"],
    "b": ["bf_alice", "bf_emma", "bf_isabella", "bf_lily", "bm_daniel", "bm_fable", "bm_george", "bm_lewis"],
    "e": ["ef_dora", "em_alex", "em_santa"],
    "f": ["ff_siwis"],
    "i": ["if_sara", "im_nicola"],
    "p": ["pf_dora", "pm_alex"],
    "j": ["jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro"],
    "z": ["zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zm_yunjian"],
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
    "jit_enabled": False,
    "caching": True,
    "num_threads": 1,
    "volume": 1.0,
    "pitch": 0.0,
    "normalize": False,
    "trim_silence": False,
    "apply_fx": True,
    "lexicon": {},
    "reverb_enabled": False,
    "reverb_room_size": 0.5,
    "reverb_wet_level": 0.3,
    "reverb_damping": 0.5,
    "reverb_dry_level": 1.0,
    "reverb_width": 1.0,
    "eq_bass": 0.0,
    "eq_treble": 0.0,
    "comp_enabled": False,
    "comp_threshold": -20.0,
    "comp_ratio": 4.0,
    "comp_attack": 1.0,
    "comp_release": 100.0,
    "distortion_enabled": False,
    "distortion_drive": 25.0,
    "chorus_enabled": False,
    "chorus_rate": 1.0,
    "chorus_depth": 0.25,
    "chorus_mix": 0.5,
    "phaser_enabled": False,
    "phaser_rate": 1.0,
    "phaser_depth": 0.5,
    "phaser_mix": 0.5,
    "clipping_enabled": False,
    "clipping_thresh": -6.0,
    "bitcrush_enabled": False,
    "bitcrush_depth": 8.0,
    "gsm_enabled": False,
    "highpass_enabled": False,
    "highpass_freq": 50.0,
    "lowpass_enabled": False,
    "lowpass_freq": 10000.0,
    "delay_enabled": False,
    "delay_time": 0.5,
    "delay_feedback": 0.0,
    "delay_mix": 0.5,
    "pitch_shift_enabled": False,
    "pitch_shift_semitones": 0.0,
    "limiter_enabled": False,
    "limiter_threshold": -1.0,
    "limiter_release": 100.0,
    "gain_enabled": False,
    "gain_db": 0.0,
}


def _engine_class():
    global KokoroEngine
    if KokoroEngine is None:
        from kokoro_engine import KokoroEngine as engine_class
        KokoroEngine = engine_class
    return KokoroEngine


def _device_selector():
    global get_inference_device
    if get_inference_device is None:
        from kokoro_engine import get_inference_device as device_selector
        get_inference_device = device_selector
    return get_inference_device


def _playback_module():
    global playback
    if playback is None:
        import playback as playback_module
        playback = playback_module
    return playback


def _boolean_argument(parser, flag, destination, help_text):
    parser.add_argument(flag, dest=destination, action=argparse.BooleanOptionalAction, default=None, help=help_text)


def _add_synthesis_arguments(parser, preview=False):
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text", help="Text to synthesize.")
    source.add_argument("--input", type=Path, help="TXT, PDF, or EPUB input file.")
    source.add_argument("--stdin", action="store_true", help="Read text from standard input.")
    parser.add_argument("--config", type=Path, help="JSON synthesis config. Explicit flags take precedence.")
    parser.add_argument("--preset", help="Base preset from presets/<name>.json.")
    parser.add_argument("--fx-preset", help="Base FX preset from presets/fx/<name>.json.")
    parser.add_argument("--language", dest="lang_code", help="Language code or alias, such as a, en-us, or ja.")
    parser.add_argument("--voice", help="Kokoro or custom voice name.")
    parser.add_argument("--speed", type=float, help="Speech speed multiplier.")
    parser.add_argument("--volume", type=float, help="Output gain multiplier.")
    parser.add_argument("--pitch", type=float, help="Pitch adjustment in semitones. This changes duration.")
    parser.add_argument("--threads", dest="num_threads", type=int, help="Parallel generation workers, from 1 to 16.")
    parser.add_argument("--split-pattern", help="Regular expression passed to Kokoro for segment splitting.")
    parser.add_argument("--name", dest="filename", help="Output filename prefix.")
    parser.add_argument("--out-dir", type=Path, help="Directory for generated files.")
    parser.add_argument("--format", choices=["wav", "flac", "mp3", "ogg"], help="Audio container format.")
    parser.add_argument("--time-id", help="Output timestamp suffix. Defaults to the current local time.")
    parser.add_argument("--replace", action="append", metavar="SOURCE=REPLACEMENT", help="Case-insensitive lexicon replacement. Repeat for multiple rules.")
    _boolean_argument(parser, "--combine", "combine", "Write a combined output file.")
    _boolean_argument(parser, "--separate", "separate", "Keep generated part files after combining.")
    _boolean_argument(parser, "--subtitles", "export_subtitles", "Write an SRT subtitle file.")
    _boolean_argument(parser, "--cache", "caching", "Read and write raw segment cache files.")
    _boolean_argument(parser, "--normalize", "normalize", "Normalize output peak level.")
    _boolean_argument(parser, "--trim-silence", "trim_silence", "Trim leading and trailing silence.")
    _boolean_argument(parser, "--apply-fx", "apply_fx", "Enable the Pedalboard effects chain.")
    if not preview:
        _boolean_argument(parser, "--jit", "jit", "Generate and play audio incrementally. Output is always WAV.")
    parser.add_argument("--reverb-room-size", type=float)
    parser.add_argument("--reverb-wet-level", type=float)
    parser.add_argument("--reverb-damping", type=float)
    parser.add_argument("--reverb-dry-level", type=float)
    parser.add_argument("--reverb-width", type=float)
    _boolean_argument(parser, "--reverb", "reverb_enabled", "Enable reverb.")
    parser.add_argument("--eq-bass", type=float, help="Low shelf gain in dB.")
    parser.add_argument("--eq-treble", type=float, help="High shelf gain in dB.")
    _boolean_argument(parser, "--compressor", "comp_enabled", "Enable compressor.")
    parser.add_argument("--comp-threshold", type=float)
    parser.add_argument("--comp-ratio", type=float)
    parser.add_argument("--comp-attack", type=float)
    parser.add_argument("--comp-release", type=float)
    _boolean_argument(parser, "--distortion", "distortion_enabled", "Enable distortion.")
    parser.add_argument("--distortion-drive", type=float)
    _boolean_argument(parser, "--chorus", "chorus_enabled", "Enable chorus.")
    parser.add_argument("--chorus-rate", type=float)
    parser.add_argument("--chorus-depth", type=float)
    parser.add_argument("--chorus-mix", type=float)
    _boolean_argument(parser, "--phaser", "phaser_enabled", "Enable phaser.")
    parser.add_argument("--phaser-rate", type=float)
    parser.add_argument("--phaser-depth", type=float)
    parser.add_argument("--phaser-mix", type=float)
    _boolean_argument(parser, "--clipping", "clipping_enabled", "Enable clipping.")
    parser.add_argument("--clipping-thresh", type=float)
    _boolean_argument(parser, "--bitcrush", "bitcrush_enabled", "Enable bitcrushing.")
    parser.add_argument("--bitcrush-depth", type=float)
    _boolean_argument(parser, "--gsm", "gsm_enabled", "Enable GSM full-rate compression.")
    _boolean_argument(parser, "--highpass", "highpass_enabled", "Enable high-pass filtering.")
    parser.add_argument("--highpass-freq", type=float)
    _boolean_argument(parser, "--lowpass", "lowpass_enabled", "Enable low-pass filtering.")
    parser.add_argument("--lowpass-freq", type=float)
    _boolean_argument(parser, "--delay", "delay_enabled", "Enable delay.")
    parser.add_argument("--delay-time", type=float)
    parser.add_argument("--delay-feedback", type=float)
    parser.add_argument("--delay-mix", type=float)
    _boolean_argument(parser, "--pitch-shift", "pitch_shift_enabled", "Enable Pedalboard pitch shift without changing duration.")
    parser.add_argument("--pitch-shift-semitones", type=float)
    _boolean_argument(parser, "--limiter", "limiter_enabled", "Enable limiter.")
    parser.add_argument("--limiter-threshold", type=float)
    parser.add_argument("--limiter-release", type=float)
    _boolean_argument(parser, "--gain", "gain_enabled", "Enable gain effect.")
    parser.add_argument("--gain-db", type=float)
    if preview:
        parser.add_argument("--output", type=Path, required=True, help="WAV file to create.")
        parser.add_argument("--play", action="store_true", help="Play the generated preview after writing it.")


def build_parser():
    parser = argparse.ArgumentParser(prog="kokoro-tts", description="Generate Kokoro TTS audio without starting the GUI.")
    subparsers = parser.add_subparsers(dest="command")
    synthesize = subparsers.add_parser("synthesize", help="Generate audio from text, stdin, or a document.")
    _add_synthesis_arguments(synthesize)
    preview = subparsers.add_parser("preview", help="Generate a short WAV preview.")
    _add_synthesis_arguments(preview, preview=True)
    voices = subparsers.add_parser("voices", help="List available standard and custom voices.")
    voices.add_argument("--language", help="Limit the output to one language code or alias.")
    voices.add_argument("--json", action="store_true", help="Print JSON for scripts.")
    devices = subparsers.add_parser("devices", help="Show the automatically selected inference device.")
    devices.add_argument("--json", action="store_true", help="Print JSON for scripts.")
    mix = subparsers.add_parser("mix", help="Create a custom voice by combining two voices.")
    mix.add_argument("--voice-a", required=True)
    mix.add_argument("--voice-b", required=True)
    mix.add_argument("--name", required=True, help="Custom voice filename without .pt.")
    mix.add_argument("--ratio", type=float, default=0.5, help="Influence of voice B, from 0.0 to 1.0.")
    mix.add_argument("--operation", choices=["mix", "add", "subtract", "multiply", "divide"], default="mix")
    mix.add_argument("--language", default="a", help="Pipeline language code or alias used to load voices.")
    config = subparsers.add_parser("config", help="Print the complete default synthesis configuration as JSON.")
    config.add_argument("--print-default", action="store_true", help="Required to print the configuration.")
    return parser


def _normalise_language(value):
    try:
        return LANGUAGE_ALIASES[value.lower()]
    except (AttributeError, KeyError):
        valid = ", ".join(sorted(LANGUAGE_ALIASES))
        raise ValueError(f"Unknown language '{value}'. Use one of: {valid}.")


def _read_text(args, engine):
    if args.text is not None:
        return args.text
    if args.stdin:
        return sys.stdin.read()
    if args.input is not None:
        if not args.input.is_file():
            raise ValueError(f"Input file does not exist: {args.input}")
        return engine.extract_text_from_file(str(args.input))
    raise ValueError("Provide one input source: --text, --input, or --stdin.")


def _read_config(path):
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
    allowed = set(DEFAULT_CONFIG) | {"jit_enabled"}
    return {key: value for key, value in data.items() if key in allowed}


def _load_preset(engine, name, fx=False):
    preset = engine.load_fx_preset(name) if fx else engine.load_preset(name)
    if preset is None:
        directory = "presets/fx" if fx else "presets"
        raise ValueError(f"Preset not found: {directory}/{name}.json")
    if "trim" in preset and "trim_silence" not in preset:
        preset["trim_silence"] = preset["trim"]
    return preset


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


def _validate_file_component(value, option):
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"{option} must contain only letters, numbers, underscores, and hyphens.")
    return value


def _build_config(args, engine):
    config = DEFAULT_CONFIG.copy()
    if args.config is not None:
        config.update(_read_config(args.config))
    if args.preset:
        config.update(_load_preset(engine, args.preset))
    if args.fx_preset:
        fx_preset = _load_preset(engine, args.fx_preset, fx=True)
        config.update(fx_preset)
        config["apply_fx"] = True
    values = vars(args)
    for key in DEFAULT_CONFIG:
        value = values.get(key)
        if value is not None:
            config[key] = str(value) if key == "out_dir" else value
    if hasattr(args, "jit") and args.jit is not None:
        config["jit_enabled"] = args.jit
    if args.lang_code is not None:
        config["lang_code"] = _normalise_language(args.lang_code)
    else:
        config["lang_code"] = _normalise_language(config["lang_code"])
    replacements = _parse_replacements(args.replace)
    if replacements:
        config["lexicon"] = {**config.get("lexicon", {}), **replacements}
    if not isinstance(config.get("lexicon"), dict):
        raise ValueError("The config field 'lexicon' must be an object.")
    if config["num_threads"] < 1 or config["num_threads"] > 16:
        raise ValueError("--threads must be between 1 and 16.")
    if config["format"] not in {"wav", "flac", "mp3", "ogg"}:
        raise ValueError("--format must be one of: wav, flac, mp3, ogg.")
    config["filename"] = _validate_file_component(str(config["filename"]), "--name")
    return config


def _status(message, is_error):
    stream = sys.stderr
    prefix = "error" if is_error else "status"
    print(f"{prefix}: {message}", file=stream)


async def _run_synthesis(args, preview=False):
    engine = _engine_class()()
    errors = []
    engine.on_status = lambda message, is_error: (errors.append(message) if is_error else None, _status(message, is_error))
    try:
        config = _build_config(args, engine)
        text = _read_text(args, engine).strip()
        if not text:
            raise ValueError("Input text is empty.")
        if not await engine.init_pipeline_async(config["lang_code"]):
            return 1
        if preview:
            success = await engine.generate_preview(text, config["voice"], config["speed"], str(args.output), config, lang_code=config["lang_code"])
            if not success:
                _status("Preview generation failed.", True)
                return 1
            print(args.output)
            if args.play:
                _playback_module().play(str(args.output), blocking=True)
            return 0
        config["time_id"] = _validate_file_component(args.time_id or time.strftime("%Y%m%d%H%M%S"), "--time-id")
        if config["jit_enabled"]:
            success = await engine._process_jit_async(text, config)
            output = Path(config["out_dir"]) / f"{config['filename']}_{config['time_id']}_jit_output.wav"
        else:
            success = await engine._process_text_async(text, config)
            output = Path(config["out_dir"]) / f"{config['filename']}_{config['time_id']}_combined.{config['format']}"
        if not success or errors:
            return 1
        if (config["jit_enabled"] or config["combine"]) and output.exists():
            print(output)
        else:
            print(config["out_dir"])
        return 0
    except ValueError as error:
        _status(str(error), True)
        return 2
    finally:
        engine.worker.stop()


async def _run_mix(args):
    if not 0.0 <= args.ratio <= 1.0:
        _status("--ratio must be between 0.0 and 1.0.", True)
        return 2
    engine = _engine_class()()
    try:
        language = _normalise_language(args.language)
        _validate_file_component(args.name, "--name")
        if not await engine.init_pipeline_async(language):
            return 1
        success, result, _ = await engine.mix_voices(args.voice_a, args.voice_b, args.ratio, args.name, args.operation)
        if not success:
            _status(result, True)
            return 1
        print(result)
        return 0
    except ValueError as error:
        _status(str(error), True)
        return 2
    finally:
        engine.worker.stop()


def _run_voices(args):
    languages = [_normalise_language(args.language)] if args.language else list(LANGUAGES)
    custom_dir = Path("custom_voices")
    custom = sorted(path.stem for path in custom_dir.glob("*.pt")) if custom_dir.is_dir() else []
    result = {LANGUAGES[code]: {"code": code, "standard": VOICE_DB[code], "custom": custom} for code in languages}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for name, voices in result.items():
            print(f"{name} ({voices['code']}):")
            for voice in voices["standard"]:
                print(f"  {voice}")
            for voice in voices["custom"]:
                print(f"  {voice} (custom)")
    return 0


def _run_devices(args):
    device, description = _device_selector()()
    if args.json:
        print(json.dumps({"device": device, "description": description}))
    else:
        print(f"{device}: {description}")
    return 0


def main(argv=None):
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"synthesize", "preview", "voices", "devices", "mix", "config"}
    if argv and argv[0] not in commands and argv[0] not in {"-h", "--help"}:
        argv.insert(0, "synthesize")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    try:
        if args.command == "synthesize":
            return asyncio.run(_run_synthesis(args))
        if args.command == "preview":
            return asyncio.run(_run_synthesis(args, preview=True))
        if args.command == "mix":
            return asyncio.run(_run_mix(args))
        if args.command == "voices":
            return _run_voices(args)
        if args.command == "devices":
            return _run_devices(args)
        if args.command == "config":
            if not args.print_default:
                parser.error("config requires --print-default")
            print(json.dumps(DEFAULT_CONFIG, indent=2, sort_keys=True))
            return 0
    except ValueError as error:
        _status(str(error), True)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
