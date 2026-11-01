"""Light fakes of the Discord objects the bot touches (docs/PLAN.md, *Code and tooling*, *Tests*). They record
what the bot sends, edits and opens, so tests call command and button callbacks directly, with no network."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import discord
from discord import app_commands

from app.discord_util import NotAllowed, on_app_command_error

_ids = itertools.count(900_000)


@dataclass(eq=False)
class FakeRole:
    id: int
    name: str = "role"
    mentionable: bool = True

    def __hash__(self) -> int:  # a key in permission overwrites, as in discord.py
        return hash(self.id)

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"


@dataclass(eq=False)
class FakeMember:
    id: int
    name: str = "member"
    roles: list[FakeRole] = field(default_factory=list)
    guild_permissions: discord.Permissions = field(default_factory=discord.Permissions.none)

    def __hash__(self) -> int:
        return hash(self.id)

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"


@dataclass
class Sent:
    content: str | None
    kwargs: dict[str, Any]
    id: int = field(default_factory=lambda: next(_ids))
    deleted: bool = False
    edits: list[dict[str, Any]] = field(default_factory=list)

    @property
    def embeds(self) -> list[Any]:
        return (self.edits[-1] if self.edits else self.kwargs).get("embeds", [])


def _not_found(what: str) -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), f"Unknown {what}")


class MessageRef:
    """``get_partial_message``: edit or delete by id, NotFound when it's gone."""

    def __init__(self, channel: Any, message_id: int) -> None:
        self.channel, self.id = channel, message_id

    def _find(self) -> Sent:
        found = next((m for m in self.channel.sent if m.id == self.id and not m.deleted), None)
        if found is None:
            raise _not_found("Message")
        return found

    async def edit(self, **changes: Any) -> Sent:
        message = self._find()
        message.edits.append(changes)
        return message

    async def delete(self) -> None:
        self._find().deleted = True


@dataclass(eq=False)
class FakeThread:
    id: int
    name: str
    parent_id: int
    archived: bool = False
    locked: bool = False
    sent: list[Sent] = field(default_factory=list)
    edits: list[dict[str, Any]] = field(default_factory=list)

    async def send(self, content: str | None = None, **kwargs: Any) -> Sent:
        if self.archived:
            raise AssertionError("sent to an archived thread")
        message = Sent(content, kwargs)
        self.sent.append(message)
        return message

    async def edit(self, **changes: Any) -> FakeThread:
        self.edits.append(changes)
        for name, value in changes.items():
            setattr(self, name, value)
        return self


@dataclass(eq=False)
class FakeChannel:
    id: int
    name: str = "channel"
    type: discord.ChannelType = discord.ChannelType.text
    permissions: discord.Permissions = field(default_factory=discord.Permissions.all)
    sent: list[Sent] = field(default_factory=list)
    guild: Any = None
    category_id: int | None = None
    overwrites: dict[Any, Any] = field(default_factory=dict)
    topic: str | None = None

    def permissions_for(self, _member: Any) -> discord.Permissions:
        return self.permissions

    @property
    def mention(self) -> str:
        return f"<#{self.id}>"

    async def send(self, content: str | None = None, **kwargs: Any) -> Sent:
        message = Sent(content, kwargs)
        self.sent.append(message)
        return message

    @property
    def messages(self) -> list[Sent]:
        """What is in the channel now, oldest first."""
        return [m for m in self.sent if not m.deleted]

    def get_partial_message(self, message_id: int) -> MessageRef:
        return MessageRef(self, message_id)

    @property
    def category(self) -> FakeChannel | None:
        return self.guild.get_channel(self.category_id) if self.category_id else None

    @property
    def channels(self) -> list[FakeChannel]:
        """A category's channels."""
        return [c for c in self.guild.channels if c.category_id == self.id]

    async def edit(self, **changes: Any) -> FakeChannel:
        if "category" in changes:
            category = changes.pop("category")
            self.category_id = category.id if category is not None else None
        for name, value in changes.items():
            setattr(self, name, value)
        return self

    async def delete(self, reason: str | None = None) -> None:
        self.guild.channel_map.pop(self.id, None)

    async def create_thread(self, *, name: str, type: discord.ChannelType, auto_archive_duration: int) -> FakeThread:
        thread = FakeThread(next(_ids), name, self.id)
        self.guild.threads[thread.id] = thread
        return thread


class FakeGuild:
    def __init__(self, id: int, name: str = "guild", channels: list[FakeChannel] = (),
                 roles: list[FakeRole] = (), me: FakeMember | None = None) -> None:
        self.id = id
        self.name = name
        self.channel_map = {c.id: c for c in channels}
        self.role_map = {r.id: r for r in roles}
        self.threads: dict[int, FakeThread] = {}
        self.me = me or FakeMember(1, "bot")
        self.default_role = FakeRole(id, "@everyone")
        for channel in channels:
            channel.guild = self

    @property
    def categories(self) -> list[FakeChannel]:
        return [c for c in self.channels if c.type == discord.ChannelType.category]

    async def create_text_channel(self, name: str, *, category: FakeChannel | None = None,
                                  overwrites: dict | None = None, topic: str | None = None, **_: Any) -> FakeChannel:
        channel = FakeChannel(next(_ids), name, category_id=category.id if category else None,
                              overwrites=dict(overwrites or {}), topic=topic)
        self.add(channel)
        return channel

    async def create_category(self, name: str, *, overwrites: dict | None = None, **_: Any) -> FakeChannel:
        category = FakeChannel(next(_ids), name, discord.ChannelType.category, overwrites=dict(overwrites or {}))
        self.add(category)
        return category

    @property
    def channels(self) -> list[FakeChannel]:
        return list(self.channel_map.values())

    @property
    def roles(self) -> list[FakeRole]:
        return list(self.role_map.values())

    def add(self, item: FakeChannel | FakeRole) -> None:
        if isinstance(item, FakeRole):
            self.role_map[item.id] = item
        else:
            item.guild = self
            self.channel_map[item.id] = item

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channel_map.get(channel_id)

    def get_role(self, role_id: int) -> FakeRole | None:
        return self.role_map.get(role_id)

    def get_thread(self, thread_id: int) -> FakeThread | None:
        """Like Discord's cache: archived threads aren't in it, only a fetch finds them."""
        thread = self.threads.get(thread_id)
        return thread if thread is not None and not thread.archived else None

    async def fetch_channel(self, channel_id: int) -> Any:
        found = self.threads.get(channel_id) or self.channel_map.get(channel_id)
        if found is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")
        return found


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[Sent] = []
        self.modals: list[Any] = []
        self.edited: list[dict[str, Any]] = []
        self.deferred = False

    def is_done(self) -> bool:
        return bool(self.sent or self.modals or self.deferred or self.edited)

    async def edit_message(self, **kwargs: Any) -> None:
        self.edited.append(kwargs)

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
                 command: Any = None, guild: FakeGuild | None = None) -> None:
        self.client = SimpleNamespace(services=services)
        self.user = user
        self.locale = locale
        self.command = command
        self.guild = guild
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.type = discord.InteractionType.component
        self.data: dict[str, Any] = {}

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
