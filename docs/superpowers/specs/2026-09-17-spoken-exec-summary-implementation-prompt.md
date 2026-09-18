# Implementation prompt: Spoken Exec Summary

Paste the block under "The prompt" into a fresh Claude Code session started from `/Users/echan/Code/KokoroGUI`. Everything above and below it is context for you, not for the next session.

Design is settled and written up in `docs/superpowers/specs/2026-09-17-spoken-exec-summary-design.md`. That spec is the contract. This file only says how to execute it.

## Before you paste

Run the daemon at least once so the next session is not debugging a cold environment on top of a feature:

```bash
cd /Users/echan/Code/KokoroGUI && .venv/bin/kokoro-ttsd ping
```

Nothing else is needed. The next session does its own baseline measurement as step one.

## The prompt

> Implement the spec at `docs/superpowers/specs/2026-09-17-spoken-exec-summary-design.md`. Read it in full before touching anything. It is an approved design: do not redesign it, and do not relitigate the decisions recorded under "Decisions taken before drafting" and "Conflicts resolved during synthesis". Where a drafted section contradicts the Conflicts resolved ruling, the ruling wins.
>
> Orchestrate this. Use a Workflow with four subagents in git worktree isolation, because they edit overlapping files and will collide otherwise. Assign opus to the daemon and to the hook, sonnet to the menu bar and to the documentation. Use the fable advisor before you commit to the execution order and again before you declare it done. Hold the plan and the synthesis in the main thread; do not let a subagent decide anything the spec already decided.
>
> **Step zero, before any agent runs and before any code is written.** Nobody has observed a `MessageDisplay` payload fire. The entire hook design rests on the schema quoted in the spec rather than on a live capture. Wire a throwaway hook that appends every `MessageDisplay` payload to a scratch file, restart a session, take one turn, and inspect what actually arrives: confirm `delta`, `index`, `final`, `message_id` and `turn_id` are present and shaped as the spec assumes, confirm whether it fires for subagent output, and confirm whether `agent_id` is the right gate for that. If the payload differs from the spec, stop and report before proceeding. This is a fifteen minute check that protects the largest single assumption in the work.
>
> **Order matters.** Land the hook before the output style. The current hook does not match `Exec summary:`, so landing the style first silently degrades every message to the opening-paragraph fallback. The `playback.py` Ogg and MP3 fix must land before the daemon's format default changes from wav, or Linux playback breaks silently: `aplay` decodes WAV, AU, VOC and raw only.
>
> **Two repositories.** `/Users/echan/Code/KokoroGUI` and `/Users/echan/Code/dot-files`. The files under `~/.claude/` are copies, not symlinks, so every dot-files change has to be mirrored there to take effect, and `~/.claude/settings.json` has diverged from the dot-files copy so both need the new hook entries. Do not commit anything in either repository. Do not run `git commit`. Configuration changes in particular are to be shown and left for review.
>
> **Acceptance gates, all four, with output shown:**
> 1. `.venv/bin/python -m pytest -q tests/test_daemon.py tests/test_menubar.py tests/test_history_store.py tests/test_daemon_queue.py` passes. Today's baseline for the first two files alone is 108 passed in 0.69 seconds. Note that the `daemon` fixture patches `_synthesize` with a four-argument coroutine returning a path, and the new signature takes a destination and returns a bool, so that fixture changes and the baseline must be re-measured after the fixture change and before the feature work, so the two are not confused with each other.
> 2. `.venv/bin/python -m pytest -q tests/test_playback.py` passes, including a new test pinning that Ogg on Linux does not go to `aplay` and that WAV on Linux still does.
> 3. The dot-files hook test file runs and passes. That repository has no test runner, no `conftest.py` and no `pyproject.toml`; use a stdlib `unittest` file so no dependency is added to a dotfiles repository. This is decided, do not spend a turn rediscovering it.
> 4. The full suite still shows exactly the seven known failures and no new ones: `.venv/bin/python -m pytest -q` gives 308 passed and 7 failed today, and it takes about 24 minutes, so run it once at the end rather than iterating on it. One of the seven, the Liberation Sans font assertion in `tests/test_gui_settings.py`, is fixed by this work and should become a skip, so expect 6 failures and 1 skip when you are done.
>
> **Out of scope, deliberately.** Do not fix the other six failing tests. Two trace to `gui.py` line 173 seeding `"lexicon": dict(DEFAULT_LEXICON)` with 32 work-specific terms, which does violate `AGENTS.md`, and four trace to commit `669d93a`. They are pre-existing, they are recorded in the spec under "Out of scope", and touching them would make this change impossible to review. Do not change `config.json`.
>
> Report what you changed, what you verified with output rather than assertion, and anything in the spec that turned out to be wrong when it met the code.

## What the next session should already know

These are settled. If the session raises them as open questions, it has not read the spec.

| Question | Ruling |
|---|---|
| Where does the history store live | New module `kokoro_history.py` holding `HistoryStore`, added to `py-modules` in `pyproject.toml` |
| Who fixes `playback.py` for Ogg on Linux | The cross-platform section owns it, and it blocks the format default |
| Is `scope` shipped | Yes. `Stop` is retained solely as the `scope: "turn"` flush trigger |
| Does `replace: true` bypass dedupe | Yes. `purge` does not clear the dedupe key set |
| Does the menu bar `stop` before `replay` | Yes, so a replayed line plays immediately |
| Does `kokoro-ttsd` gain subcommands | Yes, for `history`, `replay`, `hold`, `release` and `purge` |
| Test runner for dot-files | Stdlib `unittest`, no new dependency |

## Two things the user should hear before implementation starts

**Stream latency.** `MessageDisplay` is synchronous and fires once per batch of completed lines, not once per message. Measured cost is 20 to 38 milliseconds per flush for a fresh Python interpreter with the hook's imports, so a message that flushes ten times adds roughly 0.4 seconds of stream latency. Deferring the `socket` and `subprocess` imports into their call sites recovers about half of that. `async: true` is available and would remove the cost entirely, but it breaks the spool ordering the closing-paragraph fallback depends on, so the spec recommends against it. If the latency proves annoying in practice the honest fix is a resident or compiled helper, not `async`.

**Compliance is unmeasured.** The 36% marker rate describes the old rule on turn-final messages. There is no measurement of what the new mechanical five-line rule achieves, and no guarantee the model counts rendered lines accurately enough to hit the threshold consistently. If compliance on mid-turn messages stays low, the closing-paragraph fallback carries the feature, and that path is a heuristic rather than a contract. The measurement script used to produce the original numbers is worth re-running a day after the style lands.
