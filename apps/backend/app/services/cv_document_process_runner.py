"""Explicit Provenance Stage P6-B3a: bounded POSIX subprocess execution.

``PosixProcessRunner`` is the only place this stage ever spawns a real
subprocess. It never uses ``shell=True``, ``preexec_fn``,
``resource.setrlimit``, ``subprocess.run(capture_output=True)``, or
``Popen.communicate()``. Every process runs in its own process group
(``start_new_session=True``) so timeout/overflow cleanup can always signal
the *whole* group, never a single orphaned child.

Two bounded reader threads (one for stdout, one for stderr) each drain
their pipe to EOF, keeping only a bounded prefix in memory and setting a
shared ``overflow_event`` once their limit is exceeded -- they never call
``os.killpg``, never call ``proc.wait``, and never manage process
lifecycle. A single central coordinator, running in the calling thread, is
the *only* place that polls for completion, observes the overflow event,
enforces the deadline, and performs SIGTERM/SIGKILL/``proc.wait``
termination -- so there is never more than one thread deciding whether the
process should die.

Status precedence when multiple conditions are observed in the same
window: ``TERMINATION_FAILED`` > ``OUTPUT_LIMIT_EXCEEDED`` > ``TIMED_OUT``
> ``START_FAILED`` > ``COMPLETED``. In particular, an overflow observed in
the same window as a timeout or a natural process exit still yields
``OUTPUT_LIMIT_EXCEEDED``, provided process-group cleanup itself succeeds.

On Windows this runner always returns ``START_FAILED`` without ever
attempting POSIX process-group logic -- a real subprocess is out of scope
for this stage on that platform.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol


_MAX_DIAGNOSTICS_ENTRIES = 8
_MAX_DIAGNOSTIC_LENGTH = 1000

#: Upper bound on how long the central coordinator ever sleeps between
#: completion/overflow/deadline checks -- independent of ``timeout_seconds``,
#: so overflow/timeout is always observed promptly.
_COORDINATOR_POLL_INTERVAL_SECONDS = 0.05


class ProcessRunStatus(StrEnum):
    """The closed set of outcomes one ``ProcessRunner.run`` call may
    return."""

    COMPLETED = "COMPLETED"
    START_FAILED = "START_FAILED"
    TIMED_OUT = "TIMED_OUT"
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
    TERMINATION_FAILED = "TERMINATION_FAILED"


def _bounded_diagnostics(diagnostics: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        entry.replace("\x00", "")[:_MAX_DIAGNOSTIC_LENGTH]
        for entry in diagnostics[:_MAX_DIAGNOSTICS_ENTRIES]
    )


@dataclass(frozen=True)
class ProcessRunResult:
    """A closed, bounded process-run outcome. ``diagnostics`` are
    defensively bounded here regardless of what a caller constructs this
    with -- never a raw exception object, never unbounded text.

    ``bounded_stdout``/``bounded_stderr`` carry their own declared
    ``max_stdout_bytes``/``max_stderr_bytes`` limits, and this dataclass's
    own ``__post_init__`` verifies each buffer fits within its declared
    limit -- it never trusts that the buffer was already bounded correctly
    by whatever produced it (e.g. ``_PipeReader``). A result that violates
    its own declared bounds is rejected fail-closed (``ValueError``), never
    silently truncated to fit."""

    status: ProcessRunStatus
    exit_code: int | None
    bounded_stdout: bytes
    bounded_stderr: bytes
    termination_stage: str | None
    max_stdout_bytes: int
    max_stderr_bytes: int
    diagnostics: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.max_stdout_bytes <= 0:
            raise ValueError("max_stdout_bytes must be positive")
        if self.max_stderr_bytes <= 0:
            raise ValueError("max_stderr_bytes must be positive")
        if not isinstance(self.bounded_stdout, bytes):
            raise TypeError("bounded_stdout must be bytes")
        if not isinstance(self.bounded_stderr, bytes):
            raise TypeError("bounded_stderr must be bytes")
        if len(self.bounded_stdout) > self.max_stdout_bytes:
            raise ValueError("bounded_stdout exceeds max_stdout_bytes")
        if len(self.bounded_stderr) > self.max_stderr_bytes:
            raise ValueError("bounded_stderr exceeds max_stderr_bytes")
        # Defensive copies: bytes are immutable, but this guards against a
        # bytes subclass or any future relaxation of the isinstance checks
        # above ever letting a caller-mutable buffer alias into this result.
        object.__setattr__(self, "bounded_stdout", bytes(self.bounded_stdout))
        object.__setattr__(self, "bounded_stderr", bytes(self.bounded_stderr))
        object.__setattr__(self, "diagnostics", _bounded_diagnostics(self.diagnostics))


class ProcessRunner(Protocol):
    """A bounded subprocess execution contract. Never accepts a shell
    string -- ``argv`` is always an explicit, fully-resolved argument
    vector."""

    def run(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> ProcessRunResult: ...


class _PipeReader:
    """Drains one pipe to EOF in its own thread, keeping only a bounded
    prefix. Never calls ``os.killpg``/``proc.wait`` and never manages
    process lifecycle -- it only reads, bounds, and signals overflow."""

    def __init__(self, pipe: object, *, max_bytes: int, chunk_size: int, overflow_event: threading.Event) -> None:
        self._pipe = pipe
        self._max_bytes = max_bytes
        self._chunk_size = chunk_size
        self._overflow_event = overflow_event
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout_seconds: float) -> bool:
        self._thread.join(timeout_seconds)
        return not self._thread.is_alive()

    def bounded_bytes(self) -> bytes:
        with self._lock:
            return bytes(self._buffer)

    def _run(self) -> None:
        try:
            fd = self._pipe.fileno()
        except (OSError, ValueError, AttributeError):
            return
        try:
            while True:
                try:
                    chunk = os.read(fd, self._chunk_size)
                except OSError:
                    return
                if not chunk:
                    return
                with self._lock:
                    remaining = self._max_bytes - len(self._buffer)
                    if remaining <= 0:
                        self._overflow_event.set()
                        continue
                    if len(chunk) > remaining:
                        self._buffer.extend(chunk[:remaining])
                        self._overflow_event.set()
                    else:
                        self._buffer.extend(chunk)
        except (OSError, ValueError):
            return


def _terminate_process_group(
    proc: subprocess.Popen, *, grace_seconds: float, final_wait_seconds: float
) -> tuple[bool, str, tuple[str, ...]]:
    """The only termination sequence this runner ever performs: SIGTERM to
    the whole process group, a bounded grace ``proc.wait``, then SIGKILL to
    the whole process group, then a bounded final ``proc.wait``. Never an
    unbounded wait. Returns ``(success, termination_stage, diagnostics)``."""

    diagnostics: list[str] = []

    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return True, "already_exited", ()
    except OSError as exc:
        diagnostics.append(f"getpgid failed: {exc}")
        pgid = None

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return True, "sigterm_already_exited", tuple(diagnostics)
        except OSError as exc:
            diagnostics.append(f"SIGTERM failed: {exc}")

    try:
        proc.wait(timeout=grace_seconds)
        return True, "sigterm", tuple(diagnostics)
    except subprocess.TimeoutExpired:
        pass

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            diagnostics.append(f"SIGKILL failed: {exc}")

    try:
        proc.wait(timeout=final_wait_seconds)
        return True, "sigkill", tuple(diagnostics)
    except subprocess.TimeoutExpired:
        diagnostics.append("process did not exit after SIGKILL within final_wait_seconds")
        return False, "sigkill_timeout", tuple(diagnostics)


class PosixProcessRunner:
    """The only ``ProcessRunner`` implementation this stage ships. Every
    configured limit is validated at construction. ``run`` never raises --
    every failure path is a closed ``ProcessRunResult``."""

    def __init__(
        self,
        *,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        read_chunk_size: int,
        terminate_grace_seconds: float,
        final_wait_seconds: float,
        reader_join_timeout_seconds: float,
    ) -> None:
        if max_stdout_bytes <= 0:
            raise ValueError("max_stdout_bytes must be positive")
        if max_stderr_bytes <= 0:
            raise ValueError("max_stderr_bytes must be positive")
        if read_chunk_size <= 0:
            raise ValueError("read_chunk_size must be positive")
        if terminate_grace_seconds < 0:
            raise ValueError("terminate_grace_seconds must be non-negative")
        if final_wait_seconds < 0:
            raise ValueError("final_wait_seconds must be non-negative")
        if reader_join_timeout_seconds <= 0:
            raise ValueError("reader_join_timeout_seconds must be positive")

        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._read_chunk_size = read_chunk_size
        self._terminate_grace_seconds = terminate_grace_seconds
        self._final_wait_seconds = final_wait_seconds
        self._reader_join_timeout_seconds = reader_join_timeout_seconds

    def run(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> ProcessRunResult:
        if sys.platform == "win32":
            return ProcessRunResult(
                status=ProcessRunStatus.START_FAILED,
                exit_code=None,
                bounded_stdout=b"",
                bounded_stderr=b"",
                termination_stage=None,
                max_stdout_bytes=self._max_stdout_bytes,
                max_stderr_bytes=self._max_stderr_bytes,
                diagnostics=("PosixProcessRunner does not support win32 in this stage",),
            )

        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=str(cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            return ProcessRunResult(
                status=ProcessRunStatus.START_FAILED,
                exit_code=None,
                bounded_stdout=b"",
                bounded_stderr=b"",
                termination_stage=None,
                max_stdout_bytes=self._max_stdout_bytes,
                max_stderr_bytes=self._max_stderr_bytes,
                diagnostics=(f"process start failed: {exc}",),
            )

        overflow_event = threading.Event()
        stdout_reader = _PipeReader(
            proc.stdout, max_bytes=self._max_stdout_bytes, chunk_size=self._read_chunk_size, overflow_event=overflow_event
        )
        stderr_reader = _PipeReader(
            proc.stderr, max_bytes=self._max_stderr_bytes, chunk_size=self._read_chunk_size, overflow_event=overflow_event
        )
        stdout_reader.start()
        stderr_reader.start()

        outcome, exit_code = self._await_outcome(proc, overflow_event, timeout_seconds)

        diagnostics: list[str] = []
        termination_stage: str | None = None

        if outcome == "completed":
            status = ProcessRunStatus.COMPLETED
        else:
            termination_ok, termination_stage, term_diagnostics = _terminate_process_group(
                proc,
                grace_seconds=self._terminate_grace_seconds,
                final_wait_seconds=self._final_wait_seconds,
            )
            diagnostics.extend(term_diagnostics)
            if not termination_ok:
                status = ProcessRunStatus.TERMINATION_FAILED
            elif outcome == "overflow":
                status = ProcessRunStatus.OUTPUT_LIMIT_EXCEEDED
            else:
                status = ProcessRunStatus.TIMED_OUT

        stdout_joined = stdout_reader.join(self._reader_join_timeout_seconds)
        stderr_joined = stderr_reader.join(self._reader_join_timeout_seconds)
        if not stdout_joined or not stderr_joined:
            status = ProcessRunStatus.TERMINATION_FAILED
            diagnostics.append("reader thread failed to join within bounded timeout")

        try:
            proc.stdout.close()
        except OSError:
            pass
        try:
            proc.stderr.close()
        except OSError:
            pass

        final_exit_code = proc.poll()

        return ProcessRunResult(
            status=status,
            exit_code=final_exit_code,
            bounded_stdout=stdout_reader.bounded_bytes(),
            bounded_stderr=stderr_reader.bounded_bytes(),
            termination_stage=termination_stage,
            max_stdout_bytes=self._max_stdout_bytes,
            max_stderr_bytes=self._max_stderr_bytes,
            diagnostics=tuple(diagnostics),
        )

    def _await_outcome(
        self, proc: subprocess.Popen, overflow_event: threading.Event, timeout_seconds: float
    ) -> tuple[str, int | None]:
        """The single central coordinator loop: bounded-interval polling of
        ``proc.wait``, the shared overflow event, and the deadline. Returns
        ``("completed" | "overflow" | "timeout", exit_code_or_None)``."""

        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ("overflow" if overflow_event.is_set() else "timeout"), None
            wait_slice = min(_COORDINATOR_POLL_INTERVAL_SECONDS, remaining)
            try:
                exit_code = proc.wait(timeout=wait_slice)
                return ("overflow" if overflow_event.is_set() else "completed"), exit_code
            except subprocess.TimeoutExpired:
                if overflow_event.is_set():
                    return "overflow", None
                continue
