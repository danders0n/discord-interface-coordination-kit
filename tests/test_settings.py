from pathlib import Path

import pytest
from pydantic import ValidationError

from app.settings import SettingsStore, describe_errors, load

BASE = """
BOT_DISCORD_TOKEN=abc
BOT_GUILD_ID=1
BOT_GROUP__ACRONYM=IBC
BOT_GROUP__NAME=Test
BOT_CHANNELS__DRAFTS_CATEGORY=2
BOT_CHANNELS__EVENTS_CATEGORY=3
BOT_CHANNELS__HEADER=4
BOT_CHANNELS__ANNOUNCEMENTS=5
BOT_CHANNELS__MODERATION=6
BOT_CHANNELS__STAFF_LOG=7
BOT_ROLES__MEMBER=8
BOT_ROLES__MISSION_MAKER=9
"""


def write(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / ".env"
    path.write_text(BASE + extra, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_real_env(monkeypatch):
    import os
    for name in [n for n in os.environ if n.startswith("BOT_")]:
        monkeypatch.delenv(name)


def test_lists_take_semicolons_json_and_a_single_id(tmp_path):
    env = write(tmp_path, "BOT_ROLES__SIGNUP=10;11\nBOT_PERMISSIONS__BOT_ADMIN=12\n"
                          "BOT_PERMISSIONS__APPROVE_EVENTS=[13, 14]\nBOT_PERMISSIONS__SERVER_CONTROL=\n")
    settings = load(env)
    assert settings.roles.signup == [10, 11]
    assert settings.permissions.bot_admin == [12]
    assert settings.permissions.approve_events == [13, 14]
    assert settings.permissions.server_control == []
    assert settings.language == "pl" and str(settings.tz) == "Europe/Warsaw"


def test_environment_wins_over_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_GUILD_ID", "99")
    assert load(write(tmp_path)).guild_id == 99


def test_errors_name_the_variable(tmp_path):
    env = write(tmp_path, "BOT_TIMEZONE=Mars/Olympus\n").read_text().replace("BOT_ROLES__MEMBER=8\n", "")
    (tmp_path / ".env").write_text(env)
    with pytest.raises(ValidationError) as caught:
        load(tmp_path / ".env")
    lines = describe_errors(caught.value)
    assert any(line.startswith("BOT_TIMEZONE:") and "Mars/Olympus" in line for line in lines)
    assert any(line.startswith("BOT_ROLES__MEMBER:") for line in lines)


def test_reload_applies_and_keeps_restart_only_values(tmp_path):
    env = write(tmp_path)
    store = SettingsStore(load(env), env)
    env.write_text(BASE.replace("BOT_GUILD_ID=1", "BOT_GUILD_ID=2") + "BOT_LANGUAGE=en\n", encoding="utf-8")
    result = store.reload()
    assert result.ok
    assert store.current.language == "en"
    assert store.current.guild_id == 1
    assert result.needs_restart == ["BOT_GUILD_ID"]


def test_a_missing_section_names_its_variables(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BOT_DISCORD_TOKEN=a\nBOT_GUILD_ID=1\nBOT_GROUP__ACRONYM=IBC\nBOT_GROUP__NAME=x\n"
                   "BOT_ROLES__MEMBER=1\nBOT_ROLES__MISSION_MAKER=2\n", encoding="utf-8")
    with pytest.raises(ValidationError) as caught:
        load(env)
    lines = describe_errors(caught.value)
    assert "BOT_CHANNELS__STAFF_LOG: Field required" in lines
    assert not any(line.startswith("BOT_CHANNELS__LINK_REMINDER") for line in lines)


def _leaves(model, prefix="BOT_") -> set[str]:
    from pydantic import BaseModel
    names = set()
    for name, field in model.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            names |= _leaves(annotation, f"{prefix}{name.upper()}__")
        else:
            names.add(f"{prefix}{name.upper()}")
    return names


def test_env_example_lists_every_setting_and_nothing_else():
    import re
    from pathlib import Path

    from app.settings import Settings
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    listed = set(re.findall(r"^#?\s*(BOT_[A-Z_]+)=", example, re.M))
    assert listed == _leaves(Settings)


def test_reload_with_errors_changes_nothing(tmp_path):
    env = write(tmp_path)
    store = SettingsStore(load(env), env)
    before = store.current
    env.write_text(BASE + "BOT_LANGUAGE=de\n", encoding="utf-8")
    result = store.reload()
    assert not result.ok and any(line.startswith("BOT_LANGUAGE") for line in result.errors)
    assert store.current is before
