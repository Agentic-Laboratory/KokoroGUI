# Settings Guide

KokoroGUI saves its settings in `config.json` beside the application. Change settings in the UI where possible. The application writes the file when settings change or when it closes.

Hover an interactive setting in the GUI for a short description. The same guidance is collected here for reference.

## Recommended Starting Point

These settings give a clean narration export without unnecessary post-processing.

| Setting | Recommended value | Why |
|---|---|---|
| Speed | `1.0x` | Preserves the voice's intended pacing. |
| Volume | `100%` | Leaves level adjustment to normalization or a limiter. |
| Pitch | `0 st` | Avoids resampling artifacts. |
| Split By | Natural (Newlines) | Keeps author-supplied paragraph and line breaks meaningful. |
| Keep Segments | Off | Avoids retaining intermediate files. |
| Combine Output | On | Produces one finished file. |
| Generation Cache | On | Reuses raw synthesis for repeated text, voice, speed, and language. |
| Normalize | On | Sets the finished peak level to 98%. |
| Trim Silence | Off | Preserves natural pauses at the beginning and end of segments. |
| Apply FX | Off | Keeps narration unprocessed. |

Use `mp3` for normal exports. Use `wav` while checking subtle artifacts, or `flac` when you need a lossless master for archival or later editing.

## Parallel Threads

**Parallel Threads** controls concurrent document chunks, not a GPU's hardware-thread count. Each worker creates its own Kokoro pipeline, so increasing the setting also increases RAM and accelerator memory use. It helps only when the system can run independent chunks faster than one pipeline can complete them.

| Hardware | Backend | Start with | Notes |
|---|---|---:|---|
| This PC: Ryzen 7 5800X, Radeon RX 6900 XT, 16 GB VRAM | ROCm GPU | **2** | A short representative synthesis check found the throughput plateau at two workers. Do not use the current value of five. |
| This PC: Ryzen 7 5800X | CPU | **4** | Four workers reached the CPU throughput plateau while using less memory than higher values. |
| MacBook with M2 and 8 GB unified memory | MPS GPU | **1** | Leave memory for macOS and avoid swap. |
| MacBook with M2 and 16 GB or 24 GB unified memory | MPS GPU | **2** | A practical starting point for the smaller integrated GPU. |
| MacBook with M2 | CPU | **2** | Avoids oversubscribing the performance and efficiency cores. |
| MacBook Pro with M5 Pro | MPS GPU | **2** | Try `3` only with a long document and keep it only if the total time improves clearly. |
| MacBook Pro with M5 Pro | CPU | **4** | A conservative setting that leaves capacity for macOS. |

The settings for the MacBooks are hardware-based estimates, not measurements on those machines. Apple Silicon uses unified memory, so close other memory-heavy applications before raising the worker count. If macOS reports memory pressure or the application becomes less responsive, lower the setting by one.

> **Important:** JIT generation is sequential playback-oriented generation. It does not use the batch worker pool, so changing Parallel Threads does not make JIT playback faster.

## Generation And Output

These controls choose the model input, the generated speech, and the files written to the output directory.

| UI control | `config.json` key | What it does |
|---|---|---|
| Language | `lang_code` | Selects the Kokoro language pipeline and available voice list. Choose the language that matches the source text. |
| Voice | `voice` | Selects a bundled voice or a `.pt` voice file in `custom_voices/`. |
| Base Filename | `filename` | Prefix for generated segment and combined-output files. |
| Format | `format` | Output container: `wav`, `flac`, `mp3`, or `ogg`. MP3 is the default for regular exports. JIT always uses WAV for playback compatibility. |
| Output Directory | `out_dir` | Directory that receives generated audio, subtitles, and JIT recovery files. |
| Speed | `speed` | Speech rate from `0.5x` to `2.0x`. Start at `1.0x`; use `0.9x` for denser material and `1.1x` for lighter material. |
| Volume | `volume` | Linear output gain from 10% to 200%. This happens before FX and normalization. |
| Pitch | `pitch` | Pitch in semitones from `-12` to `+12`. The application compensates generation speed before resampling so duration stays close to the chosen Speed. |
| Split By | `split_pattern` | Text boundary pattern passed to Kokoro. Natural splits on one or more newlines, Paragraphs on blank lines, and Sentences on sentence-ending punctuation. |
| Keep Segments | `separate` | Retains individual generated files after optional combining. |
| Combine Output | `combine` | Creates one merged file after generation. |
| Export Subtitles | `export_subtitles` | Writes a sequential `.srt` file beside the combined output. |

