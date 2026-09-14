"""Tests for the engine-level load_preset/load_fx_preset.

Both resolve against kokoro_engine.PRESETS_DIR / FX_PRESETS_DIR, which are
anchored to the application directory. Isolation therefore comes from the
isolated_dirs fixture, which redirects those constants into tmp_path; the
working directory is irrelevant (see test_app_dir_paths.py).
"""
import json


def test_load_preset_reads_json(engine, isolated_dirs):
    (isolated_dirs.presets_dir / "MyPreset.json").write_text(json.dumps({"voice": "af_heart"}), encoding="utf-8")

    assert engine.load_preset("MyPreset") == {"voice": "af_heart"}


def test_load_preset_missing_returns_none(engine, isolated_dirs):
    assert engine.load_preset("DoesNotExist") is None


def test_load_preset_malformed_json_returns_none(engine, isolated_dirs):
    (isolated_dirs.presets_dir / "Bad.json").write_text("{not valid json", encoding="utf-8")

    assert engine.load_preset("Bad") is None


def test_load_preset_path_traversal_sanitized(engine, isolated_dirs):
    (isolated_dirs.presets_dir / "secret.json").write_text(json.dumps({"voice": "x"}), encoding="utf-8")

    # os.path.basename() strips any path components before the lookup.
    assert engine.load_preset("../../secret") == {"voice": "x"}


def test_load_fx_preset_reads_json(engine, isolated_dirs):
    (isolated_dirs.fx_presets_dir / "MyFx.json").write_text(json.dumps({"reverb_enabled": True}), encoding="utf-8")

    assert engine.load_fx_preset("MyFx") == {"reverb_enabled": True}


def test_load_fx_preset_missing_returns_none(engine, isolated_dirs):
    assert engine.load_fx_preset("Nope") is None


def test_load_fx_preset_path_traversal_sanitized(engine, isolated_dirs):
    (isolated_dirs.fx_presets_dir / "s.json").write_text(json.dumps({"gain_db": 3.0}), encoding="utf-8")

    assert engine.load_fx_preset("../../s") == {"gain_db": 3.0}
