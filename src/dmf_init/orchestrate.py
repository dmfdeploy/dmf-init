from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .redaction import redact_text


@dataclass(frozen=True)
class CommandStep:
    id: str
    argv: list[str]
    cwd: Path | None = None
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class CheckpointStep:
    id: str
    n: int


@dataclass(frozen=True)
class PauseStep:
    id: str
    title: str
    payload_fn: Callable[[BootstrapRun], dict[str, Any]] | None = None


Step = CommandStep | CheckpointStep | PauseStep


class CommandExecutor(Protocol):
    def run(self, step: CommandStep) -> Iterator[tuple[str, int | None]]: ...


class SubprocessExecutor:
    def run(self, step: CommandStep) -> Iterator[tuple[str, int | None]]:
        env = os.environ.copy()
        if step.env:
            env.update(step.env)
        proc = subprocess.Popen(
            step.argv,
            cwd=step.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            yield raw_line.rstrip("\n"), None
        yield "", proc.wait()


def _redact_value(value: Any, secrets: Iterable[str]) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, dict):
        return {key: _redact_value(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, secrets) for item in value)
    return value


@dataclass
class BootstrapRun:
    run_id: str
    steps: list[Step]
    env_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    cond: threading.Condition = field(default_factory=threading.Condition)
    secrets: set[str] = field(default_factory=set)
    pauses: dict[str, threading.Event] = field(init=False)
    pause_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    terminal: bool = False
    final_status: str | None = None
    passphrase: str | None = None
    remotes: list = field(default_factory=list)  # unused after download-model switch
    executor: CommandExecutor = field(default_factory=SubprocessExecutor)
    checkpoint_fn: Callable[[BootstrapRun, int], dict[str, Any]] | None = None
    # Best-effort persistence hook invoked once the run reaches a terminal
    # state (complete or error). Lets the caller record the resume cursor
    # (failed step id) to disk so a GC'd / restarted run stays resumable.
    journal_fn: Callable[[BootstrapRun], None] | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    failed_step_id: str | None = None

    def __post_init__(self) -> None:
        self.pauses = {
            step.id: threading.Event()
            for step in self.steps
            if isinstance(step, PauseStep)
        }

    def __repr__(self) -> str:
        return (
            f"BootstrapRun(run_id={self.run_id!r}, terminal={self.terminal!r}, "
            f"final_status={self.final_status!r})"
        )

    __str__ = __repr__

    def add_secret(self, value: str | None) -> None:
        if not value:
            return
        with self.cond:
            self.secrets.add(value)

    def emit(self, event: dict[str, Any], *, terminal: bool = False) -> None:
        with self.cond:
            ev = _redact_value(dict(event), self.secrets)
            self.events.append(ev)
            if terminal:
                self.terminal = True
                self.final_status = ev["event"]
                self.finished_at = time.time()
            self.cond.notify_all()
        # Persist the resume cursor outside the lock — journaling is
        # best-effort and must never crash the worker or stall streaming.
        if terminal and self.journal_fn is not None:
            try:
                self.journal_fn(self)
            except Exception:  # pragma: no cover - defensive: disk hiccup
                pass

    def wipe_secrets(self) -> None:
        with self.cond:
            self.passphrase = None
            self.secrets.clear()

    def resume(self, pause_id: str, payload: dict[str, Any] | None = None) -> None:
        with self.cond:
            if pause_id not in self.pauses:
                raise KeyError(pause_id)
            pause_event = self.pauses[pause_id]
            if pause_event.is_set():
                raise ValueError(f"pause already resumed: {pause_id}")
            self.pause_payloads[pause_id] = payload or {}
            pause_event.set()
            self.cond.notify_all()


def run_worker(run: BootstrapRun) -> None:
    try:
        run.emit(
            {
                "event": "run_start",
                "run_id": run.run_id,
                "steps": [step.id for step in run.steps],
            }
        )
        for index, step in enumerate(run.steps):
            if isinstance(step, CommandStep):
                kind = "command"
            elif isinstance(step, CheckpointStep):
                kind = "checkpoint"
            else:
                kind = "pause"
            run.emit(
                {
                    "event": "step_start",
                    "step": step.id,
                    "index": index,
                    "kind": kind,
                }
            )
            if isinstance(step, CommandStep):
                log_tail: list[str] = []
                for line, code in run.executor.run(step):
                    if code is None:
                        if line:
                            run.emit({"event": "log", "step": step.id, "line": line})
                            log_tail.append(line)
                            if len(log_tail) > 5:
                                log_tail.pop(0)
                        continue
                    if code != 0:
                        run.failed_step_id = step.id
                        hint = "\n".join(log_tail)
                        run.emit({"event": "step_complete", "step": step.id, "status": "error"})
                        run.emit(
                            {
                                "event": "error",
                                "step": step.id,
                                "error": f"command exited {code}",
                                "hint": hint,
                            },
                            terminal=True,
                        )
                        run.wipe_secrets()
                        return
                    run.emit({"event": "step_complete", "step": step.id, "status": "ok"})
            elif isinstance(step, CheckpointStep):
                info = run.checkpoint_fn(run, step.n) if run.checkpoint_fn else {}
                run.emit({"event": "checkpoint", "n": step.n, **info})
                run.emit({"event": "step_complete", "step": step.id, "status": "ok"})
            else:
                payload = step.payload_fn(run) if step.payload_fn else {}
                run.emit(
                    {
                        "event": "pause",
                        "pause_id": step.id,
                        "title": step.title,
                        "payload": payload,
                    }
                )
                run.pauses[step.id].wait()
                run.emit({"event": "resume", "pause_id": step.id})
                run.emit({"event": "step_complete", "step": step.id, "status": "ok"})
        checkpoints = [step.n for step in run.steps if isinstance(step, CheckpointStep)]
        run.emit(
            {
                "event": "complete",
                "run_id": run.run_id,
                "checkpoints": checkpoints,
            },
            terminal=True,
        )
        run.wipe_secrets()
    except Exception as exc:  # pragma: no cover - defensive safety net
        run.emit({"event": "error", "error": str(exc)}, terminal=True)
        run.wipe_secrets()


def stream_events(run: BootstrapRun, start_index: int = 0) -> Iterator[str]:
    i = start_index
    while True:
        with run.cond:
            # bind i as a default arg so the predicate captures this iteration's
            # cursor value (it is evaluated synchronously here, but the bind keeps
            # it correct and silences B023).
            run.cond.wait_for(
                lambda i=i: len(run.events) > i or run.terminal, timeout=0.5
            )
            batch = run.events[i:]
            i = len(run.events)
            done = run.terminal
        for ev in batch:
            yield json.dumps(ev, separators=(",", ":")) + "\n"
            if ev["event"] in ("complete", "error"):
                return
        if done and not batch:
            return


def build_demo_steps() -> list[Step]:
    return [
        CommandStep(
            id="demo-start",
            argv=["/bin/sh", "-lc", "printf 'bootstrap demo start\\n'"],
        ),
        PauseStep(
            id="demo-pause",
            title="Acknowledge demo bootstrap",
            payload_fn=lambda run: {"run_id": run.run_id},
        ),
        CommandStep(
            id="demo-finish",
            argv=["/bin/sh", "-lc", "printf 'bootstrap demo complete\\n'"],
        ),
    ]
