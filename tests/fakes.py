"""Light fakes of the Discord objects the bot touches (docs/PLAN.md, *Code and tooling*, *Tests*). They record
what the bot sends, edits and opens, so tests call command and button callbacks directly, with no network."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import discord
from discord import app_commands

from app.discord_util import NotAllowed, on_app_command_error


@dataclass
class FakeRole:
    id: int
    name: str = "role"
    mentionable: bool = True

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"


@dataclass
class FakeMember:
    id: int
    name: str = "member"
    roles: list[FakeRole] = field(default_factory=list)
    guild_permissions: discord.Permissions = field(default_factory=discord.Permissions.none)

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"


@dataclass
class Sent:
    content: str | None
    kwargs: dict[str, Any]


@dataclass
class FakeChannel:
    id: int
    name: str = "channel"
    type: discord.ChannelType = discord.ChannelType.text
    permissions: discord.Permissions = field(default_factory=discord.Permissions.all)
    sent: list[Sent] = field(default_factory=list)

    def permissions_for(self, _member: Any) -> discord.Permissions:
        return self.permissions

    async def send(self, content: str | None = None, **kwargs: Any) -> Sent:
        message = Sent(content, kwargs)
        self.sent.append(message)
        return message


@dataclass
class FakeGuild:
    id: int
    name: str = "guild"
    channels: dict[int, FakeChannel] = field(default_factory=dict)
    roles: dict[int, FakeRole] = field(default_factory=dict)
    me: FakeMember = field(default_factory=lambda: FakeMember(1, "bot"))

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)

    def get_role(self, role_id: int) -> FakeRole | None:
        return self.roles.get(role_id)


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[Sent] = []
        self.modals: list[Any] = []
        self.deferred = False

    def is_done(self) -> bool:
        return bool(self.sent or self.modals or self.deferred)

    async def send_message(self, content: str | None = None, **kwargs: Any) -> None:
        self.sent.append(Sent(content, kwargs))

    async def send_modal(self, modal: Any) -> None:
        self.modals.append(modal)

    async def defer(self, **kwargs: Any) -> None:
        self.deferred = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[Sent] = []

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        self.sent.append(Sent(content, kwargs))


class FakeInteraction:
    def __init__(self, services: Any, user: FakeMember, locale: discord.Locale = discord.Locale.american_english,
                 command: Any = None) -> None:
        self.client = SimpleNamespace(services=services)
        self.user = user
        self.locale = locale
        self.command = command
        self.response = FakeResponse()
        self.followup = FakeFollowup()

    @property
    def replies(self) -> list[Sent]:
        return self.response.sent + self.followup.sent


async def invoke(command: app_commands.Command, cog: Any, interaction: FakeInteraction, **params: Any) -> None:
    """Run a slash command as the tree would: its checks, then the callback, then the error handler."""
    interaction.command = command
    try:
        for check in command.checks:
            if not await discord.utils.maybe_coroutine(check, interaction):
                raise app_commands.CheckFailure()
        await command.callback(cog, interaction, **params)
    except NotAllowed as exc:
        await on_app_command_error(interaction, exc)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        await on_app_command_error(interaction, app_commands.CommandInvokeError(command, exc))  # type: ignore[arg-type]
