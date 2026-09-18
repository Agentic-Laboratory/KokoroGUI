# Spoken Exec Summary

Status: approved design, not yet implemented. Written 2026-09-17.

Claude Code speaks its replies through Kokoro. Today only the last message of each turn is spoken, the spoken text is often the preamble rather than the verdict, audio is discarded the moment a newer line arrives, and nothing is kept. This spec covers three changes requested together: document the integration in this repository, make what gets spoken configurable and actually cover the whole turn, and give the audio a format, a history, a hold switch and a way to clear it.

The work spans two repositories. `/Users/echan/Code/KokoroGUI` holds the daemon, the menu bar and the engine. `/Users/echan/Code/dot-files` holds the Claude Code hook, the output style and the settings that wire them. The copies under `~/.claude/` are copies, not symlinks, and `~/.claude/settings.json` has diverged from the dot-files copy, so both need editing.

## The problem, measured

Across 546 main-thread assistant messages in 12 recent transcripts, all of them postdating the commit that made Exec Brief the default output style (dot-files `24c2d4a`, 2026-08-28):

| Bucket | Messages | Carry a marker | Rate |
|---|---|---|---|
| turn-final, over 400 characters | 72 | 26 | 36% |
| turn-final, short | 38 | 0 | 0% |
| mid-turn, over 400 characters | 44 | 0 | 0% |
| mid-turn, short | 392 | 0 | 0% |

Three separate causes sit behind "not all responses are being voiced".

The `Stop` hook fires once per turn, so every mid-turn message is structurally unreachable. That is 77% of all assistant prose.

When no marker is found, the hook speaks the *first* paragraph. In an agentic turn the first paragraph is the preamble, so the spoken line is "I'll start by checking the config" rather than the conclusion.

The daemon discards audio whenever a newer request arrives mid-synthesis, and `playback.play` interrupts whatever is already playing. Sending more lines without fixing that would make the loss worse, not better.

## What changes

The output style gains a mechanical rule: any assistant message of five or more rendered lines ends with a line starting `Exec summary:`, wherever it falls in the turn, including between tool calls. One-liners stay exempt, because 392 of 546 messages are already a single line.

The hook moves from `Stop` to `MessageDisplay`, which fires once per assistant message and hands over the text directly. All transcript parsing is deleted, because the documentation states `transcript_path` lags the in-memory conversation.

The daemon gains a FIFO queue, dedupe keyed on `message_id`, a history store in the system temporary directory, a hold switch, and a configurable output format defaulting to Ogg Vorbis.

The menu bar gains History, Hold output, Format and Clear history, all driven by the ping it already sends every two seconds.

Expected effect on the same 546 messages: 116 spoken instead of 110, but the 44 mid-turn long messages become audible for the first time, the 38 short acknowledgements stop being read aloud, and every spoken line is the part the model marked rather than a paragraph the hook guessed at.

## Decisions taken before drafting

These were settled with the user and are not open for relitigation during implementation.

The marker is a single labelled line, never a heading with a body. `MessageDisplay` delivers batches of completed whole lines, so a label can be matched inside one batch with no buffering, while a heading plus body spans batches.

`MessageDisplay` becomes the speaking path. `Stop` survives in one reduced role only: it is the flush trigger for `scope: "turn"`, described under Conflicts resolved below. Under the default `scope: "all"` the `Stop` entry sends nothing, and no message is ever spoken twice, because the two events never both send for the same message.

Dedupe lives in the daemon, keyed on `message_id`. Hook processes run in parallel and would race on any shared state.

Every synthesized line is written to history before the play-or-hold decision, so held, superseded and queue-dropped lines all stay recoverable.

Because marker compliance is 36%, the hook falls back: marker present speaks the marker; a long message without one speaks the *closing* paragraph, since the style is verdict-last; a short message without one stays silent.

Defaults: Ogg Vorbis, FIFO queue capped at five pending with the oldest dropped on overflow, history capped at 200 entries pruned oldest-first, hold state persisted as a marker file so a LaunchAgent restart does not burst-play a backlog.

## Conflicts resolved during synthesis

The four sections below were drafted in parallel by agents that could not see each other's work. Where they disagreed or left ownership unclear, this is the ruling. It overrides any contrary statement in a section.

**The history store is a new module, `kokoro_history.py`.** The daemon section specified a `HistoryStore` class; the documentation section assumed a separate module. A separate module wins, matching this repository's existing one-concern-per-module pattern (`playback.py`, `paths.py`). Its tests live in `tests/test_kokoro_history.py`, and `AGENTS.md` gains a Project Boundaries line naming it.

**The `playback.py` fix belongs to the cross-platform section.** The daemon section correctly flagged that the Ogg default silently breaks Linux and that no section owned the fix. The menu bar and cross-platform section specifies it. It is a shipping blocker for the format default, not a follow-up.

**`scope` is kept, and `Stop` is retained solely as its flush trigger.** The hook section correctly found that `scope: "turn"` has no flush trigger of its own: `MessageDisplay` carries no signal meaning "this message ends the turn", and `final: true` is per message, not per turn. The first ruling dropped the key for that reason. That was wrong, and it is reversed here. `min_lines` is a volume dial, not a mode switch: no value of it gives a user the old "speak only the turn-final verdict" behaviour, so dropping `scope` would have quietly removed the configurability that requirement 2 explicitly asked for.

Decision C is therefore amended. `MessageDisplay` is the speaking path; `Stop` is retained in one reduced role. Under `scope: "all"` the `Stop` entry reads its pending file, finds nothing to do, and exits. Under `scope: "turn"` the `MessageDisplay` handler never sends, and only writes the message to the pending file keyed on `turn_id` and `message_id`; the `Stop` handler then reads that file and sends the last message with `key: message_id`. C's original objection, that `Stop` carries no `message_id`, does not apply, because the id is in the file rather than in the payload. Both events are wired in both `settings.json` copies, and exactly one of them sends for any given message, so there is no double-speak and no text-hash dedupe.

**The hook keeps a per-message spool file, and this is a deliberate exception.** `delta` is incremental with no cumulative field, so the closing-paragraph fallback cannot be computed without accumulating until `final: true`. The spool is one file per `message_id`, deleted at `final`, pruned after an hour. It is not shared state between concurrent hook processes, so the reason dedupe was moved into the daemon does not apply. The implementer should not silently reconcile this with the "no hook-side state" rule; it is an exception on the record.

**`kokoro-ttsd` gains subcommands** for `history`, `replay`, `hold`, `release` and `purge`, matching the existing `build_parser` style. They are not socket-only.

**The menu bar sends `stop` before `replay`** so a replayed line plays immediately instead of queueing behind pending audio. `asyncio.Queue` has no front insertion and a second player would break the single-consumer invariant.

**`replace: true` bypasses the dedupe check.** A build notifier or a voice sample deliberately repeats a line. `purge` does *not* clear the dedupe key set; it is a history clean-up, not a reset.

**A fact in the drafting brief was wrong, and the correction matters.** The brief named `sf.write` at `kokoro_engine.py:549` as the audio writer. That is the *fallback*. The primary writer is `pedalboard.io.AudioFile` at line 543, inside a `try` whose `except` falls through to `sf.write`. The daemon agent verified against the installed pedalboard 0.9.25 that `AudioFile` also selects its encoder from the file extension and produces valid 24 kHz wav, ogg, mp3 and flac that `soundfile` reads back. The conclusion is unchanged and in fact stronger: no engine change is needed on either path. An implementer working from the original brief would have patched the fallback and never touched the path that actually runs.

**Two engine behaviours constrain spoken text and must not be "fixed" here.** `generate_preview` truncates each segment to 500 characters and keeps only the first two segments, so `MAX_TEXT_CHARS = 2000` is a fiction for spoken output. The hook's `max_chars` default of 400 stays below that, which is why it works today. Separately, `parse_multispeaker_text` treats `[Anything]:` as a speaker directive, so an exec summary quoting a bracketed tag or a Markdown link will be mangled. Both are pre-existing, both are out of scope, and both belong in the documentation.

## Deployment order

The old hook does not match `Exec summary:`. Landing the output style first would silently degrade every message to the opening-paragraph fallback until the hook caught up. Land the hook first, then the style.

## Out of scope

`gui.py` line 173 seeds `"lexicon": dict(DEFAULT_LEXICON)` with 32 work-specific terms, which violates the `AGENTS.md` rule against seeding work-specific lexicon rules and breaks two tests. Four further failures in `tests/test_gui_config_assembly.py` trace to commit `669d93a`. Seven tests fail on this machine today; one is the platform-gated font test this spec does fix, and the other six are pre-existing regressions left alone deliberately.

## The Claude Code hook and the output style

Everything in this section lives in `/Users/echan/Code/dot-files/claude-code/`, plus the two live copies under `~/.claude/` that are copies rather than symlinks. `~/.claude/hooks/speak-bottom-line.py` and `~/.claude/output-styles/exec-brief.md` are byte-identical to their dot-files originals today (verified with `diff`), so each of those is one edit applied twice. `~/.claude/settings.json` has diverged from `claude-code/settings.json` and gets two separate, hand-checked edits.

### 1. The MessageDisplay contract, as the binary actually declares it

The design below depends on the exact payload, so it is quoted from the installed binary at `/Users/echan/.local/share/claude/versions/2.1.274` rather than from memory. The input schema is the common hook base intersected with five fields:

| Field | Type | The binary's own description |
|---|---|---|
| `session_id`, `transcript_path`, `cwd` | string | Common to every hook event. |
| `agent_id` | string, optional | "Subagent identifier. Present only when the hook fires from within a subagent. Absent for the main thread, even in `--agent` sessions. Use this field (not `agent_type`) to distinguish subagent calls from main-thread calls." |
| `turn_id` | string | "UUID of the current turn." |
| `message_id` | string | "UUID of the assistant message being displayed. Stable across every flush of the same message. Not the API `msg_…` id." |
| `index` | int | "Zero-based index of this delta within the message. Increments by one per flush." |
| `final` | bool | "True on the message's last flush. Exactly one flush per message has it." |
| `delta` | string | "The newly completed lines since the prior flush. Always whole lines, except on the final flush which may end mid-line. The delta of the final flush is empty when the message ends on a newline; treat final as the end-of-message signal regardless." |

Three consequences drive the rest of this section.

`delta` is incremental, never cumulative. There is no field carrying the message so far. `displayContent` exists, but it is hook **output** ("Text displayed in place of the delta. Omit to display the original"), not input. Any decision that needs the whole message therefore needs the hook to accumulate it.

`final` is a reliable single end-of-message signal, and its delta may be empty or may end mid-line. So the final flush must be processed even when `delta` is `""`, and the marker scan must not require a trailing newline.

`agent_id` replaces the `isSidechain` filter that `final_assistant_text()` used. The hook returns 0 immediately when `agent_id` is present, so subagent prose is never voiced.

### 2. The output style change

#### Why the current wording produces 0 of 44 on mid-turn long messages

Line 24 of `exec-brief.md` reads:

> Every substantive reply (explanation, status, analysis, plan, review) ends with a line starting `Bottom line:` carrying the verdict in one or two sentences. It is the last line, with nothing after it. The bottom of the message is what the reader sees first, right above the input box.

Three independent signals in that one paragraph all say "turn-final", and the model obeys all three:

1. **"reply"**. A reply is what you send back when you are done. A message emitted before a batch of tool calls is not, under that reading, a reply. It is work in progress.
2. **"Bottom line"**. The label itself names the bottom of the answer. A verdict placed halfway through a turn contradicts its own name.
3. **"right above the input box"**. Only the last message of a turn sits above the input box. The rationale explicitly scopes the rule to that position.

The measurement matches exactly: 26 of 72 final long messages carry a marker, and 0 of 44 mid-turn long messages do. The failure is not weak compliance, it is correct compliance with a rule that says something narrower than intended.

#### The decision on the label

The label becomes `Exec summary:`. This is not cosmetic. "Bottom line" is one of the three turn-final signals above, and leaving it in place while telling the model to emit it mid-turn leaves the instruction fighting its own vocabulary. "Exec summary" names the style (Exec Brief) and carries no positional connotation, so it reads naturally on a message that is followed by six tool calls.

The shared regex accepts both labels, so the two files can be deployed in either order and an unmigrated style keeps working. The one ordering constraint that does matter is in section 8.

The YAML frontmatter `description` must change too. It is loaded into the model's context alongside the body and currently ends "bottom line last", which restates the positional rule that is being removed.

#### The exact replacement

Replace line 3 of `exec-brief.md`:

```
description: Twenty lines, an ASCII sketch whenever there is structure, an Exec summary line closing any message of five lines or more. Overflow detail is written to .notes/ in the current project.
```

Replace the whole of the `## Bottom line last` section, lines 22 to 26 inclusive, with:

```markdown
## Exec summary line

Any message of five or more rendered lines ends with a line starting `Exec summary:` carrying the verdict in one or two sentences.

This is a rule about messages, not about turns. It applies wherever the message falls: the message before the first tool call, every message between two batches of tool calls, and the message that ends the turn are all governed by it equally. Before sending a message, count the non-blank lines it will render as, counting the lines of a sketch or a table. Five or more, and the last line is an Exec summary.

The Exec summary is a single line and nothing follows it. Never write it as a heading with the verdict in a paragraph underneath, never split it over two lines, and never continue after it. It is extracted as one line by a tool that has only that line to work with.

Messages of four rendered lines or fewer skip the label. They are already the summary. Do not wrap one in another.

`Bottom line:` is the older spelling of the same label and is still recognised. Write `Exec summary:`.
```

Two other lines in the same file restate the old label and must follow, or the file contradicts itself:

- Line 56, in `## Structure`: "no closing summary above the bottom line" becomes "no closing summary above the Exec summary line".
- Line 63, in `## Never write`: "Closers other than the bottom line" becomes "Closers other than the Exec summary line".

#### Why a single labelled line and never a heading with a body

`delta` carries "the newly completed lines since the prior flush". A single labelled line is always wholly contained in one flush, so a regex over that one flush either matches it in full or does not see it at all. There is no partial state to hold and no case where the interesting text is split across two hook invocations.

A heading with the verdict underneath spans flushes by construction: the heading completes in flush *k* and the body in flush *k+1* or later, with no way to know at flush *k* whether more body is coming. Extracting it would force the hook to accumulate every message to `final: true` before it could speak anything, which throws away the whole latency advantage of the per-message event and makes the marker path as slow as the fallback path. The one-line rule is what makes the fast path possible.

The one-liner exemption is also load-bearing on volume. 392 of the 546 measured messages are a single line. Labelling those would roughly quintuple the number of spoken lines and would voice acknowledgements, which is the opposite of requirement 2.

### 3. The settings.json change

A matcher-less `MessageDisplay` entry is **added**, and the existing `Stop` entry is **kept**, pointing at the same script. The two Stop blocks are byte-identical today, at `claude-code/settings.json` lines 93 to 103 and `~/.claude/settings.json` lines 117 to 127, so the edit is the same JSON in both files: leave the `Stop` block as it is and add the `MessageDisplay` block beside it.

```json
    "MessageDisplay": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hooks/speak-bottom-line.py",
            "timeout": 10
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hooks/speak-bottom-line.py",
            "timeout": 10
          }
        ]
      }
    ]
```

There is no `"matcher"` key on the `MessageDisplay` entry. MessageDisplay does not support one.

The script dispatches on `hook_event_name`. Exactly one of the two events sends for any given message, so nothing is ever spoken twice and no text-hash dedupe is needed. Under the default `scope: "all"`, `MessageDisplay` sends and the `Stop` invocation finds an empty pending file and exits. Under `scope: "turn"`, `MessageDisplay` only spools and the `Stop` invocation is what sends. This is the amendment to decision C recorded under Conflicts resolved: `Stop` is not removed, it is reduced to the `scope: "turn"` flush trigger. Its `last_assistant_message` field is not used, because the spooled text and its `message_id` are already on disk.

#### On `"async": true`

**Recommendation: do not set it.** Ship the hook synchronous.

The binary confirms `async: true` with an optional `asyncTimeout` is available on command hooks. The reason not to use it is that async removes the ordering guarantee the accumulation design rests on. Synchronous MessageDisplay blocks the stream, which means flush *k*'s hook process has exited before flush *k+1* is emitted, which is what makes an append-only spool file safe without locking. Under `async: true`, two flushes of the same message can run concurrently and append out of order, and the closing paragraph is then computed from a scrambled message. The failure is silent and intermittent, which is the worst kind.

The cost of staying synchronous is small and measurable. On this machine `python3 -c 'pass'` costs about 20ms and the hook's current import set costs about 38ms. Only one flush per message ever opens a socket, and the send is fire-and-forget with a 2s timeout against a daemon that acknowledges in milliseconds, so the worst case is one 2s stall per message when the daemon is hung, not per flush. Against the 10s default timeout there is ample headroom.

Two things reduce even the 38ms. Move `import socket` and `import subprocess` out of module scope into the functions that use them, so a flush that decides to say nothing pays only the bare interpreter. And return as early as possible: the `agent_id` check and the `enabled` check come before any file I/O.

`async: true` is safe in exactly one configuration: `"speak": "exec-summary-only"`, which never accumulates and is purely a per-flush regex. Document that as an opt-in for anyone who wants marker-only speech with zero stream impact, and do not make it the default.

### 4. The hook rewrite

#### What is deleted

- `final_assistant_text()` (lines 133 to 170) in full. The official documentation states `transcript_path` "is written asynchronously and may lag the in-memory conversation", so parsing it from a hook that fires while the message is still on screen is a race by construction. The payload now carries the text directly.
- The `transcript_path` branch in `main()` (lines 314 to 319), including the "no transcript_path in Stop payload" log line.
- The `stop_hook_active` guard (lines 308 to 312) is **kept**, not deleted. It has no meaning on a `MessageDisplay` payload, where the field is absent and the guard is a no-op, but `Stop` is retained as the `scope: "turn"` flush trigger and the guard still does its original job there: it suppresses the flush on a continuation turn driven by another blocking Stop hook, where the reply on screen is not the final one.

