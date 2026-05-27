"""Session-scoped, read-only out-of-workspace access grants.

When the user approves an out-of-scope read escalation, the granted directory
root is recorded here. Both the guardrail policy engine and the file service
consult the same store so a single grant unblocks reads under that root for the
rest of the session. Grants are read-only and in-memory (never persisted).
"""

from __future__ import annotations

from pathlib import Path


class ScopeGrantStore:
    """A set of additional directory roots the user has approved for reading."""

    def __init__(self) -> None:
        self._roots: set[Path] = set()

    def grant(self, root: Path) -> None:
        """Record a directory root as readable for the rest of the session."""
        self._roots.add(Path(root).expanduser().resolve())

    def is_granted(self, path: Path) -> bool:
        """Return whether ``path`` lies within any granted root."""
        resolved = Path(path).expanduser().resolve()
        for root in self._roots:
            if resolved == root or resolved.is_relative_to(root):
                return True
        return False

    @property
    def roots(self) -> frozenset[Path]:
        return frozenset(self._roots)
