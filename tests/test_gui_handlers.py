"""Tests for assorted GUI event handlers: lexicon add/delete, thread-count
clamp, mix-name validation, preset save-dialog sanitization, load_fx_preset
safety, and a documented existing bug in refresh_voice_lists."""
import json
import os

import pytest


def test_add_lexicon_rule_persists_and_refreshes(tts_app):
    import gui
    tts_app.lex_orig_var.set("hello")
    tts_app.lex_replace_var.set("hi")
    tts_app.add_lexicon_rule()

    assert tts_app.settings["lexicon"]["hello"] == "hi"
    with open(gui.CONFIG_FILE, "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["lexicon"]["hello"] == "hi"


def test_add_lexicon_rule_empty_original_shows_warning(tts_app):
    import gui
    tts_app.lex_orig_var.set("")
    tts_app.lex_replace_var.set("hi")
    tts_app.add_lexicon_rule()

    assert tts_app.settings.get("lexicon", {}) == {}
    assert gui.messagebox.showwarning.called


def test_delete_lexicon_rule_removes_key(tts_app):
    tts_app.settings["lexicon"] = {"hello": "hi"}
    tts_app.delete_lexicon_rule("hello")

    assert "hello" not in tts_app.settings["lexicon"]


def test_edit_lexicon_rule_updates_and_renames_rule(tts_app):
    tts_app.settings["lexicon"] = {"dives": "dive's"}

    tts_app.edit_lexicon_rule("dives")

    assert tts_app.lex_orig_var.get() == "dives"
    assert tts_app.lex_replace_var.get() == "dive's"
    assert tts_app.lex_save_button.cget("text") == "Save Rule"

    tts_app.lex_orig_var.set("dived")
    tts_app.lex_replace_var.set("dyved")
    tts_app.add_lexicon_rule()

    assert tts_app.settings["lexicon"] == {"dived": "dyved"}
    assert tts_app.lex_save_button.cget("text") == "Add Rule"


@pytest.mark.parametrize("start,delta,expected", [
    (1, -5, 1),
    (16, 5, 16),
    (5, 2, 7),
])
def test_change_threads_clamps_1_to_16(tts_app, start, delta, expected):
    tts_app.num_threads_var.set(start)
    tts_app.change_threads(delta)
    assert tts_app.num_threads_var.get() == expected


def test_mix_voice_action_rejects_invalid_name_chars(tts_app):
    tts_app.mix_name_var.set("bad name!")
    tts_app.mix_voice_action()

    assert not tts_app.engine.mix_voices.called


def test_mix_voice_action_prompts_overwrite_confirmation(tts_app):
    import gui
    existing = tts_app.get_all_voices()[0]
    tts_app.mix_name_var.set(existing)
    gui.messagebox.askyesno.return_value = False

    tts_app.mix_voice_action()

    assert not tts_app.engine.mix_voices.called


def test_save_preset_dialog_sanitizes_name(tts_app, monkeypatch):
    import gui

    class FakeDialog:
        def __init__(self, *a, **kw):
            pass

        def get_input(self):
            return 'Bad/Na:me'

    monkeypatch.setattr(gui.ctk, "CTkInputDialog", FakeDialog)
    tts_app.save_preset_dialog()

    assert os.path.exists(os.path.join(gui.PRESETS_DIR, "BadName.json"))


def test_save_fx_preset_dialog_sanitizes_name(tts_app, monkeypatch):
    import gui

    class FakeDialog:
        def __init__(self, *a, **kw):
            pass

        def get_input(self):
            return 'Weird?Nam*e'

    monkeypatch.setattr(gui.ctk, "CTkInputDialog", FakeDialog)
    tts_app.save_fx_preset_dialog()

    assert os.path.exists(os.path.join(gui.FX_PRESETS_DIR, "WeirdName.json"))


def test_load_fx_preset_basename_sanitized(tts_app):
    import gui
    os.makedirs(gui.FX_PRESETS_DIR, exist_ok=True)
    with open(os.path.join(gui.FX_PRESETS_DIR, "real.json"), "w", encoding="utf-8") as f:
        json.dump({"gain_db": 3.0}, f)

    tts_app.load_fx_preset("../../real")

    assert tts_app.gain_db.get() == 3.0


def test_refresh_voice_lists_crashes_if_custom_voices_dir_missing(tts_app):
    import gui
    # Documents an existing asymmetry at gui.py:693 (os.listdir with no
    # os.path.exists guard), unlike get_all_voices (gui.py:192) which does
    # guard. Pins current behavior - do not silently "fix" by changing this
    # assertion; if the guard is added, update this test deliberately.
    os.rmdir(gui.CUSTOM_VOICES_DIR)
    with pytest.raises(FileNotFoundError):
        tts_app.refresh_voice_lists()


def test_set_ui_state_gates_preview_mix_button(tts_app):
    """Preview Mix starts inference on its own thread, so it must be locked out
    for the duration of a conversion like the other generation buttons."""
    tts_app.set_ui_state(True)
    assert tts_app.mix_preview_btn.cget("state") == "disabled"

    tts_app.set_ui_state(False)
    assert tts_app.mix_preview_btn.cget("state") == "normal"
