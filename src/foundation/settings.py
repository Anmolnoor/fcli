"""Typed settings models and loaders for Stage 2."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

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
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

ENV_PREFIX = "FOUNDATION_"
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/api"
OPENAI_DEFAULT_API_KEY_ENV_VAR = "OPENAI_API_KEY"
OLLAMA_DEFAULT_API_KEY_ENV_VAR = "OLLAMA_API_KEY"
DEFAULT_KEYCHAIN_SERVICE = "foundation"
OPENAI_DEFAULT_KEYCHAIN_USERNAME = "openai_api_key"
OLLAMA_DEFAULT_KEYCHAIN_USERNAME = "ollama_api_key"
DEFAULT_ENV_FILE_NAME = "foundation.env"


def _provider_default_base_url(provider_name: str) -> str:
    normalized = provider_name.strip().lower()
    if normalized == "ollama":
        return OLLAMA_DEFAULT_BASE_URL
    return OPENAI_DEFAULT_BASE_URL


def _provider_default_api_key_env_var(provider_name: str) -> str:
    normalized = provider_name.strip().lower()
    if normalized == "ollama":
        return OLLAMA_DEFAULT_API_KEY_ENV_VAR
    return OPENAI_DEFAULT_API_KEY_ENV_VAR


def _provider_default_keychain_ref(provider_name: str) -> KeychainSecretRef:
    normalized = provider_name.strip().lower()
    username = (
        OLLAMA_DEFAULT_KEYCHAIN_USERNAME
        if normalized == "ollama"
        else OPENAI_DEFAULT_KEYCHAIN_USERNAME
    )
    return KeychainSecretRef(
        service=DEFAULT_KEYCHAIN_SERVICE,
        username=username,
    )


def _is_local_base_url(url: str) -> bool:
    host = urlparse(url).hostname
    return host in {"localhost", "127.0.0.1", "::1"}


def _resolve_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _platform_base_dirs() -> tuple[Path, Path, Path]:
    """Return (config_dir, data_dir, state_dir) defaults via platformdirs when available."""
    try:
        from platformdirs import user_config_dir, user_data_dir, user_state_dir

        return (
            Path(user_config_dir("foundation")),
            Path(user_data_dir("foundation")),
            Path(user_state_dir("foundation")),
        )
    except Exception:
        fallback = Path.home()
        return (
            fallback / ".config" / "foundation",
            fallback / ".local" / "share" / "foundation",
            fallback / ".local" / "state" / "foundation",
        )


_PLATFORM_CONFIG_DIR, _PLATFORM_DATA_DIR, _PLATFORM_STATE_DIR = _platform_base_dirs()


def default_config_path() -> Path:
    """Return the default config file path for local development."""
    return _PLATFORM_CONFIG_DIR / "config.toml"


def default_env_file_path(config_path: Path | None = None) -> Path:
    """Return the default env file path paired with the active config."""
    resolved_config_path = _resolve_path(config_path or default_config_path())
    return resolved_config_path.parent / DEFAULT_ENV_FILE_NAME


def default_data_dir() -> Path:
    """Return the default data directory."""
    return _PLATFORM_DATA_DIR


def default_state_dir() -> Path:
    """Return the default state directory."""
    return _PLATFORM_STATE_DIR


def default_log_dir() -> Path:
    """Return the default log directory."""
    return default_state_dir() / "logs"


def default_history_database_path() -> Path:
    """Return the default history database path."""
    return default_state_dir() / "history.sqlite3"


def default_events_dir() -> Path:
    """Return the default directory for the per-session NDJSON event log."""
    return default_state_dir() / "events"


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
    AUTO_EXCEPT_COMMIT = "auto-except-commit"
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

    service: str = DEFAULT_KEYCHAIN_SERVICE
    username: str = OPENAI_DEFAULT_KEYCHAIN_USERNAME


class ProviderSection(BaseModel):
    """Provider settings for the future model adapter."""

    name: str = "openai"
    model: str = "gpt-5-mini"
    base_url: AnyUrl | None = None
    request_timeout_seconds: PositiveInt = 60
    api_key_env_var: str | None = OPENAI_DEFAULT_API_KEY_ENV_VAR
    api_key_keychain: KeychainSecretRef | None = Field(default_factory=KeychainSecretRef)

    def normalized_name(self) -> str:
        """Return the normalized provider name."""
        return self.name.strip().lower()

    def effective_base_url(self) -> str:
        """Return the provider base URL with the provider default endpoint applied."""
        return str(self.base_url or _provider_default_base_url(self.normalized_name()))

    def effective_api_key_env_var(self) -> str | None:
        """Return the provider credential environment variable."""
        if self.api_key_env_var is None:
            return None
        if (
            self.normalized_name() == "ollama"
            and self.api_key_env_var == OPENAI_DEFAULT_API_KEY_ENV_VAR
        ):
            return OLLAMA_DEFAULT_API_KEY_ENV_VAR
        return self.api_key_env_var

    def effective_api_key_keychain(self) -> KeychainSecretRef | None:
        """Return the provider credential keychain coordinates."""
        if self.api_key_keychain is None:
            return None
        if (
            self.normalized_name() == "ollama"
            and self.api_key_keychain.service == DEFAULT_KEYCHAIN_SERVICE
            and self.api_key_keychain.username == OPENAI_DEFAULT_KEYCHAIN_USERNAME
        ):
            return _provider_default_keychain_ref("ollama")
        return self.api_key_keychain

    def credentials_required(self) -> bool:
        """Return whether the configured provider requires credentials."""
        if self.normalized_name() == "ollama":
            return not _is_local_base_url(self.effective_base_url())
        return True

    def credential_source_order(self) -> list[str]:
        """Return the configured secret source priority."""
        sources: list[str] = []
        keychain_ref = self.effective_api_key_keychain()
        if keychain_ref is not None:
            sources.append(f"keychain:{keychain_ref.service}/{keychain_ref.username}")
        env_var = self.effective_api_key_env_var()
        if env_var:
            sources.append(f"env:{env_var}")
        return sources

    def resolve_api_key(self, environment: Mapping[str, str] | None = None) -> SecretResolution:
        """Resolve provider credentials without exposing the resulting value by default."""
        env = environment or os.environ
        keychain_failure: str | None = None
        keychain_ref = self.effective_api_key_keychain()
        env_var = self.effective_api_key_env_var()

        if keychain_ref is not None:
            try:
                value = keyring.get_password(
                    keychain_ref.service,
                    keychain_ref.username,
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
                            f"{keychain_ref.service}/{keychain_ref.username}."
                        ),
                        value=SecretStr(value),
                    )

        if env_var:
            value = env.get(env_var)
            if value:
                return SecretResolution(
                    status=SecretResolutionStatus.RESOLVED,
                    source="environment",
                    detail=f"Resolved provider credentials from ${env_var}.",
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
    pass_through_foundation_env: bool = False

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


class MonitorRetentionSection(BaseModel):
    """Retention caps for the persistent NDJSON event log."""

    max_sessions: int = Field(default=200, ge=1)
    max_bytes: int = Field(default=500 * 1024 * 1024, ge=1)


class MonitorSection(BaseModel):
    """v4 Stage 2 — persistent NDJSON event log + optional live transports."""

    enabled: bool = True
    events_dir: Path = Field(default_factory=default_events_dir)
    retention: MonitorRetentionSection = Field(default_factory=MonitorRetentionSection)
    flush_interval_ms: int = Field(default=200, ge=10)
    subscriber_queue_size: int = Field(default=1024, ge=16)
    live_transports: list[str] = Field(default_factory=list)
    socket_path: Path | None = None
    http_port: int | None = Field(default=None, ge=1, le=65535)
    auth_token: SecretStr | None = None

    @field_validator("events_dir", mode="before")
    @classmethod
    def _expand_events_dir(cls, value: Any) -> Any:
        if isinstance(value, str | os.PathLike):
            return Path(os.fspath(value)).expanduser()
        return value

    @field_validator("socket_path", mode="before")
    @classmethod
    def _expand_socket_path(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, str | os.PathLike):
            return Path(os.fspath(value)).expanduser()
        return value

    @field_validator("live_transports", mode="before")
    @classmethod
    def _normalize_live_transports(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        normalized: list[str] = []
        for item in value:
            text = str(item).strip().lower()
            if text in {"unix", "http"} and text not in normalized:
                normalized.append(text)
            elif text and text not in {"unix", "http"}:
                raise ValueError(f"Unsupported monitor live transport: {text!r}")
        return normalized


class AppSettings(BaseSettings):
    """Effective application settings for Foundation CLI."""

    app: AppSection = Field(default_factory=AppSection)
    provider: ProviderSection = Field(default_factory=ProviderSection)
    shell: ShellSection = Field(default_factory=ShellSection)
    logging: LoggingSection = Field(default_factory=LoggingSection)
    history: HistorySection = Field(default_factory=HistorySection)
    approval: ApprovalSection = Field(default_factory=ApprovalSection)
    monitor: MonitorSection = Field(default_factory=MonitorSection)

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        env_ignore_empty=True,
        nested_model_default_partial_update=True,
        validate_default=True,
        extra="ignore",
    )

    @model_validator(mode="after")
    def _align_history_path_with_state_dir(self) -> AppSettings:
        """Place history DB under ``app.state_dir`` unless explicitly overridden.

        Why: operators should be able to relocate Foundation's per-user state
        by setting a single `app.state_dir`; the history sqlite file must
        follow automatically. An explicit `history.database_path` still wins.
        """
        if "database_path" not in self.history.model_fields_set:
            self.history.database_path = (self.app.state_dir / "history.sqlite3").resolve()
        if "events_dir" not in self.monitor.model_fields_set:
            self.monitor.events_dir = (self.app.state_dir / "events").resolve()
        return self

    _toml_file_path: ClassVar[Path | None] = None
    _dotenv_file_path: ClassVar[Path | None] = None
    _config_path: Path = PrivateAttr(default_factory=default_config_path)
    _config_exists: bool = PrivateAttr(default=False)
    _env_file_path: Path = PrivateAttr(default_factory=default_env_file_path)
    _env_file_exists: bool = PrivateAttr(default=False)
    _env_file_values: dict[str, str] = PrivateAttr(default_factory=dict)
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
        env_file_path = cls._dotenv_file_path or default_env_file_path(config_path)
        toml_source = TomlConfigSettingsSource(settings_cls, toml_file=config_path)
        dotenv_source = DotEnvSettingsSource(settings_cls, env_file=env_file_path)
        return (init_settings, env_settings, dotenv_source, toml_source)

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
    def env_file_path(self) -> Path:
        """Return the env file path paired with the active config."""
        return self._env_file_path

    @property
    def env_file_exists(self) -> bool:
        """Return whether the selected env file exists."""
        return self._env_file_exists

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
            "env_file_path": str(self.env_file_path),
            "workspace_root": str(self.app.workspace_root),
            "data_dir": str(self.app.data_dir),
            "capability_store": str(self.app.data_dir / "capabilities"),
            "state_dir": str(self.app.state_dir),
            "log_dir": str(self.app.log_dir),
            "history_database": str(self.history.database_path),
            "events_dir": str(self.monitor.events_dir),
        }

    def metadata_dump(self) -> dict[str, Any]:
        """Return metadata about how the settings were loaded."""
        return {
            "config_path": str(self.config_path),
            "config_exists": self.config_exists,
            "env_file_path": str(self.env_file_path),
            "env_file_exists": self.env_file_exists,
            "env_prefix": ENV_PREFIX,
            "cli_overrides": self.cli_overrides,
        }

    def provider_environment(
        self,
        environment: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Return environment variables merged with the paired env file."""
        merged = dict(self._env_file_values)
        if environment is None:
            merged.update(os.environ)
        else:
            merged.update(environment)
        return merged

    def safe_dump(self) -> dict[str, Any]:
        """Return a safe rendering of the effective config without secret values."""
        payload = self.model_dump(mode="json")
        keychain_ref = self.provider.effective_api_key_keychain()
        payload["provider"]["api_key"] = "[redacted]"
        payload["provider"]["api_key_env_var"] = self.provider.effective_api_key_env_var()
        payload["provider"]["api_key_keychain"] = (
            None if keychain_ref is None else keychain_ref.model_dump(mode="json")
        )
        payload["provider"]["resolved_base_url"] = self.provider.effective_base_url()
        payload["provider"]["credential_source_order"] = self.provider.credential_source_order()
        return payload


