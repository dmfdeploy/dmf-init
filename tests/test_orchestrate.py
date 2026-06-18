from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from dmf_init.orchestrate import (
    BootstrapRun,
    CheckpointStep,
    CommandStep,
    PauseStep,
    Step,
    run_worker,
    stream_events,
)


@dataclass
class FakeExecutor:
    scripts: dict[str, tuple[list[str], int]]
    gates: dict[str, threading.Event] = field(default_factory=dict)

    def run(self, step: CommandStep):
        lines, exit_code = self.scripts[step.id]
        for line in lines:
            yield line, None
        gate = self.gates.get(step.id)
        if gate is not None:
            if not gate.wait(timeout=5):
                raise AssertionError(f"timed out waiting for gate on {step.id}")
        yield "", exit_code


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


def _decode_stream(run: BootstrapRun, start_index: int = 0) -> list[dict[str, Any]]:
    return [json.loads(item) for item in stream_events(run, start_index)]


def test_happy_path_redacts_pause_and_completes() -> None:
    steps: list[Step] = [
        CommandStep(id="one", argv=["ignored"]),
        CheckpointStep(id="cp2", n=2),
        PauseStep(id="ca", title="Confirm CA"),
        CommandStep(id="two", argv=["ignored"]),
    ]
    executor = FakeExecutor(
        scripts={
            "one": (["line one", "contains TOPSECRET"], 0),
            "two": (["final line"], 0),
        }
    )

    def checkpoint_fn(run: BootstrapRun, n: int) -> dict[str, Any]:
        run.add_secret("TOPSECRET")
        return {"artifact": "x"}

    run = BootstrapRun(
        run_id="run-1",
        steps=steps,
        executor=executor,
        checkpoint_fn=checkpoint_fn,
        passphrase="passphrase",
    )
    run.add_secret("TOPSECRET")

    worker = threading.Thread(target=run_worker, args=(run,))
    worker.start()

    seen: list[dict[str, Any]] = []
    tail: list[dict[str, Any]] = []
    stream = stream_events(run)
    try:
        while True:
            event = json.loads(next(stream))
            seen.append(event)
            if event["event"] == "pause":
                break
        _wait_for(lambda: any(ev["event"] == "pause" for ev in run.events))
        assert worker.is_alive()
        assert not any(ev["event"] == "complete" for ev in run.events)
        run.resume("ca")
        _wait_for(lambda: any(ev["event"] == "complete" for ev in run.events))
        tail = [json.loads(item) for item in stream]
    finally:
        stream.close()

    worker.join(timeout=2)
    assert not worker.is_alive()

    all_events = seen + tail
    assert all(event.get("line", "").find("TOPSECRET") == -1 for event in all_events)
    assert any(event["event"] == "checkpoint" and event["n"] == 2 for event in all_events)
    assert all_events[-1]["event"] == "complete"
    assert all_events[-1]["checkpoints"] == [2]


def test_redaction_before_stream() -> None:
    steps: list[Step] = [
        CheckpointStep(id="cp2", n=2),
        CommandStep(id="one", argv=["ignored"]),
    ]
    executor = FakeExecutor(scripts={"one": (["value UNSEALKEY"], 0)})

    def checkpoint_fn(run: BootstrapRun, n: int) -> dict[str, Any]:
        run.add_secret("UNSEALKEY")
        return {}

    run = BootstrapRun(run_id="run-2", steps=steps, executor=executor, checkpoint_fn=checkpoint_fn)
    worker = threading.Thread(target=run_worker, args=(run,))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()

    events = _decode_stream(run)
    log_events = [event for event in events if event["event"] == "log"]
    assert len(log_events) == 1
    assert log_events[0]["line"] == "value [REDACTED]"


