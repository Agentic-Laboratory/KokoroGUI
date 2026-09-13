"""Tests for the public command-line interface without model downloads."""

import json
from pathlib import Path

import kokoro_cli


class StubWorker:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class StubEngine:
    def __init__(self):
        self.worker = StubWorker()
        self.on_status = None

    def load_preset(self, name):
        return {"voice": "af_bella", "speed": 0.8} if name == "Slow" else None

    def load_fx_preset(self, name):
        return {"reverb_enabled": True} if name == "Hall" else None

    def extract_text_from_file(self, path):
        return Path(path).read_text(encoding="utf-8")

    async def init_pipeline_async(self, lang_code):
        return True

    async def _process_text_async(self, text, config):
        output = Path(config["out_dir"]) / f"{config['filename']}_{config['time_id']}_combined.{config['format']}"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"audio")
        return True

    async def _process_jit_async(self, text, config):
        output = Path(config["out_dir"]) / f"{config['filename']}_{config['time_id']}_jit_output.wav"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"audio")
        return True

    async def generate_preview(self, text, voice, speed, output_path, config, lang_code):
        Path(output_path).write_bytes(b"audio")
        return True

    async def mix_voices(self, voice_a, voice_b, ratio, name, operation):
        output = Path("custom_voices") / f"{name}.pt"
        output.parent.mkdir(exist_ok=True)
        output.write_bytes(b"voice")
        return True, str(output), None


def test_default_command_generates_combined_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(kokoro_cli, "KokoroEngine", StubEngine)

    assert kokoro_cli.main([
        "--text", "Hello from the command line.", "--out-dir", str(tmp_path),
        "--name", "hello", "--time-id", "run",
    ]) == 0

    assert capsys.readouterr().out.strip() == str(tmp_path / "hello_run_combined.mp3")


def test_synthesis_returns_argument_error_for_invalid_threads(monkeypatch, capsys):
    monkeypatch.setattr(kokoro_cli, "KokoroEngine", StubEngine)

    assert kokoro_cli.main(["--text", "Hello.", "--threads", "0"]) == 2

    assert "--threads must be between 1 and 16" in capsys.readouterr().err


def test_synthesis_rejects_path_components_in_output_name(monkeypatch, capsys):
    monkeypatch.setattr(kokoro_cli, "KokoroEngine", StubEngine)

    assert kokoro_cli.main(["--text", "Hello.", "--name", "../outside"]) == 2

    assert "--name must contain only" in capsys.readouterr().err


def test_config_preset_and_explicit_flags_have_documented_precedence(tmp_path):
    config_path = tmp_path / "settings.json"
    config_path.write_text(json.dumps({"voice": "af_sarah", "speed": 0.5, "trim": True}), encoding="utf-8")
    args = kokoro_cli.build_parser().parse_args([
        "synthesize", "--text", "Hello.", "--config", str(config_path),
        "--preset", "Slow", "--voice", "af_heart", "--speed", "1.2",
    ])

    config = kokoro_cli._build_config(args, StubEngine())

    assert config["voice"] == "af_heart"
    assert config["speed"] == 1.2
    assert config["trim_silence"] is True


def test_gui_jit_setting_in_config_selects_jit_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(kokoro_cli, "KokoroEngine", StubEngine)
    config_path = tmp_path / "settings.json"
    config_path.write_text(json.dumps({"jit_enabled": True}), encoding="utf-8")

    assert kokoro_cli.main([
        "--text", "Hello.", "--config", str(config_path), "--out-dir", str(tmp_path),
        "--name", "jit", "--time-id", "run",
    ]) == 0

    assert capsys.readouterr().out.strip() == str(tmp_path / "jit_run_jit_output.wav")


def test_jit_prints_its_output_when_combine_is_disabled(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(kokoro_cli, "KokoroEngine", StubEngine)

    assert kokoro_cli.main([
        "--text", "Hello.", "--jit", "--no-combine", "--out-dir", str(tmp_path),
        "--name", "jit", "--time-id", "run",
    ]) == 0

    assert capsys.readouterr().out.strip() == str(tmp_path / "jit_run_jit_output.wav")


def test_every_effect_flag_maps_to_engine_config():
    args = kokoro_cli.build_parser().parse_args([
        "synthesize", "--text", "Hello.", "--reverb", "--reverb-room-size", "0.9",
        "--compressor", "--comp-ratio", "5", "--distortion", "--distortion-drive", "14",
        "--chorus", "--chorus-mix", "0.7", "--phaser", "--phaser-depth", "0.3",
        "--clipping", "--clipping-thresh", "-4", "--bitcrush", "--bitcrush-depth", "6",
        "--gsm", "--highpass", "--highpass-freq", "120", "--lowpass", "--lowpass-freq", "8000",
        "--delay", "--delay-time", "0.25", "--pitch-shift", "--pitch-shift-semitones", "3",
        "--limiter", "--limiter-threshold", "-2", "--gain", "--gain-db", "4",
    ])

    config = kokoro_cli._build_config(args, StubEngine())

    assert config["reverb_enabled"] is True
    assert config["reverb_room_size"] == 0.9
    assert config["comp_enabled"] is True
    assert config["comp_ratio"] == 5
    assert config["distortion_enabled"] is True
    assert config["distortion_drive"] == 14
    assert config["chorus_enabled"] is True
    assert config["chorus_mix"] == 0.7
    assert config["phaser_enabled"] is True
    assert config["phaser_depth"] == 0.3
    assert config["clipping_enabled"] is True
    assert config["clipping_thresh"] == -4
    assert config["bitcrush_enabled"] is True
    assert config["bitcrush_depth"] == 6
    assert config["gsm_enabled"] is True
    assert config["highpass_enabled"] is True
    assert config["highpass_freq"] == 120
    assert config["lowpass_enabled"] is True
    assert config["lowpass_freq"] == 8000
    assert config["delay_enabled"] is True
    assert config["delay_time"] == 0.25
    assert config["pitch_shift_enabled"] is True
    assert config["pitch_shift_semitones"] == 3
    assert config["limiter_enabled"] is True
    assert config["limiter_threshold"] == -2
    assert config["gain_enabled"] is True
    assert config["gain_db"] == 4


def test_voices_json_uses_language_alias(capsys):
    assert kokoro_cli.main(["voices", "--language", "ja", "--json"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["Japanese"]["code"] == "j"
    assert "jf_alpha" in result["Japanese"]["standard"]


def test_preview_writes_requested_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(kokoro_cli, "KokoroEngine", StubEngine)
    output = tmp_path / "preview.wav"

    assert kokoro_cli.main(["preview", "--text", "Preview.", "--output", str(output)]) == 0

    assert output.exists()
    assert capsys.readouterr().out.strip() == str(output)


def test_mix_writes_custom_voice(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(kokoro_cli, "KokoroEngine", StubEngine)

    assert kokoro_cli.main(["mix", "--voice-a", "af_heart", "--voice-b", "af_bella", "--name", "narrator"]) == 0

    assert (tmp_path / "custom_voices" / "narrator.pt").exists()
    assert capsys.readouterr().out.strip() == "custom_voices/narrator.pt"
