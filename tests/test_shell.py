from __future__ import annotations

import asyncio
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from foundation.services import (
    ExecutionMode,
    OutputStream,
    ShellCommandRequest,
    ShellExecutionCancelled,
    ShellExecutionTimeout,
    ShellRuntime,
)


def _runtime(tmp_path: Path) -> tuple[ShellRuntime, Path]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    runtime = ShellRuntime(
        workspace_root=workspace_root,
        default_timeout_seconds=2,
        max_timeout_seconds=10,
        capture_limit_kb=64,
    )
    return runtime, workspace_root


def _python_request(
    code: str,
    *,
    mode: ExecutionMode = ExecutionMode.BUFFERED,
    cwd: Path | None = None,
    env_overlay: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
) -> ShellCommandRequest:
    return ShellCommandRequest(
        command=sys.executable,
        args=["-c", textwrap.dedent(code)],
        cwd=cwd,
        env_overlay=env_overlay or {},
        timeout_seconds=timeout_seconds,
        mode=mode,
    )


def _assert_process_gone(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_buffered_execution_captures_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    runtime, _workspace_root = _runtime(tmp_path)
    request = _python_request(
        """
        import sys
        print("hello from stdout")
        sys.stderr.write("hello from stderr\\n")
        raise SystemExit(7)
        """
    )

    result = runtime.execute(request)

    assert result.exit_code == 7
    assert result.stdout == "hello from stdout\n"
    assert result.stderr == "hello from stderr\n"
    assert result.mode is ExecutionMode.BUFFERED


def test_execution_applies_env_overlay_and_cwd(tmp_path: Path) -> None:
    runtime, workspace_root = _runtime(tmp_path)
    working_dir = workspace_root / "nested"
    working_dir.mkdir()
    request = _python_request(
        """
        import os
        print(f"{os.environ['STAGE3_TOKEN']}|{os.getcwd()}")
        """,
        cwd=Path("nested"),
        env_overlay={"STAGE3_TOKEN": "present"},
    )

    result = runtime.execute(request)

    assert result.exit_code == 0
    assert result.stdout.strip() == f"present|{working_dir}"


@pytest.mark.asyncio
async def test_streaming_execution_preserves_stdout_order(tmp_path: Path) -> None:
    runtime, _workspace_root = _runtime(tmp_path)
    events: list[str] = []
    request = _python_request(
        """
        import sys
        import time

        for item in ("one", "two", "three"):
            sys.stdout.write(f"{item}\\n")
            sys.stdout.flush()
            time.sleep(0.2)
        """,
        mode=ExecutionMode.STREAM,
    )

    result = await runtime.execute_streaming(
        request,
        on_event=lambda event: events.append(event.text)
        if event.stream is OutputStream.STDOUT
        else None,
    )

    assert result.exit_code == 0
    assert "".join(events) == "one\ntwo\nthree\n"
    assert result.stdout == "one\ntwo\nthree\n"


def test_timeout_kills_the_child_process(tmp_path: Path) -> None:
    runtime, _workspace_root = _runtime(tmp_path)
    pid_file = tmp_path / "timeout.pid"
    request = _python_request(
        f"""
        import os
        import pathlib
        import time

        pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")
        print("started", flush=True)
        time.sleep(30)
        """,
        timeout_seconds=1,
    )

    with pytest.raises(ShellExecutionTimeout) as exc_info:
        runtime.execute(request)

    result = exc_info.value.result
    assert result is not None
    assert result.timed_out is True
    assert result.stdout == "started\n"

    pid = int(pid_file.read_text(encoding="utf-8"))
    time.sleep(0.2)
    _assert_process_gone(pid)


@pytest.mark.asyncio
async def test_streaming_cancellation_kills_the_child_process(tmp_path: Path) -> None:
    runtime, _workspace_root = _runtime(tmp_path)
    pid_file = tmp_path / "cancel.pid"
    request = _python_request(
        f"""
        import os
        import pathlib
        import time

        pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")
        print("started", flush=True)
        time.sleep(30)
        """,
        mode=ExecutionMode.STREAM,
        timeout_seconds=10,
    )

    task = asyncio.create_task(runtime.execute_streaming(request))
    for _ in range(20):
        if pid_file.exists():
            break
        await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(ShellExecutionCancelled) as exc_info:
        await task

    result = exc_info.value.result
    assert result is not None
    assert result.cancelled is True

    pid = int(pid_file.read_text(encoding="utf-8"))
    await asyncio.sleep(0.2)
    _assert_process_gone(pid)


def test_workspace_boundary_is_enforced(tmp_path: Path) -> None:
    runtime, _workspace_root = _runtime(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    request = _python_request("print('nope')", cwd=outside_dir)

    with pytest.raises(ValueError, match="workspace root"):
        runtime.execute(request)


def test_pty_mode_exposes_terminal_semantics(tmp_path: Path) -> None:
    runtime, _workspace_root = _runtime(tmp_path)
    request = _python_request(
        """
        import sys
        print(sys.stdout.isatty())
        """,
        mode=ExecutionMode.PTY,
    )

    result = runtime.execute(request)

    assert result.exit_code == 0
    assert result.stdout.strip() == "True"
    assert result.stderr == ""


def test_pty_timeout_preserves_termination_output(tmp_path: Path) -> None:
    runtime, _workspace_root = _runtime(tmp_path)
    request = _python_request(
        """
        import signal
        import time

        def handle_term(_signum, _frame):
            print("terminated", flush=True)
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, handle_term)
        print("started", flush=True)
        time.sleep(30)
        """,
        mode=ExecutionMode.PTY,
        timeout_seconds=1,
    )

    with pytest.raises(ShellExecutionTimeout) as exc_info:
        runtime.execute(request)

    result = exc_info.value.result
    assert result is not None
    assert result.timed_out is True
    assert "started" in result.stdout
    assert "terminated" in result.stdout
