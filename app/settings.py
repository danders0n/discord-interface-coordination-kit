"""Configuration: everything in `.env` (docs/PLAN.md, *Configuration and deployment*).

Read with pydantic-settings like the supervisor: prefix ``BOT_``, ``__`` between nested names, and lists as JSON
or separated by ``;`` (``BOT_PERMISSIONS__BOT_ADMIN=123;Moderator``). Real environment variables win over the file.
Channels and roles are a Discord **id or name** (:data:`Ref`); names are turned into ids on the server at start-up
and on ``/reload`` (app/resolve.py). ``/reload`` reads the file again through :class:`SettingsStore`."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, BeforeValidator, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, EnvSettingsSource, SettingsConfigDict

ENV_PREFIX = "BOT_"
ENV_FILE = ".env"
LIST_SPLIT = re.compile(r"[;\n]")


def _ref(value: Any) -> Any:
    """Digits are an id; anything else is a name (trimmed, a leading # or @ dropped)."""
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        text = text.lstrip("#@").strip()
        if not text:
            raise ValueError("an empty name")
        return text
    return value


Ref = Annotated[int | str, BeforeValidator(_ref)]


class Group(BaseModel):
    acronym: str = Field(min_length=1, max_length=10, description="Our group's acronym, e.g. IBC")
    name: str = Field(min_length=1, max_length=100)


class Channels(BaseModel):
    drafts_category: Ref = Field(description="Hidden category for drafts and events waiting for approval")
    events_category: Ref = Field(description="Category of published events")
    header: Ref = Field(description="Channel with the Create event button and the calendar")
    announcements: Ref
    staff: Ref = Field(description="Staff-only channel: the bot keeps the staff log and moderation threads in it")
    staff_log_thread: str = Field("Staff log", min_length=1, max_length=100)
    moderation_thread: str = Field("Moderacja", min_length=1, max_length=100, description="Approval requests")
    link_reminder: Ref | None = Field(None, description="Steam link reminders for members with closed DMs")
    archive_pattern: str = Field("Archiwum Q{quarter} {year}", min_length=1, max_length=90,
                                 description="Name of a quarter's archive category: {quarter} and {year} are filled in")


class Roles(BaseModel):
    member: list[Ref] = Field(min_length=1, description="The group's member roles, e.g. Klanowicz: may use the wizard")
    mission_maker: Ref
    announcement: Ref | None = Field(None, description="Mentioned in announcements and when sign-up opens")
    signup: list[Ref] = Field(default_factory=list, description="Roles that may sign up for events")


class Permissions(BaseModel):
    """Permission -> the roles that hold it."""

    approve_events: list[Ref] = Field(default_factory=list)
    manage_any_event: list[Ref] = Field(default_factory=list)
    server_control: list[Ref] = Field(default_factory=list)
    server_message: list[Ref] = Field(default_factory=list)
    server_override: list[Ref] = Field(default_factory=list)
    manage_links: list[Ref] = Field(default_factory=list)
    bot_admin: list[Ref] = Field(default_factory=list)


class DlcRoles(BaseModel):
    """Creator DLC (the keys of the preset parser's table) -> the Discord role members who own it take."""

    gm: Ref | None = Field(None, description="Global Mobilization")
    vn: Ref | None = Field(None, description="S.O.G. Prairie Fire")
    csla: Ref | None = Field(None, description="CSLA Iron Curtain")
    ws: Ref | None = Field(None, description="Western Sahara")
    spe: Ref | None = Field(None, description="Spearhead 1944")
    rf: Ref | None = Field(None, description="Reaction Forces")
    ef: Ref | None = Field(None, description="Expeditionary Forces")


class Supervisor(BaseModel):
    url: str = "http://localhost:8000"
    token: SecretStr | None = None
    event_server: str | None = Field(None, description="Slug of the default event server")


class Times(BaseModel):
    """Durations of the event rules (docs/PLAN.md, *Config contents*, times); more join them phase by phase."""

    offer_timeout_minutes: int = Field(30, ge=1, description="How long a member has to take an offered slot")
    offer_to_all_minutes: int = Field(60, ge=0, description="Before the assembly: a freed slot goes to the whole reserve list at once")
    default_duration_minutes: int = Field(180, ge=15, le=24 * 60, description="Pre-filled in the wizard")
    max_days_ahead: int = Field(120, ge=1, description="A date further ahead is refused as a likely typo")
    lead_minutes: int = Field(45, ge=0, description="The reservation starts this long before the assembly")
    buffer_minutes: int = Field(30, ge=0, description="The reservation ends this long after start + duration")
    stale_ping_hours: int = Field(48, ge=1, description="Still a draft (or waiting) this long before the assembly: ping")
    stale_cancel_hours: int = Field(24, ge=0, description="Still not published this long before the assembly: cancelled")
    reminder_minutes: list[int] = Field(default_factory=lambda: [24 * 60, 60],
                                        description="Reminder pings, this long before the assembly")
    archive_hours: int = Field(48, ge=0, description="After the end (start + duration), the channel moves to the archive")


class Events(BaseModel):
    """What the wizard offers (docs/PLAN.md, *Creating an event*)."""

    mission_types: list[str] = Field(default_factory=lambda: ["Coop", "TvT", "Zeus", "Training"], min_length=1)
    climate_roles: list[Ref] = Field(default_factory=list, description="Interest roles such as @WW2 and @Modern")


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
    dlc_roles: DlcRoles = Field(default_factory=DlcRoles)
    events: Events = Field(default_factory=Events)
    times: Times = Field(default_factory=Times)
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


def variables(model: type[BaseModel] | None = None, prefix: str = ENV_PREFIX) -> set[str]:
    """Every variable name the settings read (``BOT_CHANNELS__STAFF`` ...)."""
    names: set[str] = set()
    for name, field in (model or Settings).model_fields.items():
        if isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel):
            names |= variables(field.annotation, f"{prefix}{name.upper()}__")
        else:
            names.add(f"{prefix}{name.upper()}")
    return names


def unknown_variables(env_file: str | Path = ENV_FILE) -> list[str]:
    """``BOT_`` variables in the file or the environment that no setting reads: typos and renamed settings."""
    import os

    from dotenv import dotenv_values

    found = set(dotenv_values(env_file)) if Path(env_file).is_file() else set()
    found |= set(os.environ)
    return sorted(name for name in found if name.upper().startswith(ENV_PREFIX) and name.upper() not in variables())


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
