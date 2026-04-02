"""Service-layer package for Foundation CLI."""

from foundation.services.shell import (
    ExecutionMode,
    OutputStream,
    ShellCommandRequest,
    ShellCommandResult,
    ShellExecutionCancelled,
    ShellExecutionError,
    ShellExecutionSpawnError,
    ShellExecutionTimeout,
    ShellOutputEvent,
    ShellRuntime,
)

__all__ = [
    "ExecutionMode",
    "OutputStream",
    "ShellCommandRequest",
    "ShellCommandResult",
    "ShellExecutionCancelled",
    "ShellExecutionError",
    "ShellExecutionSpawnError",
    "ShellExecutionTimeout",
    "ShellOutputEvent",
    "ShellRuntime",
]
