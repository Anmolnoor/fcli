"""Interactive setup wizard for first-run configuration.

Authors `~/.config/foundation/config.toml` and `~/.config/foundation/foundation.env`
(chmod 0600) from a few prompts, then optionally probes the provider so the
user finds out about a bad API key before their first chat instead of after.

Pure helpers do not import Typer or prompt_toolkit; the interactive runner
isolates IO so the helpers stay unit-testable.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from foundation.models import (
    ProviderMessage,
    ProviderMessageRole,
    ProviderPrompt,
    ProviderResponseFormat,
)
from foundation.services.provider import ProviderError, build_provider_adapter
from foundation.settings import (
    OLLAMA_DEFAULT_API_KEY_ENV_VAR,
    OPENAI_DEFAULT_API_KEY_ENV_VAR,
    AppSettings,
    ProviderSection,
    _read_env_file,
    default_config_path,
    default_env_file_path,
    load_settings,
)

SUPPORTED_PROVIDERS: tuple[str, ...] = ("openai", "ollama")
DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-5-mini",
    "ollama": "qwen3:8b",
}


@dataclass
class WizardChoices:
    """User selections gathered (or provided non-interactively) for the wizard."""

    provider: str
    model: str
    workspace_root: Path
    config_path: Path
    env_file_path: Path
    api_key_env_var: str
    api_key: str | None = None  # None = user skipped (e.g., local ollama)

    def normalized(self) -> WizardChoices:
        return WizardChoices(
            provider=self.provider.strip().lower(),
            model=self.model.strip(),
            workspace_root=Path(self.workspace_root).expanduser().resolve(),
            config_path=Path(self.config_path).expanduser().resolve(),
            env_file_path=Path(self.env_file_path).expanduser().resolve(),
            api_key_env_var=self.api_key_env_var.strip(),
            api_key=self.api_key,
        )


@dataclass
class ProbeResult:
    """Outcome of the optional post-write provider probe."""

    ok: bool
    detail: str


@dataclass
class WizardOutcome:
    """Aggregate result a CLI handler can render."""

    choices: WizardChoices
    config_written: bool
    config_backed_up_to: Path | None = None
    env_written: bool = False
    probe: ProbeResult | None = None
    notes: list[str] = field(default_factory=list)


def default_choices(existing: AppSettings | None) -> WizardChoices:
    """Seed defaults from an existing AppSettings, or from raw library defaults."""
    if existing is not None:
        provider = existing.provider.normalized_name()
        env_var = existing.provider.effective_api_key_env_var() or _default_env_var(provider)
        return WizardChoices(
            provider=provider,
            model=existing.provider.model,
            workspace_root=existing.app.workspace_root,
            config_path=existing.config_path,
            env_file_path=existing.env_file_path,
            api_key_env_var=env_var,
        )

    section = ProviderSection()
    provider = section.normalized_name()
    return WizardChoices(
        provider=provider,
        model=section.model,
        workspace_root=Path.cwd().resolve(),
        config_path=default_config_path(),
        env_file_path=default_env_file_path(),
        api_key_env_var=section.effective_api_key_env_var() or _default_env_var(provider),
    )


def _default_env_var(provider: str) -> str:
    return (
        OLLAMA_DEFAULT_API_KEY_ENV_VAR if provider == "ollama" else OPENAI_DEFAULT_API_KEY_ENV_VAR
    )


def render_config_toml(choices: WizardChoices) -> str:
    """Render a minimal config.toml matching the AppSettings schema.

    Only sections the wizard actually configures are emitted; everything else
    relies on Pydantic defaults so the file stays small and readable.
    """
    choices = choices.normalized()
    env_var = choices.api_key_env_var or _default_env_var(choices.provider)
    workspace = _toml_quote(str(choices.workspace_root))
    name = _toml_quote(choices.provider)
    model = _toml_quote(choices.model)
    api_var = _toml_quote(env_var)
    return (
        "# Foundation CLI configuration — written by `foundation init`.\n"
        "# See docs/QUICKSTART.md for the full schema.\n"
        "\n"
        "[app]\n"
        f"workspace_root = {workspace}\n"
        "\n"
        "[provider]\n"
        f"name = {name}\n"
        f"model = {model}\n"
        f"api_key_env_var = {api_var}\n"
    )


def _toml_quote(value: str) -> str:
    # TOML basic strings: backslash-escape backslash and double quote, wrap in `"`.
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_config_toml(path: Path, content: str, *, overwrite: bool) -> Path | None:
    """Write the config atomically. Backs the previous file up to `.toml.bak`.

    Returns the backup path (if any). Raises FileExistsError when the file
    exists and ``overwrite`` is False.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if path.exists():
        if not overwrite:
            raise FileExistsError(f"Config already exists at {path}; pass --force to replace it.")
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_bytes(path.read_bytes())

    _atomic_write_text(path, content, mode=0o644)
    return backup