Choose **Natural (Newlines)** when the input already contains deliberate narration breaks. Choose **Sentences** when converting prose that is one long paragraph. Sentence splitting may handle abbreviations imperfectly, so preview a sample before a book-length conversion.

## Processing Settings

These options affect reuse, streaming, diagnostics, and the application interface.

| UI control | `config.json` key | What it does |
|---|---|---|
| Enable Generation Caching | `caching` | Stores raw generated WAV segments in `cache/`. A repeat run with the same text, voice, effective speed, and language skips model synthesis. Post-processing still runs. |
| Enable JIT Generation | `jit_enabled` | Changes Start Generation into real-time generation and playback. It writes a partial WAV and remaining-text file if cancelled. |
| Enable Debug Logging | `debug_logging` | Raises logging detail for diagnosis. Leave off during normal use. |
| Appearance Mode | `appearance` | Selects System, Dark, or Light UI appearance. |
| UI Scaling | `scaling` | Scales widgets from 80% to 300%. |
| Font Size | `font_size` | Changes interface text size in pixels without changing window or widget dimensions. |
| Lexicon | `lexicon` | A case-insensitive source-to-replacement dictionary applied before synthesis. For example, map `SQL` to `sequel` to force the preferred pronunciation. |

> **Common Mistake:** The cache is raw synthesis, not a complete rendered-file cache. Changing volume, normalization, trimming, or FX reprocesses the cached audio. The cache key already changes for new text, voice, language, and effective speed. Clear `cache/` when testing a changed Split By setting because that pattern is not part of the cache key.

## Audio Cleanup

These controls are applied after Kokoro generates audio, in the order shown below.

1. Trim Silence removes leading and trailing samples quieter than the built-in threshold.
2. Volume applies linear gain.
3. Pitch resamples the audio.
4. FX applies the enabled effects in the FX tab.
5. Normalize sets the peak to 98%.

| UI control | `config.json` key | What it does |
|---|---|---|
| Trim Silence | `trim` | Removes silence at the beginning and end of each generated segment. Use it for clipped voice samples, not natural narration with intended pauses. At generation time this is passed to the engine as `trim_silence`. |
| Normalize | `normalize` | Raises or lowers the final peak to 0.98. It prevents most clipping but does not match loudness between different voices. |
| Apply | `apply_fx` | Enables all selected FX. Turn it off to bypass the entire FX chain without clearing individual settings. |

**Volume** and **Gain** are different. Volume is a linear multiplier before effects; Gain is a decibel effect late in the FX chain. Use Volume for a simple adjustment. Use Gain only when building an FX preset with compression or limiting.

## FX Settings

The FX tab uses Pedalboard effects. Start with one effect, preview a sentence, then adjust. Multiple effects can make speech muddy or harsh quickly. Each effect other than bass and treble has an `*_enabled` setting, such as `reverb_enabled` or `limiter_enabled`, which must be on as well as the global `apply_fx` setting.

