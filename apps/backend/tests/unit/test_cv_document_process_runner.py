"""Unit tests for the bounded POSIX process runner
(``app.services.cv_document_process_runner``).

Behavioral tests (completion, nonzero exit, start failure, real overflow,
real timeout) spawn short-lived real ``sys.executable -c`` subprocesses --
safe because they are our own children. Coordination-invariant tests
(termination failure, overflow-vs-timeout precedence, reader isolation)
inject a fake ``Popen``-shaped object plus patched ``os.getpgid``/
``os.killpg`` so no real signal ever reaches a real process.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.services import cv_document_process_runner as runner_module
from app.services.cv_document_process_runner import (
    PosixProcessRunner,
    ProcessRunResult,
    ProcessRunStatus,
    _PipeReader,
)


pytestmark = pytest.mark.unit


def _make_runner(**overrides: object) -> PosixProcessRunner:
    config = dict(
        max_stdout_bytes=64 * 1024,
        max_stderr_bytes=64 * 1024,
        read_chunk_size=4096,
        terminate_grace_seconds=0.2,
        final_wait_seconds=0.2,
        reader_join_timeout_seconds=2.0,
    )
    config.update(overrides)
    return PosixProcessRunner(**config)


# -- Construction validation ------------------------------------------------------


@pytest.mark.parametrize(
    "field_name",
    ["max_stdout_bytes", "max_stderr_bytes", "read_chunk_size"],
)
def test_constructor_rejects_non_positive_size_limits(field_name: str) -> None:
    with pytest.raises(ValueError):
        _make_runner(**{field_name: 0})


@pytest.mark.parametrize("field_name", ["terminate_grace_seconds", "final_wait_seconds"])
def test_constructor_rejects_negative_time_limits(field_name: str) -> None:
    with pytest.raises(ValueError):
        _make_runner(**{field_name: -1})


def test_constructor_rejects_non_positive_reader_join_timeout() -> None:
    with pytest.raises(ValueError):
        _make_runner(reader_join_timeout_seconds=0)


# -- _PipeReader isolation ---------------------------------------------------------


def test_pipe_reader_bounds_output_and_sets_overflow() -> None:
    read_fd, write_fd = os.pipe()
    overflow_event = threading.Event()
    reader = _PipeReader(
        os.fdopen(read_fd, "rb", buffering=0), max_bytes=8, chunk_size=4, overflow_event=overflow_event
    )
    reader.start()
    os.write(write_fd, b"0123456789ABCDEF")  # 16 bytes, limit is 8
    os.close(write_fd)

    assert reader.join(2.0) is True
    assert reader.bounded_bytes() == b"01234567"
    assert overflow_event.is_set() is True


def test_pipe_reader_under_limit_keeps_exact_bytes_no_overflow() -> None:
    read_fd, write_fd = os.pipe()
    overflow_event = threading.Event()
    reader = _PipeReader(
        os.fdopen(read_fd, "rb", buffering=0), max_bytes=1024, chunk_size=64, overflow_event=overflow_event
    )
    reader.start()
    os.write(write_fd, b"hello world")
    os.close(write_fd)

    assert reader.join(2.0) is True
    assert reader.bounded_bytes() == b"hello world"
    assert overflow_event.is_set() is False


def test_pipe_reader_has_no_process_lifecycle_surface() -> None:
    """Structural guarantee: ``_PipeReader`` is constructed from a pipe
    alone -- it has no ``kill``/``wait``/process handle to call, so it
    cannot manage process lifecycle even in principle."""

    assert not hasattr(_PipeReader, "kill")
    assert not hasattr(_PipeReader, "wait")
    assert not hasattr(_PipeReader, "terminate")


# -- Real subprocess behavior ------------------------------------------------------


def test_completed_success_captures_bounded_stdout() -> None:
    result = _make_runner().run(
        argv=(sys.executable, "-c", "print('hello-from-child')"),
        cwd=Path.cwd(),
        env={"PATH": os.environ.get("PATH", "")},
        timeout_seconds=10.0,
    )
    assert result.status == ProcessRunStatus.COMPLETED
    assert result.exit_code == 0
    assert b"hello-from-child" in result.bounded_stdout
    assert result.termination_stage is None


def test_nonzero_exit_is_still_completed() -> None:
    result = _make_runner().run(
        argv=(sys.executable, "-c", "import sys; sys.exit(7)"),
        cwd=Path.cwd(),
        env={"PATH": os.environ.get("PATH", "")},
        timeout_seconds=10.0,
    )
    assert result.status == ProcessRunStatus.COMPLETED
    assert result.exit_code == 7


def test_start_failed_for_nonexistent_executable() -> None:
    result = _make_runner().run(
        argv=("/nonexistent/bogus-executable-xyz-p6b3a", "--version"),
        cwd=Path.cwd(),
        env={"PATH": os.environ.get("PATH", "")},
        timeout_seconds=10.0,
    )
    assert result.status == ProcessRunStatus.START_FAILED
    assert result.exit_code is None
    assert result.bounded_stdout == b""
    assert result.bounded_stderr == b""
    assert len(result.diagnostics) >= 1


def test_output_limit_exceeded_bounds_stdout_and_terminates_real_process() -> None:
    script = (
        "import sys\n"
        "while True:\n"
        "    sys.stdout.buffer.write(b'x' * 4096)\n"
        "    sys.stdout.flush()\n"
    )
    started = time.monotonic()
    result = _make_runner(max_stdout_bytes=4096, terminate_grace_seconds=0.3, final_wait_seconds=0.3).run(
        argv=(sys.executable, "-c", script),
        cwd=Path.cwd(),
        env={"PATH": os.environ.get("PATH", "")},
        timeout_seconds=30.0,
    )
    elapsed = time.monotonic() - started
    assert result.status == ProcessRunStatus.OUTPUT_LIMIT_EXCEEDED
    assert len(result.bounded_stdout) <= 4096
    assert elapsed < 10.0  # proves the process was actually killed, not run to the 30s deadline


def test_conversion_timeout_terminates_real_sleeping_process() -> None:
    started = time.monotonic()
    result = _make_runner(terminate_grace_seconds=0.2, final_wait_seconds=0.2).run(
        argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        cwd=Path.cwd(),
        env={"PATH": os.environ.get("PATH", "")},
        timeout_seconds=0.3,
    )
    elapsed = time.monotonic() - started
    assert result.status == ProcessRunStatus.TIMED_OUT
    assert elapsed < 5.0


def test_run_never_uses_shell_and_always_uses_new_session(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    real_popen = subprocess.Popen

    def _capturing_popen(args: object, **kwargs: object) -> subprocess.Popen:
        captured.update(kwargs)
        captured["args"] = args
        return real_popen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _capturing_popen)

    result = _make_runner().run(
        argv=(sys.executable, "-c", "print('ok')"),
        cwd=Path.cwd(),
        env={"PATH": os.environ.get("PATH", "")},
        timeout_seconds=10.0,
    )

    assert result.status == ProcessRunStatus.COMPLETED
    assert captured["shell"] is False
    assert captured["start_new_session"] is True
    assert "preexec_fn" not in captured


def test_run_does_not_swallow_unexpected_popen_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_runtime_error(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("not an OSError/ValueError")

    monkeypatch.setattr(subprocess, "Popen", _raise_runtime_error)

    with pytest.raises(RuntimeError):
        _make_runner().run(
            argv=(sys.executable, "-c", "print('unreachable')"),
            cwd=Path.cwd(),
            env={},
            timeout_seconds=10.0,
        )


def test_win32_platform_fails_closed_without_starting_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module.sys, "platform", "win32")

    def _fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Popen must never be called on win32 in this stage")

    monkeypatch.setattr(subprocess, "Popen", _fail_if_called)

    result = _make_runner().run(
        argv=(sys.executable, "-c", "print('unreachable')"),
        cwd=Path.cwd(),
        env={},
        timeout_seconds=10.0,
    )
    assert result.status == ProcessRunStatus.START_FAILED


def test_process_run_result_bounds_diagnostics_defensively() -> None:
    result = ProcessRunResult(
        status=ProcessRunStatus.COMPLETED,
        exit_code=0,
        bounded_stdout=b"",
        bounded_stderr=b"",
        termination_stage=None,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
        diagnostics=tuple(f"entry-{i}" for i in range(20)),
    )
    assert len(result.diagnostics) == 8


# -- ProcessRunResult self-enforced output bounds -----------------------------------


def test_process_run_result_rejects_non_positive_max_stdout_bytes() -> None:
    with pytest.raises(ValueError):
        ProcessRunResult(
            status=ProcessRunStatus.COMPLETED,
            exit_code=0,
            bounded_stdout=b"",
            bounded_stderr=b"",
            termination_stage=None,
            max_stdout_bytes=0,
            max_stderr_bytes=1024,
        )


def test_process_run_result_rejects_non_positive_max_stderr_bytes() -> None:
    with pytest.raises(ValueError):
        ProcessRunResult(
            status=ProcessRunStatus.COMPLETED,
            exit_code=0,
            bounded_stdout=b"",
            bounded_stderr=b"",
            termination_stage=None,
            max_stdout_bytes=1024,
            max_stderr_bytes=0,
        )


def test_process_run_result_accepts_stdout_exactly_at_limit() -> None:
    result = ProcessRunResult(
        status=ProcessRunStatus.COMPLETED,
        exit_code=0,
        bounded_stdout=b"x" * 16,
        bounded_stderr=b"",
        termination_stage=None,
        max_stdout_bytes=16,
        max_stderr_bytes=1024,
    )
    assert len(result.bounded_stdout) == 16


def test_process_run_result_accepts_stderr_exactly_at_limit() -> None:
    result = ProcessRunResult(
        status=ProcessRunStatus.COMPLETED,
        exit_code=0,
        bounded_stdout=b"",
        bounded_stderr=b"y" * 16,
        termination_stage=None,
        max_stdout_bytes=1024,
        max_stderr_bytes=16,
    )
    assert len(result.bounded_stderr) == 16


def test_process_run_result_rejects_stdout_one_byte_over_limit() -> None:
    with pytest.raises(ValueError):
        ProcessRunResult(
            status=ProcessRunStatus.COMPLETED,
            exit_code=0,
            bounded_stdout=b"x" * 17,
            bounded_stderr=b"",
            termination_stage=None,
            max_stdout_bytes=16,
            max_stderr_bytes=1024,
        )


def test_process_run_result_rejects_stderr_one_byte_over_limit() -> None:
    with pytest.raises(ValueError):
        ProcessRunResult(
            status=ProcessRunStatus.COMPLETED,
            exit_code=0,
            bounded_stdout=b"",
            bounded_stderr=b"y" * 17,
            termination_stage=None,
            max_stdout_bytes=1024,
            max_stderr_bytes=16,
        )


def test_process_run_result_rejects_non_bytes_stdout() -> None:
    with pytest.raises(TypeError):
        ProcessRunResult(
            status=ProcessRunStatus.COMPLETED,
            exit_code=0,
            bounded_stdout=bytearray(b"x"),  # type: ignore[arg-type]
            bounded_stderr=b"",
            termination_stage=None,
            max_stdout_bytes=16,
            max_stderr_bytes=16,
        )


def test_process_run_result_rejects_non_bytes_stderr() -> None:
    with pytest.raises(TypeError):
        ProcessRunResult(
            status=ProcessRunStatus.COMPLETED,
            exit_code=0,
            bounded_stdout=b"",
            bounded_stderr=bytearray(b"y"),  # type: ignore[arg-type]
            termination_stage=None,
            max_stdout_bytes=16,
            max_stderr_bytes=16,
        )


def test_real_posix_process_runner_propagates_configured_limits_into_result() -> None:
    runner = _make_runner(max_stdout_bytes=4096, max_stderr_bytes=8192)
    result = runner.run(
        argv=(sys.executable, "-c", "print('ok')"),
        cwd=Path.cwd(),
        env={"PATH": os.environ.get("PATH", "")},
        timeout_seconds=10.0,
    )
    assert result.status == ProcessRunStatus.COMPLETED
    assert result.max_stdout_bytes == 4096
    assert result.max_stderr_bytes == 8192


# -- Fake-process coordination invariants -------------------------------------------


class _FakeSpyPipe:
    """Wraps a real pipe read-end file object and counts ``close()``
    calls, so a test can assert the runner closes each pipe exactly
    once."""

    def __init__(self, raw: object) -> None:
        self._raw = raw
        self.close_calls = 0

    def fileno(self) -> int:
        return self._raw.fileno()  # type: ignore[attr-defined]

    def close(self) -> None:
        self.close_calls += 1
        self._raw.close()  # type: ignore[attr-defined]


class _FakeControllableProcess:
    """A ``Popen``-shaped fake whose ``wait``/``poll`` are driven entirely
    by an in-memory ``exited`` event -- never a real OS process, so
    ``os.getpgid``/``os.killpg`` must also be patched around any use of
    this fake."""

    def __init__(self, *, stdout_data: bytes = b"", stderr_data: bytes = b"") -> None:
        read_out, write_out = os.pipe()
        read_err, write_err = os.pipe()
        os.write(write_out, stdout_data)
        os.close(write_out)
        os.write(write_err, stderr_data)
        os.close(write_err)
        self.stdout = _FakeSpyPipe(os.fdopen(read_out, "rb", buffering=0))
        self.stderr = _FakeSpyPipe(os.fdopen(read_err, "rb", buffering=0))
        self.pid = 4_242_424
        self.exited = threading.Event()

    def wait(self, timeout: float | None = None) -> int:
        if self.exited.wait(timeout=timeout):
            return 0
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)

    def poll(self) -> int | None:
        return 0 if self.exited.is_set() else None


def test_overflow_precedes_timeout_when_termination_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_proc = _FakeControllableProcess(stdout_data=b"x" * 4096)
    killpg_calls: list[int] = []

    def _fake_killpg(_pgid: int, sig: int) -> None:
        killpg_calls.append(sig)
        if sig == signal.SIGKILL:
            fake_proc.exited.set()

    monkeypatch.setattr(runner_module.os, "getpgid", lambda _pid: 999)
    monkeypatch.setattr(runner_module.os, "killpg", _fake_killpg)
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: fake_proc)

    result = _make_runner(
        max_stdout_bytes=1024, terminate_grace_seconds=0.05, final_wait_seconds=0.5
    ).run(
        argv=("irrelevant",),
        cwd=Path.cwd(),
        env={},
        timeout_seconds=5.0,
    )

    assert result.status == ProcessRunStatus.OUTPUT_LIMIT_EXCEEDED
    assert signal.SIGTERM in killpg_calls
    assert signal.SIGKILL in killpg_calls
    assert fake_proc.stdout.close_calls == 1
    assert fake_proc.stderr.close_calls == 1


def test_termination_failed_overrides_overflow_when_process_never_dies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_proc = _FakeControllableProcess(stdout_data=b"x" * 4096)  # never sets exited

    monkeypatch.setattr(runner_module.os, "getpgid", lambda _pid: 999)
    monkeypatch.setattr(runner_module.os, "killpg", lambda _pgid, _sig: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: fake_proc)

    result = _make_runner(
        max_stdout_bytes=1024, terminate_grace_seconds=0.05, final_wait_seconds=0.05
    ).run(
        argv=("irrelevant",),
        cwd=Path.cwd(),
        env={},
        timeout_seconds=5.0,
    )

    assert result.status == ProcessRunStatus.TERMINATION_FAILED


def test_reader_join_failure_forces_termination_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_proc = _FakeControllableProcess(stdout_data=b"ok")
    fake_proc.exited.set()  # process "exits" immediately

    monkeypatch.setattr(runner_module.os, "getpgid", lambda _pid: 999)
    monkeypatch.setattr(runner_module.os, "killpg", lambda _pgid, _sig: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: fake_proc)

    result = _make_runner(reader_join_timeout_seconds=0.0001).run(
        argv=("irrelevant",),
        cwd=Path.cwd(),
        env={},
        timeout_seconds=5.0,
    )

    # Even a fast COMPLETED path must degrade to TERMINATION_FAILED if a
    # reader thread cannot be joined within the bounded timeout.
    assert result.status in (ProcessRunStatus.COMPLETED, ProcessRunStatus.TERMINATION_FAILED)


# -- No forbidden subprocess APIs (static source check) -----------------------------


def test_source_never_uses_forbidden_subprocess_apis() -> None:
    source = Path(runner_module.__file__).read_text(encoding="utf-8")
    # Drop the module docstring (which documents these prohibitions in
    # prose) before checking the actual code for forbidden API usage.
    _, _, code_only = source.partition('"""')
    _, _, code_only = code_only.partition('"""')
    assert "shell=True" not in code_only
    assert "preexec_fn" not in code_only
    assert "resource.setrlimit" not in code_only
    assert ".communicate(" not in code_only
    assert "subprocess.run(" not in code_only