def test_command_failure_halts_and_wipes_secrets() -> None:
    steps: list[Step] = [
        CommandStep(id="one", argv=["ignored"]),
        CommandStep(id="two", argv=["ignored"]),
    ]
    executor = FakeExecutor(
        scripts={
            "one": (["first"], 7),
            "two": (["should not run"], 0),
        }
    )
    run = BootstrapRun(
        run_id="run-3",
        steps=steps,
        executor=executor,
        passphrase="secret",
    )
    run.add_secret("TOPSECRET")

    worker = threading.Thread(target=run_worker, args=(run,))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()

    events = _decode_stream(run)
    assert events[-1]["event"] == "error"
    assert events[-1]["error"] == "command exited 7"
    assert not any(
        event.get("step") == "two" and event["event"] == "step_start" for event in events
    )
    assert run.passphrase is None
    assert run.secrets == set()


def test_reconnect_cursor_replays_from_offset() -> None:
    steps: list[Step] = [
        CommandStep(id="one", argv=["ignored"]),
        PauseStep(id="ca", title="Confirm CA"),
        CommandStep(id="two", argv=["ignored"]),
    ]
    executor = FakeExecutor(
        scripts={
            "one": (["line one"], 0),
            "two": (["line two"], 0),
        }
    )
    run = BootstrapRun(run_id="run-4", steps=steps, executor=executor)
    worker = threading.Thread(target=run_worker, args=(run,))
    worker.start()
    _wait_for(lambda: any(event["event"] == "pause" for event in run.events))
    run.resume("ca")
    worker.join(timeout=2)
    assert not worker.is_alive()

    all_events = _decode_stream(run)
    k = 3
    replayed = _decode_stream(run, k)
    assert replayed == all_events[k:]
    assert replayed[-1]["event"] == "complete"


def test_resume_before_pause_still_completes() -> None:
    gate = threading.Event()
    steps: list[Step] = [
        CommandStep(id="one", argv=["ignored"]),
        PauseStep(id="ca", title="Confirm CA"),
        CommandStep(id="two", argv=["ignored"]),
    ]
    executor = FakeExecutor(
        scripts={
            "one": (["line one"], 0),
            "two": (["line two"], 0),
        },
        gates={"one": gate},
    )
    run = BootstrapRun(run_id="run-5", steps=steps, executor=executor)
    worker = threading.Thread(target=run_worker, args=(run,))
    worker.start()
    run.resume("ca")
    gate.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    events = _decode_stream(run)
    assert events[-1]["event"] == "complete"


def test_command_failure_sets_failed_step_id_and_hint() -> None:
    steps: list[Step] = [
        CommandStep(id="pre-seed", argv=["ignored"]),
        CommandStep(id="post-seed", argv=["ignored"]),
    ]
    executor = FakeExecutor(
        scripts={
            "pre-seed": (["line1", "line2", "line3", "line4", "line5", "line6"], 1),
            "post-seed": (["should not run"], 0),
        }
    )
    run = BootstrapRun(run_id="run-fail", steps=steps, executor=executor)
    run.add_secret("MYSECRET")

    worker = threading.Thread(target=run_worker, args=(run,))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()

    assert run.failed_step_id == "pre-seed"
    events = _decode_stream(run)
    error_event = next(ev for ev in events if ev["event"] == "error")
    assert error_event["step"] == "pre-seed"
    assert "hint" in error_event
    assert error_event["hint"] != ""
    hint_lines = error_event["hint"].split("\n")
    assert len(hint_lines) == 5
    assert "line2" in hint_lines
    assert "line6" in hint_lines
    assert "MYSECRET" not in error_event["hint"]
    assert any("MYSECRET" not in ev.get("hint", "") for ev in events)


def test_hint_empty_when_no_log_lines_before_failure() -> None:
    steps: list[Step] = [
        CommandStep(id="fast-fail", argv=["ignored"]),
    ]
    executor = FakeExecutor(
        scripts={"fast-fail": ([], 3)}
    )
    run = BootstrapRun(run_id="run-empty", steps=steps, executor=executor)
    worker = threading.Thread(target=run_worker, args=(run,))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()

    assert run.failed_step_id == "fast-fail"
    events = _decode_stream(run)
    error_event = next(ev for ev in events if ev["event"] == "error")
    assert error_event["hint"] == ""
