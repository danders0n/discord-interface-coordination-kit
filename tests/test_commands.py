import asyncio

import discord
from conftest import ADMIN, MEMBER
from fakes import FakeInteraction, FakeMember, FakeRole, invoke

from app.admin.cog import AdminCog
from app.privacy.cog import PrivacyCog

POLISH = discord.Locale.polish
ENGLISH = discord.Locale.british_english


async def test_privacy_answers_privately_in_the_members_language(services):
    cog = PrivacyCog(services)
    polish = FakeInteraction(services, FakeMember(1, roles=[FakeRole(MEMBER)]), POLISH)
    await invoke(cog.privacy, cog, polish)
    (sent,) = polish.replies
    assert sent.kwargs["ephemeral"] is True
    assert "Co bot o Tobie zapisuje" in sent.kwargs["embed"].description
    english = FakeInteraction(services, FakeMember(1), ENGLISH)
    await invoke(cog.privacy, cog, english)
    assert "What the bot stores" in english.replies[0].kwargs["embed"].description


async def test_a_long_privacy_text_is_split_into_messages(services):
    services.settings.data_dir.mkdir(parents=True, exist_ok=True)
    (services.settings.data_dir / "privacy.en.md").write_text("\n".join(["x" * 1000] * 9), encoding="utf-8")
    cog = PrivacyCog(services)
    interaction = FakeInteraction(services, FakeMember(1), ENGLISH)
    await invoke(cog.privacy, cog, interaction)
    assert len(interaction.replies) == 3
    assert all(len(r.kwargs["embed"].description) <= 4096 for r in interaction.replies)
    assert interaction.followup.sent  # the parts after the first are follow-ups


async def test_staff_can_replace_the_privacy_text(services):
    services.settings.data_dir.mkdir(parents=True, exist_ok=True)
    (services.settings.data_dir / "privacy.en.md").write_text("Our own words.", encoding="utf-8")
    cog = PrivacyCog(services)
    interaction = FakeInteraction(services, FakeMember(1), ENGLISH)
    await invoke(cog.privacy, cog, interaction)
    assert interaction.replies[0].kwargs["embed"].description == "Our own words."


async def test_reload_is_for_bot_admins_only(services):
    cog = AdminCog(services)
    interaction = FakeInteraction(services, FakeMember(1, roles=[FakeRole(MEMBER)]), ENGLISH)
    await invoke(cog.reload, cog, interaction)
    assert interaction.replies[0].content == "You don't have the permission for this."


async def test_reload_reports_errors_and_keeps_the_settings(services, tmp_path):
    services.store.env_file = tmp_path / "broken.env"
    services.store.env_file.write_text("BOT_LANGUAGE=de\n", encoding="utf-8")
    before = services.settings
    cog = AdminCog(services)
    interaction = FakeInteraction(services, FakeMember(1, roles=[FakeRole(ADMIN)]), ENGLISH)
    await invoke(cog.reload, cog, interaction)
    assert "nothing was changed" in interaction.replies[0].content and "BOT_LANGUAGE" in interaction.replies[0].content
    assert services.settings is before


async def test_plain_replies_disappear_and_ones_to_use_or_read_stay(services):
    from app.discord_util import finish, reply

    interaction = FakeInteraction(services, FakeMember(1), ENGLISH)
    await reply(interaction, "Saved.")
    await reply(interaction, "Pick one", view=discord.ui.View())
    await reply(interaction, embed=discord.Embed(title="Profile"))
    assert [r.kwargs.get("delete_after") for r in interaction.response.sent] == [60]
    assert all("delete_after" not in r.kwargs for r in interaction.followup.sent)  # (deleted by delay instead)
    services.store.current = services.settings.model_copy(update={"times": services.settings.times.model_copy(
        update={"reply_seconds": 0})})
    stays = FakeInteraction(services, FakeMember(1), ENGLISH)
    await reply(stays, "Saved.")
    assert stays.response.sent[0].kwargs["delete_after"] is None
    pressed = FakeInteraction(services, FakeMember(1), ENGLISH)
    await finish(pressed, "Done.")
    assert pressed.response.edited[0] == {"content": "Done.", "view": None, "delete_after": None}
    deferred = FakeInteraction(services, FakeMember(1), ENGLISH)
    await deferred.response.defer()  # slow work first: the pressed message is edited afterwards
    await finish(deferred, "Done.")
    assert deferred.original_edits == [{"content": "Done.", "view": None}]


async def test_slow_panel_work_answers_thinking_first(services, monkeypatch):
    """Discord drops an interaction not answered within 3 seconds; cancelling archives the channel, which takes longer."""
    from app.events import ui

    async def slow_cancel():
        assert interaction.response.deferred_with == {"ephemeral": True, "thinking": True}  # answered before the work
        return True

    interaction = FakeInteraction(services, FakeMember(1), ENGLISH)
    modal = ui.TextModal(interaction, "Cancel event", "", 500, lambda _text: slow_cancel(), done="Cancelled.")
    modal.text._value = "no Zeus"
    await modal.on_submit(interaction)
    assert interaction.followup.sent[0].content == "Cancelled."


async def test_a_failing_command_answers_and_logs(services, caplog, monkeypatch):
    cog = AdminCog(services)
    monkeypatch.setattr(services.store, "reload", lambda: 1 / 0)
    interaction = FakeInteraction(services, FakeMember(1, roles=[FakeRole(ADMIN)]), POLISH)
    await invoke(cog.reload, cog, interaction)
    assert interaction.replies[0].kwargs["ephemeral"] is True
    assert any(r.levelname == "ERROR" and "reload" in r.getMessage() for r in caplog.records)
    await asyncio.sleep(0)
