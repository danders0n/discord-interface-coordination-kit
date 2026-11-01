import sqlite3

import pytest
from conftest import make_settings

from app.__main__ import main, prepare_database
from app.bot import EventBot, intents
from app.db import migrate


async def test_a_new_database_is_created_without_a_backup(tmp_path):
    settings = make_settings(tmp_path)
    db = await prepare_database(settings)
    assert await migrate.current(db) == migrate.head()
    await db.close()
    assert not (tmp_path / "backups").exists()


async def test_an_old_database_is_backed_up_before_migrating(tmp_path):
    settings = make_settings(tmp_path)
    sqlite3.connect(settings.database_path).close()  # exists, never migrated
    db = await prepare_database(settings)
    await db.close()
    assert len(list((tmp_path / "backups").glob("bot-*.db"))) == 1


async def test_run_wires_the_services_and_starts_the_bot(tmp_path, monkeypatch):
    import app.bot
    from app import __main__ as entry, logs

    started = []

    class Bot:
        def __init__(self, services):
            self.services = services

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            await self.services.supervisor.close()
            await self.services.db.close()

        async def start(self, token):
            started.append((token, self.services))

    monkeypatch.setattr(app.bot, "EventBot", Bot)
    monkeypatch.setattr(logs, "setup", lambda *_a, **_k: None)
    await entry.run(make_settings(tmp_path), "none.env")
    ((token, services),) = started
    assert token == "token" and services.reservations.store is services.store


def test_check_config_reports_errors_and_exits(tmp_path, capsys, monkeypatch):
    import os
    for name in [n for n in os.environ if n.startswith("BOT_")]:
        monkeypatch.delenv(name)
    env = tmp_path / "bad.env"
    env.write_text("BOT_GUILD_ID=x\n", encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        main(["check-config", "--env-file", str(env)])
    assert caught.value.code == 2
    assert "BOT_GUILD_ID" in capsys.readouterr().err


async def test_the_bot_asks_for_the_privileged_intents(services):
    chosen = intents()
    assert chosen.members and chosen.message_content
    bot = EventBot(services)
    assert bot.guild_object.id == services.settings.guild_id
    await bot.http.close()


async def test_setup_registers_the_guild_commands_and_starts_the_tasks(services, monkeypatch):
    from discord import Locale, app_commands

    from app import heartbeat

    synced = []

    async def sync(*, guild=None):
        synced.append(guild.id)
        return []

    async def no_heartbeat(client):
        return None

    monkeypatch.setattr(heartbeat, "run", no_heartbeat)
    bot = EventBot(services)
    monkeypatch.setattr(bot.tree, "sync", sync)
    await bot.setup_hook()
    names = {c.name for c in bot.tree.get_commands(guild=bot.guild_object)}
    assert names == {"reload", "privacy", "server", "event", "slot", "events", "link", "unlink", "links"} and bot.tree.get_commands() == []
    assert synced == [services.settings.guild_id]
    translated = await bot.tree.translator.translate(app_commands.locale_str("privacy"), Locale.polish,
                                                     app_commands.TranslationContext(
                                                         app_commands.TranslationContextLocation.command_name, None))
    assert translated == "prywatność"
    assert {t.get_name() for t in bot._tasks} == {"scheduler", "staff-log", "heartbeat"}
    for task in bot._tasks:
        task.cancel()
    await bot.http.close()
