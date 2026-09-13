"""Tests for _process_jit_async, the real-time/JIT pipeline
(kokoro_engine.py:747-908). caching=False throughout via make_config."""
import asyncio
import threading

import numpy as np

import kokoro_engine


def test_jit_conversion_writes_combined_jit_output_file(engine, fake_pipeline, make_config, isolated_dirs):
    config = make_config(filename="jitrun", time_id="1")
    asyncio.run(engine._process_jit_async("Hello world. This is JIT.", config))

    assert (isolated_dirs.out_dir / "jitrun_1_jit_output.wav").exists()


def test_jit_conversion_cancel_writes_remaining_txt(engine, fake_pipeline, make_config, isolated_dirs):
    config = make_config(filename="jitrun", time_id="1")
    engine.cancel_event.set()  # cancel before starting -> deterministic "nothing played" branch

    asyncio.run(engine._process_jit_async("Hello world. This is JIT playback text.", config))

    remaining = isolated_dirs.out_dir / "jitrun_1_remaining.txt"
    assert remaining.exists()
    content = remaining.read_text(encoding="utf-8")
    assert "Hello world" in content

    assert not (isolated_dirs.out_dir / "jitrun_1_jit_output.wav").exists()


def test_jit_conversion_calls_on_finish_even_when_no_text(engine, fake_pipeline, make_config, callback_recorder):
    config = make_config()
    asyncio.run(engine._process_jit_async("   ", config))

    assert callback_recorder.finished.is_set()


def test_jit_conversion_leaves_inspectable_output(engine, fake_pipeline, make_config, timestamped_output_dir):
    config = make_config(out_dir=str(timestamped_output_dir), filename="jitsmoke", time_id="1")
    asyncio.run(engine._process_jit_async("Hello from the JIT smoke test.", config))

    assert (timestamped_output_dir / "jitsmoke_1_jit_output.wav").exists()


def test_jit_plays_first_segment_before_generation_finishes(engine, make_config, monkeypatch):
    first_segment_yielded = threading.Event()
    allow_second_segment = threading.Event()

    class StalledPipeline:
        lang_code = "a"

        def __call__(self, text, voice=None, speed=1.0, split_pattern=r"\n+"):
            audio = np.zeros(1200, dtype=np.float32)
            yield "First segment.", "", audio
            first_segment_yielded.set()
            assert allow_second_segment.wait(5), "test did not allow the second segment to generate"
            yield "Second segment.", "", audio

    monkeypatch.setattr(kokoro_engine, "get_thread_pipeline", lambda lang_code="a": StalledPipeline())
    config = make_config(filename="jitstream", time_id="1")

    async def run_jit():
        task = asyncio.create_task(engine._process_jit_async("First\nSecond", config))
        try:
            for _ in range(100):
                if first_segment_yielded.is_set() and kokoro_engine.playback.play.called:
                    break
                await asyncio.sleep(0.01)

            assert first_segment_yielded.is_set()
            assert kokoro_engine.playback.play.called
            assert "part0_0.wav" in kokoro_engine.playback.play.call_args.args[0]
        finally:
            allow_second_segment.set()
        await task

    asyncio.run(run_jit())


def test_jit_reuses_initialized_pipeline(engine, fake_pipeline, make_config, monkeypatch):
    engine.pipeline = fake_pipeline
    monkeypatch.setattr(
        kokoro_engine,
        "get_thread_pipeline",
        lambda lang_code="a": (_ for _ in ()).throw(AssertionError("JIT should reuse the initialized pipeline")),
    )

    asyncio.run(engine._process_jit_async("Reuse this pipeline.", make_config()))