def write_env_file(path: Path, key_var: str, key_value: str) -> None:
    """Set ``KEY=value`` in the env file, preserving any other entries.

    Atomic via tmp + replace. File mode is forced to ``0o600``.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, str]
    if path.exists():
        # Reuse the same parser the loader uses so we don't accept lines the
        # rest of the codebase would reject.
        existing = _read_env_file(path, config_path=path)
    else:
        existing = {}

    existing[key_var] = key_value

    lines = [f"{key}={value}" for key, value in existing.items()]
    body = "\n".join(lines) + "\n"
    _atomic_write_text(path, body, mode=0o600)


def _atomic_write_text(path: Path, content: str, *, mode: int) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".foundation.", dir=str(directory))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    # Ensure the final file has the requested mode even if replace inherited
    # something else on the destination (mostly defensive for re-runs).
    os.chmod(path, mode)


def probe_provider(settings: AppSettings) -> ProbeResult:
    """Send a 1-token ping to the configured provider and surface the result."""
    try:
        adapter = build_provider_adapter(settings)
        adapter.complete(
            ProviderPrompt(
                messages=[
                    ProviderMessage(role=ProviderMessageRole.USER, content="ping"),
                ],
                response_format=ProviderResponseFormat.TEXT,
            )
        )
    except ProviderError as exc:
        return ProbeResult(ok=False, detail=f"{exc.code.value}: {exc}")
    except Exception as exc:  # pragma: no cover - defensive, surface root cause
        return ProbeResult(ok=False, detail=f"{type(exc).__name__}: {exc}")
    return ProbeResult(ok=True, detail="Provider responded successfully.")


def apply_choices(
    choices: WizardChoices,
    *,
    overwrite: bool,
    write_key: bool,
) -> tuple[Path | None, bool]:
    """Persist a `WizardChoices`. Returns (config_backup_path, env_written)."""
    choices = choices.normalized()
    backup = write_config_toml(
        choices.config_path,
        render_config_toml(choices),
        overwrite=overwrite,
    )
    env_written = False
    if write_key and choices.api_key:
        write_env_file(choices.env_file_path, choices.api_key_env_var, choices.api_key)
        env_written = True
    return backup, env_written


def run_wizard(
    *,
    existing: AppSettings | None,
    choices: WizardChoices | None = None,
    overwrite: bool = False,
    probe: bool = True,
    interactive_prompts: WizardPrompts | None = None,
) -> WizardOutcome:
    """Drive the wizard end-to-end.

    When ``choices`` is supplied the caller is non-interactive (CLI flags or
    tests). Otherwise ``interactive_prompts`` is invoked to gather choices from
    the user.
    """
    if choices is None:
        if interactive_prompts is None:
            raise ValueError("run_wizard requires either explicit choices or interactive_prompts.")
        seed = default_choices(existing)
        choices = interactive_prompts.gather(seed=seed, existing=existing)

    notes: list[str] = []
    backup, env_written = apply_choices(
        choices,
        overwrite=overwrite,
        write_key=choices.api_key is not None,
    )
    if backup is not None:
        notes.append(f"Backed up previous config to {backup}.")
    if not env_written and choices.api_key is None:
        notes.append(
            f"No API key written — set ${choices.api_key_env_var} in the environment "
            f"or rerun `foundation init` to populate {choices.env_file_path}."
        )

    probe_result: ProbeResult | None = None
    if probe:
        try:
            settings = load_settings(choices.config_path)
        except Exception as exc:  # pragma: no cover - load just succeeded above
            probe_result = ProbeResult(ok=False, detail=f"Could not reload config: {exc}")
        else:
            probe_result = probe_provider(settings)

    return WizardOutcome(
        choices=choices,
        config_written=True,
        config_backed_up_to=backup,
        env_written=env_written,
        probe=probe_result,
        notes=notes,
    )


class WizardPrompts:
    """Strategy interface — keeps prompt_toolkit IO out of the pure helpers."""

    def gather(
        self,
        *,
        seed: WizardChoices,
        existing: AppSettings | None,
    ) -> WizardChoices:  # pragma: no cover - interface
        raise NotImplementedError
