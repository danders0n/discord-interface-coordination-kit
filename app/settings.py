"""Configuration: everything in `.env` (docs/PLAN.md, *Configuration and deployment*).

Read with pydantic-settings like the supervisor: prefix ``BOT_``, ``__`` between nested names, and lists as JSON
or separated by ``;`` (``BOT_PERMISSIONS__BOT_ADMIN=123;456``). Real environment variables win over the file.
Channels and roles are Discord ids. ``/reload`` reads the file again through :class:`SettingsStore`."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, EnvSettingsSource, SettingsConfigDict

ENV_PREFIX = "BOT_"
ENV_FILE = ".env"
LIST_SPLIT = re.compile(r"[;\n]")


class Group(BaseModel):
    acronym: str = Field(min_length=1, max_length=10, description="Our group's acronym, e.g. IBC")
    name: str = Field(min_length=1, max_length=100)


class Channels(BaseModel):
    drafts_category: int = Field(description="Hidden category for drafts and events waiting for approval")
    events_category: int = Field(description="Category of published events")
    header: int = Field(description="Channel with the Create event button and the calendar")
    announcements: int
    moderation: int = Field(description="Approval requests")
    staff_log: int
    link_reminder: int | None = Field(None, description="Steam link reminders for members with closed DMs")


class Roles(BaseModel):
    member: int = Field(description="The group's member role, e.g. Klanowicz: may use the wizard")
    mission_maker: int
    announcement: int | None = Field(None, description="Mentioned in announcements and when sign-up opens")
    signup: list[int] = Field(default_factory=list, description="Roles that may sign up for events")


class Permissions(BaseModel):
    """Permission -> the roles that hold it."""

    approve_events: list[int] = Field(default_factory=list)
    manage_any_event: list[int] = Field(default_factory=list)
    server_control: list[int] = Field(default_factory=list)
    server_message: list[int] = Field(default_factory=list)
    server_override: list[int] = Field(default_factory=list)
    manage_links: list[int] = Field(default_factory=list)
    bot_admin: list[int] = Field(default_factory=list)


class Supervisor(BaseModel):
    url: str = "http://localhost:8000"
    token: SecretStr | None = None
    event_server: str | None = Field(None, description="Slug of the default event server")


class Backup(BaseModel):
    keep: int = Field(14, ge=1, description="How many daily database backups to keep")


class Logs(BaseModel):
    keep_days: int = Field(14, ge=1)


class Settings(BaseSettings):
    # An empty variable means the default, as in .env.example's blank lines.
    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, env_nested_delimiter="__", env_file=ENV_FILE,
                                      env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore")

    discord_token: SecretStr
    guild_id: int
    language: Literal["pl", "en"] = Field("pl", description="Language of public messages")
    timezone: str = Field("Europe/Warsaw", description="Times are typed and shown in this zone")
    data_dir: Path = Path("data")

    group: Group
    channels: Channels
    roles: Roles
    permissions: Permissions = Field(default_factory=Permissions)
    supervisor: Supervisor = Field(default_factory=Supervisor)
    backup: Backup = Field(default_factory=Backup)
    logs: Logs = Field(default_factory=Logs)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown time zone '{value}'") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "bot.db"

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                   file_secret_settings):
        env_file = dotenv_settings.env_file if isinstance(dotenv_settings, DotEnvSettingsSource) else ENV_FILE
        return (init_settings, ListFriendlyEnvSource(settings_cls),
                ListFriendlyDotEnvSource(settings_cls, env_file=env_file), file_secret_settings)


def _is_list(field: Any) -> bool:
    return getattr(field.annotation, "__origin__", None) is list


def decode(value: Any, field: Any, strict: Any) -> Any:
    """JSON from the environment, or for a list field also ``1;2`` (or one per line) and a single bare id."""
    if not isinstance(value, str) or not _is_list(field):
        return strict(value)
    text = value.strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
    except ValueError:
        return [part.strip() for part in LIST_SPLIT.split(text) if part.strip()]
    return decoded if isinstance(decoded, list) else [decoded]


class ListFriendlyEnvSource(EnvSettingsSource):
    def decode_complex_value(self, field_name, field, value):
        return decode(value, field, lambda v: super(ListFriendlyEnvSource, self).decode_complex_value(field_name, field, v))


class ListFriendlyDotEnvSource(DotEnvSettingsSource):
    def decode_complex_value(self, field_name, field, value):
        return decode(value, field, lambda v: super(ListFriendlyDotEnvSource, self).decode_complex_value(field_name, field, v))


def load(env_file: str | Path = ENV_FILE) -> Settings:
    """Read and validate the settings; raises :class:`pydantic.ValidationError`."""
    return Settings(_env_file=env_file)  # type: ignore[call-arg]


def _required_leaves(model: type[BaseModel], prefix: str) -> list[str]:
    names = []
    for name, field in model.model_fields.items():
        if not field.is_required():
            continue
        if isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel):
            names += _required_leaves(field.annotation, f"{prefix}{name.upper()}__")
        else:
            names.append(f"{prefix}{name.upper()}")
    return names


def describe_errors(exc: ValidationError) -> list[str]:
    """One line per problem, with the variable name a person would edit (a missing section names its variables)."""
    lines = []
    for error in exc.errors():
        path = "__".join(str(part) for part in error["loc"]).upper()
        field = Settings.model_fields.get(str(error["loc"][0])) if len(error["loc"]) == 1 else None
        nested = field.annotation if field is not None else None
        if error["type"] == "missing" and isinstance(nested, type) and issubclass(nested, BaseModel):
            lines += [f"{name}: {error['msg']}" for name in _required_leaves(nested, f"{ENV_PREFIX}{path}__")]
        else:
            lines.append(f"{ENV_PREFIX}{path}: {error['msg']}")
    return lines


# Read once: changing them needs a restart (the connection to Discord and the supervisor, and where data lives).
RESTART_ONLY = ("discord_token", "guild_id", "data_dir", "supervisor.url", "supervisor.token")


def _get(settings: Settings, dotted: str) -> Any:
    value: Any = settings
    for part in dotted.split("."):
        value = getattr(value, part)
    return value.get_secret_value() if isinstance(value, SecretStr) else value


class ReloadResult(BaseModel):
    ok: bool
    errors: list[str] = Field(default_factory=list)
    needs_restart: list[str] = Field(default_factory=list)


class SettingsStore:
    """The settings in force. Services read :attr:`current` each time, so ``/reload`` reaches them at once."""

    def __init__(self, settings: Settings, env_file: str | Path = ENV_FILE) -> None:
        self.current = settings
        self.env_file = env_file

    def reload(self) -> ReloadResult:
        """Read the file again. On an error nothing changes. Restart-only values keep their old value."""
        try:
            fresh = load(self.env_file)
        except ValidationError as exc:
            return ReloadResult(ok=False, errors=describe_errors(exc))
        changed = [name for name in RESTART_ONLY if _get(fresh, name) != _get(self.current, name)]
        kept = fresh.model_copy(update={
            "discord_token": self.current.discord_token,
            "guild_id": self.current.guild_id,
            "data_dir": self.current.data_dir,
            "supervisor": fresh.supervisor.model_copy(update={"url": self.current.supervisor.url,
                                                              "token": self.current.supervisor.token}),
        })
        self.current = kept
        return ReloadResult(ok=True, needs_restart=[f"{ENV_PREFIX}{name.replace('.', '__').upper()}" for name in changed])