class SettingsLoadError(RuntimeError):
    """Raised when Foundation settings cannot be loaded safely."""

    def __init__(self, message: str, *, config_path: Path):
        super().__init__(message)
        self.config_path = config_path


def _read_env_file(
    env_file_path: Path,
    *,
    config_path: Path,
) -> dict[str, str]:
    if not env_file_path.exists():
        return {}

    try:
        lines = env_file_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SettingsLoadError(
            f"Could not read env file {env_file_path}: {exc}",
            config_path=config_path,
        ) from exc

    values: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()
        if "=" not in stripped:
            raise SettingsLoadError(
                (f"Invalid env assignment in {env_file_path} at line {line_number}: {line!r}"),
                config_path=config_path,
            )

        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            raise SettingsLoadError(
                (f"Invalid env assignment in {env_file_path} at line {line_number}: {line!r}"),
                config_path=config_path,
            )

        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


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
    resolved_env_file_path = default_env_file_path(resolved_config_path)
    config_exists = _validate_config_file(resolved_config_path)
    env_file_values = _read_env_file(
        resolved_env_file_path,
        config_path=resolved_config_path,
    )
    init_overrides = dict(overrides or {})

    try:
        AppSettings._toml_file_path = resolved_config_path
        AppSettings._dotenv_file_path = resolved_env_file_path
        settings = AppSettings(**init_overrides)
    except ValidationError as exc:
        raise SettingsLoadError(
            f"Configuration validation failed for {resolved_config_path}:\n{exc}",
            config_path=resolved_config_path,
        ) from exc
    finally:
        AppSettings._toml_file_path = None
        AppSettings._dotenv_file_path = None

    settings._config_path = resolved_config_path
    settings._config_exists = config_exists
    settings._env_file_path = resolved_env_file_path
    settings._env_file_exists = resolved_env_file_path.exists()
    settings._env_file_values = env_file_values
    settings._cli_overrides = _flatten_overrides(init_overrides)
    return settings


def render_settings_payload(settings: AppSettings) -> dict[str, Any]:
    """Return the safe config payload used by the CLI."""
    return {
        "metadata": settings.metadata_dump(),
        "settings": settings.safe_dump(),
    }
