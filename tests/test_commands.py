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
    assert "Co jest o Tobie zapisane" in sent.kwargs["embed"].description
    english = FakeInteraction(services, FakeMember(1), ENGLISH)
    await invoke(cog.privacy, cog, english)
    assert "What is stored about you" in english.replies[0].kwargs["embed"].description


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


async def test_a_failing_command_answers_and_logs(services, caplog, monkeypatch):
    cog = AdminCog(services)
    monkeypatch.setattr(services.store, "reload", lambda: 1 / 0)
    interaction = FakeInteraction(services, FakeMember(1, roles=[FakeRole(ADMIN)]), POLISH)
    await invoke(cog.reload, cog, interaction)
    assert interaction.replies[0].kwargs["ephemeral"] is True
    assert any(r.levelname == "ERROR" and "reload" in r.getMessage() for r in caplog.records)
    await asyncio.sleep(0)


async def test_a_failing_button_or_modal_answers_the_member(services, caplog):
    import discord as d

    from app.discord_util import install_error_handlers

    install_error_handlers()
    view, modal = d.ui.View(), d.ui.Modal(title="x")
    pressed = FakeInteraction(services, FakeMember(1), d.Locale.british_english)
    await view.on_error(pressed, RuntimeError("boom"), d.ui.Button(label="b"))
    assert pressed.replies[0].content == "Something went wrong. Staff can find the details in the log."
    submitted = FakeInteraction(services, FakeMember(1), d.Locale.british_english)
    await modal.on_error(submitted, RuntimeError("boom"))
    assert submitted.replies[0].content.startswith("Something went wrong")
    assert "boom" in caplog.text and "by member (1) failed" in caplog.text  # who it was, for staff


async def test_a_refusal_is_logged_with_who_asked(services, caplog):
    import logging

    import discord as d

    from app.discord_util import install_error_handlers
    from app.supervisor_client import SupervisorError

    install_error_handlers()
    caplog.set_level(logging.INFO, logger="app.discord_util")
    submitted = FakeInteraction(services, FakeMember(7, "anna_dc"), d.Locale.british_english)
    await d.ui.Modal(title="x").on_error(submitted, SupervisorError(400, "A custom Steam address can't be used"))
    assert submitted.replies[0].content == "A custom Steam address can't be used"
    assert "by anna_dc (7): the supervisor answered 400" in caplog.text


def test_every_persistent_button_answers_its_own_failures():
    """discord.py never passes what a ``DynamicItem``'s callback raises to ``View.on_error``; it only logs it. So each
    one's callback carries ``discord_util.answers``, or a refusal leaves the member without an answer."""
    import app.approval.ui
    import app.attendance.ui
    import app.links.ui
    import app.operations.ui
    import app.signup.ui  # noqa: F401 (the modules that define them)

    def every(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from every(sub)

    found = [cls for cls in every(discord.ui.DynamicItem) if cls.__module__.startswith("app.")]
    assert len(found) >= 9
    assert [cls.__name__ for cls in found if not getattr(cls.callback, "answers", False)] == []


async def test_a_refused_persistent_button_answers_in_words(services, caplog):
    import logging

    from app.signup.ui import SignupAction
    from app.supervisor_client import SupervisorError

    async def refused(*_args, **_kwargs):
        raise SupervisorError(403, "You may not sign up", {"detail": "You may not sign up", "code": "forbidden"})

    services.roster = type("R", (), {"as_member": staticmethod(refused)})()
    caplog.set_level(logging.INFO, logger="app.discord_util")
    pressed = FakeInteraction(services, FakeMember(7, "anna_dc"), ENGLISH)
    await SignupAction(1, "maybe").callback(pressed)  # as discord.py runs it: no on_error after it
    assert pressed.original_edits or pressed.replies  # the member heard back
    assert "SignupAction by anna_dc (7): the supervisor answered 403" in caplog.text
