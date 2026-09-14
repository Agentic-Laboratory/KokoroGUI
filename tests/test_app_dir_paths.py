"""The application's state directories are anchored to the application itself.

Regression cover for launching from somewhere other than the repository root.
A Finder/Dock/.app launch on macOS gives CWD="/", which is read-only, so
creating cache/, presets/ or custom_voices/ there failed outright; launched
from any other writable directory, the app quietly scattered a second set of
them and stopped persisting settings.

paths.py holds the definitions; kokoro_engine, kokoro_cli and gui each
re-export them. These tests deliberately use no fixture that redirects the
constants - they assert the real, unpatched values.
"""
import json
import os
from pathlib import Path

import pytest

import kokoro_cli
import kokoro_engine
import paths

APP_DIR = os.path.dirname(os.path.abspath(paths.__file__))

PATH_CONSTANTS = [
    "CUSTOM_VOICES_DIR",
    "CACHE_DIR",
    "PRESETS_DIR",
    "FX_PRESETS_DIR",
    "CONFIG_FILE",
]


@pytest.fixture
def gui_module():
    return pytest.importorskip("gui")


@pytest.mark.parametrize("name", PATH_CONSTANTS)
def test_path_constants_are_absolute_strings(name):
    value = getattr(paths, name)
    assert isinstance(value, str), f"{name} must stay a str - os.path.join and the test monkeypatches rely on it"
    assert os.path.isabs(value)


@pytest.mark.parametrize("name", PATH_CONSTANTS)
def test_path_constants_live_in_the_application_directory(name):
    assert os.path.commonpath([APP_DIR, getattr(paths, name)]) == APP_DIR


@pytest.mark.parametrize(
    "name", ["APP_DIR", "CACHE_DIR", "CUSTOM_VOICES_DIR", "PRESETS_DIR", "FX_PRESETS_DIR"]
)
def test_engine_reexports_match_paths(name):
    assert getattr(kokoro_engine, name) == getattr(paths, name)


@pytest.mark.parametrize("name", ["CUSTOM_VOICES_DIR", "PRESETS_DIR", "FX_PRESETS_DIR"])
def test_cli_reexports_match_paths(name):
    # A drifting copy would mean `kokoro-tts mix` writes a voice that
    # `kokoro-tts voices` cannot see.
    assert getattr(kokoro_cli, name) == getattr(paths, name)


def test_every_front_end_shares_one_custom_voices_definition(gui_module):
    """The engine writes custom voices and both front ends list them.

    Identity, not just equality: `from paths import X` rebinds the same str
    object, so a reintroduced local copy computed from `__file__` would still
    compare equal while being a separate definition free to drift. Checking
    `is` makes that refactor fail loudly.
    """
    for module in (kokoro_engine, kokoro_cli, gui_module):
        assert module.CUSTOM_VOICES_DIR == paths.CUSTOM_VOICES_DIR
        assert module.CUSTOM_VOICES_DIR is paths.CUSTOM_VOICES_DIR, (
            f"{module.__name__} defines its own CUSTOM_VOICES_DIR instead of importing paths'"
        )


@pytest.mark.parametrize(
    "name", ["APP_DIR", "CONFIG_FILE", "CUSTOM_VOICES_DIR", "PRESETS_DIR", "FX_PRESETS_DIR"]
)
def test_gui_reexports_match_paths(gui_module, name):
    assert getattr(gui_module, name) == getattr(paths, name)


def test_constants_do_not_follow_the_working_directory(tmp_path, monkeypatch, gui_module):
    before = [getattr(paths, name) for name in PATH_CONSTANTS]

    monkeypatch.chdir(tmp_path)

    after = [getattr(paths, name) for name in PATH_CONSTANTS]
    assert after == before
    for value in after:
        # is_relative_to rather than commonpath: on Windows, tmp_path and the
        # repository can sit on different drives, which makes commonpath raise.
        assert not Path(value).is_relative_to(tmp_path), f"{value} resolved under the working directory"


def test_load_preset_ignores_a_preset_in_the_working_directory(engine, isolated_dirs, tmp_path, monkeypatch):
    (isolated_dirs.presets_dir / "Shared.json").write_text(json.dumps({"voice": "anchored"}), encoding="utf-8")

    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "presets").mkdir(parents=True)
    (elsewhere / "presets" / "Shared.json").write_text(json.dumps({"voice": "decoy"}), encoding="utf-8")
    monkeypatch.chdir(elsewhere)

    assert engine.load_preset("Shared") == {"voice": "anchored"}


def test_load_fx_preset_ignores_an_fx_preset_in_the_working_directory(engine, isolated_dirs, tmp_path, monkeypatch):
    (isolated_dirs.fx_presets_dir / "Shared.json").write_text(json.dumps({"gain_db": 1.0}), encoding="utf-8")

    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "presets" / "fx").mkdir(parents=True)
    (elsewhere / "presets" / "fx" / "Shared.json").write_text(json.dumps({"gain_db": 99.0}), encoding="utf-8")
    monkeypatch.chdir(elsewhere)

    assert engine.load_fx_preset("Shared") == {"gain_db": 1.0}


def test_cli_missing_preset_error_names_the_anchored_directory(isolated_dirs, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class NoPresets:
        def load_preset(self, name):
            return None

        def load_fx_preset(self, name):
            return None

    with pytest.raises(ValueError) as error:
        kokoro_cli._load_preset(NoPresets(), "Nope")
    assert str(error.value) == f"Preset not found: {os.path.join(str(isolated_dirs.presets_dir), 'Nope.json')}"

    with pytest.raises(ValueError) as fx_error:
        kokoro_cli._load_preset(NoPresets(), "Nope", fx=True)
    assert str(fx_error.value) == f"Preset not found: {os.path.join(str(isolated_dirs.fx_presets_dir), 'Nope.json')}"


def test_cli_voices_lists_the_anchored_custom_voices_dir(isolated_dirs, tmp_path, monkeypatch, capsys):
    (isolated_dirs.custom_voices / "anchored.pt").write_bytes(b"voice")

    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "custom_voices").mkdir(parents=True)
    (elsewhere / "custom_voices" / "decoy.pt").write_bytes(b"voice")
    monkeypatch.chdir(elsewhere)

    assert kokoro_cli.main(["voices", "--language", "en-us", "--json"]) == 0

    custom = json.loads(capsys.readouterr().out)["American English"]["custom"]
    assert custom == ["anchored"]
