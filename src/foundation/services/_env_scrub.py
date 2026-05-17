"""Sanitize ambient environment before spawning subprocesses.

Prevents Foundation's own configuration vars from leaking into child
processes. Without this, running verification commands (e.g. pytest) from
inside an agent inherits any ``FOUNDATION_*`` overrides the operator set,
which silently redirects the child's settings resolution and can pollute
shared state like the history database.
"""

from __future__ import annotations

from collections.abc import Mapping

DEFAULT_SCRUB_PREFIX = "FOUNDATION_"


def scrub_ambient_env(
    env: Mapping[str, str],
    *,
    prefix: str = DEFAULT_SCRUB_PREFIX,
) -> dict[str, str]:
    """Return a copy of ``env`` with keys starting with ``prefix`` removed."""
    return {key: value for key, value in env.items() if not key.startswith(prefix)}
