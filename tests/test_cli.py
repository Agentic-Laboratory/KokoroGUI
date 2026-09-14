"""Tests for the headless Kokoro CLI."""
from pathlib import Path
from types import SimpleNamespace

import kokoro_engine

from kokoro_gui import cli


class StubEngine:
    instance = None

    def __init__(self):
        self.worker = SimpleNamespace(stopped=False, stop=self._stop)
        self.cancel_event = SimpleNamespace(is_set=lambda: False)
        self.on_status = None
        self.config = None
        StubEngine.instance = self

    def _stop(self):
        self.worker.stopped = True

    async def init_pipeline_async(self, lang_code, device=None):
        self.lang_code = lang_code
        self.device = device
        return True

    async def _process_text_async(self, text, config):
        self.text = text
        self.config = config
        out_dir = Path(config["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        if config["combine"]:
            (out_dir / f"{config['filename']}_{config['time_id']}_combined.{config['format']}").write_bytes(b"audio")
        else:
            (out_dir / f"{config['filename']}_{config['time_id']}_part0_0.{config['format']}").write_bytes(b"audio")

    def load_preset(self, name):
        return {"voice": "af_bella", "speed": 1.25, "trim": True, "fx_preset": "narrator_fx", "out_dir": "untrusted"}

    def load_fx_preset(self, name):
        return {"reverb_enabled": True, "filename": "untrusted"}

    def extract_text_from_file(self, path):
        return Path(path).read_text(encoding="utf-8")

    def resolve_voice_path(self, name):
        return name


class MissingOutputEngine(StubEngine):
    async def _process_text_async(self, text, config):
        self.text = text
        self.config = config


def test_implicit_synthesize_writes_combined_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "KokoroEngine", StubEngine)

    result = cli.main([
        "--text", "Hello from the CLI.", "--out-dir", str(tmp_path),
        "--name", "hello", "--time-id", "run1", "--device", "cpu",
    ])

    output = tmp_path / "hello_run1_combined.wav"
    assert result == 0
    assert capsys.readouterr().out.strip() == str(output)
    assert output.is_file()
    assert StubEngine.instance.text == "Hello from the CLI."
    assert StubEngine.instance.lang_code == "a"
    assert StubEngine.instance.device == "cpu"
    assert StubEngine.instance.worker.stopped is True


def test_config_preset_and_explicit_flags_have_documented_precedence(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "KokoroEngine", StubEngine)
    config = tmp_path / "config.json"
    config.write_text('{"voice": "af_alloy", "speed": 0.8, "time_id": "from-config"}', encoding="utf-8")

    result = cli.main([
        "synthesize", "--text", "Hello.", "--config", str(config), "--preset", "narrator",
        "--speed", "1.5", "--out-dir", str(tmp_path), "--name", "output", "--time-id", "explicit",
    ])

    assert result == 0
    assert StubEngine.instance.config["voice"] == "af_bella"
    assert StubEngine.instance.config["speed"] == 1.5
    assert StubEngine.instance.config["out_dir"] == str(tmp_path)
    assert StubEngine.instance.config["time_id"] == "explicit"
    assert StubEngine.instance.config["trim_silence"] is True
    assert StubEngine.instance.config["reverb_enabled"] is True
    assert "untrusted" not in StubEngine.instance.config.values()


def test_cli_reports_failure_when_engine_creates_no_expected_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "KokoroEngine", MissingOutputEngine)

    result = cli.main([
        "--text", "Hello.", "--out-dir", str(tmp_path), "--name", "missing", "--time-id", "run1",
    ])

    assert result == 1
    assert "Expected output was not created" in capsys.readouterr().err
    assert MissingOutputEngine.instance.worker.stopped is True


def test_cli_rejects_unsafe_output_name(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "KokoroEngine", StubEngine)

    result = cli.main([
        "--text", "Hello.", "--out-dir", str(tmp_path), "--name", "../escape", "--time-id", "run1",
    ])

    assert result == 2
    assert "--name must contain only" in capsys.readouterr().err
    assert StubEngine.instance.worker.stopped is True


def test_cli_reads_text_input_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "KokoroEngine", StubEngine)
    source = tmp_path / "input.txt"
    source.write_text("File input.", encoding="utf-8")

    result = cli.main([
        "--input", str(source), "--out-dir", str(tmp_path), "--name", "file", "--time-id", "run1",
    ])

    assert result == 0
    assert StubEngine.instance.text == "File input."


def test_cli_accepts_individual_fx_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "KokoroEngine", StubEngine)
    config = tmp_path / "config.json"
    config.write_text('{"reverb_enabled": true, "reverb_wet_level": 0.4}', encoding="utf-8")

    result = cli.main([
        "--text", "FX config.", "--config", str(config), "--out-dir", str(tmp_path),
        "--name", "fx", "--time-id", "run1",
    ])

    assert result == 0
    assert StubEngine.instance.config["reverb_enabled"] is True
    assert StubEngine.instance.config["reverb_wet_level"] == 0.4


def test_cli_rejects_existing_part_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "KokoroEngine", StubEngine)
    (tmp_path / "parts_run1_part0_0.wav").write_bytes(b"audio")

    result = cli.main([
        "--text", "Hello.", "--out-dir", str(tmp_path), "--name", "parts", "--time-id", "run1",
        "--no-combine",
    ])

    assert result == 2
    assert "Output already exists" in capsys.readouterr().err


def test_cli_rejects_existing_subtitle_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "KokoroEngine", StubEngine)
    (tmp_path / "subtitle_run1_combined.srt").write_text("existing", encoding="utf-8")

    result = cli.main([
        "--text", "Hello.", "--out-dir", str(tmp_path), "--name", "subtitle", "--time-id", "run1",
        "--format", "flac", "--subtitles",
    ])

    assert result == 2
    assert "Output already exists" in capsys.readouterr().err


def test_cli_rejects_invalid_config_boolean(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "KokoroEngine", StubEngine)
    config = tmp_path / "config.json"
    config.write_text('{"combine": "false", "num_threads": true}', encoding="utf-8")

    result = cli.main([
        "--text", "Hello.", "--config", str(config), "--out-dir", str(tmp_path),
        "--name", "invalid", "--time-id", "run1",
    ])

    assert result == 2
    assert "combine must be a boolean" in capsys.readouterr().err


def test_cli_generates_audio_with_the_current_engine_contract(monkeypatch, fake_pipeline, isolated_dirs, capsys):
    monkeypatch.setattr(cli, "KokoroEngine", kokoro_engine.KokoroEngine)

    result = cli.main([
        "--text", "Current engine contract.", "--out-dir", str(isolated_dirs.out_dir),
        "--name", "engine", "--time-id", "run1", "--no-cache",
    ])

    output = isolated_dirs.out_dir / "engine_run1_combined.wav"
    assert result == 0
    assert capsys.readouterr().out.strip() == str(output)
    assert output.is_file()
