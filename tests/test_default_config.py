"""First-launch seeding of config.json.

These exercise module-level helpers in `gui`, not `TTSApp`, so they run without
a display. Every case writes into tmp_path; none touches the real config.json.
"""
import json
import os


def test_creates_config_file_when_missing(tmp_path):
    import gui

    target = tmp_path / "config.json"

    settings = gui.create_default_config(str(target))

    assert target.exists(), "first launch should leave a config.json on disk"
    assert json.loads(target.read_text(encoding="utf-8")) == settings


def test_does_not_overwrite_an_existing_config(tmp_path):
    import gui

    target = tmp_path / "config.json"
    mine = {"voice": "af_sky", "out_dir": "/Users/echan/Downloads",
            "lexicon": {"SCADA": "skay da"}}
    target.write_text(json.dumps(mine), encoding="utf-8")

    gui.create_default_config(str(target))

    assert json.loads(target.read_text(encoding="utf-8")) == mine


def test_seeded_out_dir_is_relative_to_the_app(tmp_path):
    import gui

    settings = gui.create_default_config(str(tmp_path / "config.json"))

    assert not os.path.isabs(settings["out_dir"])


def test_seeded_lexicon_ships_no_work_specific_terms(tmp_path):
    import gui

    settings = gui.create_default_config(str(tmp_path / "config.json"))

    lexicon = settings["lexicon"]
    assert lexicon, "an empty lexicon tab does not show the feature exists"
    for term in ("SCADA", "WWTP", "NERC", "NPDES", "CIS", "AMI", "dives"):
        assert term not in lexicon


def test_seeded_lexicon_omits_rules_that_only_respell_the_acronym(tmp_path):
    """550490d pruned 46 of these; the seed must not reintroduce the pattern."""
    import gui

    settings = gui.create_default_config(str(tmp_path / "config.json"))

    for source, replacement in settings["lexicon"].items():
        assert replacement.upper() != " ".join(source.upper()), (
            f"{source!r} -> {replacement!r} only spaces out the letters"
        )