`LEGACY_FLAG` stays. `clean_for_speech()` stays unchanged.

#### The per-flush algorithm

Marker and fallback need different handling, and the hybrid below is the point of the design.

The marker is decidable from a single flush, because the style guarantees it is one whole line. The fallback is not decidable until `final: true`, because "the closing paragraph" and "is this message five lines or more" are both properties of the finished message. So: scan every flush for the marker and send on the first match, and additionally accumulate, so that a message which never produced a marker can still be handled once at `final: true`.

```python
SPOOL_DIR = Path(tempfile.gettempdir()) / "claude-speak-spool"   # 0o700 on create

def spool_paths(message_id: str) -> tuple[Path, Path]:
    """(<id>.txt accumulated text, <id>.sent send-once flag)."""

def prune_spool(max_age_seconds: int = 3600) -> None:
    """Delete spool files older than max_age_seconds. Called only on index == 0."""

def handle_message_display(payload: dict, config: dict) -> int:
    ...
```

The body, in order:

1. `if payload.get("agent_id"): return 0` (subagent output, never voiced).
2. `message_id = payload.get("message_id") or ""`; if empty, return 0. Everything below is keyed on it.
3. `if int(payload.get("index") or 0) == 0:` truncate both spool files for this id and call `prune_spool()`. `index` is documented as the zero-based delta ordinal within the message, so this fires exactly once per message and clears any wreckage from a message that crashed before its final flush.
4. `before = text_path.read_text()` if it exists else `""`. Then append `payload.get("delta") or ""` to it.
5. **Fast path, every flush including the final one.** Skipped when `config["scope"] == "turn"` or `config["speak"] == "first-paragraph"` or the `.sent` flag exists. Otherwise `line = marker_in_delta(before, delta)`; if non-empty, `speak(line, config, key=message_id)`, then create the `.sent` flag.
6. **End of message.** `if payload.get("final"):` and the `.sent` flag does not exist, run the full ladder on the accumulated text, `spoken_line(text, config)`. If it returns text, speak it with `key=message_id` (or, under `scope: "turn"`, write the pending file described in section 5) and create the `.sent` flag. Then delete both spool files regardless.
7. Return 0. Print nothing on stdout. Emitting `displayContent` would replace what the user sees on screen, which this hook must never do.

The `.sent` flag is what makes "at most one spoken line per message" a local property rather than something the daemon has to enforce. It is written **after** `speak()` returns, whichever path spoke, so a flush that fell through to the `say` fallback because the daemon was down does not get a second `say` at `final: true`. The daemon's `message_id` dedupe (design E) remains the authority for duplicates arriving from anywhere else, and is what covers the case where the daemon restarted mid-message and the flag file was already written.

#### Why a spool file is not the hook-side state design E forbids

Design E rules out hook-side state because parallel hook processes race on it. That objection is about state shared between messages and between sessions, which is exactly what a dedupe key set is. The spool is different on all three counts, and this is a deliberate, narrow deviation from E's letter:

- One file per `message_id`, and `message_id` is a UUID, so two different messages never touch the same file.
- Only one assistant message streams at a time within a session, and MessageDisplay is synchronous, so the flushes of one message are strictly serialized by the harness. There is no concurrent writer.
- The file is deleted at `final: true`, and stale files are pruned after an hour.

Cross-session concurrency is not a hazard for the same reason: different sessions produce different `message_id`s.

#### Fence-aware marker scanning

This repository and its documentation quote `Exec summary:` inside fenced code blocks, and so does the hook's own source. A naive scan would speak a code sample.

