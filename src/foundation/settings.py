"""Typed settings models and loaders for Stage 2."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, ClassVar

import keyring
from keyring.errors import KeyringError, NoKeyringError
from pydantic import (
    AnyUrl,
    BaseModel,
    Field,
    PositiveInt,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

ENV_PREFIX = "FOUNDATION_"


def _resolve_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def default_config_path() -> Path:
    """Return the default config file path for local development."""
    return Path.home() / ".config" / "foundation" / "config.toml"


def default_data_dir() -> Path:
    """Return the default data directory."""
    return Path.home() / ".local" / "share" / "foundation"


def default_state_dir() -> Path:
    """Return the default state directory."""
    return Path.home() / ".local" / "state" / "foundation"


def default_log_dir() -> Path:
    """Return the default log directory."""
    return default_state_dir() / "logs"


def default_history_database_path() -> Path:
    """Return the default history database path."""
    return default_state_dir() / "history.sqlite3"


class LogLevel(StrEnum):
    """Supported logging levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class ApprovalMode(StrEnum):
    """Approval strategies for future execution stages."""

    PROMPT = "prompt"
    AUTO = "auto"
    MANUAL = "manual"


class SecretResolutionStatus(StrEnum):
    """Outcome for provider secret resolution."""

    RESOLVED = "resolved"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


class SecretResolution(BaseModel):
    """Normalized provider secret lookup result."""

    status: SecretResolutionStatus
    source: str | None = None
    detail: str
    value: SecretStr | None = Field(default=None, exclude=True)


class AppSection(BaseModel):
    """Application directories and workspace settings."""

    name: str = "foundation"
    workspace_root: Path = Field(default_factory=lambda: Path.cwd().resolve())
    data_dir: Path = Field(default_factory=default_data_dir)
    state_dir: Path = Field(default_factory=default_state_dir)
    log_dir: Path = Field(default_factory=default_log_dir)

    @field_validator("workspace_root", "data_dir", "state_dir", "log_dir", mode="before")
    @classmethod
    def _normalize_paths(cls, value: str | Path) -> Path:
        return _resolve_path(value)


class KeychainSecretRef(BaseModel):
    """Keychain lookup coordinates for a provider credential."""

    service: str = "foundation"
    username: str = "openai_api_key"


class ProviderSection(BaseModel):
    """Provider settings for the future model adapter."""

    name: str = "openai"
    model: str = "gpt-5-mini"
    base_url: AnyUrl | None = None
    request_timeout_seconds: PositiveInt = 60
    api_key_env_var: str | None = "OPENAI_API_KEY"
    api_key_keychain: KeychainSecretRef | None = Field(default_factory=KeychainSecretRef)

    def credential_source_order(self) -> list[str]:
        """Return the configured secret source priority."""
        sources: list[str] = []
        if self.api_key_keychain is not None:
            sources.append(
                f"keychain:{self.api_key_keychain.service}/{self.api_key_keychain.username}"
            )
        if self.api_key_env_var:
            sources.append(f"env:{self.api_key_env_var}")
        return sources

    def resolve_api_key(self, environment: Mapping[str, str] | None = None) -> SecretResolution:
        """Resolve provider credentials without exposing the resulting value by default."""
        env = environment or os.environ
        keychain_failure: str | None = None

        if self.api_key_keychain is not None:
            try:
                value = keyring.get_password(
                    self.api_key_keychain.service,
                    self.api_key_keychain.username,
                )
            except NoKeyringError as exc:
                keychain_failure = f"Keychain backend unavailable: {exc}"
            except KeyringError as exc:
                keychain_failure = f"Keychain lookup failed: {exc}"
            else:
                if value:
                    return SecretResolution(
                        status=SecretResolutionStatus.RESOLVED,
                        source="keychain",
                        detail=(
                            "Resolved provider credentials from "
                            f"{self.api_key_keychain.service}/{self.api_key_keychain.username}."
                        ),
                        value=SecretStr(value),
                    )

        if self.api_key_env_var:
            value = env.get(self.api_key_env_var)
            if value:
                return SecretResolution(
                    status=SecretResolutionStatus.RESOLVED,
                    source="environment",
                    detail=f"Resolved provider credentials from ${self.api_key_env_var}.",
                    value=SecretStr(value),
                )

        if keychain_failure is not None:
            return SecretResolution(
                status=SecretResolutionStatus.UNAVAILABLE,
                source="keychain",
                detail=keychain_failure,
            )

        return SecretResolution(
            status=SecretResolutionStatus.MISSING,
            detail=(
                "Provider credentials were not found in the configured keychain entry or "
                "environment variable."
            ),
        )


class ShellSection(BaseModel):
    """Shell execution policy defaults for the next stage."""

    default_timeout_seconds: PositiveInt = 300
    max_timeout_seconds: PositiveInt = 3600
    allow_pty: bool = True
    capture_limit_kb: PositiveInt = 256
    enforce_workspace_boundary: bool = True

    @model_validator(mode="after")
    def _validate_timeout_bounds(self) -> ShellSection:
        if self.max_timeout_seconds < self.default_timeout_seconds:
            raise ValueError("shell.max_timeout_seconds must be >= shell.default_timeout_seconds")
        return self


class LoggingSection(BaseModel):
    """Logging configuration."""

    level: LogLevel = LogLevel.WARNING
    structured: bool = False


