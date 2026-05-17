"""Temp-file staging helpers for safer workspace rewrites."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from foundation.models.history import StagedWorkspaceWrite


def _utcnow() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class WorkspaceRewriteStager:
    """Stage workspace file rewrites in temp files before atomic replacement."""

    def __init__(self, *, workspace_root: Path, state_dir: Path) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._staging_dir = Path(state_dir).expanduser().resolve() / "staging"
        self._staging_dir.mkdir(parents=True, exist_ok=True)

    def stage_text(self, *, target_path: Path, content: str) -> StagedWorkspaceWrite:
        resolved_target = self._resolve_target(target_path)
        staged_path = self._staging_dir / f"{uuid4().hex}.tmp"
        staged_path.write_text(content, encoding="utf-8")
        return StagedWorkspaceWrite(
            target_path=str(resolved_target),
            staged_path=str(staged_path),
            created_at=_utcnow(),
        )

    def commit(self, staged_write: StagedWorkspaceWrite) -> None:
        resolved_target = self._resolve_target(Path(staged_write.target_path))
        staged_path = Path(staged_write.staged_path).expanduser().resolve()
        if not staged_path.exists():
            raise FileNotFoundError(f"Staged file does not exist: {staged_path}")
        resolved_target.parent.mkdir(parents=True, exist_ok=True)
        staged_path.replace(resolved_target)

    def _resolve_target(self, target_path: Path) -> Path:
        resolved = (
            target_path.resolve()
            if target_path.is_absolute()
            else (self._workspace_root / target_path).resolve()
        )
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError as exc:
            raise ValueError("Target path must stay within the configured workspace root.") from exc
        return resolved
