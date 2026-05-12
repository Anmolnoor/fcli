"""Install-mechanism detection, update, and uninstall helpers.

`foundation update` and `foundation uninstall` both need to figure out *how*
Foundation was installed (pipx vs pip --user vs dev checkout) so they can
print or run the right upgrade/removal command. None of this code runs the
commands itself; the CLI handler decides whether to execute or just display.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from foundation.settings import AppSettings
from foundation.shell_alias import _BLOCK_RE, detect_shell

DEFAULT_GIT_URL = "https://github.com/Anmolnoor/fcli.git"
DEFAULT_REF = "main"
PACKAGE_NAME = "foundation-cli"
LATEST_COMMIT_URL = "https://api.github.com/repos/Anmolnoor/fcli/commits/main"


class InstallMechanism(StrEnum):
    PIPX = "pipx"
    PIP_USER = "pip-user"
    DEV_CHECKOUT = "dev-checkout"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InstallProbe:
    mechanism: InstallMechanism
    executable: Path
    detail: str
    install_root: Path | None = None


@dataclass
class UpdatePlan:
    mechanism: InstallMechanism
    command: list[str] | None
    detail: str  # Human-readable explanation (always populated).


@dataclass
class AliasRemoval:
    removed: bool
    rc_path: Path | None
    backup_path: Path | None
    detail: str


@dataclass
class PurgeResult:
    removed: list[Path]
    skipped: list[Path]


def detect_install_mechanism(
    *,
    executable: Path | None = None,
    repo_root: Path | None = None,
) -> InstallProbe:
    """Classify how the running Foundation was installed.

    Heuristic, in priority order:
      1. ``sys.executable`` lives under ``~/.local/pipx/venvs/foundation-cli/`` → pipx.
      2. ``sys.executable`` lives under a sibling ``.venv/`` of a ``pyproject.toml``
         that declares ``foundation-cli`` → dev checkout.
      3. ``sys.executable`` lives under ``site-packages`` reachable from ``~/.local``
         → pip --user.
      4. Otherwise UNKNOWN with detail.
    """
    exe = Path(executable or sys.executable).resolve()
    home = Path.home().resolve()

    pipx_root = (home / ".local" / "pipx" / "venvs" / "foundation-cli").resolve()
    if _is_within(exe, pipx_root):
        return InstallProbe(
            mechanism=InstallMechanism.PIPX,
            executable=exe,
            detail=f"pipx venv at {pipx_root}",
            install_root=pipx_root,
        )

    # Dev checkout: walk up from the executable looking for a venv parent
    # whose grandparent contains pyproject.toml with our name.
    repo = repo_root or _find_dev_repo_root(exe)
    if repo is not None:
        return InstallProbe(
            mechanism=InstallMechanism.DEV_CHECKOUT,
            executable=exe,
            detail=f"dev checkout at {repo}",
            install_root=repo,
        )

    user_base_bin = home / ".local" / "bin"
    if _is_within(exe, home / ".local"):
        return InstallProbe(
            mechanism=InstallMechanism.PIP_USER,
            executable=exe,
            detail=f"pip --user install ({user_base_bin})",
            install_root=home / ".local",
        )

    return InstallProbe(
        mechanism=InstallMechanism.UNKNOWN,
        executable=exe,
        detail=f"could not classify install at {exe}",
    )


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _find_dev_repo_root(exe: Path) -> Path | None:
    """Walk ancestors of ``exe`` looking for a sibling pyproject.toml we own."""
    for candidate in [*exe.parents][:6]:
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file():
            try:
                text = pyproject.read_text(encoding="utf-8")
            except OSError:
                continue
            if 'name = "foundation-cli"' in text:
                return candidate.resolve()
    return None


def fetch_latest_sha(
    *,
    url: str = LATEST_COMMIT_URL,
    timeout_seconds: float = 5.0,
) -> str | None:
    """Return the short SHA of the latest commit on ``main``, or None on failure."""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "foundation-cli-updater",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    sha = payload.get("sha")
    if not isinstance(sha, str):
        return None
    return sha[:7]


def build_update_plan(
    probe: InstallProbe,
    *,
    git_url: str = DEFAULT_GIT_URL,
    ref: str = DEFAULT_REF,
) -> UpdatePlan:
    """Return the command (or guidance) to upgrade Foundation for this install."""
    target = f"git+{git_url}@{ref}"
    if probe.mechanism is InstallMechanism.PIPX:
        return UpdatePlan(
            mechanism=probe.mechanism,
            command=["pipx", "install", "--force", target],
            detail=f"Reinstalling via pipx from {target}.",
        )
    if probe.mechanism is InstallMechanism.PIP_USER:
        return UpdatePlan(
            mechanism=probe.mechanism,
            command=[sys.executable, "-m", "pip", "install", "--user", "--upgrade", target],
            detail=f"Upgrading via pip --user from {target}.",
        )
    if probe.mechanism is InstallMechanism.DEV_CHECKOUT:
        repo = probe.install_root or Path.cwd()
        return UpdatePlan(
            mechanism=probe.mechanism,
            command=None,
            detail=(
                f"Detected dev checkout at {repo}. "
                "Run `git pull && ./scripts/uv sync --extra dev` to refresh it."
            ),
        )
    return UpdatePlan(
        mechanism=probe.mechanism,
        command=None,
        detail=(
            f"Could not classify the install at {probe.executable}. "
            "Reinstall manually: `pipx install --force "
            f"{target}` (or pip equivalent)."
        ),
    )


def build_uninstall_plan(probe: InstallProbe) -> UpdatePlan:
    """Mirror of ``build_update_plan`` for uninstall."""
    if probe.mechanism is InstallMechanism.PIPX:
        return UpdatePlan(
            mechanism=probe.mechanism,
            command=["pipx", "uninstall", PACKAGE_NAME],
            detail="Removing the pipx-managed venv.",
        )
    if probe.mechanism is InstallMechanism.PIP_USER:
        return UpdatePlan(
            mechanism=probe.mechanism,
            command=[sys.executable, "-m", "pip", "uninstall", "-y", PACKAGE_NAME],
            detail="Uninstalling the pip --user install.",
        )
    if probe.mechanism is InstallMechanism.DEV_CHECKOUT:
        return UpdatePlan(
            mechanism=probe.mechanism,
            command=None,
            detail=(
                "Dev checkout — delete the repo directory yourself when you're done, "
                "or `pip uninstall foundation-cli` if you installed it editable elsewhere."
            ),
        )
    return UpdatePlan(
        mechanism=probe.mechanism,
        command=None,
        detail=(
            "Could not classify the install. "
            f"Try `pipx uninstall {PACKAGE_NAME}` or `pip uninstall {PACKAGE_NAME}`."
        ),
    )


def remove_alias_block(rc_path: Path | None = None) -> AliasRemoval:
    """Remove the marker-fenced `foundation init` alias block from the shell rc.

    If ``rc_path`` is omitted, we auto-detect via ``detect_shell``. Backs up
    to ``<rc>.bak`` if anything is removed; idempotent otherwise.
    """
    if rc_path is None:
        detected = detect_shell()
        if detected is None:
            return AliasRemoval(
                removed=False,
                rc_path=None,
                backup_path=None,
                detail="Could not detect shell; no alias removed.",
            )
        rc_path = detected.rc_path
    rc_path = Path(rc_path).expanduser()
    if not rc_path.exists():
        return AliasRemoval(
            removed=False,
            rc_path=rc_path,
            backup_path=None,
            detail=f"No rc file at {rc_path}.",
        )

    text = rc_path.read_text(encoding="utf-8")
    if not _BLOCK_RE.search(text):
        return AliasRemoval(
            removed=False,
            rc_path=rc_path,
            backup_path=None,
            detail=f"No managed alias block in {rc_path}.",
        )

    backup_path = rc_path.with_suffix(rc_path.suffix + ".bak")
    backup_path.write_text(text, encoding="utf-8")
    new_text = _BLOCK_RE.sub("", text, count=1)
    # Squash the leading/trailing blank line our removal introduced.
    new_text = new_text.replace("\n\n\n", "\n\n")
    rc_path.write_text(new_text, encoding="utf-8")
    return AliasRemoval(
        removed=True,
        rc_path=rc_path,
        backup_path=backup_path,
        detail=f"Removed alias block from {rc_path} (backup: {backup_path}).",
    )


def purge_state_dirs(settings: AppSettings) -> PurgeResult:
    """Delete the three Foundation platform directories.

    Returns lists of (removed, skipped) absolute paths. Skipped means the
    directory did not exist; we never raise for that case.
    """
    candidates = [
        Path(settings.app.data_dir),
        Path(settings.app.state_dir),
        # The config dir is the parent of config.toml.
        Path(settings.config_path).parent,
    ]
    removed: list[Path] = []
    skipped: list[Path] = []
    for raw in candidates:
        target = raw.expanduser().resolve()
        if target.exists():
            shutil.rmtree(target, ignore_errors=False)
            removed.append(target)
        else:
            skipped.append(target)
    # Best-effort: also remove the user log dir if it's distinct.
    log_dir = Path(settings.app.log_dir).expanduser().resolve()
    if log_dir.exists() and log_dir not in {p.resolve() for p in removed}:
        shutil.rmtree(log_dir, ignore_errors=False)
        removed.append(log_dir)
    return PurgeResult(removed=removed, skipped=skipped)


def pipx_available() -> bool:
    """Return True iff `pipx` is on PATH (informational; callers may still try it)."""
    return shutil.which("pipx") is not None


def _os_path_for_display(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    if text.startswith(home + os.sep):
        return "~" + text[len(home) :]
    return text
