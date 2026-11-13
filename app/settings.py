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
    events_category: Ref = Field(description="Category of the operations: drafts (marked DRAFT, no sign-up yet), "
                                             "operations waiting for approval and published ones")
    header: Ref = Field(description="Channel with the Create operation button and the calendar")
    announcements: Ref
    top: list[Ref] = Field(default_factory=list,
                           description="Channels kept at the top of the operations category, above the operations, "
                                       "in this order; the header goes first unless listed")
    staff: Ref = Field(description="Staff-only channel: the bot keeps the staff log and moderation threads in it")
    staff_log_thread: str = Field("Staff log", min_length=1, max_length=100)
    moderation_thread: str = Field("Moderacja", min_length=1, max_length=100, description="Approval requests")
    link_reminder: Ref | None = Field(None, description="Steam link reminders for members with closed DMs")
    archive_pattern: str = Field("Archiwum Q{quarter} {year}", min_length=1, max_length=90,
                                 description="Name of a quarter's archive category: {quarter} and {year} are filled in")


class Roles(BaseModel):
    announcement: Ref | None = Field(None, description="Mentioned in announcements and when sign-up opens")


class Permissions(BaseModel):
    """Permission -> the roles that hold it. Only the bot's own commands: every other right is the supervisor's
    (its role map from the Discord roles)."""

    bot_admin: list[Ref] = Field(default_factory=list, description="/reload, /privacy-erase, every archived "
                                                                   "operation, Steam link reminders")


class Supervisor(BaseModel):
    url: str = "http://localhost:8000"
    token: SecretStr | None = None


class Times(BaseModel):
    """The bot's own timing (the operations' rules are the supervisor's)."""

    default_duration_minutes: int = Field(180, ge=15, le=24 * 60, description="Pre-filled in the wizard")
    reminder_minutes: list[int] = Field(default_factory=lambda: [24 * 60, 60],
                                        description="Reminder pings, this long before the assembly")
    archive_hours: int = Field(48, ge=0, description="After the end of an operation, its channel moves to the "
                                                     "archive this long later")
    calendar_days: int = Field(14, ge=1, le=60, description="The header's calendar shows the operations of this "
                                                            "many days")
    reply_seconds: int = Field(60, ge=0, le=840, description="The bot's private replies without buttons disappear "
                                                             "after this long; 0: they stay (Discord allows 15 min)")


class Events(BaseModel):
    """The operation channels."""

    discussion: str | None = Field(None, min_length=1, max_length=100,
                                   description="Name of each operation's discussion thread; default: Discussion / "
                                               "Dyskusja")


class Links(BaseModel):
    """Steam link reminders (docs/PLAN-front-end.md; the links and squad.xml are the supervisor's)."""

    reminder_days: int = Field(14, ge=1, description="A member with a rank and no link is reminded every this many days")
    dms_per_hour: int = Field(20, ge=1, description="Reminder DMs sent per hour at most")


class Archive(BaseModel):
    """The long-term archive (docs/PLAN.md, *Long-term archive*)."""

    keep_quarters: int = Field(4, ge=2, le=20, description="Quarters kept as channels (the current one included); "
                                                            "older ones move into the database")
    attachments_dir: Path | None = Field(None, description="Attachments of archived events; default: <data dir>/archive")


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
    roles: Roles = Field(default_factory=Roles)
    permissions: Permissions = Field(default_factory=Permissions)
    events: Events = Field(default_factory=Events)
    times: Times = Field(default_factory=Times)
    supervisor: Supervisor = Field(default_factory=Supervisor)
    links: Links = Field(default_factory=Links)
    archive: Archive = Field(default_factory=Archive)
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
