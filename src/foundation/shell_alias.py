"""Install a shell alias (`alias fcli="foundation"`) into the user's rc file.

Idempotent: writes a marker-fenced block (``# >>> foundation cli alias >>>`` …
``# <<< foundation cli alias <<<``) and replaces the block contents on re-run
instead of duplicating it. Pure helpers — the wizard CLI handler does the
prompting.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

MARKER_START = "# >>> foundation cli alias >>>"
MARKER_END = "# <<< foundation cli alias <<<"

_BLOCK_RE = re.compile(
    rf"(?:^|\n){re.escape(MARKER_START)}.*?{re.escape(MARKER_END)}\n?",
    re.DOTALL,
)


class ShellKind(StrEnum):
    BASH = "bash"
    ZSH = "zsh"
    FISH = "fish"


@dataclass(frozen=True)
class DetectedShell:
    kind: ShellKind
    rc_path: Path


@dataclass
class AliasInstallResult:
    installed: bool
    replaced: bool
    rc_path: Path
    backup_path: Path | None
    detail: str


_RC_BY_SHELL: dict[ShellKind, tuple[Path, ...]] = {
    ShellKind.ZSH: (Path("~/.zshrc"),),
    ShellKind.BASH: (Path("~/.bashrc"), Path("~/.bash_profile")),
    ShellKind.FISH: (Path("~/.config/fish/config.fish"),),
}


def detect_shell(
    *,
    shell_env: str | None = None,
    home: Path | None = None,
) -> DetectedShell | None:
    """Return the user's active shell and rc path, or None if undetectable.

    Strategy: read ``$SHELL`` to decide which family we're in, then pick the
    first rc path for that family that already exists. If none exist, return
    the canonical path for that shell so the caller can create it.
    """
    shell_value = (shell_env if shell_env is not None else os.environ.get("SHELL", "")).strip()
    home_dir = home or Path.home()

    kind: ShellKind | None = None
    basename = Path(shell_value).name if shell_value else ""
    if basename == "zsh":
        kind = ShellKind.ZSH
    elif basename == "bash":
        kind = ShellKind.BASH
    elif basename == "fish":
        kind = ShellKind.FISH

    if kind is None:
        # Fall back to filesystem evidence in a stable preference order.
        for candidate, fallback_kind in (
            (home_dir / ".zshrc", ShellKind.ZSH),
            (home_dir / ".bashrc", ShellKind.BASH),
            (home_dir / ".bash_profile", ShellKind.BASH),
            (home_dir / ".config" / "fish" / "config.fish", ShellKind.FISH),
        ):
            if candidate.exists():
                return DetectedShell(kind=fallback_kind, rc_path=candidate)
        return None

    candidates = [_expand(home_dir, p) for p in _RC_BY_SHELL[kind]]
    for candidate in candidates:
        if candidate.exists():
            return DetectedShell(kind=kind, rc_path=candidate)
    return DetectedShell(kind=kind, rc_path=candidates[0])


def _expand(home_dir: Path, path: Path) -> Path:
    text = str(path)
    if text.startswith("~/"):
        return home_dir / text[2:]
    if text == "~":
        return home_dir
    return path


def render_alias_block(alias_name: str, target: str, shell: ShellKind) -> str:
    """Return the marker-fenced block to write into the user's rc file."""
    line = _format_alias_line(alias_name, target, shell)
    return f"{MARKER_START}\n{line}\n{MARKER_END}\n"


def _format_alias_line(alias_name: str, target: str, shell: ShellKind) -> str:
    if shell is ShellKind.FISH:
        # Fish uses single quotes and `alias name 'cmd'`.
        escaped = target.replace("'", "'\\''")
        return f"alias {alias_name} '{escaped}'"
    # Bash + Zsh: double-quoted value.
    escaped = target.replace("\\", "\\\\").replace('"', '\\"')
    return f'alias {alias_name}="{escaped}"'


def is_block_present(text: str) -> bool:
    return _BLOCK_RE.search(text) is not None


def install_alias(
    rc_path: Path,
    block: str,
) -> AliasInstallResult:
    """Atomically install the alias block in ``rc_path``.

    On re-run the previous block is replaced in-place and the old file is
    backed up to ``<rc>.bak``. Lines outside the marker fence are never
    touched.
    """
    rc_path = Path(rc_path).expanduser()
    rc_path.parent.mkdir(parents=True, exist_ok=True)

    existing = rc_path.read_text(encoding="utf-8") if rc_path.exists() else ""
    replaced = is_block_present(existing)
    backup_path: Path | None = None

    if replaced:
        backup_path = rc_path.with_suffix(rc_path.suffix + ".bak")
        backup_path.write_text(existing, encoding="utf-8")
        new_text = _BLOCK_RE.sub("\n" + block, existing, count=1)
        if not new_text.endswith("\n"):
            new_text += "\n"
    else:
        prefix = existing if existing.endswith("\n") or not existing else existing + "\n"
        new_text = f"{prefix}\n{block}" if prefix else block

    _atomic_write_text(rc_path, new_text)

    detail = (
        f"Replaced existing alias block in {rc_path}."
        if replaced
        else f"Added alias block to {rc_path}."
    )
    return AliasInstallResult(
        installed=True,
        replaced=replaced,
        rc_path=rc_path,
        backup_path=backup_path,
        detail=detail,
    )


def _atomic_write_text(path: Path, content: str) -> None:
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(prefix=".foundation.alias.", dir=str(directory))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        # Preserve a sane default mode for shell rc files.
        if path.exists():
            os.chmod(tmp_path, path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