`marker_in_delta(before: str, delta: str) -> str` computes fence parity from `before` first: count the lines of `before` whose stripped form starts with ``` ``` ```. Odd means the delta begins inside an open fence. Then walk the delta's lines in order, toggling parity on each fence line, and consider only lines at even parity. Return the text of the **last** qualifying match in the delta, or `""`.

The whole-message ladder does not need parity tracking because `strip_fences()` has already removed the fenced regions.

### 5. The fallback ladder

```python
MARKER_RE = re.compile(
    r"^\s*\**\s*(Exec summary|Bottom line)\s*:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)

def spoken_line(text: str, config: dict) -> str:
    """Pick what to say for a finished message. Empty string means stay silent."""
```

The ladder, in order:

1. `marker_line(text)` is non-empty, so return it, capped at `config["max_chars"]`.
2. No marker and `rendered_line_count(text) >= config["min_lines"]`, so return `closing_paragraph(text)`, capped.
3. Otherwise return `""`. Nothing is spoken and no socket is opened.

Mode mapping, matching the frozen contract:

| `speak` | Behaviour |
|---|---|
| `exec-summary` | The full ladder. The default. |
| `exec-summary-only` | Rung 1 only. Never accumulates, so it is the one stateless mode and the one mode where `async: true` is safe. |
| `bottom-line` | Alias of `exec-summary`. The regex matches both labels regardless, so the two are genuinely identical; retained so an unmigrated config keeps working. |
| `first-paragraph` | Legacy. Always `first_paragraph()` of the accumulated text, decided at `final: true` only, fast path skipped. |
| `bottom-line-only` | No longer a mode. Aliased at load to `exec-summary-only` with a log line. |

#### Matching the marker

Iterate the lines of `strip_fences(text)` in reverse. For each, apply `clean_for_speech(line).lstrip("# ").strip()` **first**, then match `MARKER_RE`. Return `match.group(2).strip()` on the first hit from the bottom.

The clean-then-match order is not optional and is worth preserving from the current `_bottom_line()` at line 188. The label is written both as `Exec summary:` and as `**Exec summary:**`. If the regex ran first, `\s*\**\s*` would consume the opening `**`, and group 2 would then begin with the orphaned closing `**`, which `clean_for_speech`'s `\*\*([^*]+)\*\*` rule cannot remove because there is no matching pair left. The spoken line would start "star star". Cleaning the line first turns `**Exec summary:**` into `Exec summary:` and the problem does not arise. The `\**` in the frozen regex stays as belt and braces for an unpaired `**Exec summary:` with no closing asterisks.

Scanning in reverse means the closing marker wins when a message discusses the label and then uses it.

#### Counting rendered lines

```python
def rendered_line_count(text: str) -> int:
    """Non-blank physical lines of the raw message, fences and tables included."""
```

Count physical lines of the **raw** accumulated text that contain at least one non-whitespace character. Fence delimiters, code lines, table rows and sketch lines all count, because the reader sees each of them occupy a line. Blank lines do not count, because markdown renders them as spacing and counting them would make a three-paragraph note look twice as long as it is.

This deliberately matches what the style asks the model to count ("count the non-blank lines it will render as"), so the hook's threshold and the model's threshold agree on the same text.

It is not the number of lines the terminal paints. Word wrap can only ever increase that number, so the hook systematically undercounts long-paragraph messages. The bias is one-directional and lands on the safe side: undercounting can only cause rung 2 to decline a message it would otherwise have read, and it cannot affect rung 1 at all, because a message carrying a marker is spoken regardless of length.

#### Identifying the closing paragraph

```python
def strip_fences(text: str) -> str:
    """Drop fenced regions by scanning lines and toggling on ``` markers.
    An unterminated fence drops everything from its opening line onward."""

def closing_paragraph(text: str) -> str:
    """The last block of the message that is prose rather than structure."""
```

`strip_fences` must be a line scanner, not the current `re.sub(r"```.*?```", "", text, flags=re.DOTALL)` at line 197. That regex needs a closing fence to match anything, and the hook now sees text that can legitimately end inside an open fence.

`closing_paragraph` splits the stripped text on blank lines and walks the blocks from the **end** backwards, returning `clean_for_speech()` of the first block that is not structural. Returns `""` if every block is structural, which is the correct outcome for a message that is a table and a bullet list and nothing else. Reading a table aloud helps nobody.

`_is_structural(block)` returns true when any of these hold:

- The block's first line starts with `#` (a heading), `|` (a table), or `>` (a blockquote). Same first-character test as today's `_first_paragraph` at line 199.
- Every non-empty line matches `^\s*([-*+]|\d+[.)])\s` (the block is entirely a list). This is the one place the closing-paragraph rule is deliberately stricter than reading the last thing on screen: the style's messages routinely end with a bullet list, and a list read aloud is a stream of sentence fragments. Skipping it lands on the prose paragraph above, which is the sentence the reader wants.
- The block is a single line containing no whitespace. This catches the bare `.notes/YYYY-MM-DD-topic.md` path the style puts on its own line, and bare URLs.
- The block looks like an ASCII sketch: any line contains `-->`, `<--` or `+--`, or at least half its non-empty lines contain `|`. The style mandates ASCII sketches and bans em dashes in prose, so arrow and box-drawing runs are a reliable signal and cost nothing in false positives. Most sketches are inside fences and have already been removed; this catches the unfenced ones.

`first_paragraph()` is today's `_first_paragraph` with the same predicate, walking forwards, kept only for the legacy `first-paragraph` mode. The two share `_is_structural` and differ only in iteration order.

Choosing the closing paragraph rather than the opening one is the third fix to requirement 2. Today's fallback speaks the first prose block, which in an agentic turn is the preamble ("I'll start by reading the config") and never the verdict. The style is verdict-last by construction, so the last prose block is the best available proxy when the marker is missing.

### 6. Sending

```python
def speak_via_daemon(text: str, config: dict, key: str | None = None) -> bool
def speak(text: str, config: dict, key: str | None = None) -> None
```

The request gains two fields against today's `{"command": "speak", "text": ...}` at line 219:

- `"key": key` when `key` is not `None`. This is the `message_id`, verbatim. It is the dedupe key of the frozen protocol and nothing else in the hook computes or stores it.
- `"format": config["format"]` when that is not `None`.

`voice`, `speed` and `lang` keep mapping from `voice`, `speed` and `language` exactly as they do now. `replace` and `wait` are not sent, so both take their protocol defaults of `false`: lines queue FIFO instead of interrupting, which is the point of the queue.

`--test` sends no `key`, so a test line is never swallowed as a duplicate of a previous test, and prints the daemon's reply line so `queued` and the assigned id are visible.

Forward compatibility is free here: `kokoro_daemon.py` reads only `text`, `voice`, `speed`, `lang` and `wait` from the request (lines 150 to 158) and ignores everything else, so a hook sending `key` and `format` to today's unmodified daemon behaves exactly as it does now. The hook can therefore ship before the daemon work lands.

### 7. Config, `--check`, `--test` and platform gating

#### New and changed keys

`DEFAULTS` gains three entries, ordered so that `--check`'s existing `for key in DEFAULTS` loop prints them in a sensible place:

```python
"scope": "all",          # "all" | "turn"
"format": None,          # "ogg" | "mp3" | "wav" | None (= daemon's default)
"min_lines": 5,          # non-blank lines before the closing-paragraph fallback fires
```

`"speak"` changes default from `"bottom-line"` to `"exec-summary"`, and `SPEAK_MODES` becomes `("exec-summary", "exec-summary-only", "bottom-line", "first-paragraph")` with `SPEAK_ALIASES = {"bottom-line-only": "exec-summary-only"}` applied before the membership check.

Validation added to `load_config()`, in the same style as the existing `max_chars` and `speed` handling:

- `scope` not in `("all", "turn")` logs and falls back to `"all"`.
- `format` not in `("ogg", "mp3", "wav", None)` logs and falls back to `None`.
- `min_lines` coerced with `max(1, int(...))`, logging and falling back to 5 on failure.
- `fallback == "say"` on a non-darwin platform, see below.

`load_config()` also accumulates human-readable strings in `config["_notes"]: list[str]`. Keys beginning with `_` are exempt from the unknown-key rejection at line 106, and the `--check` settings dump iterates `DEFAULTS` so `_notes` never appears there. It is printed separately.

#### Graceful degradation of the `say` fallback

`say` is macOS only. Today it is attempted on every platform and its absence surfaces as an `OSError` swallowed into the log at line 253, while `--check` still prints "falling back to `say` (Samantha)" as though it worked.

Two explicit gates:

1. In `load_config()`: `if config["fallback"] == "say" and sys.platform != "darwin":` log `config: fallback "say" needs macOS (sys.platform is <x>); using "none"`, append the same sentence to `config["_notes"]`, and set `config["fallback"] = "none"`.
2. In `speak_via_say()`: `if sys.platform != "darwin": return` as the first statement, before the `fallback` check. Defence in depth for anyone calling it directly.

`--check` then reports the fallback as unavailable and says why, instead of implying it works.

#### What `--check` reports now

Keep the current layout and add four things:

- The three new keys appear automatically in the settings dump. Alongside `speak`, print the pre-alias value when an alias was applied: `speak  "exec-summary-only"  (from "bottom-line-only")`.
- A `notes:` block printing `config["_notes"]`, one per line, omitted when empty. This is where the platform rejection and any coercion surfaces.
- A `wiring:` line. Read `~/.claude/settings.json`, walk the `hooks` object, and list the event names whose command string contains `speak-bottom-line.py`. Print `wiring : MessageDisplay`, or `wiring : MessageDisplay, Stop   (stale Stop entry, remove it)`, or `wiring : not wired to any event`. This is the single most useful migration check and costs ten lines.
- The ping reply is parsed as JSON instead of echoed raw, and printed as a block:

```
daemon : up (pid 48213)
  voice          "af_sky"
  lang           "a"
  format         "ogg"
  held           false
  queued         0
  history        37 entries, 793 KB
```

Any field the reply does not carry prints as `(not reported)` and triggers one trailing line, `daemon predates the history protocol; held/queued/history are unavailable`. That keeps `--check` useful against both the old and the new daemon during the rollout.

Also print the spool directory and the number of stale files in it, since a growing spool is the visible symptom of messages that never reach `final: true`.

`--check` keeps returning 0 in all cases, including an unreachable daemon, so anything scripted around it does not change behaviour.

#### The example config

Full replacement content for `claude-code/speak-bottom-line.json.example`, keeping the existing full-line comment style. The filename stays `speak-bottom-line.json`: `kokoro_menubar.py` line 57 hard-codes `Path.home() / ".claude" / "speak-bottom-line.json"`, and renaming buys nothing.

```
// ~/.claude/speak-bottom-line.json
//
// Controls the MessageDisplay hook that speaks each assistant message's
// `Exec summary:` line through Kokoro. Delete the file to turn speaking off
// entirely; every key below is optional and falls back to the value shown.
//
// Check what is actually in effect, and whether the daemon is answering:
//   python3 ~/.claude/hooks/speak-bottom-line.py --check
// Hear the current settings without waiting for a message:
//   python3 ~/.claude/hooks/speak-bottom-line.py --test
//
// Full-line # and // comments are stripped before parsing. Inline comments
// after a value are not: keep them on their own line.
{
  // Master switch. false keeps the file and its settings but stays silent.
  "enabled": true,

  // Which messages are eligible.
  //   "all"   every assistant message, including the ones between tool calls
  //   "turn"  only the message that ends the turn
  "scope": "all",

  // Voice name from `kokoro-tts voices`. Omit to use the daemon's default
  // (af_heart). af_* are American female, am_* American male, bf_/bm_ British.
  "voice": "af_sky",

  // Speed multiplier. 1.0 is normal; 1.15 reads briskly without distorting.
  "speed": 1.0,

  // Kokoro language code or alias: a (en-us), b (en-gb), e, f, i, p, j, z.
  "language": "a",

  // Container for the audio the daemon keeps as history. null uses the
  // daemon's own default (ogg). "wav" is the only one Linux `aplay` decodes.
  "format": null,

  // What to speak:
  //   "exec-summary"       the Exec summary line, else the closing paragraph
  //   "exec-summary-only"  the Exec summary line, else nothing
  //   "bottom-line"        the older name for "exec-summary"
  //   "first-paragraph"    always the opening paragraph
  "speak": "exec-summary",

  // How many non-blank lines a message needs before the closing-paragraph
  // fallback will read it. Matches the Exec Brief style's own threshold.
  "min_lines": 5,

  // Hard cap on the spoken text. A verdict should be a sentence or two.
  "max_chars": 400,

  // When the daemon is unreachable: "say" uses the macOS voice, so a dead
  // daemon is audible rather than silent; "none" stays quiet. "say" is
  // rejected with a logged reason on anything other than macOS.
  "fallback": "say",
  "fallback_voice": "Samantha",

  // Daemon socket. Omit to use $KOKORO_TTS_SOCKET, then the default
  // ~/Code/KokoroGUI/daemon.sock.
  "socket": null
}
```

### 8. Migration and first run

**A config that predates the new keys** needs no action. `load_config()` starts from `DEFAULTS` and overlays what the file has, so `scope`, `format` and `min_lines` take their defaults. The hook never writes the config file, and `kokoro_menubar.py` only rewrites the single lines for `enabled`, `voice` and `language`, so hand-added comments and absent keys both survive.

**`"speak": "bottom-line-only"`** is aliased to `"exec-summary-only"` at load with a log line and a `--check` note. Without the alias it would hit the unknown-mode branch at line 111 and silently become the default, turning a deliberately quiet configuration into a talkative one. **`"speak": "bottom-line"`** stays valid as a name for the full ladder.

**The legacy extensionless `~/.claude/speak-bottom-line`** keeps working exactly as it does now: when no `.json` exists, its presence sets `enabled` and everything else takes defaults. Its behaviour does change, and the change is the point of the project. Such a user goes from at most one spoken line per turn to one per qualifying message, which on the measured corpus is roughly 116 eligible messages across 12 transcripts instead of 72. The one-liner exemption keeps the other 392 silent. Anyone who wants the old shape creates a `.json` with `"scope": "turn"`, which is why that value exists, and should read the open question about it first.

**Deployment order matters in one direction.** The new hook accepts both `Exec summary:` and `Bottom line:`, but the old hook's `_bottom_line()` at line 189 matches only `Bottom line:`. So the hook ships first. Landing the output style first would leave every message unmatched and fall the old hook back to its opening paragraph, which is the bug this project exists to remove.

**The daemon can lag.** Today's daemon ignores the `key` and `format` fields (`kokoro_daemon.py` lines 150 to 158), so the hook works against it unchanged, minus dedupe and format selection. `--check` says so explicitly.

**Copying to the live tree.** `~/.claude/hooks/speak-bottom-line.py` and `~/.claude/output-styles/exec-brief.md` are copies, not symlinks, and must be re-copied after the dot-files edit. The two `settings.json` files have diverged and each needs its own edit at the line numbers given in section 3.

### 9. Documentation

`claude-code/docs/spoken-bottom-line.md` is wrong in five places after this change and is part of this section's scope: the opening sentence ("the last line of each reply"), the "What gets spoken" section, the ASCII diagram that shows `Stop hook ... reads transcript_path`, the config table, and the Files table rows that say "The Stop hook" and "Wires the hook to the `Stop` event". The KokoroGUI-side documentation of the same chain is another section's work; these two must not contradict each other on the socket protocol or the config keys.

Exec summary: the output style's `Bottom line:` becomes a position-independent `Exec summary:` on any message of five or more lines, and the hook moves from Stop to a matcher-less, synchronous MessageDisplay that scans each flush for that one line, accumulates into a per-message spool file only so the closing-paragraph fallback can be decided at `final: true`, gates subagents out on `agent_id`, and sends `message_id` as the daemon's dedupe key.

## The daemon

`kokoro_daemon.py` grows from "synthesize one line and interrupt whatever is playing" into a queued, deduplicated, persistent-history service. `kokoro_engine.py` does not change at all. This section specifies every behaviour change in the daemon, the new history store module, and the CLI subcommands that expose the new protocol commands.

### 1. What the daemon looks like after the change

Today's `SynthesisDaemon` (kokoro_daemon.py lines 69-199) does three things in one coroutine: synthesize, check whether a newer request arrived, play. The new shape splits that into an accept path that runs synchronously inside `_handle_request`, a render task per accepted line, and one consumer task that owns playback.

```
socket line -> _handle_request()          synchronous, no awaits
                 validate command/text/format
                 dedupe check on `key`
                 reserve entry id from the store
                 read the hold marker
                 build the reply dict
                 create_task(self._render(job))
              -> reply written back to the client immediately
                 (unless `wait`, which withholds it until job.done fires)

_render(job)  -> synthesize under self._synth_lock, writing directly to
                 <store>/NNNN.<fmt>
              -> store.record(job)            history written BEFORE any play decision
              -> if job.held: job.done.set(); return
                 if job.replace: playback.stop(); drain the queue
                 enqueue job (dropping the OLDEST pending on overflow)

_consume()    -> forever: job = await queue.get()
                 await asyncio.to_thread(playback.play, job.path, True)
                 job.done.set()
```

Synthesis deliberately happens in `_render`, not in the consumer. Requirement F says every synthesized line is written to history before the play/hold decision; if synthesis ran in the consumer, a line dropped by queue overflow would never have been rendered and would not be recoverable. The cost is that up to five pending jobs plus the one rendering can be in flight, but `self._synth_lock` still serializes them one at a time, so the engine sees exactly the same access pattern it sees today.

New module-level constants in `kokoro_daemon.py`, beside the existing `MAX_TEXT_CHARS = 2000` (line 41):

```python
DEFAULT_FORMAT = "ogg"
ACCEPTED_FORMATS = ("wav", "ogg", "mp3")
QUEUE_MAXSIZE = 5          # pending jobs, not counting the one being played
DEDUPE_KEYS = 200          # message_id values remembered
HISTORY_MAX_ENTRIES = 200
HISTORY_LIMIT_DEFAULT = 15
```

New constructor:

```python
def __init__(self, voice=DEFAULT_VOICE, lang=DEFAULT_LANG, speed=DEFAULT_SPEED,
             fmt=DEFAULT_FORMAT, store=None):
    self.voice = voice
    self.lang = lang
    self.speed = speed
    self.format = fmt
    self.engine = None
    self.store = store if store is not None else HistoryStore()
    self._synth_lock = asyncio.Lock()
    self._queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    self._consumer_task = None
    self._seen_keys = collections.OrderedDict()   # message_id -> None, insertion ordered
    self._shutdown = asyncio.Event()
```

`self._request_id` (line 78) is deleted outright. See section 3.

The consumer task is started lazily by `_ensure_consumer()` on the first enqueue rather than in `start()`, because the existing tests build a `SynthesisDaemon` and assign `instance.engine = object()` without ever calling `start()` (tests/test_daemon.py, the `daemon` fixture). `start()` also calls `_ensure_consumer()` so a real serve run has the consumer up before the first client connects.

```python
def _ensure_consumer(self):
    if self._consumer_task is None or self._consumer_task.done():
        self._consumer_task = asyncio.get_running_loop().create_task(self._consume())
```

`close()` additionally cancels `self._consumer_task` when it exists.

### 2. Dedupe

`MessageDisplay` fires several times for one assistant message, once per batch of completed lines, and every fire carries the same `message_id`. The hook sends that value as `key`. The daemon keeps the last `DEDUPE_KEYS = 200` keys and silently ignores a repeat.

```python
def _is_duplicate(self, key):
    """True when this key was already accepted. Records it when it is new."""
    if not key:
        return False
    if key in self._seen_keys:
        return True
    self._seen_keys[key] = None
    while len(self._seen_keys) > DEDUPE_KEYS:
        self._seen_keys.popitem(last=False)
    return False
```

`collections.OrderedDict` used as an insertion-ordered set. A hit does **not** call `move_to_end`: recency is arrival order, so a key evicts 200 accepted lines after it was first seen regardless of how many repeats it drew. A request with no `key` is never deduplicated, which keeps `kokoro-ttsd say` and build-notifier callers able to repeat the same text.

A duplicate is rejected before an id is reserved and before any file is created. The reply is `{"ok": true, "deduped": true}` with no `id`, matching the frozen protocol.

**Why the daemon and not the hook.** `MessageDisplay` hooks run as separate short-lived processes, and several can be in flight at once: batches of one message can overlap, and several Claude Code sessions share one daemon socket. Any hook-side memory of what was already spoken would have to be a file, and two hook processes reading, comparing and rewriting that file would race with no lock to arbitrate. The daemon is a single-threaded asyncio process, and `_handle_request` performs the membership test, the insert and the id reservation with no `await` between them, so the sequence is atomic against every other client by construction. There is nothing to lock.

### 3. The FIFO queue, and the removal of the supersede logic

Today `speak()` (lines 117-134) increments `self._request_id`, synthesizes, then discards its own audio if `request_id != self._request_id` (lines 129-130). That is the third silencer identified in the analysis: under `MessageDisplay` the daemon receives several lines per turn, and the current rule guarantees that only the last survives. It is deleted.

The replacement is `asyncio.Queue(maxsize=5)` holding fully rendered jobs, and one consumer:

```python
async def _consume(self):
    while True:
        job = await self._queue.get()
        try:
            await asyncio.to_thread(_playback_module().play, job.path, True)
        except Exception as error:  # noqa: BLE001 - one bad line must not kill the consumer
            _status(f"Playback failed: {error}", True)
        finally:
            job.done.set()
            self._queue.task_done()
```

`playback.play(path, True)` blocks until the line has finished, so the consumer processes one job at a time in arrival order and the next line does not start until the previous one is done.

**Overflow drops the oldest.** `asyncio.Queue.put` would block a `_render` task while holding nothing useful, so the enqueue is non-blocking:

```python
def _enqueue(self, job):
    self._ensure_consumer()
    if self._queue.full():
        dropped = self._queue.get_nowait()
        self._queue.task_done()
        dropped.done.set()
        _status(f"Queue full: dropped history entry {dropped.entry_id}.")
    self._queue.put_nowait(job)
```

The dropped job stays in `index.jsonl` with its audio file intact, because `_render` wrote history before enqueueing. It is recoverable with `replay`. `dropped.done.set()` is mandatory: a `wait: true` client whose job was dropped would otherwise never get its reply.

**`replace: true`.** Build-notifier callers want today's interrupt-immediately behaviour. `replace` is honoured at enqueue time, not after synthesis.

Per the synthesis ruling under Conflicts resolved, `replace: true` also **bypasses the dedupe check**, which reverses the order stated earlier in this section. A caller passing `replace` is deliberately repeating itself: a voice sample from the menu bar and a build notifier both resend the same text, and `kokoro_menubar.speak_sample` sends `replace: true` for exactly that reason. `_handle_request` therefore tests `request.get("replace")` before it calls `_is_duplicate`, and skips the duplicate test entirely when it is set. The key is still recorded, so a later non-replace send of the same key is still suppressed.

```python
def _drain(self):
    """Discard every pending job. Returns how many were discarded."""
    discarded = 0
    while True:
        try:
            job = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return discarded
        self._queue.task_done()
        job.done.set()
        discarded += 1
```

For a `replace` job, `_render` calls `_playback_module().stop()` and then `self._drain()` immediately before `_enqueue(job)`. `playback.stop()` bumps `playback._request_id`, which makes the consumer's in-flight `play` return early; the consumer then picks the replace job off the now-empty queue. The drained jobs remain in history.

**`stop`.** `_handle_request` for `stop` becomes `_playback_module().stop()` followed by `self._drain()`, reply `{"ok": true}` unchanged. Draining is the point: without it, cancelling the current line would immediately start the next pending one, which is not what a user pressing Stop means.

No post-synthesis "has a newer request arrived" check survives anywhere. `_request_id` exists only inside `playback.py`, where it is that module's own interrupt bookkeeping and is untouched.

### 4. Serialization: what runs where

Unchanged rule, restated because the queue makes it easy to get wrong. One `KPipeline` is not safe to drive concurrently, so every call into `engine.generate_preview` stays inside `async with self._synth_lock`. Playback must never hold that lock, or a two-second line would block the synthesis of the next one and the queue would gain nothing.

| Work | Where it runs | Holds `_synth_lock` |
| --- | --- | --- |
| Validation, dedupe, id reservation, hold read, reply construction | `_handle_request`, on the event loop, synchronously | no |
| Synthesis | `_render` -> `_synthesize` -> `engine.generate_preview` (which itself uses `asyncio.to_thread`) | yes |
| `store.record` append and prune | `_render`, on the event loop | no |
| Enqueue, drain, `playback.stop()` | `_render` and `_handle_request`, on the event loop | no |
| Playback | `_consume` -> `asyncio.to_thread(playback.play, path, True)` | no |

The store's file writes are small synchronous `open`/`write`/`close` calls on the event loop. An `index.jsonl` line is a few hundred bytes; the prune rewrite touches at most 201 lines. Both are well under a millisecond and not worth a thread.

### 5. The history store

A new module `kokoro_history.py` at the repository root, importable without torch, in the same spirit as `paths.py`. It is a front-end-agnostic store: `kokoro_menubar.py` reaches it through the socket, never directly.

```python
STORE_DIRNAME = "kokoro-ttsd"
INDEX_NAME = "index.jsonl"
HELD_NAME = "held"

def default_store_dir():
    """tempfile.gettempdir()/kokoro-ttsd - created on first use."""

class HistoryStore:
    def __init__(self, directory=None, max_entries=200):
        """directory defaults to default_store_dir(). Creates it with mode 0o700."""

    def sweep(self) -> int:
        """Delete audio files not referenced by a parseable index line. Returns the count.
        Called once at daemon start, before the first next_id()."""

    def next_id(self) -> str:
        """Reserve and return the next id, zero-padded to at least 4 digits."""

    def path_for(self, entry_id: str, fmt: str) -> str:
        """<directory>/<entry_id>.<fmt>"""

    def record(self, entry_id: str, text: str, voice: str, fmt: str, path: str) -> dict:
        """Append one entry to index.jsonl and prune to max_entries. Returns the entry."""

    def entries(self, limit: int = 15) -> list[dict]:
        """Newest first, at most `limit`. Corrupt lines are skipped."""

    def find(self, entry_id: str) -> dict | None

    def counts(self) -> tuple[int, int]:
        """(entry count, total bytes of the audio files that exist)."""

    def purge(self) -> tuple[int, int]:
        """Delete every audio file and truncate index.jsonl. Returns (removed, bytes)."""

    def held(self) -> bool
    def set_held(self, value: bool) -> bool
```

The directory is a constructor parameter so tests pass `tmp_path`. Tests must not monkeypatch `tempfile.gettempdir`.

**Layout**, matching the frozen protocol exactly:

```
tempfile.gettempdir()/kokoro-ttsd/
  index.jsonl      one JSON object per line, append-only, newest LAST
  0001.ogg 0002.ogg ...
  held             marker file; present means hold is engaged
```

**Index line shape**, identical to what the `history` reply returns per entry:

```json
{"id":"0042","text":"Exec summary: the queue now drops the oldest line.","voice":"af_sky","format":"ogg","bytes":24100,"created":"2026-09-17T10:31:02Z"}
```

`created` is UTC ISO 8601 with a trailing `Z`: `datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")`. `text` is the text as sent to the engine, after the `MAX_TEXT_CHARS` truncation. `bytes` is `os.path.getsize(path)` measured after synthesis.

**Choosing the next id after a restart.** `HistoryStore.__init__` reads `index.jsonl` if it exists, parses every line it can, and sets `self._next = max(int(entry["id"]) for entry in parsed) + 1`, or `1` when nothing parses. `sweep()` runs before the first `next_id()` so an orphaned file never influences the counter. `next_id()` returns `f"{self._next:04d}"` and increments. Ids beyond 9999 simply grow to five digits; comparison and ordering are always done on `int(entry["id"])`, never on the string, so the padding is cosmetic.

**Id gaps are legal.** `next_id()` is called at accept time so the frozen `queued`/`held` reply can carry `id`, but `index.jsonl` is only appended after synthesis succeeds. A failed synthesis consumes an id that never appears in the index. Do not write code that assumes contiguous ids.

**Pruning on insert.** `record()` appends, then counts entries; while the count exceeds `max_entries` it removes the oldest entry and `os.unlink`s its audio file with `missing_ok=True`. Because `index.jsonl` is append-only, pruning rewrites it: write the surviving lines to `index.jsonl.tmp` in the same directory and `os.replace` it over `index.jsonl`, which is atomic within one filesystem. Prune only when the count actually exceeds the cap, so the common path is a single append with no rewrite.

**Appending.** One `open(index, "a", encoding="utf-8")` and one `f.write(json.dumps(entry) + "\n")`. A single write of a short line means the only possible corruption is a truncated final line, which the reader tolerates.

**Crash safety.**

- Died after writing `NNNN.ogg`, before appending to `index.jsonl`: an orphaned audio file. The next daemon start calls `sweep()`, which lists the directory, collects the ids named by parseable index lines, and unlinks every `*.wav`/`*.ogg`/`*.mp3` not in that set. `index.jsonl`, `index.jsonl.tmp` and `held` are never swept.
- Died after appending, before the line played: the entry is in the index with its audio present. It was never spoken, and `history` lists it and `replay` plays it. This is the design working, not a fault, and it is why requirement F puts the history write before the play decision.
- Died mid-prune: either `index.jsonl.tmp` exists and `index.jsonl` is the pre-prune file (correct, just over the cap until the next insert), or the replace completed. A stale `index.jsonl.tmp` is removed by `sweep()`.

**Corrupt-line tolerance.** Every read of `index.jsonl` iterates lines, skips blank ones, and wraps `json.loads` in `try/except json.JSONDecodeError: continue`. An entry missing `id` or `format` is also skipped. Each skipped line is reported once through `_status(..., True)` at daemon start; readers during normal operation skip silently to avoid flooding stderr on every `history` call.

### 6. Hold

`hold` writes an empty marker file `<store>/held`; `release` unlinks it. The state lives on disk, not in memory, so a daemon restarted by a LaunchAgent comes back still held. `HistoryStore.held()` is `os.path.exists(self._held_path)`.

When held, a `speak` request is **not** rejected. It is validated, deduplicated, given an id, synthesized and written to history exactly as usual, and then `_render` returns without enqueueing. The reply, built at accept time, is `{"ok": true, "held": true, "id": "0042"}`.

`held` is read once, at accept time, and stored on the job. A line accepted while not held but still synthesizing when `hold` arrives will play. This is intended: the client was already told `queued`, and re-reading the marker after synthesis would make the reply a lie. A user who wants immediate silence sends `stop` as well, which drains the queue.

**`release` does not drain a backlog.** It removes the marker and nothing else, so only lines accepted after it play. The justification is the same one that put the marker on disk: hold exists so that a pile of lines accumulated while the user was away does not burst-play at them. Releasing and then hearing eleven queued summaries in a row is precisely the failure hold was added to prevent. The held lines are not lost; they are in `index.jsonl` with their audio, and the menu bar reaches them through `history` and `replay`, which is the "go back if you missed a line" path requirement 3 asks for.

### 7. The new commands

All of these are handled synchronously in `_handle_request`. Replies are verbatim the frozen protocol.

**`ping`** gains fields. Current reply at line 140 returns four keys; the new one is

```python
count, size = self.store.counts()
return {"ok": True, "pid": os.getpid(), "voice": self.voice, "lang": self.lang,
        "format": self.format, "held": self.store.held(),
        "queued": self._queue.qsize(), "history_count": count, "history_bytes": size}
```

`queued` is pending jobs only and does not count the one currently playing. The additions are backward compatible: `kokoro_menubar.display_voice` reads `voice` out of the ping reply by key and ignores unknown keys.

**`history`**. `limit = request.get("limit", 15)`; reject a non-integer or a value below 1 with `{"ok": false, "error": "limit must be a positive integer."}`. Reply `{"ok": true, "entries": [...]}` with entries newest first, each carrying `id`, `text`, `voice`, `format`, `bytes`, `created`. An empty store returns `{"ok": true, "entries": []}`, not an error.

**`replay`**. `id` is required. `{"ok": false, "error": "replay requires an id."}` when absent, `{"ok": false, "error": "No history entry 0042."}` when `store.find` misses, `{"ok": false, "error": "Audio for 0042 is gone."}` when the entry exists but its file does not. Otherwise build a job with the existing path, no synthesis, no dedupe, no id reservation, and enqueue it through `_enqueue` so it obeys the same FIFO and overflow rules. Reply `{"ok": true}`. Replay ignores the hold marker: it is an explicit user action, and refusing it while held would leave no way to hear the line the user held in order to come back to.

**`hold`** / **`release`**. `self.store.set_held(True)` / `set_held(False)`, reply `{"ok": true, "held": true}` and `{"ok": true, "held": false}`. Both are idempotent.

**`purge`**. Calls `_playback_module().stop()` and `self._drain()` first, because deleting a file a queued job points at would make the consumer play a missing path. Then `store.purge()` unlinks every audio file, sums their sizes as it goes, and truncates `index.jsonl` to empty. Reply `{"ok": true, "removed": 12, "bytes": 290100}`. The id counter is not reset, so ids stay unique across a purge and any surviving client reference cannot be confused with a new line. The `held` marker is left alone: hold is a mode, not data.

**`wait: true`.** The frozen protocol says the reply is withheld until the line has finished playing. The reply content is unchanged: the same `{"ok": true, "queued": true, "id": "0042"}` object, just sent later. `_handle_request` returns the reply dict paired with `job.done`, and `handle_client` awaits the event before writing. A deduped or held request returns immediately, because there is nothing to wait for. `handle_client`'s current collapse `reply = {"ok": await reply}` (lines 184-185) is deleted; it predates the reply shapes and would flatten `queued`/`id` away.

### 8. Format

**Serve-time default.** `serve` gains

```python
serve.add_argument("--format", choices=ACCEPTED_FORMATS,
                   default=os.environ.get("KOKORO_DAEMON_FORMAT", DEFAULT_FORMAT))
```

so precedence is `--format`, then `$KOKORO_DAEMON_FORMAT`, then `ogg`, matching how `--voice`/`--language`/`--speed` already resolve at lines 280-282. `_serve` passes it as `fmt=args.format`, and `main()`'s attribute-default loop (line 306) gains `("format", DEFAULT_FORMAT)` so `kokoro-ttsd` with no subcommand still works.

**Per request.** `format` on a `speak` request overrides the daemon default for that line only; it never mutates `self.format`.

**Validation.** Checked in `_handle_request` before an id is reserved and before any file is named:

```python
fmt = request.get("format") or self.format
if fmt not in ACCEPTED_FORMATS:
    return {"ok": False, "error": f"Unknown format: {fmt}. Use one of: wav, ogg, mp3."}
```

Accepted values are exactly `wav`, `ogg`, `mp3`. `flac` encodes and decodes correctly on this machine but is not in the frozen protocol; do not add it. Nothing is created on the rejection path, so a bad format cannot leave an orphan for `sweep()` to clean up.

**Where encoding happens, and why the engine needs no change.** `_synthesize` gains a destination path and a format, and its only job is to name the file:

```python
async def _synthesize(self, text, voice, speed, lang, path):
    """Render one line to `path`. The extension selects the encoder. True on success."""
    config = {"lexicon": {}, "normalize": True, "trim_silence": True}
    async with self._synth_lock:
        try:
            return await self.engine.generate_preview(text, voice, speed, path, config, lang_code=lang)
        except Exception as error:  # noqa: BLE001 - one bad line must not kill the daemon
            _status(f"Synthesis failed: {error}", True)
            return False
```

The `tempfile.mkstemp` at line 101 is removed from this path. `_render` passes `self.store.path_for(entry_id, fmt)`, so the file is synthesized directly into the store: same directory, no cross-filesystem move, and the id is already reserved. The one remaining `mkstemp` user is the warm-up call in `start()` (lines 90-92), which keeps writing a `.wav` to the system temp directory and unlinking it, so startup never pollutes history.

**No change is required in `kokoro_engine.py`.** The encoder is selected by the file extension in both write paths. `generate_preview` writes through `pedalboard.io.AudioFile(output_path, 'w', samplerate=24000, num_channels=1)` at kokoro_engine.py:543, and falls back to `sf.write(output_path, full_audio, 24000)` at line 549 only when that raises. Neither call takes a subtype or format argument. Verified on this machine with pedalboard 0.9.25, soundfile 0.13.1 and libsndfile 1.2.2: one second of 24 kHz mono through `AudioFile` produces 48104 bytes of wav, 3832 bytes of ogg, 21600 bytes of mp3 and a valid flac, and `soundfile.read` decodes every one of them back at 24000 Hz with the right sample count. Do not add a format parameter to `generate_preview`, and do not add a dependency; there is nothing to fix.

### 9. CLI subcommands

`build_parser()` keeps its current shape. `--socket` and `--timeout` belong **after** the subcommand, which is where every existing subparser accepts them and what `kokoro_menubar.py` already relies on (see its comment at line 357 and the command it builds at line 361).

`say` gains three options:

```python
say.add_argument("--format", choices=ACCEPTED_FORMATS, help="Override the daemon's audio format for this line.")
say.add_argument("--key", help="Dedupe key. A repeat of a key the daemon has seen is ignored.")
say.add_argument("--replace", action="store_true", help="Interrupt playback and drop pending lines instead of queueing.")
```

`main()`'s `say` branch adds `payload["format"] = args.format`, `payload["key"] = args.key` and `payload["replace"] = True` only when each is set, so an old daemon receiving a plain `say` sees the payload it sees today.

`hold`, `release` and `purge` join the existing control-command loop at lines 293-296, since each is a bare `{"command": name}` round trip:

```python
for name, help_text in (
    ("ping", "Check that a daemon is running."),
    ("stop", "Stop playback and drop pending lines."),
    ("shutdown", "Ask the daemon to exit."),
    ("hold", "Stop playing new lines. They are still synthesized and kept."),
    ("release", "Resume playing new lines. Held lines are not replayed."),
    ("purge", "Delete every stored audio file."),
):
    control = subparsers.add_parser(name, help=help_text)
    control.add_argument("--socket", help=socket_help)
    control.add_argument("--timeout", type=float, default=5.0)
```

`history` and `replay` need their own parsers because they carry arguments:

```python
history = subparsers.add_parser("history", help="List stored lines, newest first.")
history.add_argument("--limit", type=int, default=HISTORY_LIMIT_DEFAULT)
history.add_argument("--socket", help=socket_help)
history.add_argument("--timeout", type=float, default=5.0)

replay = subparsers.add_parser("replay", help="Play a stored line again by id.")
replay.add_argument("id", help="History entry id, as `kokoro-ttsd history` prints it.")
replay.add_argument("--socket", help=socket_help)
replay.add_argument("--timeout", type=float, default=5.0)
```

`main()` dispatches them before the catch-all at line 321:

```python
if command == "history":
    return _client_command({"command": "history", "limit": args.limit}, args)
if command == "replay":
    return _client_command({"command": "replay", "id": args.id}, args)
```

`_client_command` is unchanged: it prints the decoded reply as one JSON line on stdout and returns 1 when `ok` is false, which keeps the machine-readable contract AGENTS.md requires and keeps every new command scriptable from the menu bar.

### 10. Effective text limits, stated for the record

`MAX_TEXT_CHARS = 2000` caps what the daemon accepts, but `generate_preview` truncates each parsed segment to 500 characters (kokoro_engine.py:482) and keeps only the first two segments (line 471). For a normal spoken line, which has no `[Speaker]:` markers and therefore parses as one segment, the effective spoken length is **500 characters**, not 2000. Anything past that is silently dropped. This is pre-existing behaviour of a method named for previews and is out of scope here, but the hook and the docs must not promise 2000 characters of speech.

## The menu bar and cross-platform behaviour

This section specifies `kokoro_menubar.py` and `playback.py`: the four new menu items (History, Hold output, Format, Clear history), how the existing 2-second poll carries their state without a second timer, the held-state title variant, and the Linux playback bug that ogg-by-default would otherwise expose. Every function below is additive or a signature change to a function verified in the current file; nothing here touches `kokoro_daemon.py`, `kokoro_engine.py` or `gui.py` beyond one test file's skip condition.

### 1. New menu items

All four items follow the module's existing split: a plain function above the class does the logic and is unit-testable without rumps (`tests/test_menubar.py`'s stated policy), and `KokoroMenuBarApp` wires that function to a `rumps.MenuItem` and a `Snapshot` field. Two gating rules apply uniformly:

- **Config-backed** items (`Speaking`, `Voice`, and the new `Format`) stay hidden unless `snap.has_config`, exactly as `_render` already does for `self._speaking` and `self._voice` (lines 527-528).
- **Daemon-state** items (`Stop speaking now`, and the new `Hold output`, `History`, `Clear history`) are never hidden but grey their callback (`set_callback(None)`) unless `snap.status == STATUS_WARM`, exactly as `self._stop` already does (line 529). `Clear history` additionally requires `snap.history_count > 0`. History and Hold are daemon state, not hook config, so they stay visible and merely show a placeholder or grey out when the daemon is cold, they do not depend on `~/.claude/speak-bottom-line.json` existing at all.

#### History submenu

rumps' menu items cannot be rebuilt by key: `KokoroMenuBarApp.__init__` already relies on this (lines 482-484, "held as attributes, never looked up by key... assigning over an existing key is a silent no-op"), and `FakeMenuItem.__setitem__` in `tests/test_menubar.py:818-821` pins the same rule with `setdefault`. A History submenu whose fifteen rows change every poll therefore cannot be rebuilt each render; it must be fifteen fixed slots allocated once and mutated in place, the same pattern `self._error` already uses for its single line (lines 489-490, 537-538).

```python
self._history = rumps.MenuItem("History")
self._history_items = []
for index in range(15):
    item = rumps.MenuItem("", callback=None)
    item.hidden = True
    self._history[str(index)] = item
    self._history_items.append(item)
self._history_empty = rumps.MenuItem("(no history)", callback=None)
self._history["(no history)"] = self._history_empty
```

Render, called unconditionally from `_render` (cheap: 15 items, well under the 44-item voice list `_render` already walks on every voice change):

```python
def _render_history(self, snap):
    entries = snap.history
    if snap.status != STATUS_WARM:
        for item in self._history_items:
            item.hidden = True
        self._history_empty.title = "(daemon not running)"
        self._history_empty.hidden = False
        return
    if not entries:
        for item in self._history_items:
            item.hidden = True
        self._history_empty.title = "(no history)"
        self._history_empty.hidden = False
        return
    self._history_empty.hidden = True
    for index, item in enumerate(self._history_items):
        if index < len(entries):
            item.title = history_label(entries[index].get("text", ""))
            item.set_callback(self._make_history_callback(index))
            item.hidden = False
        else:
            item.hidden = True
```

`entries` comes from the `history` socket command's reply, which the frozen protocol already returns newest first, so no reordering happens here. Truncation is a pure function, testable without rumps:

```python
def history_label(text, limit=60):
    """One line for a menu row: whitespace-collapsed, truncated with an ellipsis."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"
```

A click closes over the slot's **index**, not an id, and resolves the id at click time against whatever is in the live snapshot, the entry a given slot points at can shift between the click and the worker running:

```python
def _make_history_callback(self, index):
    def callback(_sender):
        self._start_action(lambda: self._replay_worker(index))
    return callback

def _replay_worker(self, index):
    self._publish(message=None)
    entries = self._snapshot.history
    if index >= len(entries):
        return  # the menu changed under a stale click; nothing to replay
    entry_id = entries[index].get("id")
    if not replay(entry_id):
        self._publish(message="could not replay that line")
```

New socket wrapper, in the shape of `stop_playback`:

```python
def replay(entry_id, timeout=PING_TIMEOUT, socket_override=None):
    """Ask the daemon to play back one history entry by id."""
    try:
        reply = kokoro_daemon.request(
            {"command": "replay", "id": entry_id},
            path=socket_override or resolved_socket(),
            timeout=timeout,
        )
    except (OSError, ValueError):
        return False
    return isinstance(reply, dict) and bool(reply.get("ok"))
```

#### Hold output checkbox

```python
self._hold = rumps.MenuItem("Hold output", callback=self._on_toggle_hold)
```

Render:

```python
self._hold.state = 1 if snap.held else 0
self._hold.title = f"Hold output ({snap.queued} queued)" if snap.queued else "Hold output"
self._hold.set_callback(self._on_toggle_hold if snap.status == STATUS_WARM else None)
```

The queued count rides on the Hold item's own title rather than the status title, since it is only meaningful in the context of "things waiting to play once you release."

```python
def _on_toggle_hold(self, _sender):
    self._start_action(self._toggle_hold_worker)

def _toggle_hold_worker(self):
    self._publish(message=None)
    result = set_hold(not self._snapshot.held)
    if result is None:
        self._publish(message="could not update hold state")
    else:
        self._publish(held=result, message=None)
```

`_publish` (lines 551-559) gains a third optional parameter, following the same "None means unchanged" convention `status` already uses:

```python
def _publish(self, status=None, message=None, held=None):
    with self._publish_lock:
        base = self._snapshot
        self._snapshot = dataclasses.replace(
            base,
            status=base.status if status is None else status,
            held=base.held if held is None else held,
            message=message,
            stamp=time.monotonic(),
        )
```

Reporting the daemon's own `held` field back from `set_hold`, rather than the value the click assumed, keeps the checkbox correct even when something else (a LaunchAgent restart re-reading a persisted hold marker file, or another client) set hold state in between, the same reasoning `_publish_config` already applies to the config file (lines 561-566, "the checkmark follows the file, so a write that was refused never shows a change that did not land").

```python
def set_hold(held, timeout=PING_TIMEOUT, socket_override=None):
    """Engage or release hold. Returns the daemon's reported held state, or None on failure."""
    try:
        reply = kokoro_daemon.request(
            {"command": "hold" if held else "release"},
            path=socket_override or resolved_socket(),
            timeout=timeout,
        )
    except (OSError, ValueError):
        return None
    if isinstance(reply, dict) and reply.get("ok"):
        return bool(reply.get("held"))
    return None
```

#### Format submenu

Structurally identical to the Voice submenu, but flat (three items, no language grouping):

```python
self._format = rumps.MenuItem("Format")
self._format_items = {}
for value in ("wav", "ogg", "mp3"):
    item = rumps.MenuItem(value, callback=self._make_format_callback(value))
    self._format[value] = item
    self._format_items[value] = item
```

```python
def _make_format_callback(self, value):
    def callback(_sender):
        self._start_action(lambda: self._format_worker(value))
    return callback

def _format_worker(self, value):
    self._publish(message=None)
    if write_config_token("format", value, insert_if_absent=True):
        self._publish_config(None)
    else:
        self._publish_config(f"could not update {config_path()}")
```

Checkmark, mirroring `display_voice` (lines 114-125) exactly:

```python
UNKNOWN_FORMAT = "—"

def display_format(config, ping_reply):
    """The audio format that will actually be used: the config outranks the daemon."""
    for source in (config, ping_reply):
        if isinstance(source, dict):
            value = source.get("format")
            if isinstance(value, str) and value.strip():
                return value
    return UNKNOWN_FORMAT
```

**`rewrite_config_token` and the absent-key case.** The existing function (lines 157-193) refuses whenever a key claims zero non-comment lines (`test_rewrite_config_token_refuses_on_zero_matching_lines` pins this, and must keep passing unchanged). `format` is a **new** key: on an older config that predates this feature, or on the shipped `speak-bottom-line.json.example` before it is regenerated, the key will not exist yet, so the Format submenu needs an insert path, not just a rewrite path. The candidate scan is factored out so both functions share it:

```python
def _config_key_lines(lines, key_pattern):
    """Every non-comment line whose stripped body matches key_pattern."""
    candidates = []
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        if body.lstrip().startswith(("#", "//")):
            continue
        if key_pattern.match(body):
            candidates.append((index, body, line[len(body):]))
    return candidates
```

`rewrite_config_token` calls this in place of its inline loop; its refusal behavior is unchanged.