| Effect | Settings | Use |
|---|---|---|
| Compressor | `comp_threshold`, `comp_ratio`, `comp_attack`, `comp_release` | Reduces loudness variation. A gentle narration starting point is `-20 dB` and `4:1`. |
| Limiter | `limiter_threshold`, `limiter_release` | Caps peaks. Use after compression when the output must not clip. |
| Gain | `gain_db` | Adds or removes level after other dynamics effects. |
| Bass shelf | `eq_bass` | Boosts or cuts frequencies below 250 Hz. Small adjustments, such as `+2 dB`, are usually enough. |
| Treble shelf | `eq_treble` | Boosts or cuts frequencies above 4 kHz. Cut a little if speech is sharp. |
| High-pass filter | `highpass_freq` | Removes low-frequency rumble below the chosen frequency. Try 60 to 100 Hz for narration cleanup. |
| Low-pass filter | `lowpass_freq` | Removes high frequencies above the chosen frequency. Use sparingly because it dulls speech. |
| Reverb | `reverb_room_size`, `reverb_wet_level`, `reverb_damping`, `reverb_dry_level`, `reverb_width` | Adds room ambience. For subtle room tone, start at room `0.2` and wet `0.1`. |
| Delay | `delay_time`, `delay_feedback`, `delay_mix` | Repeats audio after a delay. Usually unsuitable for narration. |
| Pitch Shift | `pitch_shift_semitones` | Changes pitch without intentionally changing duration. Use this instead of the main Pitch control when duration must remain fixed. |
| Chorus | `chorus_rate`, `chorus_depth`, `chorus_mix` | Adds doubled, modulated texture. Better for character effects than clear narration. |
| Phaser | `phaser_rate`, `phaser_depth`, `phaser_mix` | Sweeps a filtered phase effect. Use for stylized voices only. |
| Distortion | `distortion_drive` | Adds saturation and harmonic distortion. Low values are already audible. |
| Clipping | `clipping_thresh` | Hard-limits peaks, adding distortion above the threshold. |
| Bitcrush | `bitcrush_depth` | Reduces bit depth for lo-fi sound. Smaller values are harsher. |
| GSM Compressor | `gsm_enabled` | Applies telephone-like GSM quality. |

Some fields are saved in `config.json` but are not currently adjustable in the FX tab: `reverb_dry_level`, `comp_attack`, `comp_release`, `limiter_release`, `chorus_mix`, `phaser_depth`, and `phaser_mix`. They retain their saved values and can be included in an FX preset. The default values are appropriate for a neutral starting point.

## Presets And Multi-Speaker Text

Generation presets store voice, speed, volume, pitch, split pattern, cleanup options, output format, and an optional FX preset. FX presets store the effect parameters only. Save presets when you find a combination that works for a voice.

The built-in generation presets use MP3. They are starting points, not fixed voice recommendations.

| Preset | Use | Key choices |
|---|---|---|
| Default | General short-form speech | `1.0x`, no processing. |
| Narrative Training | Dense instructional narration | `0.9x`, natural newline breaks, normalized. |
| Audiobook Natural | Long-form fiction and nonfiction | `1.0x`, natural newline breaks, normalized. |
| Accessibility Slow | Deliberate, easy-to-follow delivery | `0.75x`, natural newline breaks, normalized. |
| Podcast Clean | Conversational spoken-word programs | `1.05x`, natural newline breaks, normalized. |

The built-in FX presets serve distinct roles. Generated speech has no microphone noise or room rumble to remove, so leave FX off for normal narration.

| FX preset | Use | Effect |
|---|---|---|
| Broadcast Voice | Briefings, announcements, and status updates | Gentle compression and limiting with a small low-frequency cut. |
| Small Room | Scripted dialogue that should sound slightly in-scene | A short, quiet room reverb. |
| Telephone | Radio, phone, or intercom lines inside a script | GSM processing with a 300 Hz to 3.4 kHz band-pass. |

Loading an FX preset changes the FX controls but does not turn on the global **Apply** setting. Enable **Apply** in the Generation tab to process a whole export. In multi-speaker text, an FX marker such as `[Narrator:Telephone]` enables that FX preset only for the marked passage.

You can switch speaker and FX presets inside text using these markers:

```text
[Narrator]: The storm passed before dawn.
[Radio:Telephone]: This is a weather update.
```

`Narrator` refers to a generation preset in `presets/`. `Telephone` refers to an FX preset in `presets/fx/`. The text after each marker uses that preset until the next marker.

## Configuration File Reference

`config.json` is persisted application state, not a required hand-edited configuration file. The application accepts unknown keys but does not use them. It keeps all documented keys listed above, including FX values, when it saves.

To reset settings, close the application and remove or rename `config.json`; the next launch recreates it with defaults. This does not remove custom voices, presets, output audio, or cached audio.
