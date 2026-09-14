# Command Line Interface

The Kokoro CLI generates audio without starting the Qt application. It currently supports the
Kokoro engine only because the shared backend interface does not yet define a common generation
operation.

## Run Synthesis

Run the module from the repository root or another workspace containing `presets/` and
`custom_voices/`.

```bash
python -m kokoro_gui.cli synthesize \
  --text "The build completed successfully." \
  --voice af_heart \
  --out-dir artifacts \
  --name build \
  --time-id run-001
```

With the default `--combine`, the command writes the generated file path to standard output.
`--no-combine` writes the output directory instead. Status messages and errors go to standard
error. A successful command returns `0`; invalid input returns `2`; model initialization or
synthesis failure returns `1`.

The `synthesize` subcommand is optional when the first argument is an option:

```bash
python -m kokoro_gui.cli --text "Short form." --out-dir artifacts --name short --time-id run-002
```

## Input Sources

Choose one input source per invocation:

```bash
python -m kokoro_gui.cli synthesize --text "Direct text" --out-dir artifacts --name direct --time-id run-003
printf '%s' "Standard input" | python -m kokoro_gui.cli synthesize --stdin --out-dir artifacts --name stdin --time-id run-004
python -m kokoro_gui.cli synthesize --input chapter.epub --out-dir artifacts --name chapter --time-id run-005
```

`--input` accepts TXT, PDF, and EPUB files. `--time-id` prevents a repeated automation run from
overwriting a previous artifact. Output names and timestamps may contain only letters, numbers,
underscores, and hyphens.

## Options

`--language` accepts Kokoro codes and aliases, including `a` or `en-us`, `b` or `en-gb`, `es`,
`fr`, `it`, `pt`, `ja`, and `zh`. Use `--device auto`, `--device cpu`, or `--device cuda` to select
the inference device.

Generation options include `--voice`, `--speed`, `--threads`, `--split-pattern`, `--format`,
`--combine` or `--no-combine`, `--separate` or `--no-separate`, `--subtitles`, and `--cache` or
`--no-cache`. The default output is a combined WAV with one worker and caching disabled.

Use `--preset NAME` and `--fx-preset NAME` to load `presets/NAME.json` and
`presets/fx/NAME.json`. Use `--replace SOURCE=REPLACEMENT` more than once to add lexicon rules.

## Configuration Files

`--config FILE` loads a JSON object before presets and explicit command-line options. The
precedence order is configuration file, speaker preset, FX preset, then explicit options.

The file accepts synthesis settings only: `lang_code`, `voice`, `speed`, `split_pattern`,
`filename`, `format`, `out_dir`, `separate`, `combine`, `export_subtitles`, `caching`,
`num_threads`, `volume`, `pitch`, `normalize`, `trim_silence`, `apply_fx`, `lexicon`, `device`,
and `time_id`. All individual FX settings accepted by `presets/fx/*.json` are also valid. The
legacy `trim` key maps to `trim_silence`. GUI and project settings are rejected.
