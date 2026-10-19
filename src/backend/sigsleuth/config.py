"""Local configuration loaded from environment variables and `.env`."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values

from sigsleuth.radio import PRESET_SPREADING_FACTORS, preset_name

DEFAULT_RETENTION_DAYS = 14
DEFAULT_COVERAGE_RETENTION_DAYS = 90
DEFAULT_PRESET = "LongFast"


class ConfigurationError(ValueError):
    """Raised when application settings are missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Every setting, loaded once at startup. MQTT host and topic are blank unless capturing."""

    database_path: Path
    # Shown beside "SigSleuth" in the page, to tell instances for different networks apart.
    name: str | None = None
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "INFO"
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_topic: str = ""
    mqtt_username: str | None = None
    mqtt_password: str | None = field(default=None, repr=False)
    mqtt_tls: bool = False
    mqtt_client_id: str | None = None
    retention_days: int = DEFAULT_RETENTION_DAYS
    # Survey measurements stay this many days, never fewer than retention_days
    # (retention.py); 0 sets no limit.
    coverage_retention_days: int = DEFAULT_COVERAGE_RETENTION_DAYS
    default_preset: str = DEFAULT_PRESET
    # Guards Diagnostics; without one, Diagnostics is open.
    admin_password: str | None = field(default=None, repr=False)
    # Guards survey uploads; without one, uploads are refused.
    upload_password: str | None = field(default=None, repr=False)
    # Hide nodes that turned OK to MQTT off (opt_out.py); off, every page
    # shows every node.
    hide_opted_out: bool = True
    # The API's clock, stopped here, for a demo of a saved database; the page
    # reads the same setting when it is built.
    fixed_now: datetime | None = None


def load_environment(
    dotenv_path: str | Path = ".env",
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge `.env` with the process environment, which takes precedence."""
    settings = {
        key: value for key, value in dotenv_values(dotenv_path).items() if value is not None
    }
    settings.update(os.environ if environ is None else environ)
    return settings


def settings_from_environment(environment: Mapping[str, str], *, capture: bool) -> Settings:
    """Validate settings without exposing their values in errors.

    The MQTT host and topic are required only when `capture` is true.
    """
    api_host = environment.get("SIGSLEUTH_API_HOST", "127.0.0.1").strip()
    if not api_host:
        raise ConfigurationError("SIGSLEUTH_API_HOST must not be blank")
    log_level = environment.get("SIGSLEUTH_LOG_LEVEL", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError("SIGSLEUTH_LOG_LEVEL must name a standard logging level")
    username = _optional(environment.get("SIGSLEUTH_MQTT_USERNAME"))
    password = _optional(environment.get("SIGSLEUTH_MQTT_PASSWORD"))
    if password is not None and username is None:
        raise ConfigurationError(
            "SIGSLEUTH_MQTT_USERNAME is required when SIGSLEUTH_MQTT_PASSWORD is set"
        )
    fixed_now = _time(environment.get("SIGSLEUTH_FIXED_NOW", ""), "SIGSLEUTH_FIXED_NOW")
    if fixed_now is not None and capture:
        raise ConfigurationError("SIGSLEUTH_FIXED_NOW only works without capture")

    return Settings(
        database_path=Path(environment.get("SIGSLEUTH_DATABASE_PATH", "./data/sigsleuth.db")),
        name=_optional(environment.get("SIGSLEUTH_NAME", "").strip()),
        api_host=api_host,
        api_port=_port(environment.get("SIGSLEUTH_API_PORT", "8000"), "SIGSLEUTH_API_PORT"),
        log_level=log_level,
        mqtt_host=_required(environment, "SIGSLEUTH_MQTT_HOST") if capture else "",
        mqtt_port=_port(environment.get("SIGSLEUTH_MQTT_PORT", "1883"), "SIGSLEUTH_MQTT_PORT"),
        mqtt_topic=_topic_filter(_required(environment, "SIGSLEUTH_MQTT_TOPIC")) if capture else "",
        mqtt_username=username,
        mqtt_password=password,
        mqtt_tls=_boolean(environment.get("SIGSLEUTH_MQTT_TLS", "false"), "SIGSLEUTH_MQTT_TLS"),
        mqtt_client_id=_optional(environment.get("SIGSLEUTH_MQTT_CLIENT_ID")),
        retention_days=_non_negative_integer(
            environment.get("SIGSLEUTH_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)),
            "SIGSLEUTH_RETENTION_DAYS",
        ),
        coverage_retention_days=_non_negative_integer(
            environment.get(
                "SIGSLEUTH_COVERAGE_RETENTION_DAYS", str(DEFAULT_COVERAGE_RETENTION_DAYS)
            ),
            "SIGSLEUTH_COVERAGE_RETENTION_DAYS",
        ),
        default_preset=_preset(environment.get("SIGSLEUTH_DEFAULT_PRESET", DEFAULT_PRESET)),
        admin_password=_optional(environment.get("SIGSLEUTH_ADMIN_PASSWORD")),
        upload_password=_optional(environment.get("SIGSLEUTH_UPLOAD_PASSWORD")),
        hide_opted_out=_boolean(
            environment.get("SIGSLEUTH_HIDE_OPTED_OUT", "").strip() or "true",
            "SIGSLEUTH_HIDE_OPTED_OUT",
        ),
        fixed_now=fixed_now,
    )


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or not value.strip():
        raise ConfigurationError(f"{name} is required")
    return value


def _topic_filter(value: str) -> str:
    """Check MQTT wildcard rules so a bad topic fails at startup, not at subscribe."""
    levels = value.split("/")
    for position, level in enumerate(levels):
        misplaced_hash = "#" in level and (level != "#" or position != len(levels) - 1)
        if misplaced_hash or ("+" in level and level != "+") or "\0" in level:
            raise ConfigurationError(
                "SIGSLEUTH_MQTT_TOPIC must be a valid MQTT topic filter: "
                "'+' and '#' fill a whole level, and '#' only the last one"
            )
    return value


def _preset(value: str) -> str:
    preset = preset_name(value.strip())
    if preset is None:
        raise ConfigurationError(
            "SIGSLEUTH_DEFAULT_PRESET must be one of " + ", ".join(PRESET_SPREADING_FACTORS)
        )
    return preset


def _optional(value: str | None) -> str | None:
    return value if value else None


def _port(value: str, name: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error
    if not 1 <= port <= 65_535:
        raise ConfigurationError(f"{name} must be between 1 and 65535")
    return port


def _boolean(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off", ""}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _non_negative_integer(value: str, name: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a non-negative integer") from error
    if result < 0:
        raise ConfigurationError(f"{name} must be a non-negative integer")
    return result


def _time(value: str, name: str) -> datetime | None:
    if not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.strip())
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an ISO 8601 time") from error
    if result.utcoffset() is None:
        raise ConfigurationError(f"{name} must include a UTC offset, such as Z")
    return result
