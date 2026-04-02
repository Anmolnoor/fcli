"""Shell execution runtime for Foundation CLI."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import pty
import select
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator

logger = logging.getLogger("foundation.services.shell")


class ExecutionMode(StrEnum):
    """Supported shell execution modes."""

    BUFFERED = "buffered"
    STREAM = "stream"
    PTY = "pty"


class OutputStream(StrEnum):
    """Output stream identifiers emitted by the runtime."""

    STDOUT = "stdout"
    STDERR = "stderr"
    PTY = "pty"


class ShellOutputEvent(BaseModel):
    """A chunk of output emitted during streaming execution."""

    stream: OutputStream
    text: str


class ShellCommandRequest(BaseModel):
    """A normalized shell command execution request."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    cwd: Path | None = None
    env_overlay: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: PositiveInt | None = None
    mode: ExecutionMode = ExecutionMode.BUFFERED
    approval_context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("cwd", mode="before")
    @classmethod
    def _normalize_cwd(cls, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        return Path(value).expanduser()

    @field_validator("args", mode="before")
    @classmethod
    def _normalize_args(cls, value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list | tuple):
            raise TypeError("args must be a list or tuple of strings")
        return [str(item) for item in value]

    @field_validator("env_overlay", mode="before")
    @classmethod
    def _normalize_env_overlay(cls, value: object) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError("env_overlay must be a mapping of strings")
        return {str(key): str(item) for key, item in value.items()}

    @property
    def argv(self) -> list[str]:
        """Return the command argv for subprocess execution."""
        return [self.command, *self.args]


class ShellCommandResult(BaseModel):
    """Normalized execution result suitable for logs and orchestration."""

    command: str
    args: list[str]
    cwd: Path
    mode: ExecutionMode
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = Field(ge=0.0)
    timed_out: bool = False
    cancelled: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    approval_context: dict[str, Any] = Field(default_factory=dict)

    @property
    def argv(self) -> list[str]:
        """Return the executed argv."""
        return [self.command, *self.args]

    @property
    def display_command(self) -> str:
        """Return a shell-escaped command string for display."""
        return shlex.join(self.argv)

    @property
    def ok(self) -> bool:
        """Return whether the command completed successfully."""
        return not self.timed_out and not self.cancelled and self.exit_code == 0


class ShellExecutionError(RuntimeError):
    """Base error for shell runtime failures that are not normal exit codes."""

    def __init__(
        self,
        message: str,
        *,
        request: ShellCommandRequest,
        result: ShellCommandResult | None = None,
    ) -> None:
        super().__init__(message)
        self.request = request
        self.result = result


class ShellExecutionTimeout(ShellExecutionError):
    """Raised when a command exceeds its allowed runtime."""


class ShellExecutionCancelled(ShellExecutionError):
    """Raised when a command is cancelled during execution."""


class ShellExecutionSpawnError(ShellExecutionError):
    """Raised when a command cannot be spawned at all."""


OutputCallback = Callable[[ShellOutputEvent], None]


@dataclass(slots=True)
class _PreparedCommand:
    request: ShellCommandRequest
    cwd: Path
    env: dict[str, str]
    timeout_seconds: int
    capture_limit_bytes: int

    @property
    def argv(self) -> list[str]:
        return self.request.argv


class _TextCapture:
    """Bounded text capture that records truncation without growing forever."""

    def __init__(self, limit_bytes: int) -> None:
        self._limit_bytes = limit_bytes
        self._parts: list[str] = []
        self._size_bytes = 0
        self.truncated = False

    def append(self, text: str) -> None:
        if not text:
            return
        if self._size_bytes >= self._limit_bytes:
            self.truncated = True
            return

        encoded = text.encode("utf-8")
        remaining = self._limit_bytes - self._size_bytes
        if len(encoded) <= remaining:
            self._parts.append(text)
            self._size_bytes += len(encoded)
            return

        truncated_text = encoded[:remaining].decode("utf-8", errors="ignore")
        if truncated_text:
            self._parts.append(truncated_text)
        self._size_bytes = self._limit_bytes
        self.truncated = True

    @property
    def text(self) -> str:
        return "".join(self._parts)


class ShellRuntime:
    """Execute shell commands within a configured workspace boundary."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        default_timeout_seconds: int,
        max_timeout_seconds: int,
        allow_pty: bool = True,
        capture_limit_kb: int = 256,
        enforce_workspace_boundary: bool = True,
        termination_grace_seconds: float = 1.0,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._default_timeout_seconds = default_timeout_seconds
        self._max_timeout_seconds = max_timeout_seconds
        self._allow_pty = allow_pty
        self._capture_limit_bytes = capture_limit_kb * 1024
        self._enforce_workspace_boundary = enforce_workspace_boundary
        self._termination_grace_seconds = termination_grace_seconds

    def execute(
        self,
        request: ShellCommandRequest,
        *,
        on_event: OutputCallback | None = None,
    ) -> ShellCommandResult:
        """Execute a request using the requested execution mode."""
        prepared = self._prepare_request(request)
        logger.info(
            "shell_execute_started mode=%s cwd=%s argv=%s timeout_seconds=%s",
            prepared.request.mode.value,
            prepared.cwd,
            prepared.argv,
            prepared.timeout_seconds,
        )
        if prepared.request.mode is ExecutionMode.BUFFERED:
            return self._execute_buffered(prepared)
        if prepared.request.mode is ExecutionMode.STREAM:
            return asyncio.run(self._execute_streaming(prepared, on_event=on_event))
        return self._execute_pty(prepared, on_event=on_event)

    async def execute_streaming(
        self,
        request: ShellCommandRequest,
        *,
        on_event: OutputCallback | None = None,
    ) -> ShellCommandResult:
        """Execute a request asynchronously in streaming mode."""
        prepared = self._prepare_request(request)
        if prepared.request.mode is not ExecutionMode.STREAM:
            raise ValueError("execute_streaming requires ExecutionMode.STREAM")
        return await self._execute_streaming(prepared, on_event=on_event)

    def _prepare_request(self, request: ShellCommandRequest) -> _PreparedCommand:
        timeout_seconds = request.timeout_seconds or self._default_timeout_seconds
        if timeout_seconds > self._max_timeout_seconds:
            raise ValueError(
                "Command timeout exceeds shell.max_timeout_seconds: "
                f"{timeout_seconds} > {self._max_timeout_seconds}"
            )

        cwd = self._resolve_cwd(request.cwd)
        if not cwd.exists():
            raise ValueError(f"Execution cwd does not exist: {cwd}")
        if not cwd.is_dir():
            raise ValueError(f"Execution cwd is not a directory: {cwd}")

        if self._enforce_workspace_boundary:
            try:
                cwd.relative_to(self._workspace_root)
            except ValueError as exc:
                raise ValueError(
                    "Execution cwd must stay within the configured workspace root: "
                    f"{self._workspace_root}"
                ) from exc

        if request.mode is ExecutionMode.PTY and not self._allow_pty:
            raise ValueError("PTY execution is disabled by configuration")

        env = os.environ.copy()
        env.update(request.env_overlay)

        return _PreparedCommand(
            request=request,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
            capture_limit_bytes=self._capture_limit_bytes,
        )

    def _resolve_cwd(self, value: Path | None) -> Path:
        if value is None:
            return self._workspace_root
        if value.is_absolute():
            return value.resolve()
        return (self._workspace_root / value).resolve()

    def _execute_buffered(self, prepared: _PreparedCommand) -> ShellCommandResult:
        started_at = time.monotonic()
        try:
            process = subprocess.Popen(
                prepared.argv,
                cwd=str(prepared.cwd),
                env=prepared.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except OSError as exc:
            logger.info(
                "shell_execute_spawn_failed mode=%s cwd=%s argv=%s error=%s",
                prepared.request.mode.value,
                prepared.cwd,
                prepared.argv,
                exc,
            )
            raise ShellExecutionSpawnError(
                f"Could not start command {prepared.request.command!r}: {exc}",
                request=prepared.request,
            ) from exc

        try:
            stdout, stderr = process.communicate(timeout=prepared.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self._terminate_sync_process(process)
            trailing_stdout, trailing_stderr = process.communicate()
            result = self._build_result(
                prepared,
                duration_seconds=time.monotonic() - started_at,
                exit_code=None,
                stdout=self._coalesce_timeout_output(exc.stdout, trailing_stdout),
                stderr=self._coalesce_timeout_output(exc.stderr, trailing_stderr),
                timed_out=True,
            )
            logger.info(
                "shell_execute_timed_out mode=%s cwd=%s argv=%s timeout_seconds=%s",
                prepared.request.mode.value,
                prepared.cwd,
                prepared.argv,
                prepared.timeout_seconds,
            )
            raise ShellExecutionTimeout(
                f"Command timed out after {prepared.timeout_seconds}s: {prepared.request.command}",
                request=prepared.request,
                result=result,
            ) from exc
        except KeyboardInterrupt as exc:
            self._terminate_sync_process(process)
            stdout, stderr = process.communicate()
            result = self._build_result(
                prepared,
                duration_seconds=time.monotonic() - started_at,
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                cancelled=True,
            )
            logger.info(
                "shell_execute_cancelled mode=%s cwd=%s argv=%s",
                prepared.request.mode.value,
                prepared.cwd,
                prepared.argv,
            )
            raise ShellExecutionCancelled(
                f"Command execution was cancelled: {prepared.request.command}",
                request=prepared.request,
                result=result,
            ) from exc

        result = self._build_result(
            prepared,
            duration_seconds=time.monotonic() - started_at,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )
        logger.info(
            "shell_execute_completed mode=%s cwd=%s argv=%s exit_code=%s duration_seconds=%.3f",
            prepared.request.mode.value,
            prepared.cwd,
            prepared.argv,
            result.exit_code,
            result.duration_seconds,
        )
        return result

    async def _execute_streaming(
        self,
        prepared: _PreparedCommand,
        *,
        on_event: OutputCallback | None = None,
    ) -> ShellCommandResult:
        started_at = time.monotonic()
        stdout_capture = _TextCapture(prepared.capture_limit_bytes)
        stderr_capture = _TextCapture(prepared.capture_limit_bytes)

        try:
            process = await asyncio.create_subprocess_exec(
                *prepared.argv,
                cwd=str(prepared.cwd),
                env=prepared.env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            logger.info(
                "shell_execute_spawn_failed mode=%s cwd=%s argv=%s error=%s",
                prepared.request.mode.value,
                prepared.cwd,
                prepared.argv,
                exc,
            )
            raise ShellExecutionSpawnError(
                f"Could not start command {prepared.request.command!r}: {exc}",
                request=prepared.request,
            ) from exc

        assert process.stdout is not None
        assert process.stderr is not None

        async def _consume_stream(
            stream: asyncio.StreamReader,
            stream_name: OutputStream,
            capture: _TextCapture,
        ) -> None:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                text = chunk.decode("utf-8", errors="replace")
                capture.append(text)
                if on_event is not None:
                    on_event(ShellOutputEvent(stream=stream_name, text=text))

        consumers = [
            asyncio.create_task(
                _consume_stream(process.stdout, OutputStream.STDOUT, stdout_capture)
            ),
            asyncio.create_task(
                _consume_stream(process.stderr, OutputStream.STDERR, stderr_capture)
            ),
        ]

        try:
            await asyncio.wait_for(process.wait(), timeout=prepared.timeout_seconds)
            await asyncio.gather(*consumers)
        except TimeoutError as exc:
            await self._terminate_async_process(process)
            await asyncio.gather(*consumers, return_exceptions=True)
            result = self._build_result(
                prepared,
                duration_seconds=time.monotonic() - started_at,
                exit_code=None,
                stdout=stdout_capture.text,
                stderr=stderr_capture.text,
                timed_out=True,
                stdout_truncated=stdout_capture.truncated,
                stderr_truncated=stderr_capture.truncated,
            )
            logger.info(
                "shell_execute_timed_out mode=%s cwd=%s argv=%s timeout_seconds=%s",
                prepared.request.mode.value,
                prepared.cwd,
                prepared.argv,
                prepared.timeout_seconds,
            )
            raise ShellExecutionTimeout(
                f"Command timed out after {prepared.timeout_seconds}s: {prepared.request.command}",
                request=prepared.request,
                result=result,
            ) from exc
        except asyncio.CancelledError as exc:
            await self._terminate_async_process(process)
            await asyncio.gather(*consumers, return_exceptions=True)
            result = self._build_result(
                prepared,
                duration_seconds=time.monotonic() - started_at,
                exit_code=None,
                stdout=stdout_capture.text,
                stderr=stderr_capture.text,
                cancelled=True,
                stdout_truncated=stdout_capture.truncated,
                stderr_truncated=stderr_capture.truncated,
            )
            logger.info(
                "shell_execute_cancelled mode=%s cwd=%s argv=%s",
                prepared.request.mode.value,
                prepared.cwd,
                prepared.argv,
            )
            raise ShellExecutionCancelled(
                f"Command execution was cancelled: {prepared.request.command}",
                request=prepared.request,
                result=result,
            ) from exc

        result = self._build_result(
            prepared,
            duration_seconds=time.monotonic() - started_at,
            exit_code=process.returncode,
            stdout=stdout_capture.text,
            stderr=stderr_capture.text,
            stdout_truncated=stdout_capture.truncated,
            stderr_truncated=stderr_capture.truncated,
        )
        logger.info(
            "shell_execute_completed mode=%s cwd=%s argv=%s exit_code=%s duration_seconds=%.3f",
            prepared.request.mode.value,
            prepared.cwd,
            prepared.argv,
            result.exit_code,
            result.duration_seconds,
        )
        return result

    def _execute_pty(
        self,
        prepared: _PreparedCommand,
        *,
        on_event: OutputCallback | None = None,
    ) -> ShellCommandResult:
        started_at = time.monotonic()
        capture = _TextCapture(prepared.capture_limit_bytes)
        master_fd, slave_fd = pty.openpty()

        try:
            try:
                process = subprocess.Popen(
                    prepared.argv,
                    cwd=str(prepared.cwd),
                    env=prepared.env,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as exc:
                logger.info(
                    "shell_execute_spawn_failed mode=%s cwd=%s argv=%s error=%s",
                    prepared.request.mode.value,
                    prepared.cwd,
                    prepared.argv,
                    exc,
                )
                raise ShellExecutionSpawnError(
                    f"Could not start command {prepared.request.command!r}: {exc}",
                    request=prepared.request,
                ) from exc
        finally:
            os.close(slave_fd)

        try:
            while True:
                self._raise_for_pty_timeout(
                    prepared,
                    started_at,
                    process,
                    master_fd,
                    capture,
                    on_event,
                )
                if process.poll() is not None and not self._pty_has_data(master_fd):
                    break

                ready, _, _ = select.select(
                    [master_fd],
                    [],
                    [],
                    min(0.1, self._pty_timeout_remaining(prepared, started_at)),
                )
                if not ready:
                    continue

                data = self._read_pty_chunk(master_fd)
                if not data:
                    if process.poll() is not None:
                        break
                    continue

                text = data.decode("utf-8", errors="replace")
                capture.append(text)
                if on_event is not None:
                    on_event(ShellOutputEvent(stream=OutputStream.PTY, text=text))
        except KeyboardInterrupt as exc:
            self._terminate_pty_process(process, master_fd, capture, on_event)
            result = self._build_result(
                prepared,
                duration_seconds=time.monotonic() - started_at,
                exit_code=None,
                stdout=capture.text,
                stderr="",
                cancelled=True,
                stdout_truncated=capture.truncated,
            )
            logger.info(
                "shell_execute_cancelled mode=%s cwd=%s argv=%s",
                prepared.request.mode.value,
                prepared.cwd,
                prepared.argv,
            )
            raise ShellExecutionCancelled(
                f"Command execution was cancelled: {prepared.request.command}",
                request=prepared.request,
                result=result,
            ) from exc
        finally:
            os.close(master_fd)

        result = self._build_result(
            prepared,
            duration_seconds=time.monotonic() - started_at,
            exit_code=process.wait(),
            stdout=capture.text,
            stderr="",
            stdout_truncated=capture.truncated,
        )
        logger.info(
            "shell_execute_completed mode=%s cwd=%s argv=%s exit_code=%s duration_seconds=%.3f",
            prepared.request.mode.value,
            prepared.cwd,
            prepared.argv,
            result.exit_code,
            result.duration_seconds,
        )
        return result

    def _build_result(
        self,
        prepared: _PreparedCommand,
        *,
        duration_seconds: float,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        timed_out: bool = False,
        cancelled: bool = False,
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
    ) -> ShellCommandResult:
        stdout_capture = _TextCapture(prepared.capture_limit_bytes)
        stderr_capture = _TextCapture(prepared.capture_limit_bytes)
        stdout_capture.append(stdout)
        stderr_capture.append(stderr)
        return ShellCommandResult(
            command=prepared.request.command,
            args=prepared.request.args,
            cwd=prepared.cwd,
            mode=prepared.request.mode,
            exit_code=exit_code,
            stdout=stdout_capture.text,
            stderr=stderr_capture.text,
            duration_seconds=duration_seconds,
            timed_out=timed_out,
            cancelled=cancelled,
            stdout_truncated=stdout_truncated or stdout_capture.truncated,
            stderr_truncated=stderr_truncated or stderr_capture.truncated,
            approval_context=prepared.request.approval_context,
        )

    def _raise_for_pty_timeout(
        self,
        prepared: _PreparedCommand,
        started_at: float,
        process: subprocess.Popen[bytes],
        master_fd: int,
        capture: _TextCapture,
        on_event: OutputCallback | None,
    ) -> None:
        if time.monotonic() - started_at <= prepared.timeout_seconds:
            return
        self._terminate_pty_process(process, master_fd, capture, on_event)
        result = self._build_result(
            prepared,
            duration_seconds=time.monotonic() - started_at,
            exit_code=None,
            stdout=capture.text,
            stderr="",
            timed_out=True,
            stdout_truncated=capture.truncated,
        )
        logger.info(
            "shell_execute_timed_out mode=%s cwd=%s argv=%s timeout_seconds=%s",
            prepared.request.mode.value,
            prepared.cwd,
            prepared.argv,
            prepared.timeout_seconds,
        )
        raise ShellExecutionTimeout(
            f"Command timed out after {prepared.timeout_seconds}s: {prepared.request.command}",
            request=prepared.request,
            result=result,
        )

    def _pty_timeout_remaining(self, prepared: _PreparedCommand, started_at: float) -> float:
        remaining = prepared.timeout_seconds - (time.monotonic() - started_at)
        return max(remaining, 0.0)

    def _pty_has_data(self, master_fd: int) -> bool:
        ready, _, _ = select.select([master_fd], [], [], 0)
        return bool(ready)

    def _drain_pty(
        self,
        master_fd: int,
        capture: _TextCapture,
        on_event: OutputCallback | None,
        *,
        settle_timeout_seconds: float = 0.0,
    ) -> None:
        last_activity = time.monotonic()
        while True:
            if settle_timeout_seconds > 0:
                quiet_period = time.monotonic() - last_activity
                timeout = max(0.0, min(0.05, settle_timeout_seconds - quiet_period))
                if timeout == 0.0 and not self._pty_has_data(master_fd):
                    return
                ready, _, _ = select.select([master_fd], [], [], timeout)
                if not ready:
                    if time.monotonic() - last_activity >= settle_timeout_seconds:
                        return
                    continue
            elif not self._pty_has_data(master_fd):
                return

            data = self._read_pty_chunk(master_fd)
            if not data:
                if settle_timeout_seconds == 0 or (
                    time.monotonic() - last_activity >= settle_timeout_seconds
                ):
                    return
                continue
            self._record_pty_output(data, capture, on_event)
            last_activity = time.monotonic()

    def _terminate_pty_process(
        self,
        process: subprocess.Popen[bytes],
        master_fd: int,
        capture: _TextCapture,
        on_event: OutputCallback | None,
    ) -> None:
        if process.poll() is None:
            self._signal_process_group(process.pid, signal.SIGTERM)
            deadline = time.monotonic() + self._termination_grace_seconds
            self._pump_pty_until(process, master_fd, capture, on_event, deadline=deadline)
            if process.poll() is None:
                self._signal_process_group(process.pid, signal.SIGKILL)
                self._pump_pty_until(
                    process,
                    master_fd,
                    capture,
                    on_event,
                    deadline=time.monotonic() + 0.1,
                )
                process.wait()

        self._drain_pty(
            master_fd,
            capture,
            on_event,
            settle_timeout_seconds=min(0.1, self._termination_grace_seconds),
        )

    def _pump_pty_until(
        self,
        process: subprocess.Popen[bytes],
        master_fd: int,
        capture: _TextCapture,
        on_event: OutputCallback | None,
        *,
        deadline: float,
    ) -> None:
        while time.monotonic() < deadline:
            if process.poll() is not None and not self._pty_has_data(master_fd):
                return
            timeout = min(0.05, max(0.0, deadline - time.monotonic()))
            ready, _, _ = select.select([master_fd], [], [], timeout)
            if not ready:
                continue
            data = self._read_pty_chunk(master_fd)
            if not data:
                if process.poll() is not None:
                    return
                continue
            self._record_pty_output(data, capture, on_event)

    def _record_pty_output(
        self,
        data: bytes,
        capture: _TextCapture,
        on_event: OutputCallback | None,
    ) -> None:
        text = data.decode("utf-8", errors="replace")
        capture.append(text)
        if on_event is not None:
            on_event(ShellOutputEvent(stream=OutputStream.PTY, text=text))

    def _read_pty_chunk(self, master_fd: int) -> bytes:
        try:
            return os.read(master_fd, 4096)
        except OSError as exc:
            if exc.errno == errno.EIO:
                return b""
            raise

    async def _terminate_async_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return

        self._signal_process_group(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=self._termination_grace_seconds)
        except TimeoutError:
            self._signal_process_group(process.pid, signal.SIGKILL)
            await process.wait()

    def _terminate_sync_process(
        self,
        process: subprocess.Popen[str] | subprocess.Popen[bytes],
    ) -> None:
        if process.poll() is not None:
            return

        self._signal_process_group(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=self._termination_grace_seconds)
        except subprocess.TimeoutExpired:
            self._signal_process_group(process.pid, signal.SIGKILL)
            process.wait()

    def _signal_process_group(self, pid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except ProcessLookupError:
            return

    def _coalesce_timeout_output(
        self,
        first: str | bytes | None,
        second: str | bytes | None,
    ) -> str:
        initial = self._coerce_text(first)
        final = self._coerce_text(second)
        if not final:
            return initial
        if not initial:
            return final
        if final.startswith(initial):
            return final
        if initial.endswith(final):
            return initial

        overlap = min(len(initial), len(final))
        for size in range(overlap, 0, -1):
            if initial.endswith(final[:size]):
                return f"{initial}{final[size:]}"
        return f"{initial}{final}"

    def _coerce_text(self, value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value