class HistorySection(BaseModel):
    """History retention and storage settings."""

    database_path: Path = Field(default_factory=default_history_database_path)
    retention_days: PositiveInt = 30
    max_entries: PositiveInt = 5000

    @field_validator("database_path", mode="before")
    @classmethod
    def _normalize_database_path(cls, value: str | Path) -> Path:
        return _resolve_path(value)


class ApprovalSection(BaseModel):
    """Approval defaults for future risky actions."""

    mode: ApprovalMode = ApprovalMode.PROMPT
    require_destructive: bool = True
    require_network: bool = True
    require_outside_workspace: bool = True


class AppSettings(BaseSettings):
    """Effective application settings for Foundation CLI."""

    app: AppSection = Field(default_factory=AppSection)
    provider: ProviderSection = Field(default_factory=ProviderSection)
    shell: ShellSection = Field(default_factory=ShellSection)
    logging: LoggingSection = Field(default_factory=LoggingSection)
    history: HistorySection = Field(default_factory=HistorySection)
    approval: ApprovalSection = Field(default_factory=ApprovalSection)

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        env_ignore_empty=True,
        nested_model_default_partial_update=True,
        validate_default=True,
        extra="ignore",
    )

    _toml_file_path: ClassVar[Path | None] = None
    _config_path: Path = PrivateAttr(default_factory=default_config_path)
    _config_exists: bool = PrivateAttr(default=False)
    _cli_overrides: dict[str, Any] = PrivateAttr(default_factory=dict)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        config_path = cls._toml_file_path or default_config_path()
        toml_source = TomlConfigSettingsSource(settings_cls, toml_file=config_path)
        return (init_settings, env_settings, toml_source)

    @property
    def app_name(self) -> str:
        """Return the configured application name."""
        return self.app.name

    @property
    def workspace_root(self) -> Path:
        """Return the configured workspace root."""
        return self.app.workspace_root

    @property
    def config_path(self) -> Path:
        """Return the config path used for loading settings."""
        return self._config_path

    @property
    def config_exists(self) -> bool:
        """Return whether the selected config file exists."""
        return self._config_exists

    @property
    def cli_overrides(self) -> dict[str, Any]:
        """Return the active CLI overrides used to build these settings."""
        return dict(self._cli_overrides)

    @property
    def debug(self) -> bool:
        """Return whether debug logging is enabled."""
        return self.logging.level is LogLevel.DEBUG

    def config_locations(self) -> dict[str, str]:
        """Return key filesystem locations relevant to the current settings."""
        return {
            "config_path": str(self.config_path),
            "workspace_root": str(self.app.workspace_root),
            "data_dir": str(self.app.data_dir),
            "state_dir": str(self.app.state_dir),
            "log_dir": str(self.app.log_dir),
            "history_database": str(self.history.database_path),
        }

    def metadata_dump(self) -> dict[str, Any]:
        """Return metadata about how the settings were loaded."""
        return {
            "config_path": str(self.config_path),
            "config_exists": self.config_exists,
            "env_prefix": ENV_PREFIX,
            "cli_overrides": self.cli_overrides,
        }

    def safe_dump(self) -> dict[str, Any]:
        """Return a safe rendering of the effective config without secret values."""
        payload = self.model_dump(mode="json")
        payload["provider"]["api_key"] = "[redacted]"
        payload["provider"]["credential_source_order"] = self.provider.credential_source_order()
        return payload


class SettingsLoadError(RuntimeError):
    """Raised when Foundation settings cannot be loaded safely."""

    def __init__(self, message: str, *, config_path: Path):
        super().__init__(message)
        self.config_path = config_path


def _validate_config_file(config_path: Path) -> bool:
    if not config_path.exists():
        return False

    try:
        with config_path.open("rb") as config_file:
            tomllib.load(config_file)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsLoadError(
            f"Invalid TOML in {config_path}: {exc}",
            config_path=config_path,
        ) from exc
    except OSError as exc:
        raise SettingsLoadError(
            f"Could not read config file {config_path}: {exc}",
            config_path=config_path,
        ) from exc

    return True


def _flatten_overrides(data: Mapping[str, Any], *, prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in data.items():
        composed_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            flattened.update(_flatten_overrides(value, prefix=composed_key))
            continue
        if isinstance(value, Path):
            flattened[composed_key] = str(value)
            continue
        if isinstance(value, Enum):
            flattened[composed_key] = value.value
            continue
        flattened[composed_key] = value
    return flattened


def load_settings(
    config_path: Path | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> AppSettings:
    """Load typed settings using defaults, TOML, environment variables, and CLI overrides."""
    resolved_config_path = _resolve_path(config_path or default_config_path())
    config_exists = _validate_config_file(resolved_config_path)
    init_overrides = dict(overrides or {})

    try:
        AppSettings._toml_file_path = resolved_config_path
        settings = AppSettings(**init_overrides)
    except ValidationError as exc:
        raise SettingsLoadError(
            f"Configuration validation failed for {resolved_config_path}:\n{exc}",
            config_path=resolved_config_path,
        ) from exc
    finally:
        AppSettings._toml_file_path = None

    settings._config_path = resolved_config_path
    settings._config_exists = config_exists
    settings._cli_overrides = _flatten_overrides(init_overrides)
    return settings


def render_settings_payload(settings: AppSettings) -> dict[str, Any]:
    """Return the safe config payload used by the CLI."""
    return {
        "metadata": settings.metadata_dump(),
        "settings": settings.safe_dump(),
    }