```python
def insert_config_token(text, key, value):
    """Insert `"key": value` as a new line just after the opening brace.

    Refuses unless exactly one non-comment line's stripped body is a bare
    `{` — a config whose object opens on the same line as its first key
    (a hand-collapsed one-liner) is refused rather than guessed at, the
    same convention rewrite_config_token uses for an ambiguous match.
    Indentation is copied from the first existing key line after the
    brace, or defaults to two spaces when the object is otherwise empty.
    The new line reuses the brace line's own end-of-line sequence, so a
    CRLF file stays CRLF.
    """
    if isinstance(value, (dict, list)):
        raise TypeError("insert_config_token writes JSON scalars only")

    lines = text.splitlines(keepends=True)
    brace_lines = [
        index for index, line in enumerate(lines)
        if not line.rstrip("\r\n").lstrip().startswith(("#", "//"))
        and re.match(r'^\s*\{\s*$', line.rstrip("\r\n"))
    ]
    if len(brace_lines) != 1:
        return None

    brace_index = brace_lines[0]
    brace_line = lines[brace_index]
    ending = brace_line[len(brace_line.rstrip("\r\n")):] or "\n"

    indent = "  "
    has_following_key = False
    for line in lines[brace_index + 1:]:
        body = line.rstrip("\r\n")
        if body.lstrip().startswith(("#", "//")) or not body.strip():
            continue
        if body.strip() == "}":
            break
        stripped = body.lstrip(" \t")
        indent = body[: len(body) - len(stripped)]
        has_following_key = True
        break

    comma = "," if has_following_key else ""
    new_line = f'{indent}"{key}": {json.dumps(value)}{comma}{ending}'
    lines.insert(brace_index + 1, new_line)
    return "".join(lines)
```

`write_config_token` (lines 236-238) gains the flag. It must still refuse, not insert, when the key already claims two or more lines, insertion is only correct for zero:

```python
def write_config_token(key, value, insert_if_absent=False):
    """Rewrite one key in the config file. True only when the bytes landed.

    With insert_if_absent=True, a key on zero non-comment lines is
    inserted after the opening brace instead of refusing. A key on two
    or more lines still refuses either way: ambiguity is never resolved
    by guessing.
    """
    def rewrite(text):
        updated = rewrite_config_token(text, key, value)
        if updated is not None or not insert_if_absent:
            return updated
        lines = text.splitlines(keepends=True)
        key_pattern = re.compile(_KEY_PATTERN.format(key=re.escape(key)))
        if _config_key_lines(lines, key_pattern):
            return None  # 1+ non-comment lines already claim the key
        return insert_config_token(text, key, value)

    return _rewrite_config_file(rewrite)
```

The existing call site (`write_config_token("enabled", not self._snapshot.enabled)` in `_toggle_speaking_worker`, line 687) is unaffected: it passes no third argument, so it keeps refusing on an absent `enabled` key exactly as today. `write_voice_and_language` calls `rewrite_config_token` directly, not `write_config_token`, so it is untouched by this change.

`_publish_config` (lines 561-579) gains the same carry-forward-if-unnamed treatment for format that it already gives voice:

```python
fmt=display_format(config, {"format": base.fmt}),
```

#### Clear history item

```python
self._clear_history = rumps.MenuItem(
    "Clear history (0 files, 0.0 MB)", callback=self._on_clear_history
)
```

```python
def format_megabytes(num_bytes):
    """Decimal MB, one decimal place — matches Finder's units for the same tmp directory."""
    return f"{num_bytes / 1_000_000:.1f} MB"

def clear_history_label(count, num_bytes):
    unit = "file" if count == 1 else "files"
    return f"Clear history ({count} {unit}, {format_megabytes(num_bytes)})"
```

Render:

```python
self._clear_history.title = clear_history_label(snap.history_count, snap.history_bytes)
active = snap.status == STATUS_WARM and snap.history_count > 0
self._clear_history.set_callback(self._on_clear_history if active else None)
```

Empty state is exactly `"Clear history (0 files, 0.0 MB)"`, greyed, the same convention `self._stop` uses when nothing is playing.

```python
def _on_clear_history(self, _sender):
    self._start_action(self._clear_history_worker)

def _clear_history_worker(self):
    self._publish(message=None)
    reply = purge_history()
    if reply is None:
        self._publish(message="could not clear history")
        return
    with self._publish_lock:
        base = self._snapshot
        self._snapshot = dataclasses.replace(
            base, history_count=0, history_bytes=0, history=(),
            message=None, stamp=time.monotonic(),
        )

def purge_history(timeout=PING_TIMEOUT, socket_override=None):
    """Ask the daemon to delete every stored history entry."""
    try:
        reply = kokoro_daemon.request(
            {"command": "purge"},
            path=socket_override or resolved_socket(),
            timeout=timeout,
        )
    except (OSError, ValueError):
        return None
    return reply if isinstance(reply, dict) and reply.get("ok") else None
```

Zeroing the counts directly from the purge reply, rather than waiting for the next poll, means the label updates the instant the click resolves instead of up to 2 seconds later.

### 2. Polling and the Snapshot dataclass

`Snapshot` (lines 426-436) gains six fields, all appended after the existing ones with defaults, so every positional and keyword construction of `Snapshot` already in the codebase and in `tests/test_menubar.py`'s `_bare_app` helper (lines 670-684) keeps working unchanged:

```python
@dataclasses.dataclass(frozen=True)
class Snapshot:
    status: str
    voice: str
    enabled: bool
    has_config: bool
    pid: int | None
    message: str | None
    stamp: float
    fmt: str = UNKNOWN_FORMAT
    held: bool = False
    queued: int = 0
    history_count: int = 0
    history_bytes: int = 0
    history: tuple = ()
```

`history` is a tuple, not a list, to honor the class's own "frozen so a render cannot race it" docstring, a list handed out of one snapshot could be mutated by a caller and corrupt a snapshot that is supposed to be immutable.

The extended `ping` reply already carries `held`, `queued`, `history_count`, `history_bytes` and `format` per the frozen protocol. An older daemon binary predating this feature will not send them, so every read goes through `.get(... default)`, never `[...]`. `_poll_worker` (lines 581-619) is extended, not duplicated, this is the same worker thread the existing 2-second `Timer` already spawns, so no second poll is introduced:

```python
def _poll_worker(self):
    try:
        generation = self._action_generation
        previous = self._snapshot
        config = read_config()
        reply = ping()
        child = self._child
        child_alive = child is not None and child.poll() is None
        status = derive_status(reply, child_alive, self._started_at)
        ok_reply = isinstance(reply, dict) and reply.get("ok")

        held = bool(reply.get("held", False)) if ok_reply else previous.held
        queued = reply.get("queued", 0) if ok_reply else previous.queued
        history_count = reply.get("history_count", 0) if ok_reply else previous.history_count
        history_bytes = reply.get("history_bytes", 0) if ok_reply else previous.history_bytes

        history_entries = previous.history
        if ok_reply and history_count != previous.history_count:
            hist_reply = history(limit=15)
            if isinstance(hist_reply, dict) and hist_reply.get("ok"):
                history_entries = tuple(hist_reply.get("entries", ()))

        snap = Snapshot(
            status=status,
            voice=display_voice(config, reply),
            fmt=display_format(config, reply),
            enabled=config_enabled(config),
            has_config=config is not None,
            pid=reply.get("pid") if status == STATUS_WARM else None,
            held=held,
            queued=queued,
            history_count=history_count,
            history_bytes=history_bytes,
            history=history_entries,
            message=previous.message,
            stamp=time.monotonic(),
        )
        with self._publish_lock:
            if self._action_busy or self._action_generation != generation:
                return
            self._snapshot = snap
    finally:
        self._poll_busy = False
```

```python
def history(limit=15, timeout=PING_TIMEOUT, socket_override=None):
    """Ask the daemon for its most recent entries, newest first. None on failure."""
    try:
        return kokoro_daemon.request(
            {"command": "history", "limit": limit},
            path=socket_override or resolved_socket(),
            timeout=timeout,
        )
    except (OSError, ValueError):
        return None
```

`history()` is called only when `history_count` changed since the previous snapshot, which is exactly the ticks on which a `speak`, `purge`, or `replay` from anywhere (this app, the Stop hook, a manual `kokoro-tts` invocation) could have changed the store. A tick where nothing changed costs nothing extra beyond the existing `ping`. A tick where it did changes costs one more round trip, up to `PING_TIMEOUT` (1.5s), so that single poll can take up to roughly 3s instead of a fraction of a second; `_poll_busy` already serializes polls, so this cannot stack, it can only make one tick's poll finish late.

`_render` calls `_render_history(snap)` unconditionally (cheap at 15 items) and updates the Hold checkbox, Format checkmarks (gated the same way the 44 voice items are: `if snap.fmt != self._rendered_fmt`, tracked with a new `self._rendered_fmt = None` initialized alongside `self._rendered_voice`), and the Clear history label on every render, using only fields already on the snapshot, no new I/O happens in `_render` itself, consistent with the existing rule that rendering is main-thread-only and touches no sockets or files.

### 3. The status title

`title_text` (lines 410-413) gains an optional `held` parameter, defaulting to `False` so all three existing tests (`test_title_text_exact_format_for_warm` and its neighbors) keep passing unchanged:

```python
def title_text(status, voice, held=False):
    """The whole status line, which is the menu bar title itself."""
    glyph = STATUS_GLYPHS.get(status, STATUS_GLYPHS[STATUS_STOPPED])
    suffix = ", held" if held else ""
    return f"{glyph} Kokoro ({status}, {voice or UNKNOWN_VOICE}{suffix})"
```

`● Kokoro (warm, af_sky, held)` is the held variant. No new glyph: the glyph table (`STATUS_GLYPHS`) is a liveness table (warm/starting/stopped), and held is an orthogonal axis, a held daemon is still warm, still answering pings, just not letting audio through. Overloading the glyph would conflate two different questions the menu already answers separately (Stop's greyed state answers "is anything playing," the glyph answers "is the pipeline up"). `_render` calls `self.app.title = title_text(snap.status, snap.voice, snap.held)`.

The queued-count is deliberately **not** in the title; it lives on the Hold item's own row (`Hold output (3 queued)`), since it only matters once the user has found the Hold control, and stacking two suffixes onto one title line (`, held, 3 queued`) reads worse than the existing three-field format was designed for.

### 4. Cross-platform gating

#### a. The playback.py Linux bug

`_use_aplay()` (lines 28-30) currently ignores what is actually being played:

```python
def _use_aplay() -> bool:
    """Use ALSA's PipeWire endpoint when it is available on Linux."""
    return sys.platform.startswith("linux") and shutil.which("aplay") is not None
```

`aplay` decodes WAV, AU, VOC and raw PCM only, not Ogg Vorbis, not MP3 (confirmed against `aplay`'s own format list; there is no Vorbis or MPEG decoder path in ALSA's `aplay`). With ogg as the daemon's new default format, every Linux user with `aplay` on `PATH` would have every spoken line routed into a player that cannot decode it, and would hear nothing, with the failure surfacing only as a nonzero exit code logged at `logger.error` inside `_play_with_aplay` (lines 140-146), silent from the user's chair.

The fix takes the path into account and keeps everything else in the module unchanged:

```python
def _use_aplay(path: str) -> bool:
    """Use ALSA's PipeWire endpoint for WAV on Linux; everything else falls through."""
    return (
        sys.platform.startswith("linux")
        and shutil.which("aplay") is not None
        and path.lower().endswith(".wav")
    )
```

`play()` (line 173) changes its one call site:

```python
player = _play_with_aplay if _use_aplay(path) else _play
```

Non-WAV audio on Linux now falls through to `_play`, which already reads `soundfile as sf` (imported at the top of `playback.py`, line 14) and already handles ogg and mp3 by extension the same way `kokoro_engine.py`'s `sf.write` (line 549) does on the encode side, no new dependency, no ffmpeg, matching the measured encode/readback numbers already gathered for this format (ogg 4762 B, mp3 2520 B for one second of audio, both round-tripping cleanly through `soundfile` 0.13.1 / libsndfile 1.2.2). `_play_with_aplay`'s docstring (line 115, "Play a WAV through PipeWire...") already says WAV; no change needed there beyond confirming it now reflects an enforced contract rather than an assumption.

**Test impact.** `tests/test_playback.py`'s `configure_backend` (line 26) currently does `monkeypatch.setattr(playback, "_use_aplay", lambda: False)`, a zero-argument lambda, which raises `TypeError` the instant `play()` calls `_use_aplay(path)` under the new signature. It becomes `lambda path: False`. `test_linux_playback_uses_pipewire_aplay` (line 87) does `monkeypatch.setattr(playback, "_use_aplay", lambda: True)` (line 93) and calls `playback.play("preview.wav", blocking=True)`, the filename is already `.wav`, so once the lambda takes `path` (`lambda path: True`) the test's assertions are unaffected; it is still testing the WAV/PipeWire path, just now honestly. A new test proves the fix without stubbing `_use_aplay` at all: with `sys.platform` and `shutil.which` left real (or monkeypatched to look like Linux with aplay present) and `playback.play("line.ogg", blocking=True)` called against a mocked `sounddevice` backend, `subprocess.Popen` must never be called and `backend.play` must be. A second, narrower unit test exercises `_use_aplay` directly with `sys.platform`/`shutil.which` monkeypatched, asserting `.wav`/`.WAV` are True and `.ogg`/`.mp3` are False.

#### b. The menu bar's existing gating

`kokoro_menubar.py` needs no new gating for these items. `pyproject.toml:23` already carries `"rumps==0.4.0; sys_platform == 'darwin'"`, so `rumps` itself is never installed on Linux or Windows CI. The module's whole design (documented in its own top-of-file docstring, lines 13-18) already keeps every line above `KokoroMenuBarApp` free of rumps so the module imports cleanly everywhere, and `docs/menubar.md` already documents the resulting `ModuleNotFoundError: No module named 'AppKit'` on non-macOS as expected behavior for `main()`, not a bug. History, Hold, Format and Clear history are built entirely inside `__init__` and their support functions live above the class alongside `display_voice`, `rewrite_config_token`, and friends, the same seam, so `tests/test_menubar.py` continues to test all of this on Linux and Windows runners without ever importing rumps for real, using the existing `fake_rumps` fixture (lines 848-855) for the handful of tests that do touch `KokoroMenuBarApp` itself.

#### c. The history store and TMPDIR

The store lives under `tempfile.gettempdir()`, which is cross-platform by construction. This section's code never touches that directory directly, only through the socket's `history`/`replay`/`purge` commands, so there is no TMPDIR mismatch to reconcile between the menu bar's process and the daemon's: whatever `tempfile.gettempdir()` resolves to is entirely the daemon's concern. The caveat worth documenting is that macOS periodically clears items under `$TMPDIR` that have gone unused for roughly three days, so `docs/menubar.md`'s History row should say plainly that history is a recent-session convenience, not an archive, and that a long-idle machine's history may already be empty by the time someone opens the menu. `docs/daemon.md`'s own description of the store (a different section's file) needs the same caveat next to wherever it documents the store layout, flagged here so the two docs do not end up saying different things about the same directory.

Suggested addition to the menu table in `docs/menubar.md` (new rows, same style as the existing table):

```markdown
| History | Submenu of the last 15 spoken lines, newest first, each labelled with a truncated single line of its text. Selecting one replays it. Shows "(no history)" when nothing has been spoken yet, or "(daemon not running)" when the daemon is cold. History lives under the system temp directory and is a recent-session convenience, not an archive: macOS clears items there after roughly three days idle, so do not rely on it surviving a long gap between sessions. |
| Hold output | Checkbox. When held, spoken lines are queued rather than played; queued lines are still written to history. Shows the pending count, for example "Hold output (3 queued)", once anything is waiting. Greyed out unless the daemon is warm. |
| Format | Submenu of wav / ogg / mp3. Selecting one writes `format` to the config, inserting the key if the config predates this setting. Hidden, along with Speaking and Voice, unless the config file exists. |
| Clear history (N files, X.X MB) | Sends a purge request that deletes every stored line. Greyed out when history is empty or the daemon is not warm. |
```

And a title line addendum: "`● Kokoro (warm, af_sky, held)` is the held variant. The `, held` suffix appears whenever Hold output is engaged, regardless of the glyph, since held is independent of whether the pipeline itself is warm, starting, or stopped."

#### d. The font test

`tests/test_gui_settings.py:147` asserts `rendered_family.casefold() == "liberation sans"`, a Linux-only system font, so it fails outright on macOS and on any Linux box that lacks that specific font package. A `@pytest.mark.skipif` cannot fix this correctly: `skipif`'s condition runs at collection time, before any fixture exists, and the only reliable way to know whether Liberation Sans is actually available to Tk is to ask a live Tk root with a display, exactly the `tts_app` fixture this test already receives. `TTSApp` (`gui.py:208`) subclasses `ctk.CTk`, which subclasses `tkinter.Tk`, so `tts_app` itself is a valid Tk root to query.

The fix is an in-body skip, evaluated once the fixture is live, following the same "state the platform-specific assumption, then get out of the way" spirit as the `@pytest.mark.skipif(sys.platform == "win32", ...)` convention at `tests/test_menubar.py:385`, adapted because the condition here is not the platform but font availability:

```python
def test_change_font_size_resizes_text_without_widget_scaling(tts_app, monkeypatch):
    import gui
    import tkinter.font
    families = {name.casefold() for name in tkinter.font.families(tts_app)}
    if "liberation sans" not in families:
        pytest.skip("Liberation Sans is not available to Tk on this system")
    ...
```

`tests/test_gui_settings.py` does not currently import `pytest` (only `json` and `unittest.mock`, lines 1-4), so that import needs adding alongside this change. This gates on the font actually being present rather than on `sys.platform`, so it correctly skips on a Linux box that never installed the `fonts-liberation` package too, not only on macOS and Windows.

One thing this does not fix, flagged rather than solved: `gui.py:652`'s `ui_font` requests the family `"Roboto"`, and the test's assertion is really checking fontconfig's *substitution* for a missing `Roboto`, the box happens to substitute Liberation Sans. A Linux box that has Liberation Sans installed but no fontconfig rule mapping Roboto to it would still fail even under the corrected skip. That is a pre-existing assumption baked into the test's own intent, not something this gating change is meant to address, and the brief marks the other 6 failing tests (this being one of the 7, now addressed) as out of scope beyond the platform-gating fix itself.

## Documentation and the test plan

### Where this documentation lives, and why

`dot-files/claude-code/docs/spoken-bottom-line.md` is already the correct home for the hook's own story: what triggers it, the config keys in `~/.claude/speak-bottom-line.json`, the LaunchAgents, and the `--check`/`--test` workflow for the person configuring their own machine. It reads as "how do I turn this on and tune it," and it does that well.

What is missing is the server side, told from inside the KokoroGUI repo, for a reader who has never seen the dot-files repo and is looking at `kokoro_daemon.py` wondering why a TTS daemon has a dedupe key, a FIFO queue, a history folder and a hold flag. Those features exist only because of the Claude Code integration; without an explanation in this repo they look like unexplained daemon complexity. The new file is `docs/claude-code.md`, and it documents the protocol contract and the daemon-side behavior it drives, cross-linking to `docs/daemon.md`, `docs/menubar.md` and the dot-files doc instead of restating any of them. The split is: dot-files owns the hook's code and its config keys; KokoroGUI owns the daemon's protocol surface and what the daemon does with it.

### docs/claude-code.md (new)

Section-by-section outline, matching the house voice of `docs/daemon.md`: plain declarative prose, tables for option lists, no emoji, no hard line wraps.

- **# Claude Code Integration** (opening paragraph): one paragraph stating what this integration is (Claude Code speaks selected assistant messages through the same daemon the CLI, GUI and menu bar already use) and that the hook side lives in a different repo, with the link to `spoken-bottom-line.md` given immediately so a reader who wants the client side does not have to keep reading.
- **## What Claude Code sends**: explains that Claude Code's `MessageDisplay` hook event fires once per completed batch of assistant output lines, not once per turn, and that this is why the daemon (not the hook) has to do dedupe: one logical message can arrive as several hook invocations sharing one `message_id`.
- **## Protocol fields the hook uses**: a short table of the request fields the hook actually sets (`text`, `key`, `format`, `wait`) with a one-line cross-reference to the full request/reply table in `docs/daemon.md#protocol`, so the field list is not duplicated here.
- **## The marker contract**: states the single-line rule (`Exec summary:` or `Bottom line:`, plain or bold, case-insensitive) and explains why it must be one line rather than a heading with a body: `MessageDisplay` delivers whole completed lines in batches, so a single-line marker can be regex-matched the moment its batch arrives, while a heading-plus-body would force the hook to buffer until `final: true`.
- **## The fallback ladder**: a three-row table (marker present, long message with no marker, short message with no marker) naming what gets spoken in each case and why the "long, no marker" row reads the closing paragraph rather than the opening one.
- **## Dedupe and the speak queue**: explains that dedupe is keyed on `message_id` and lives in the daemon (single-threaded asyncio, so no race between hook processes), and that speak requests queue FIFO with a small cap rather than the old newest-wins interrupt, with `replace: true` kept for callers, such as a build notifier, that still want the old behavior.
- **## History and hold**: describes the on-disk store from the operator's side: where it lives (system tmp, not the repository), that every line is recorded before the play-or-hold decision so a held, superseded or overflow-dropped line is still recoverable, and that hold survives a daemon restart because it is a marker file rather than in-memory state.
- **## Audio format and the Linux constraint**: states the ogg default, that it is configurable per line and per daemon instance, that encoding happens in `kokoro_engine.py` (via the existing extension-driven `soundfile`/`pedalboard` write path), and flags plainly that Linux's `aplay` backend in `playback.py` can only decode WAV/AU/VOC, so non-WAV formats must not be routed to `aplay` there.
- **## Verifying the chain end to end**: a numbered command sequence: ping the daemon, run the hook's own `--check`, send a `--test` line, then inspect history (`nc -U` against the socket, since `kokoro-ttsd`'s subcommand list may not yet cover the new commands, see Risks) to confirm the line landed.
- **## When nothing is spoken**: a troubleshooting table (config `enabled` false, `scope: "turn"` on a mid-turn message, message under `min_lines` with no marker, hold engaged, daemon down, queue full) each mapped to the specific check that confirms it.
- **## See also**: links to `docs/daemon.md`, `docs/menubar.md`, and `~/Code/dot-files/claude-code/docs/spoken-bottom-line.md`.

### Updates to existing KokoroGUI docs

**docs/daemon.md**

- The `## Interruption` section (current text: "Playback deliberately runs outside that lock: `playback.play` already replaces whatever is playing, so a new request interrupts an older line rather than queueing behind it. A request that is superseded while it is still synthesizing is dropped without playing...") is wrong once FIFO queueing is the default. It must say instead that a `speak` request queues behind whatever is already pending (cap 5), that the queue drops the *oldest* pending item on overflow rather than the newest, and that `replace: true` is what restores the old newest-wins interrupt for a caller, such as the build notifier, that wants it. The paragraph about synthesis being serialized behind one lock stays true and stays.
- The `## Protocol` section's request table (`command`, `text`, `voice`/`speed`/`lang`, `wait`) and its one-line reply description need the full frozen table: the `command` enum grows to `speak | ping | stop | shutdown | history | replay | hold | release | purge`; `key`, `format` are new request fields; the reply table grows past "a queued line answers `{"ok": true, "queued": true}`" to cover `deduped`, `held`, `history`, `replay`, `hold`/`release` and `purge` replies. This is the same table already frozen in this spec's protocol section; `docs/daemon.md` should reproduce it verbatim rather than a paraphrase, since it is the canonical protocol reference other docs (including the new `docs/claude-code.md`) link to.
- `## Cost` currently only names the 1.2 GB resident model. It needs a second short paragraph noting the on-disk cost: history is capped (200 entries by default, oldest pruned first) and reported via `ping`'s `history_bytes`, so the practical size is a few megabytes, not a concern on the order of the model's memory footprint, but worth naming since it is a new, if small, resource this daemon now owns.
- No change needed to `## Install`, `## Run it`, `## The socket`, or `## Running it at login on macOS`.

**docs/menubar.md**

- `## The menu` table currently lists five rows (Status line, Speaking, Voice, Stop speaking now, Restart daemon, Quit). It needs new rows: a **Format** submenu paralleling **Voice** (writes the config's `format` key the same way Voice writes `voice`), a **History** submenu (recent entries with a way to replay one), a **Hold** checkbox (mirrors the daemon's hold state, sent as `hold`/`release`), and a **Clear History** action (sends `purge` and reports the count and bytes removed, the same way **Restart daemon** reports success or failure inline). The exact labels and menu depth are a UX call for whichever section designs the menu bar changes; this doc update only needs to state that these rows exist and what each one sends.
- `## Configuration` currently describes only `enabled` and `voice`/`language` as menu-bar-writable keys. It needs `format` added to that list, using the same "every edit rewrites the value token in place" mechanism already described, since `rewrite_config_token` (kokoro_menubar.py) is generic over any scalar key and needs no new write path for this.
- `## Status glyphs` is unaffected; hold is a separate indicator, not a fourth daemon status.

**README.md**

- This is a new version's worth of user-visible change (FIFO queueing, history, hold, purge, a documented format default, and the new Claude Code doc), not an addition to the already-shipped 3.2.0 entry, so it warrants a new `## New in 3.3.0` heading above `## New in 3.2.0` rather than appending bullets to a list that already describes work that shipped in earlier commits. Suggested bullets: history and hold for spoken lines (with a link to `docs/daemon.md`), a FIFO speak queue replacing newest-wins interrupt as the daemon default, and the new `docs/claude-code.md` covering the Claude Code integration.

**AGENTS.md**

- `## Start Here` already cross-links `docs/daemon.md` and `docs/menubar.md` in one sentence ("The warm-pipeline socket service is `kokoro-ttsd`... Its menu bar front end is `kokoro-ttsmenu`..."); it needs a third clause pointing at `docs/claude-code.md` for the Claude Code integration, the same way the other two front ends are named there.
- `## Project Boundaries` needs one new bullet for whatever module ends up owning the history store (see Risks for the naming question), phrased the same way the existing bullets are: it owns the append-only index and the audio files under system tmp, and it adapts state for `kokoro_daemon.py` to call into, not a fourth synthesis path.
- `## Working Rules` needs one line stating that the history store lives under system tmp (`tempfile.gettempdir()`), never under the repository, so it needs no entry in the "do not commit generated..." bullet, but any code that resolves its location must not be pointed at a path inside the repo by a future change.

### Updates to the dot-files doc

`claude-code/docs/spoken-bottom-line.md` needs four changes:

- The `## How it fits together` diagram and the paragraph naming `Stop hook` need to become `MessageDisplay`: the diagram box that reads "reads transcript_path, takes the last main-thread assistant message, pulls out the Bottom line" is wrong on two counts once this ships, it is no longer `Stop`, and it no longer reads `transcript_path` at all (that parsing is deleted because `transcript_path` is documented as written asynchronously, per Claude Code's own docs, and racy to read from a hook that now needs to act per message). The new diagram box should read `MessageDisplay hook`, act on the payload's own `delta`/`final`/`message_id` fields, and note that it can fire more than once for a single reply.
- `## Configuration`'s key table gains `scope` (`all`/`turn`, default `all`), `format` (`ogg`/`mp3`/`wav`/`null`, default `null`), and `min_lines` (default 5), and the `speak` row's four listed values need the two new modes added (`exec-summary`, the new default, and `exec-summary-only`), with the existing `bottom-line`/`first-paragraph` values kept for backward compatibility.
- `## What gets spoken`'s opening paragraph currently says "The Exec Brief output style ends every substantive reply with a line starting `Bottom line:`"; this needs to become "any assistant message of five or more rendered lines" to match the new mechanical style rule, and the fallback description ("falls back to its opening paragraph") needs to change to "the closing paragraph" to match the corrected fallback ladder.
- The `## Files` table's first row ("`claude-code/hooks/speak-bottom-line.py` | The Stop hook.") needs "The Stop hook" corrected to "The `MessageDisplay` hook," and if the automated hook tests proposed below are adopted, a new row for `claude-code/hooks/test_speak_bottom_line.py`.

### The test plan

This is the acceptance gate. Every test below runs with a mocked pipeline or a stubbed daemon call, no model download, and no real audio device, following the mocking patterns already in `tests/test_daemon.py` (`StubPlayback`, the `daemon` fixture with a stubbed `_synthesize`, `sock_dir` for short Unix socket paths) and `tests/conftest.py` (patch the module that reads a constant, never the defining module, since `from x import Y` binds at import time).

**Daemon protocol and queue, `tests/test_daemon.py`**

| Behavior | Assertion |
|---|---|
| Dedupe ignores a repeated key | Two `speak` requests with the same `key` produce one `_synthesize` call (`len(daemon.calls) == 1`); the second reply is `{"ok": true, "deduped": true}`. |
| FIFO order preserved | Three queued `speak` requests without `replace` play in submission order, not newest-first; extend `StubPlayback` to record submission order and assert it against playback order. |
| Overflow drops the oldest | Six pending requests submitted faster than they drain (hold synthesis open with an `asyncio.Event` gate, the same pattern `test_a_newer_request_discards_the_older_audio` already uses) leave the *first* submitted line unplayed, not a later one, and the queue never exceeds 5 pending. |
| History written before the play decision | A held, a superseded, and a queue-dropped line each still produce a history append call even though `stub_playback.played` never grows for them; assert the mocked history-append function's call count independently of playback. |
| Hold suppresses playback but still records | `hold` then `speak` returns `{"ok": true, "held": true, "id": ...}`, `stub_playback.played` stays empty, and the history store gained one entry. |
| Replay by id | `{"command": "replay", "id": "0001"}` against a store seeded with that id plays its stored audio and returns `{"ok": true}`; an unknown id returns `{"ok": false, "error": ...}`. |
| Purge empties and reports counts | Seed N history entries with real placeholder files under `tmp_path`, send `purge`, assert `{"ok": true, "removed": N, "bytes": <total>}`, the files are gone, and `index.jsonl` is empty. |
| Unknown format rejected | `{"command": "speak", "text": "x", "format": "aiff"}` returns `{"ok": false, "error": ...}` naming the bad format, and neither `_synthesize` nor the history append is called. |
| Ping carries the new fields | `ping`'s reply includes `format` (str), `held` (bool), `queued` (int, the pending-queue depth, not to be confused with the boolean `queued` a `speak` ack carries), `history_count` (int) and `history_bytes` (int), alongside the existing `pid`/`voice`/`lang`. |

**History store, `tests/test_kokoro_history.py` (new)**

| Behavior | Assertion |
|---|---|
| Id allocation after restart | Seed `index.jsonl` with ids `0001`..`0005`, construct a fresh store instance against that directory (simulating a daemon restart), and assert the next allocated id is `0006`, not `0001`. |
| Pruning deletes orphaned audio | Seed more entries than the history cap; after a prune, the oldest entries' audio files no longer exist on disk and no longer appear in `index.jsonl`, while the kept entries' files remain. |
| A corrupt line in `index.jsonl` is tolerated | One line replaced with `not valid json` does not raise; the surrounding valid entries still load, mirroring the same "log and fall back" tolerance already used for a broken hook config and for a malformed daemon request. |

**Hook, `claude-code/hooks/test_speak_bottom_line.py` (new, dot-files repo)**

The dot-files repo has no pytest installed and no `conftest.py` or `pytest.ini` today (confirmed: `import pytest` fails against the system interpreter, and no test infrastructure exists anywhere under `claude-code/`). Introducing pytest as a new dependency for one hook file is a bigger decision than this section should make unilaterally, so this test file is proposed as stdlib `unittest`, runnable with `python3 -m unittest claude-code/hooks/test_speak_bottom_line.py` and nothing to install. It exercises the hook's pure functions directly (the same style already used for `_bottom_line`/`_first_paragraph`/`clean_for_speech` inside `speak-bottom-line.py`), never a live socket or a live Claude Code process.

| Behavior | Assertion |
|---|---|
| Marker regex matches both labels, plain and bold | `Exec summary: x`, `**Exec summary:** x`, `Bottom line: x`, and `**Bottom line:** x` (and lowercase variants) all extract `x`. |
| Fallback ladder picks the closing paragraph | A synthetic multi-paragraph message at or above `min_lines` with no marker resolves to its last paragraph's text, not its first. |
| A short unmarked message is silent | A message under `min_lines` with no marker produces no outgoing speak payload. |
| `message_id` is sent as `key` | A simulated `MessageDisplay` payload with `message_id: "abc123"` produces a request payload containing `"key": "abc123"`. |

**Menu bar, `tests/test_menubar.py`**

| Behavior | Assertion |
|---|---|
| New menu items render from a ping snapshot | Extending `Snapshot` with `format`, `held` and `history_count` fields, a menu built through the existing `fake_rumps` fixture (the same pattern as `test_the_menu_is_built_with_every_voice_nested_under_its_language`) shows a Format submenu entry, a checked/unchecked Hold state, and a History submenu populated from `history_count`. |
| Format write goes through the existing token rewrite and refuses cleanly | A config with exactly one `format` line writes successfully through `write_config_token("format", ...)`; a config with two `format` lines (ambiguous) and a config with no `format` line at all (absent) both refuse, mirroring the existing ambiguous/absent refusal already covered for `voice`/`language`. |

**Playback, `tests/test_playback.py`**

| Behavior | Assertion |
|---|---|
| Ogg on Linux does not go to `aplay` | With the platform gate forced on but the path ending in `.ogg`, playback routes through the `sounddevice` backend (`_play`), not `_play_with_aplay`, since `aplay` cannot decode Ogg Vorbis. |
| Wav on Linux still does | The same forced-Linux scenario with a `.wav` path still routes through `_play_with_aplay`, unchanged from today. |

**Baseline and acceptance gate**

`tests/test_daemon.py` plus `tests/test_menubar.py` runs 108 passed in 0.69s today; the new and extended tests above must all pass and that combined runtime should stay well under a few seconds, since everything here is mocked. The full suite runs 308 passed, 7 failed in about 24 minutes today; those 7 (one platform-specific font assertion, four in `test_gui_config_assembly.py`, two lexicon-related) are pre-existing and out of scope for this work, so the implementer's acceptance check is that the failure count stays at exactly 7 with the same names, not fewer (a coincidental fix is not this task's to claim) and not more.

## Files touched

The history store is `kokoro_history.py`, holding the `HistoryStore` class. The drafted sections used three different names for it; this table is the authority.

| File | New | Change |
|---|---|---|
| `/Users/echan/Code/KokoroGUI/pyproject.toml` |  | Add `kokoro_history` to the `[tool.setuptools] py-modules` list. A new top-level module that is not listed there is not installed by `pip install -e .`, so the daemon would import fine from a source checkout and fail from an install. No dependency changes: ogg and mp3 encoding both work through the pinned `soundfile==0.13.1` and the already-present `pedalboard`. |
| `/Users/echan/Code/dot-files/claude-code/output-styles/exec-brief.md` |  | Frontmatter description line 3 rewritten; the `## Bottom line last` section (lines 22-26) replaced wholesale by `## Exec summary line` with the mechanical, position-independent rule; line 56 and line 63 reworded from "bottom line" to "Exec summary line". |
| `/Users/echan/.claude/output-styles/exec-brief.md` |  | Live copy, byte-identical to the dot-files original today. Re-copy after the edit; it is a copy, not a symlink. |
| `/Users/echan/Code/dot-files/claude-code/hooks/speak-bottom-line.py` |  | Moves from Stop to MessageDisplay. Deletes final_assistant_text(), the transcript_path branch and the stop_hook_active guard. Adds handle_message_display(), the per-message spool with a .sent flag, marker_in_delta() with fence parity, strip_fences(), rendered_line_count(), closing_paragraph(), _is_structural(), the new ladder in spoken_line(), key/format on the daemon request, the scope/format/min_lines config keys with the new speak modes and aliases, darwin gating of the say fallback, and an extended --check. |
| `/Users/echan/.claude/hooks/speak-bottom-line.py` |  | Live copy, byte-identical to the dot-files original today. Re-copy after the edit; it is a copy, not a symlink. |
| `/Users/echan/Code/dot-files/claude-code/settings.json` |  | The "Stop" key at lines 93-103 is renamed to "MessageDisplay" with an identical body and no matcher key. |
| `/Users/echan/.claude/settings.json` |  | Same edit at lines 117-127. This file has diverged from the dot-files copy (extra rtk PreToolUse entry, extra top-level keys) and must be edited independently. |
| `/Users/echan/Code/dot-files/claude-code/speak-bottom-line.json.example` |  | Replaced with the version in section 7: adds scope, format and min_lines, renames the speak modes, notes the darwin-only say fallback, and drops the Stop wording from the header comment. |
| `/Users/echan/Code/dot-files/claude-code/docs/spoken-bottom-line.md` |  | Opening paragraph, "What gets spoken", the ASCII diagram (Stop hook reading transcript_path), the config table (add scope/format/min_lines, rename the speak modes) and the Files table rows naming the Stop event all rewritten for MessageDisplay. |
| `/Users/echan/Code/dot-files/claude-code/hooks/tests/test_speak_bottom_line.py` | yes | New. Pure-function and monkeypatched-socket tests for the hook. dot-files has no test infrastructure today, so this file and its conftest are the first. |
| `/Users/echan/Code/dot-files/claude-code/hooks/tests/conftest.py` | yes | New. Loads speak-bottom-line.py via importlib.util.spec_from_file_location (the filename has hyphens so it cannot be imported normally) and exposes it as a `hook` fixture. |
| `/Users/echan/Code/KokoroGUI/kokoro_daemon.py` |  | Replaces the supersede-based speak() with an accept/render/consume split: bounded dedupe set keyed on message_id, asyncio.Queue(maxsize=5) with oldest-drop overflow, single consumer task holding playback outside the synthesis lock, hold marker read at accept time, per-request and serve-time format with validation, new history/replay/hold/release/purge handlers, extended ping reply, new say options and subparsers. Deletes self._request_id, the mkstemp in _synthesize, and the {"ok": await reply} collapse in handle_client. |
| `/Users/echan/Code/KokoroGUI/kokoro_history.py` | yes | New torch-free module holding HistoryStore: store directory under tempfile.gettempdir()/kokoro-ttsd, append-only index.jsonl, zero-padded ids, startup orphan sweep, id continuity across restarts, prune to 200 oldest-first with atomic tmp-and-replace rewrite, corrupt-line tolerance, counts/purge, and the held marker file. |
| `/Users/echan/Code/KokoroGUI/kokoro_engine.py` |  | No change. Both write paths in generate_preview (pedalboard AudioFile at line 543, sf.write fallback at line 549) already select the encoder from the file extension; ogg and mp3 at 24 kHz were verified to encode and read back. |
| `/Users/echan/Code/KokoroGUI/tests/test_daemon.py` |  | The daemon fixture's fake_synthesize signature changes (it now receives a destination path and returns a bool), so every test using the fixture is touched. test_a_newer_request_discards_the_older_audio is deleted with the supersede logic it asserts, and test_waiting_requests_block_until_the_line_has_played updates to the new wait reply shape. |
| `/Users/echan/Code/KokoroGUI/tests/test_kokoro_history.py` | yes | New file covering HistoryStore in isolation against a tmp_path directory: id continuity, pruning, sweep, corrupt lines, purge, counts, held marker. |
| `/Users/echan/Code/KokoroGUI/tests/test_daemon_queue.py` | yes | New file covering queue ordering, overflow, replace, stop draining, dedupe and hold behaviour, kept separate so test_daemon.py stays the protocol-shape suite. |
| `/Users/echan/Code/KokoroGUI/docs/daemon.md` |  | Documents the new protocol commands, the format flag and environment variable, the store layout, hold semantics and the new CLI subcommands. Owned by the docs section of this spec; listed here because the daemon change is what obsoletes the current text. |
| `/Users/echan/Code/KokoroGUI/kokoro_menubar.py` |  | Add History, Hold output, Format and Clear history menu items; extend Snapshot with fmt/held/queued/history_count/history_bytes/history; add display_format, history(), replay(), set_hold(), purge_history(), history_label(), format_megabytes(), clear_history_label(); refactor rewrite_config_token's candidate scan into _config_key_lines() and add insert_config_token() plus write_config_token(insert_if_absent=); extend _poll_worker to conditionally fetch history; extend title_text(held=); extend speak_sample to send replace:true and report a held outcome. |
| `/Users/echan/Code/KokoroGUI/playback.py` |  | _use_aplay() takes the path and returns True only for sys.platform Linux + aplay present + a .wav extension; play() passes the path through; _play_with_aplay's docstring narrows to WAV. |
| `/Users/echan/Code/KokoroGUI/tests/test_playback.py` |  | Fix configure_backend and test_linux_playback_uses_pipewire_aplay for the new one-argument _use_aplay signature; add a pure _use_aplay unit test and a test that ogg/mp3 on Linux falls through to the sounddevice path. |
| `/Users/echan/Code/KokoroGUI/tests/test_menubar.py` |  | Update _bare_app and the menu-shape test for the new items and separator count; add tests for display_format, history_label, format_megabytes, clear_history_label, insert_config_token, write_config_token(insert_if_absent=True), the history/hold/format/clear-history render and click paths, the conditional history refetch in _poll_worker, the held title suffix, and speak_sample's replace:true/held outcome. |
| `/Users/echan/Code/KokoroGUI/tests/test_gui_settings.py` |  | Add `import pytest`; replace the sys.platform-based assumption in test_change_font_size_resizes_text_without_widget_scaling with an in-body skip keyed on tkinter.font.families(tts_app) actually containing Liberation Sans. |
| `/Users/echan/Code/KokoroGUI/docs/menubar.md` |  | Document the History, Hold output, Format and Clear history rows, the held title suffix, and the TMPDIR-is-not-archival caveat for history. |
| `/Users/echan/Code/KokoroGUI/docs/claude-code.md` | yes | New file. Documents the Claude Code integration from the daemon's side: MessageDisplay-per-message model, protocol fields the hook uses, the marker contract, the fallback ladder, dedupe/queueing, history and hold, the format default and Linux aplay constraint, end-to-end verification, and a troubleshooting table. Cross-links docs/daemon.md, docs/menubar.md and the dot-files hook doc rather than duplicating them. |
| `/Users/echan/Code/KokoroGUI/README.md` |  | Add a new '## New in 3.3.0' heading above '## New in 3.2.0' covering history/hold, the FIFO speak queue default, and the new docs/claude-code.md. |
| `/Users/echan/Code/KokoroGUI/AGENTS.md` |  | Add docs/claude-code.md to the Start Here cross-link sentence; add a Project Boundaries bullet for the history-store module; add a Working Rules line that the history store lives under system tmp, never the repo. |
| `/Users/echan/Code/dot-files/claude-code/hooks/test_speak_bottom_line.py` | yes | New stdlib-unittest test file (no pytest dependency introduced) covering the marker regex against plain/bold Exec summary and Bottom line forms, the closing-paragraph fallback, silence on short unmarked messages, and message_id forwarded as the request's key. |

## Test plan

| File | Assertion |
|---|---|
| `claude-code/hooks/tests/test_speak_bottom_line.py` | marker_line matches `Exec summary: x`, `**Exec summary:** x`, `## Exec summary: x` and the legacy `Bottom line: x`, and the returned text never begins with an asterisk or a hash (guards the clean-then-match ordering). |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | marker_line returns the LAST marker in a message that quotes the label before using it. |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | marker_line ignores an `Exec summary:` line that sits inside a fenced code block. |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | marker_in_delta with before='```\n' (odd fence parity) ignores a marker in the delta, and with before='```\n```\n' (even parity) returns it. |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | rendered_line_count ignores blank lines, counts fence delimiters, code lines and table rows, and returns 1 for a one-line message. |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | strip_fences removes a closed fenced block and, for an unterminated fence, drops everything from the opening line onward. |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | closing_paragraph skips a trailing bullet list, a trailing markdown table, a trailing bare `.notes/2026-09-17-topic.md` path line and a trailing unfenced ASCII sketch containing `-->`, returning the prose paragraph above; returns '' when every block is structural. |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | spoken_line: marker wins over length; a 6-line message with no marker returns the closing paragraph; a 3-line message with no marker returns ''; min_lines=2 in config makes the 3-line case speak. |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | spoken_line in exec-summary-only mode returns '' for a long message with no marker, and first-paragraph mode returns the opening prose block. |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | load_config aliases speak='bottom-line-only' to 'exec-summary-only' and records a note; rejects scope='sometimes', format='flac' and min_lines='five' back to their defaults with a note each. |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | load_config with monkeypatched sys.platform='linux' and fallback='say' sets fallback='none' and records the reason in _notes; speak_via_say returns without spawning anything (monkeypatched subprocess.Popen asserts it is never called). |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | handle_message_display over three simulated flushes of one message (index 0, 1, 2 with final=True on the last) accumulates the deltas in order into the spool and sends exactly once; both spool files are deleted after the final flush. Socket is monkeypatched to a fake recording the request JSON. |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | The recorded request carries command='speak', key equal to the payload's message_id, and format only when config['format'] is not None. |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | A marker spoken at flush 1 writes the .sent flag, so the final flush sends nothing further even though its accumulated text would also yield a fallback line. |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | A flush whose daemon send fails and falls back to `say` still writes .sent, so the final flush does not speak a second time. |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | A payload carrying agent_id returns 0 immediately, writes no spool file and opens no socket. |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | index=0 truncates a pre-existing spool file for the same message_id, and prune_spool deletes a spool file whose mtime is set two hours in the past while leaving a fresh one. |
| `claude-code/hooks/tests/test_speak_bottom_line.py` | A final flush with delta='' still runs the ladder over the accumulated text (the binary documents an empty final delta when the message ends on a newline). |
| `tests/test_daemon_queue.py` | Three speak requests play in arrival order: the stub playback records the three paths in the order the requests were accepted, proving the supersede behaviour is gone. |
| `tests/test_daemon_queue.py` | With the consumer blocked, a seventh accepted line evicts the oldest pending job; the evicted entry is still present in index.jsonl with its audio file, so an overflow drop stays replayable. |
| `tests/test_daemon_queue.py` | A speak with replace:true calls playback.stop() once, empties the pending queue, and is the next job the consumer plays. |
| `tests/test_daemon_queue.py` | The stop command calls playback.stop() and drains every pending job, so nothing plays afterwards without a new request. |
| `tests/test_daemon_queue.py` | A repeated key returns {"ok": true, "deduped": true} and reserves no id and creates no file; a request with no key is never deduplicated; the 201st distinct key evicts the first, and re-sending that first key is accepted again. |
| `tests/test_daemon_queue.py` | With the held marker present, a speak request is synthesized, appears in index.jsonl, plays nothing, and replies {"ok": true, "held": true, "id": ...}. After release, the held line is not played retroactively and only a new request plays. |
| `tests/test_daemon_queue.py` | A wait:true request whose job is dropped by overflow still receives its reply rather than hanging. |
| `tests/test_daemon.py` | ping returns pid, voice, lang, format, held, queued, history_count and history_bytes, with queued reflecting pending jobs only. |
| `tests/test_daemon.py` | history returns entries newest first, honours limit, defaults to 15, returns an empty list for an empty store, and rejects a non-positive limit. |
| `tests/test_daemon.py` | replay of a known id enqueues the stored path with no call into _synthesize; an unknown id and an entry whose audio file was deleted each return ok:false with a distinct message. |
| `tests/test_daemon.py` | purge stops playback, drains the queue, deletes every audio file, empties index.jsonl, and reports the removed count and byte total. |
| `tests/test_daemon.py` | hold and release are idempotent and return {"ok": true, "held": true\|false}. |
| `tests/test_daemon.py` | An unknown format is rejected with ok:false before any id is reserved or any file appears in the store directory; wav, ogg and mp3 are each accepted and produce a store path with the matching extension. |
| `tests/test_daemon.py` | A wait:true speak withholds the reply until the consumer finishes the line and then returns {"ok": true, "queued": true, "id": ...}. |
| `tests/test_kokoro_history.py` | A second HistoryStore over the same directory continues ids from max parsed id + 1, including when the index contains a gap left by a failed synthesis. |
| `tests/test_kokoro_history.py` | Inserting the 201st entry deletes the oldest entry's line and unlinks its audio file, leaving exactly 200 entries. |
| `tests/test_kokoro_history.py` | sweep() deletes an audio file with no index line and a stale index.jsonl.tmp, and leaves index.jsonl, held and every referenced audio file untouched. |
| `tests/test_kokoro_history.py` | A truncated final line and a line missing required keys are skipped by entries(), find() and the id scan, and the store stays usable. |
| `tests/test_kokoro_history.py` | The held marker written by one HistoryStore instance is visible to a fresh instance over the same directory, so hold survives a daemon restart. |
| `tests/test_kokoro_history.py` | counts() returns the entry count and the summed size of the audio files that still exist, ignoring entries whose file was removed. |
| `tests/test_menubar.py` | display_format prefers config['format'] over the ping reply's 'format', falls back to the ping reply when the config names none, and returns UNKNOWN_FORMAT when neither does (mirrors the three existing display_voice tests). |
| `tests/test_menubar.py` | history_label collapses internal whitespace, passes text at or under the 60-char limit through unchanged, and truncates longer text to 59 chars plus an ellipsis. |
| `tests/test_menubar.py` | format_megabytes(0) == '0.0 MB' and format_megabytes(1_000_000) == '1.0 MB' (decimal MB, one decimal place). |
| `tests/test_menubar.py` | clear_history_label(0, 0) == 'Clear history (0 files, 0.0 MB)'; clear_history_label(1, n) uses the singular 'file'; clear_history_label(12, 290100) uses the plural and format_megabytes' rendering. |
| `tests/test_menubar.py` | insert_config_token inserts a new key line directly after a bare opening-brace line, taking indentation from the first existing key line and a trailing comma only when a following key exists; refuses when the brace shares a line with other content; refuses when the opening brace line appears more than once. |
| `tests/test_menubar.py` | write_config_token('format', 'ogg', insert_if_absent=True) inserts the key into a config that has never had a 'format' line, and leaves a config with two conflicting 'format' lines untouched, still returning False. |
| `tests/test_menubar.py` | write_config_token(..., insert_if_absent=False) (the default, used by 'enabled') keeps refusing on an absent key exactly as it does today, so test_write_config_token_is_false_when_the_config_does_not_exist and the zero-match refusal test are unaffected. |
| `tests/test_menubar.py` | _render_history hides all 15 slots and shows '(daemon not running)' when status is not warm; shows '(no history)' when warm with an empty history tuple; populates slots newest-first up to len(history) and hides the remainder otherwise. |
| `tests/test_menubar.py` | Clicking a populated history slot sends {command: replay, id: <that entry's id>}; clicking a slot whose index has since fallen outside a shrunk snapshot.history is a no-op rather than sending a stale id. |
| `tests/test_menubar.py` | The Hold output item's checkbox state follows snapshot.held, its title gains a '(N queued)' suffix only when queued > 0, and its callback is None unless status is warm. |
| `tests/test_menubar.py` | Toggling Hold output publishes the held value the daemon's reply actually reports (not the value the click assumed), and publishes an error message when set_hold returns None. |
| `tests/test_menubar.py` | Format submenu items are hidden when has_config is False, and only the entry matching snapshot.fmt is checked, exactly like the existing voice-checkmark test. |
| `tests/test_menubar.py` | Clear history's callback is None when history_count is 0 or status is not warm, and set otherwise; a successful purge zeroes history_count/history_bytes/history in the snapshot without waiting for the next poll. |
| `tests/test_menubar.py` | _poll_worker calls history(limit=15) only when the new ping's history_count differs from the previous snapshot's, and reuses the previous snapshot's history tuple otherwise (asserted via a call counter on a monkeypatched history()). |
| `tests/test_menubar.py` | title_text('warm', 'af_sky', held=True) == '● Kokoro (warm, af_sky, held)'; title_text('warm', 'af_sky') (held defaulted) still equals today's string. |
| `tests/test_menubar.py` | speak_sample's payload now includes 'replace': True; a reply carrying held: true makes speak_sample return the sentinel the voice worker reports as 'saved but held', distinct from the existing ok:false-is-False and ok:true/no-held-is-True cases. |
| `tests/test_playback.py` | _use_aplay('line.wav') is True and _use_aplay('LINE.WAV') is True only on Linux with aplay present; _use_aplay('line.ogg') and _use_aplay('line.mp3') are False under the same conditions. |
| `tests/test_playback.py` | playback.play('line.ogg', blocking=True) on a Linux platform with aplay present takes the sounddevice path (Popen is never called, backend.play is called), proving ogg does not get routed through aplay even though the older test's stub made aplay look universally available. |
| `tests/test_gui_settings.py` | test_change_font_size_resizes_text_without_widget_scaling skips itself (rather than erroring) on any system where tkinter.font.families(tts_app) does not contain 'liberation sans', case-insensitively, instead of gating on sys.platform. |
| `tests/test_daemon.py` | A repeated speak key is deduped: only one _synthesize call, second reply is {ok:true, deduped:true}. |
| `tests/test_daemon.py` | Queued speak requests without replace play back in FIFO submission order. |
| `tests/test_daemon.py` | Overflow beyond the pending cap drops the oldest queued request, not the newest, and the queue never exceeds the cap. |
| `tests/test_daemon.py` | Held, superseded and overflow-dropped lines each still produce a history append call even though they never play. |
| `tests/test_daemon.py` | hold followed by speak returns held:true, suppresses playback, and still records a history entry; release restores normal playback. |
| `tests/test_daemon.py` | replay by a seeded history id plays that entry's audio; an unknown id returns ok:false. |
| `tests/test_daemon.py` | purge removes all seeded history files and reports the correct removed count and byte total. |
| `tests/test_daemon.py` | A speak request with an unsupported format is rejected without touching synthesis or history. |
| `tests/test_daemon.py` | ping's reply includes format, held, queued (pending count), history_count and history_bytes alongside pid/voice/lang. |
| `tests/test_kokoro_history.py` | Restarting the store against an existing index.jsonl with ids 0001-0005 allocates 0006 next, never reusing an id. |
| `tests/test_kokoro_history.py` | Pruning past the history cap deletes the oldest entries' audio files from disk and removes them from index.jsonl. |
| `tests/test_kokoro_history.py` | A single corrupt line in index.jsonl is skipped without raising, and the surrounding valid entries still load. |
| `claude-code/hooks/test_speak_bottom_line.py` | The marker regex matches Exec summary and Bottom line in both plain and bold form, case-insensitively, and extracts the trailing text. |
| `claude-code/hooks/test_speak_bottom_line.py` | The fallback ladder resolves an unmarked long message to its closing paragraph, not its opening paragraph. |
| `claude-code/hooks/test_speak_bottom_line.py` | An unmarked message under min_lines produces no outgoing speak payload. |
| `claude-code/hooks/test_speak_bottom_line.py` | A MessageDisplay payload's message_id is forwarded as the outgoing request's key field. |
| `tests/test_menubar.py` | A Snapshot carrying format, held and history_count renders a Format submenu entry, a Hold state and a populated History submenu via the existing fake_rumps fixture. |
| `tests/test_menubar.py` | Writing the format config key refuses cleanly (returns falsy, config file unchanged) when the key appears twice or not at all. |
| `tests/test_playback.py` | With the Linux aplay gate forced on, a .ogg path is played through the sounddevice backend, not through aplay. |
| `tests/test_playback.py` | With the same forced-Linux gate, a .wav path is still played through aplay, unchanged from current behavior. |

Baseline to compare against: `tests/test_daemon.py` plus `tests/test_menubar.py` is 108 passed in 0.69 seconds today. The full suite is 308 passed and 7 failed in 24 minutes, and those 7 are the pre-existing failures listed under Out of scope. Note that the `daemon` fixture in `tests/test_daemon.py` patches `_synthesize` with a four-argument coroutine returning a path; the new signature takes a destination and returns a bool, so every test using that fixture needs editing. Re-measure the 108-test baseline after the fixture change and before the feature work, so the two are not confused.

## Risks carried into implementation

### hook

- scope: "turn" has no flush trigger. MessageDisplay carries turn_id and message_id but nothing that says "this message ends the turn", and final:true is per message, not per turn. The only event that knows is Stop, which design C removes. I considered a detached sleep-and-check debounce and rejected it: the gap between two mid-turn messages is a tool call, and tool calls routinely exceed any tolerable debounce, so it would speak most mid-turn messages anyway, which is the opposite of what the setting promises. As specified, scope:"turn" writes a pending file keyed on turn_id and nothing consumes it. The default is "all", so the shipped path is unaffected, but scope:"turn" is non-functional until the open question below is resolved.
- The spool is hook-side state, which design E's letter forbids. I judge the deviation safe (one file per UUID message_id, flushes of one message serialized by the synchronous hook, deleted at final:true, pruned after an hour) and it is unavoidable: the binary's own schema says delta is "the newly completed lines since the prior flush" and there is no cumulative input field, so the closing-paragraph fallback cannot be computed any other way. The implementer should not reconcile this silently; it is a decision the reviewer should see.
- rendered_line_count counts source lines, not terminal-painted lines. Word wrap can only increase the painted count, so the hook undercounts and rung 2 declines slightly more messages than the style rule fires on. The bias is one-directional and towards silence, but a user with a narrow terminal and long paragraphs will see the fallback fire less often than min_lines suggests.
- Cost per flush is a fresh Python process. Measured on this machine: about 20ms for a bare interpreter and about 38ms with the hook's current imports. MessageDisplay is synchronous, so that is added stream latency on every flush of every message, not once per turn. A long message that flushes ten times costs about 0.4s. Deferring the socket and subprocess imports into their call sites recovers roughly half. If this proves annoying in practice the honest fix is a compiled or resident helper, not async:true, which breaks the spool ordering.
- I could not confirm that MessageDisplay never fires for subagent output. The agent_id gate is the documented mechanism ("Present only when the hook fires from within a subagent") and should cover it, but the event description says only "while an assistant message streams" and I did not observe a subagent turn. If subagent messages do reach the hook without agent_id, subagent prose gets voiced and the gate needs a different field.
- The exact field name of the settings entry is MessageDisplay and it appears in the binary's event list, but I did not observe the hook firing end to end. The whole design rests on the schema text quoted in section 1 rather than on a live capture. A single smoke test that logs every payload to a file for one turn should be the implementer's first step, before writing any of the ladder.
- The 546-message evidence predates the style change, so the 36% marker compliance figure describes the OLD rule. There is no measurement of what the new mechanical rule achieves, and no guarantee the model counts lines accurately enough to hit the five-line threshold consistently. If compliance on mid-turn long messages stays low, the closing-paragraph fallback carries the feature, and that path is heuristic.
- The style's own prose in exec-brief.md now tells the model that its output is being extracted by a tool. That is honest and probably helps compliance, but it is a new kind of statement for that file and the user may prefer it framed without reference to the tooling.
- Deployment order is a real constraint, not a nicety: the old hook does not match `Exec summary:`. Landing the output style before the hook silently degrades every message to the opening-paragraph fallback.
- Section 9 lists docs/spoken-bottom-line.md as in scope, but the KokoroGUI-side documentation (requirement 1) is another section's work. The socket protocol table and the config key table will exist in both repos and will drift unless one is declared canonical.

### daemon

- The brief states kokoro_engine.py:549 `sf.write(output_path, full_audio, 24000)` is the writer. It is the FALLBACK. The primary writer is `pedalboard.io.AudioFile(output_path, 'w', samplerate=24000, num_channels=1)` at line 543, inside a try whose except prints 'Preview write error' and then calls sf.write. The conclusion is unchanged and in fact stronger: I verified with the installed pedalboard 0.9.25 that AudioFile also picks the encoder from the extension and writes valid 24 kHz wav/ogg/mp3/flac that soundfile.read decodes. No engine change is needed on either path. An implementer who reads only the brief might patch the fallback and never notice the primary path.
- Linux is broken by the ogg default, and the fix is not in this section. playback._use_aplay() routes to `aplay`, which decodes WAV/AU/VOC/raw only. A Linux user on defaults gets a history file aplay cannot play. Minimal fix: in playback._play_with_aplay, when the path is not .wav, decode with sf.read (verified working for ogg and mp3 here) and hand aplay a temporary wav. Ownership is unclear because playback.py is not my section; flagged as an open question rather than specified here.
- generate_preview truncates each segment to 500 characters at kokoro_engine.py:482 and keeps only the first two segments at line 471. MAX_TEXT_CHARS = 2000 is therefore a fiction for spoken output: a marker-less closing paragraph longer than 500 characters is silently cut mid-sentence. Out of scope to change the engine, but any doc or hook that promises 2000 characters is wrong.
- generate_preview calls parse_multispeaker_text on every line (kokoro_engine.py:469). Text containing `[Anything]:` is parsed as a speaker-preset directive: the bracketed text is stripped from what is spoken and a preset lookup is attempted. An exec summary quoting a log tag, a Markdown link, or a bracketed file reference will be mangled. Pre-existing, but the daemon is about to feed it far more model-authored prose than it does today.
- On macOS tempfile.gettempdir() resolves to $TMPDIR under /var/folders, which the OS purges after roughly three days idle, and a reboot can clear it. History is ephemeral by requirement 3 ('stored in system tmp'), but the menu bar must tolerate a store directory and index.jsonl that vanished between two pings rather than raising.
- Two daemons on different sockets share one store directory and will collide on ids: each holds its own in-memory counter and both will write 0043.ogg. Either accept it (one daemon per machine is the intended deployment and _clear_stale_socket already enforces one per socket path) or key the directory by socket path. I did not specify a fix because the frozen protocol names a single fixed store path.
- The daemon fixture in tests/test_daemon.py patches _synthesize with a four-argument coroutine returning a path. The new _synthesize takes a destination path and returns a bool, so every test that uses that fixture needs editing, not just the queue tests. This is larger test churn than the feature size suggests; the 108-test daemon+menubar baseline should be re-measured after the fixture change and before the feature work.
- handle_client reads exactly one line per connection and closes. Nothing in this design changes that, but `wait: true` now holds the connection open for the whole queue wait, not just one synthesis. A client behind four pending lines can block far past the 60s default timeout on `kokoro-ttsd say --wait`. I did not add a server-side cap; the client timeout is the only bound.
- replay enqueues behind whatever is pending rather than playing immediately, which can feel unresponsive from the menu bar. asyncio.Queue offers no front insertion, and adding a second player would break the single-consumer invariant. The menu bar can send `stop` before `replay` to get immediate behaviour; that pairing is not specified anywhere and should be decided in the menu bar section.

### menubar

- speak_sample() currently sends no `replace` key, so under the new FIFO queue a voice sample queues behind pending lines instead of being heard immediately, and under hold it would get an {"ok":true,"held":true} reply that today's `bool(reply.get('ok'))` reads as plain success. This section adds `replace: true` to the payload and has _voice_worker report a distinct message when the reply carries held:true, but the daemon section owns what `replace` and `held` actually mean operationally (e.g. whether a replace-queued sample can jump a queue at all), confirm the two sections agree on the reply shape for this exact case.
- Whether `stop` drains the pending FIFO queue, and whether `replay` bypasses an engaged hold, are daemon-side behaviors this section only consumes (the 'Stop speaking now' row and the History click both assume the daemon does the sensible thing). If the daemon section decides otherwise, the doc rows in docs/menubar.md for Stop and History need to be re-worded.
- History is exposed only through the socket (`history`/`replay`/`purge`), never by reading the store directory, so a TMPDIR mismatch between the daemon's process and the menu bar's process cannot occur here, this is a property of the design, not a gap, but flag it since another section (daemon docs) also needs the same TMPDIR-is-not-archival note in docs/daemon.md's store description; this section only places it in docs/menubar.md's History row.
- insert_config_token() refuses whenever the opening brace does not sit alone on its own non-comment line (e.g. a hand-collapsed one-line config `{"voice": "af_sky"}`), matching the existing refuse-rather-than-guess convention, but it means a user with such a config cannot get 'format' auto-inserted through the menu at all and must add the line by hand once. This is a deliberate corner, not a bug, but it is a rough edge worth confirming with the user.
- format_megabytes() commits to decimal MB (bytes / 1_000_000, one decimal), matching Finder's convention on the same tmp directory rather than binary MiB. If the daemon section's own docs (docs/daemon.md) ever quote history_bytes in MiB, the two docs would disagree on arithmetic for the same number; worth a one-line cross-check once that section is written.
- The font-availability skip fixes the sys.platform mismatch but does not fix the deeper issue: gui.py's ui_font() requests the family 'Roboto' (gui.py:652), and fontconfig substitutes based on Roboto's own availability, not Liberation Sans's. A Linux box that has Liberation Sans installed but lacks a fontconfig substitution rule mapping Roboto to it would still fail the assertion even under the new skip. Flagged per the brief's instruction not to design a fix for this, only the platform-gating half.
- The condittional history() refetch in _poll_worker adds a second socket round trip (up to PING_TIMEOUT beyond the existing ping) on any tick where history_count changed since the previous snapshot; _poll_busy already prevents this from stacking across ticks, but a poll that lands on exactly the tick after a purge/speak/replay elsewhere can occasionally take close to 3s instead of the usual well under 1.5s. Worth noting in code comments so a future reader does not mistake it for a bug.
- The menu's separator layout (this spec groups Speaking/Voice/Format, then Hold/History/Clear history/Stop, then Restart) is a judgment call with no test currently pinning any particular grouping beyond a count; the exact grouping is easy to change without touching any other logic, but the test asserting `menu.count(None) == 2` needs updating to whatever grouping is actually chosen.

### docs

- The history store's module boundary is not fixed by the shared context. This section assumes a new kokoro_history.py owning index.jsonl, id allocation, pruning and the held marker file, tested in a new tests/test_kokoro_history.py, consistent with AGENTS.md's pattern of one small module per concern (playback.py, kokoro_engine.py). If the daemon-implementation section instead puts this logic directly inside kokoro_daemon.py, the test file name and the AGENTS.md Project Boundaries bullet both need to change to match.
- kokoro_daemon.py's _synthesize (verified in the source) currently hardcodes tempfile.mkstemp(..., suffix='.wav'), regardless of any requested format. For the format field and the history store's per-line extension to mean anything, this must become format-driven so kokoro_engine.py's extension-driven encoder (sf.write/AudioFile at kokoro_engine.py line 549) actually produces ogg/mp3/wav. This is a real code dependency behind two of the daemon test rows (unknown format rejected, ping's format field) and is not itself specified here since it is an implementation change, not documentation.
- playback.py's _use_aplay() (verified in the source) currently takes no arguments and is not format-aware; it only checks platform and the presence of the aplay binary. The 'ogg on Linux does not go to aplay' requirement needs this function or its caller to also gate on the file's extension. The test plan specifies the required behavior; the exact signature change belongs to whichever section edits playback.py.
- kokoro-ttsd's CLI (kokoro_daemon.py's build_parser, verified in the source) currently only defines serve/say/ping/stop/shutdown subcommands. Whether history/replay/hold/release/purge also gain matching kokoro-ttsd subcommands, or stay reachable only via raw JSON over the socket (as the menu bar and the hook would use them), is undecided; the 'Verifying the chain end to end' outline in docs/claude-code.md uses the raw nc -U form as the guaranteed path for this reason.
- The dot-files repo has no test runner today: import pytest fails against the system interpreter, and there is no conftest.py or pytest.ini anywhere under claude-code/. The proposed hook test file uses stdlib unittest specifically to avoid unilaterally adding a new dependency to that repo; if the team would rather standardize on pytest there too, that is a repo-level decision beyond this section's scope.
- docs/menubar.md's new menu row wording (Format submenu placement, History submenu depth, Hold checkbox label, Clear History wording) is specified here only at the level of 'these rows exist and what they send,' since the exact UX is a design call for whichever section owns kokoro_menubar.py changes. If that section lands on different labels or a different menu structure, docs/menubar.md's update needs to follow that instead of this section's placeholders.

## Open questions

The synthesis ruling above settles ownership of the history module, the `playback.py` fix, the `scope` key, the spool file, the CLI subcommands, replay ordering and dedupe interaction. What follows is what remains genuinely undecided.

### hook

- Should the hook keep a fixed 2.0s socket timeout now that a hung daemon stalls the on-screen stream rather than a turn boundary? The .sent flag bounds it to one stall per message, so 2.0s survives, but a lower value (1.0s) costs nothing when the daemon is healthy and halves the visible hang when it is not.
- dot-files has no test infrastructure at all: no tests/, no conftest.py, no pytest config, no pyproject.toml. Adding the first one means deciding where tests live, how they are run (a line in AGENTS.md, a rebuild-script target, or nothing) and whether pytest is an acceptable dependency for a dotfiles repo. The alternative is a self-testing `--selftest` flag on the hook itself, which needs no dependency but is less conventional.
- Does the Exec summary line belong on a message that is entirely a code block or a table, where five lines is trivially exceeded but there is no verdict to give? The style as written says yes. Exempting structure-only messages would reduce noise but adds a judgement call to a rule whose whole value is being mechanical.
- min_lines is duplicated: the number 5 lives in the output style prose and in the config default, and they must agree or the hook's fallback fires on messages the style never labelled. Nothing enforces that. Worth a comment in each file pointing at the other, or accepting the drift.

### daemon

- Should the store directory be keyed per socket path so two daemons cannot collide on ids, at the cost of the menu bar no longer knowing one fixed path? The frozen protocol says one fixed directory, so changing this needs the protocol reopened.
- Is there a size cap as well as the 200-entry cap? 200 ogg lines is roughly 1 MB and harmless, but 200 wav lines at the same length is around 10 MB in a directory the OS may not purge promptly. history_bytes is already in the ping reply, so a byte cap would be cheap to add if wanted.

### menubar

- Should the History submenu's 'daemon not running' placeholder still be reachable/visible when has_config is False (no speak-bottom-line.json at all)? This spec keeps History/Hold/Clear history visible regardless of has_config, since they reflect the daemon's own audio state rather than the hook's config, confirm that reading is what the user wants, since it diverges from Voice/Format's has_config gating.
- Should 'Clear history' ask for confirmation before sending purge, given it is destructive and irreversible? This spec omits a confirmation dialog to match the existing one-click convention used by 'Stop speaking now' and 'Restart daemon', but purge is the first genuinely destructive action in this menu.
- Exact wording for the three new error messages ('could not replay that line', 'could not update hold state', 'could not clear history') follows the existing terse style of 'could not stop playback' and 'daemon did not stop' but was not specified by the brief; confirm the wording before implementation locks it into tests.

### docs

- Should the dot-files repo adopt pytest for hook testing going forward, or is a single stdlib-unittest file the right scope for this change? Affects whether claude-code/docs/spoken-bottom-line.md's Files table gains a test-file row and whether a broader test-runner setup belongs in this task at all.
- Exact labels, order and submenu depth for the new kokoro-ttsmenu rows (Format, History, Hold, Clear History) are left to the menu bar/GUI design section; docs/menubar.md's update in this spec only fixes what must be true (rows driven by the ping snapshot, format written through the existing token rewrite) rather than final wording.

