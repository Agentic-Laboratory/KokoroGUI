# Kokoro TTS CLI

`kokoro-tts` generates Kokoro audio without opening the Tk interface. It accepts text, standard input, TXT files, PDFs, and EPUB files.

## Install

Install the project in editable mode from the repository root:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .
```

The installed command is `kokoro-tts`. During development, use `.venv/bin/python -m kokoro_cli` instead.

The first synthesis run may download Kokoro model weights. Standard batch synthesis does not require an audio device. Preview playback and JIT playback do.

## Quick Use

Generate speech from direct text:

```bash
kokoro-tts --text "The build completed successfully." --voice af_heart --out-dir audio_output
```

Convert a document and write an OGG plus subtitles:

```bash
kokoro-tts synthesize --input chapter.epub --language en-gb --voice bf_emma \
  --format ogg --subtitles --out-dir audio_output --name chapter-01
```

Pipe text from another program:

```bash
printf '%s' "Read from standard input." | kokoro-tts --stdin --out-dir audio_output
```

Generated paths are printed to stdout. Status messages and errors are written to stderr. A successful command exits with status `0`; invalid arguments exit with `2`; model or synthesis failures exit with `1`.

## Commands

| Command | Purpose |
|---|---|
| `synthesize` | Generate audio. This is the default command when the first argument is an option. |
| `preview` | Generate a bounded WAV preview. Use `--play` to send it to the audio device. |
| `voices` | List standard and custom voice names. |
| `devices` | Show the auto-selected Torch backend. |
| `mix` | Create a custom `.pt` voice by mixing two voices. |
| `config --print-default` | Print the complete JSON synthesis configuration. |

Run `kokoro-tts COMMAND --help` for the executable reference.

## Synthesis Options

`synthesize` accepts exactly one input source.

| Option | Meaning |
|---|---|
| `--text TEXT` | Synthesize literal text. |
| `--input PATH` | Read TXT, PDF, or EPUB text. |
| `--stdin` | Read text from standard input. |
| `--config PATH` | Read synthesis settings from JSON. Explicit flags override it. |
| `--preset NAME` | Apply `presets/NAME.json`. |
| `--fx-preset NAME` | Apply `presets/fx/NAME.json`. |
| `--language VALUE` | Language code or alias. Accepted aliases include `a`, `en-us`, `b`, `en-gb`, `es`, `fr`, `it`, `pt`, `ja`, and `zh`. |
| `--voice NAME` | Standard or custom voice. Use `voices` to inspect names. |
| `--speed NUMBER` | Speech speed multiplier. |
| `--volume NUMBER` | Output gain multiplier. |
| `--pitch NUMBER` | Pitch shift in semitones that also changes duration. |
| `--threads 1..16` | Parallel generation worker count. |
| `--split-pattern REGEX` | Segment split regular expression. |
| `--name PREFIX` | Output filename prefix. |
| `--out-dir PATH` | Output directory. Default: `audio_output`. |
| `--format FORMAT` | `wav`, `flac`, `mp3`, or `ogg`. |
| `--time-id ID` | Deterministic output suffix. Default: current local timestamp. |
| `--replace SOURCE=TARGET` | Add a case-insensitive lexicon rule. Repeat this option for more rules. |

Boolean settings have both positive and negative forms. For example, `--cache` enables the raw audio cache and `--no-cache` disables it.

| Setting | Default | Positive form |
|---|---:|---|
| Combine parts | On | `--combine` |
| Keep parts | On | `--separate` |
| Export subtitles | Off | `--subtitles` |
| Cache raw segments | On | `--cache` |
| Normalize peak | Off | `--normalize` |
| Trim silence | Off | `--trim-silence` |
| Apply effects | On | `--apply-fx` |
| JIT playback | Off | `--jit` |

`--jit` streams generated WAV segments to the audio device and writes a combined `*_jit_output.wav`. Use batch synthesis for unattended agents and headless systems.

## Effects

Every audio control in the Generate and FX tabs has a CLI option. Each effect toggle also accepts a `--no-...` form.

| Effect | Toggle | Parameters |
|---|---|---|
| Reverb | `--reverb` | `--reverb-room-size`, `--reverb-wet-level`, `--reverb-damping`, `--reverb-dry-level`, `--reverb-width` |
| Shelf EQ | None | `--eq-bass`, `--eq-treble` |
| Compressor | `--compressor` | `--comp-threshold`, `--comp-ratio`, `--comp-attack`, `--comp-release` |
| Distortion | `--distortion` | `--distortion-drive` |
| Chorus | `--chorus` | `--chorus-rate`, `--chorus-depth`, `--chorus-mix` |
| Phaser | `--phaser` | `--phaser-rate`, `--phaser-depth`, `--phaser-mix` |
| Clipping | `--clipping` | `--clipping-thresh` |
| Bitcrush | `--bitcrush` | `--bitcrush-depth` |
| GSM compression | `--gsm` | None |
| High-pass filter | `--highpass` | `--highpass-freq` |
| Low-pass filter | `--lowpass` | `--lowpass-freq` |
| Delay | `--delay` | `--delay-time`, `--delay-feedback`, `--delay-mix` |
| Pedalboard pitch shift | `--pitch-shift` | `--pitch-shift-semitones` |
| Limiter | `--limiter` | `--limiter-threshold`, `--limiter-release` |
| Gain | `--gain` | `--gain-db` |

For example, create a filtered and compressed WAV:

```bash
kokoro-tts --text "Radio check." --out-dir audio_output --name radio \
  --highpass --highpass-freq 250 --lowpass --lowpass-freq 3400 \
  --compressor --comp-threshold -24 --comp-ratio 4
```

## Config, Presets, and Speakers

`config --print-default` prints every accepted synthesis field and its default value:

```bash
kokoro-tts config --print-default > synthesis.json
kokoro-tts --config synthesis.json --text "Configured synthesis."
```

The merge order is built-in defaults, JSON config, `--preset`, `--fx-preset`, then explicit command-line flags. The CLI accepts existing GUI `config.json` files but ignores appearance, scaling, and font fields. It maps the GUI's `trim` key to `trim_silence` and honors `jit_enabled`. Output names and timestamp IDs use letters, numbers, underscores, and hyphens only.

Text can choose a preset and FX preset for each segment:

```text
[Narrator]: This uses presets/Narrator.json.

[Narrator:Radio]: This also applies presets/fx/Radio.json.
```

Inline speaker presets override the base settings for their segment.

## Voices and Devices

List voices in a script-friendly form:

```bash
kokoro-tts voices --language ja --json
kokoro-tts devices --json
```

Create a custom voice in `custom_voices/`:

```bash
kokoro-tts mix --voice-a af_heart --voice-b af_bella --name warm_narrator --ratio 0.35
```

`mix` supports `mix`, `add`, `subtract`, `multiply`, and `divide`. The last four operations can produce unstable voices. Use `mix` unless you are testing voice tensors deliberately.

## Preview

`preview` writes a WAV using the same voice, processing, presets, and lexicon options as synthesis. It limits input to the engine's preview behavior, at most two speaker segments and 500 characters per segment.

```bash
kokoro-tts preview --text "Check this voice." --voice af_sky --output preview.wav
```

Add `--play` only when the machine has a working audio device:

```bash
kokoro-tts preview --text "Play this preview." --output preview.wav --play
```
