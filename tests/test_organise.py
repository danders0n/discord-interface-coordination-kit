"""Creating and organising operations on Discord (docs/PLAN-front-end.md, phase 4): the wizard, the organiser panel
with ``actions`` and ``If-Match``, the ORBAT tools, ``/operation preset | image | mission`` and ``/operations``, on
the Discord fakes and the fake supervisor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import HEADER
from fakes import FakeAttachment, invoke
from test_channel import row, run, stack  # noqa: F401  (stack: a fixture)
from test_signup import ORBAT, OPORD, PUBLISH, acting, controls, front, moderation_thread, press, said  # noqa: F401

from app.discord_util import on_modal_error
from app.links.service import LinkService
from app.operations import ui
from app.operations.cog import OperationCog
from app.operations.orbat_ui import OrbatTools, SlotTools
from app.operations.ui import CreateOperation, PanelAction, PanelButton

CREATE = "operations.create"


@pytest.fixture
async def organise(front, fake):  # noqa: F811
    """The front end, plus Steam links' person lookup (the panel maps picked members to persons) and the meta the
    feed reads. Anna (1) may create operations; Bob (2) may create and publish."""
    front.links = LinkService(front.db, front.supervisor, front.roster.as_member, front.scheduler.clock)
    fake.persons["1"]["permissions"] += [CREATE]
    fake.persons["2"]["permissions"] += [CREATE, PUBLISH]
    fake.climates = [{"id": 1, "name": "Winter", "role_id": None}]
    await run(front)  # (the feed reads the meta on its first run)
    yield front


def submit(modal, **values) -> None:
    """Type into a modal's fields (by attribute name)."""
    for name, value in values.items():
        getattr(modal, name)._value = value


def modal_of(interaction):
    return interaction.response.modals[-1]


async def panel_press(organise, guild, who: int, op_id: int, action: str):
    pressed = acting(organise, guild, who)
    await press(PanelAction(op_id, action), pressed)
    return pressed


# ---------------------------------------------------------------- the wizard


async def test_the_wizard_makes_a_draft_and_the_feed_brings_its_channel(organise, guild, fake, clock):
    started = acting(organise, guild, 1)
    await press(CreateOperation(), started)
    choices = started.replies[-1].kwargs["view"]
    kinds = controls(choices)[f"wizard:{choices.token}:kind"]
    assert "guest" not in [o.value for o in kinds.options]  # (a guest operation needs Publish operations)
    await choices._chosen(acting(organise, guild, 1), "kind", ["internal"])
    await choices._chosen(acting(organise, guild, 1), "climate", ["Winter"])
    go = acting(organise, guild, 1)
    await choices._go(go)
    details = modal_of(go)
    submit(details, name="Ibis", date="10.11", assembly="19:30", start="20:00", duration="2h30")
    made = acting(organise, guild, 1)
    await details.on_submit(made)
    assert said(made) == ("Your draft OP-1 is made; its channel appears in a moment. Finish it there with Organiser "
                          "panel.")
    op = fake.operations[1]
    assert (op["title"], op["kind"], op["climate"], op["duration_minutes"]) == ("Ibis", "internal", "Winter", 150)
    assert op["assembly_at"] == datetime(2026, 11, 10, 18, 30, tzinfo=UTC)  # 19:30 in Warsaw
    await run(organise)
    channel = guild.get_channel((await row(organise, 1)).channel_id)
    assert list(controls(channel.messages[0].view)) == ["operation:panel:1"]  # the panel under the info
    assert ui.LIVE == set()  # (the wizard is done)


async def test_the_wizard_words_a_mistyped_date(organise, guild):
    started = acting(organise, guild, 1)
    await press(CreateOperation(), started)
    choices = started.replies[-1].kwargs["view"]
    await choices._chosen(acting(organise, guild, 1), "kind", ["internal"])
    go = acting(organise, guild, 1)
    await choices._go(go)
    details = modal_of(go)
    submit(details, name="Ibis", date="31.02", assembly="19:30", start="20:00", duration="3h")
    typed = acting(organise, guild, 1)
    await details.on_submit(typed)
    assert said(typed) == "Type the date as dd.mm or dd.mm.yyyy."


async def test_a_wizard_from_before_a_restart_says_it_expired(organise, guild):
    pressed = acting(organise, guild, 1)
    pressed.data = {"custom_id": "wizard:0123456789ab:go"}
    assert await ui.expired(pressed)
    assert said(pressed) == "This wizard has expired (the bot restarted). Press Create operation again."


async def test_the_header_carries_create_operation(organise, guild):
    await organise.channels.header.draw()
    (header,) = guild.get_channel(HEADER).messages
    assert list(controls(header.view)) == ["operation:create"]


# ---------------------------------------------------------------- the panel


async def test_the_panel_shows_what_the_supervisor_allows(organise, guild, fake):
    op = fake.operation("Ibis", author_person_id=1)
    opened = acting(organise, guild, 1)
    await press(PanelButton(op["id"]), opened)
    text, view = said(opened), opened.replies[-1].kwargs["view"]
    assert text.startswith("**Ibis** · OP-1 · Draft\nStill missing: the OPORD's Situation, the OPORD's Mission.")
    sections = [custom_id.split(":")[-1] for custom_id in controls(view)]
    assert sections == ["details", "choices", "opord", "orbat", "preset", "mission", "signup", "organisers", "hold",
                        "autoend", "submit", "copy", "delete"]
    stranger = acting(organise, guild, 3)
    await press(PanelButton(op["id"]), stranger)
    assert said(stranger) == "The panel is for this operation's organisers."


async def test_the_opord_is_written_in_one_modal_and_drawn(organise, guild, fake):
    op = fake.operation("Ibis", author_person_id=1)
    await run(organise)
    pressed = await panel_press(organise, guild, 1, op["id"], "opord")
    modal = modal_of(pressed)
    modal.fields["situation"]._value, modal.fields["mission"]._value = "Enemy north.", "Take the bridge."
    saved = acting(organise, guild, 1)
    await modal.on_submit(saved)
    assert said(saved) == "Saved."
    assert fake.operations[op["id"]]["opord"] == {"situation": "Enemy north.", "mission": "Take the bridge."}
    await run(organise)
    channel = guild.get_channel((await row(organise, op["id"])).channel_id)
    assert any(m.text.endswith("## Sytuacja\nEnemy north.") for m in channel.messages)


async def test_a_change_made_meanwhile_is_answered_with_reopen(organise, guild, fake):
    op = fake.operation("Ibis", author_person_id=1)
    pressed = await panel_press(organise, guild, 1, op["id"], "details")
    modal = modal_of(pressed)
    fake.change(op["id"], title="Ibis (renamed in the app)")
    submit(modal, name="Ibis II")
    saved = acting(organise, guild, 1)
    await modal.on_submit(saved)
    assert said(saved) == "The operation changed meanwhile; open it again to see the change."
    assert list(controls(saved.replies[-1].kwargs["view"])) == [f"operation:act:{op['id']}:details"]
    assert fake.operations[op["id"]]["title"] == "Ibis (renamed in the app)"


async def test_new_times_of_an_approved_members_operation_go_to_the_moderators(organise, guild, fake):
    op = fake.operation("Ibis", status="published", opord=OPORD, author_person_id=1)
    pressed = await panel_press(organise, guild, 1, op["id"], "details")
    modal = modal_of(pressed)
    submit(modal, start="21:00")
    saved = acting(organise, guild, 1)
    await modal.on_submit(saved)
    assert said(saved) == "New times sent to the moderators; nothing changes until they approve."
    assert fake.operations[op["id"]]["start_at"] == op["start_at"]
    await run(organise)
    await organise.notices.run_once()
    message = (await moderation_thread(organise, guild)).sent[-1]
    assert message.embeds[0].title == "Przenieść operację: Ibis"


async def test_send_for_approval_needs_the_opord_and_can_be_withdrawn(organise, guild, fake):
    op = fake.operation("Ibis", author_person_id=1)
    early = await panel_press(organise, guild, 1, op["id"], "submit")
    assert said(early) == "Still missing: the OPORD's Situation, the OPORD's Mission."
    fake.change(op["id"], opord=OPORD)
    sent = await panel_press(organise, guild, 1, op["id"], "submit")
    assert said(sent) == "Sent to the moderators for approval."
    assert fake.operations[op["id"]]["status"] == "pending"
    back = await panel_press(organise, guild, 1, op["id"], "withdraw")
    assert said(back) == "The request is withdrawn; the operation is a draft again."
    assert fake.operations[op["id"]]["status"] == "draft"


async def test_a_publisher_publishes_and_a_button_no_longer_allowed_says_so(organise, guild, fake):
    op = fake.operation("Ibis", opord=OPORD, author_person_id=2, by="bob")
    done = await panel_press(organise, guild, 2, op["id"], "publish")
    assert said(done) == "Published."
    again = await panel_press(organise, guild, 2, op["id"], "publish")
    assert said(again) == "That can't be done now; open the panel again to see what can."


async def test_co_organisers_hold_and_cancel(organise, guild, fake):
    op = fake.operation("Ibis", status="published", opord=OPORD, author_person_id=1)
    pressed = await panel_press(organise, guild, 1, op["id"], "organisers")
    organisers = pressed.replies[-1].kwargs["view"]
    added = acting(organise, guild, 1)
    await organisers._change(added, [guild.members[3]], True)
    assert said(added) == "Saved." and fake.operations[op["id"]]["organisers"] == [3]
    held = await panel_press(organise, guild, 1, op["id"], "hold")
    assert said(held) == "The host is not held for this operation." and fake.operations[op["id"]]["hold"] == "off"
    cancelled = await panel_press(organise, guild, 1, op["id"], "cancel")
    modal = modal_of(cancelled)
    submit(modal, text="Rain")
    done = acting(organise, guild, 1)
    await modal.on_submit(done)
    assert said(done) == "Cancelled."
    assert (fake.operations[op["id"]]["status"], fake.operations[op["id"]]["cancel_reason"]) == ("cancelled", "Rain")


async def test_a_zeus_operations_specialist_roles_and_a_guest_operations_author(organise, guild, fake):
    zeus = fake.operation("Night", kind="zeus", author_person_id=1, specialist_roles=[{"name": "Medic", "limit": 2}])
    pressed = await panel_press(organise, guild, 1, zeus["id"], "roles")
    modal = modal_of(pressed)
    assert modal.text.default == "Medic x2"
    submit(modal, text="Command x1\n\n  Medic  \nPilot x0")
    saved = acting(organise, guild, 1)
    await modal.on_submit(saved)
    assert said(saved) == "Saved."
    assert fake.operations[zeus["id"]]["specialist_roles"] == [
        {"name": "Command", "limit": 1, "min": None}, {"name": "Medic", "limit": None, "min": None},
        {"name": "Pilot", "limit": None, "min": None}]
    guest = fake.operation("Theirs", kind="guest", author_person_id=1)
    opened = acting(organise, guild, 1)
    await press(PanelButton(guest["id"]), opened)
    assert "author" in [c.split(":")[-1] for c in controls(opened.replies[-1].kwargs["view"])]
    pressed = await panel_press(organise, guild, 1, guest["id"], "author")
    modal = modal_of(pressed)
    submit(modal, text=" Cpt. Smith ")
    saved = acting(organise, guild, 1)
    await modal.on_submit(saved)
    assert said(saved) == "Saved." and fake.operations[guest["id"]]["author_name"] == "Cpt. Smith"


async def test_a_zeus_operation_picks_a_map_instead_of_a_preset_and_a_mission(organise, guild, fake):
    """Zeus and training use their base profile's mods and mission; the panel's *Map* adds one from the
    supervisor's Zeus maps list (its one mod and its Zeus mission), the info shows it, and the players get the
    supervisor's preset (the base's mods and the map's) to download in the channel."""
    cup = {"id": 5, "name": "CUP Terrains", "workshop_id": "583496184"}
    fake.zeus_maps = [{"id": 1, "name": "Tanoa", "mission": "zeus.Tanoa", "world": "Tanoa", "mod": cup, "mod_id": 5}]
    zeus = fake.operation("Night", kind="zeus", author_person_id=1)
    opened = acting(organise, guild, 1)
    await press(PanelButton(zeus["id"]), opened)
    sections = [c.split(":")[-1] for c in controls(opened.replies[-1].kwargs["view"])]
    assert "map" in sections and "preset" not in sections and "mission" not in sections
    pressed = await panel_press(organise, guild, 1, zeus["id"], "map")
    assert said(pressed).startswith("Map: none (the base profile's own mission)")
    picker = pressed.replies[-1].kwargs["view"]
    assert [o.label for o in picker.children[0].options] == ["None: the base profile's own mission", "Tanoa"]
    chose = acting(organise, guild, 1)
    await picker._chosen(chose, "Tanoa")
    assert chose.original_edits[-1]["content"] == "Saved."
    assert fake.operations[zeus["id"]]["zeus_map"]["mission"] == "zeus.Tanoa"
    await run(organise)
    channel = guild.get_channel((await row(organise, zeus["id"])).channel_id)
    fields = {f.name: f.value for f in channel.messages[0].embeds[0].fields}
    assert fields["Mapa"] == "Tanoa" and fields["Modset"] == "Standard + Tanoa: 2 mody"
    drawn = await row(organise, zeus["id"])
    preset = channel.get_partial_message(drawn.messages["preset"])._find()
    assert preset.content.endswith("## Modset do Arma 3 Launchera: Standard + Tanoa")
    assert preset.kwargs["files"][0].filename == "Standard + Tanoa.html"
    again = await panel_press(organise, guild, 1, zeus["id"], "map")
    assert said(again).startswith("Map: Tanoa")
    none = acting(organise, guild, 1)
    await again.replies[-1].kwargs["view"]._chosen(none, ui.NO_MAP)
    assert fake.operations[zeus["id"]]["zeus_map"] is None
    assert fake.operations[zeus["id"]]["files"]["preset"]["preset_name"] == "Standard"  # (the base's alone)
    internal = fake.operation("Raid", author_person_id=1)
    refused = await panel_press(organise, guild, 1, internal["id"], "map")
    assert said(refused) == "That can't be done now; open the panel again to see what can."


async def test_a_zeus_operations_minimum_is_set_in_the_panel(organise, guild, fake, clock):
    """*Minimum to go ahead* (Zeus only): when sign-up closes, how many must come, cancel or tell when short; a
    role's own minimum is typed with the roles (``Medic 1-3``)."""
    zeus = fake.operation("Night", kind="zeus", author_person_id=1)
    opened = acting(organise, guild, 1)
    await press(PanelButton(zeus["id"]), opened)
    assert "minimum" in [c.split(":")[-1] for c in controls(opened.replies[-1].kwargs["view"])]
    pressed = await panel_press(organise, guild, 1, zeus["id"], "minimum")
    modal = modal_of(pressed)
    submit(modal, date="x", time="18:00", players="4")
    wrong = acting(organise, guild, 1)
    await modal.on_submit(wrong)
    assert said(wrong).startswith("Type the date")  # (the date's own words)
    closes = zeus["assembly_at"].astimezone(organise.settings.tz) - timedelta(hours=2)
    submit(modal, date=f"{closes:%d.%m}", time=f"{closes:%H:%M}", players="4")
    modal.auto._values = ["yes"]
    saved = acting(organise, guild, 1)
    await modal.on_submit(saved)
    assert said(saved) == "Saved."
    op = fake.operations[zeus["id"]]
    assert (op["min_players"], op["auto_cancel"]) == (4, True) and op["signup_closes_at"] == closes.astimezone(UTC)
    roles = await panel_press(organise, guild, 1, zeus["id"], "roles")
    modal = modal_of(roles)
    submit(modal, text="Medic 1-3\nPilot 2+\nCommand x1")
    await modal.on_submit(acting(organise, guild, 1))
    assert fake.operations[zeus["id"]]["specialist_roles"] == [
        {"name": "Medic", "limit": 3, "min": 1}, {"name": "Pilot", "limit": None, "min": 2},
        {"name": "Command", "limit": 1, "min": None}]
    training = fake.operation("Drill", kind="training", author_person_id=1)
    opened = acting(organise, guild, 1)
    await press(PanelButton(training["id"]), opened)
    assert "minimum" not in [c.split(":")[-1] for c in controls(opened.replies[-1].kwargs["view"])]


async def test_a_draft_is_deleted_after_a_confirmation(organise, guild, fake):
    op = fake.operation("Ibis", author_person_id=1)
    asked = await panel_press(organise, guild, 1, op["id"], "delete")
    confirm = asked.replies[-1].kwargs["view"]
    sure = acting(organise, guild, 1)
    await confirm._delete(sure)
    assert sure.original_edits[-1]["content"] == "Deleted." and op["id"] not in fake.operations


# ---------------------------------------------------------------- the ORBAT


async def test_the_orbat_is_pasted_saved_as_a_template_and_taken_from_it(organise, guild, fake):
    op = fake.operation("Ibis", author_person_id=1)
    pressed = await panel_press(organise, guild, 1, op["id"], "orbat")
    tools = pressed.replies[-1].kwargs["view"]
    assert isinstance(tools, OrbatTools)
    asked = acting(organise, guild, 1)
    await tools._paste(asked)
    modal = modal_of(asked)
    submit(modal, text=ORBAT)
    saved = acting(organise, guild, 1)
    await modal.on_submit(saved)
    assert said(saved) == "Saved: 2 squads: Alpha (3), Bravo (1)."
    naming = acting(organise, guild, 1)
    await tools._save(naming)
    name = modal_of(naming)
    submit(name, text="Platoon")
    kept = acting(organise, guild, 1)
    await name.on_submit(kept)
    assert said(kept) == "Template saved." and fake.templates[0]["name"] == "Platoon"
    other = fake.operation("Heron", author_person_id=1)
    tools = OrbatTools(acting(organise, guild, 1), fake.out(fake.operations[other["id"]], fake.persons["1"]))
    listed = acting(organise, guild, 1)
    await tools._template(listed)
    select = listed.replies[-1].kwargs["view"].children[0]
    select._values = [str(fake.templates[0]["id"])]
    taken = acting(organise, guild, 1)
    await select.callback(taken)
    assert said(taken) == "Saved: 2 squads: Alpha (3), Bravo (1)."
    assert len(fake.slots(fake.operations[other["id"]])) == 4


async def test_a_bad_orbat_lists_its_problems(organise, guild, fake):
    op = fake.operation("Ibis", author_person_id=1)
    tools = OrbatTools(acting(organise, guild, 1), fake.out(fake.operations[op["id"]], fake.persons["1"]))
    asked = acting(organise, guild, 1)
    await tools._paste(asked)
    modal = modal_of(asked)
    submit(modal, text="Lead\n# Alpha\nRifleman [Medic]")
    saved = acting(organise, guild, 1)
    try:  # (as Discord runs a modal: its error handler on a refusal)
        await modal.on_submit(saved)
    except Exception as exc:  # noqa: BLE001
        await on_modal_error(modal, saved, exc)
    assert said(saved).startswith("The ORBAT has problems:\n- Line 1: a slot before the first squad")


async def test_an_organiser_assigns_a_member_to_a_slot(organise, guild, fake):
    op = fake.operation("Ibis", status="published", opord=OPORD, author_person_id=1)
    fake.set_orbat(op["id"], ORBAT)
    lead = fake.slots(fake.operations[op["id"]])[0]["id"]
    tools = SlotTools(acting(organise, guild, 1), fake.out(fake.operations[op["id"]], fake.persons["1"]))
    tools.slot_id = lead
    done = acting(organise, guild, 1)
    await tools._act(done, "assign", guild.members[3])
    assert said(done) == "Saved."
    slot = fake.slot(fake.operations[op["id"]], lead)
    assert (slot["holder"], slot["locked"]) == (fake.persons["3"]["id"], True)
    fake.set_orbat(op["id"], "# HQ {hq}\n" + ORBAT.replace("# Alpha", "# Alpha < HQ"))
    tools = SlotTools(acting(organise, guild, 1), fake.out(fake.operations[op["id"]], fake.persons["1"]))
    assert "HQ" not in [option.label for option in tools.children[0].options]  # (a squad without slots: nothing to pick)


# ---------------------------------------------------------------- files and the list


async def test_files_are_uploaded_in_the_operations_channel(organise, guild, fake):
    op = fake.operation("Ibis", author_person_id=1)
    fake.server_missions = ["ibis.Altis.pbo"]
    await run(organise)
    channel_id = (await row(organise, op["id"])).channel_id
    cog = OperationCog(organise)
    preset = '<tr data-type="ModContainer"></tr>' * 2
    uploaded = acting(organise, guild, 1, channel_id)
    await invoke(cog.preset, cog, uploaded, file=FakeAttachment("ibis.html", preset.encode()))
    assert said(uploaded) == "Preset saved: 2 mods.\nIt reaches the server once the operation is approved."
    image = acting(organise, guild, 1, channel_id)
    await invoke(cog.image, cog, image, file=FakeAttachment("map.png", b"png"), slot="map")
    assert said(image) == "Saved." and "map" in fake.operations[op["id"]]["files"]
    picked = acting(organise, guild, 1, channel_id)
    await invoke(cog.mission, cog, picked, on_server="ibis.Altis.pbo")
    assert said(picked).startswith("Mission set: ibis.Altis.pbo")
    elsewhere = acting(organise, guild, 1, 12345)
    await invoke(cog.preset, cog, elsewhere, file=FakeAttachment("ibis.html", preset.encode()))
    assert said(elsewhere) == "Use this in an operation's channel or its thread."


async def test_the_panel_takes_the_preset_and_the_mission_as_files(organise, guild, fake):
    op = fake.operation("Ibis", author_person_id=1)
    fake.server_missions = ["big.Altis.pbo"]
    pressed = await panel_press(organise, guild, 1, op["id"], "preset")
    modal = modal_of(pressed)
    modal.file._values = [FakeAttachment("ibis.html", b'<tr data-type="ModContainer"></tr>' * 3)]
    saved = acting(organise, guild, 1)
    await modal.on_submit(saved)
    assert said(saved) == "Preset saved: 3 mods.\nIt reaches the server once the operation is approved."
    assert fake.operations[op["id"]]["files"]["preset"]["staged"]
    wrong = acting(organise, guild, 1)
    modal.file._values = [FakeAttachment("ibis.txt", b"mods")]
    await modal.on_submit(wrong)
    assert said(wrong) == "Send the .html preset exported from the Arma 3 Launcher."

    pressed = await panel_press(organise, guild, 1, op["id"], "mission")
    modal = modal_of(pressed)
    empty = acting(organise, guild, 1)
    await modal.on_submit(empty)
    assert said(empty) == "Attach the mission's .pbo, or pick a mission already on the server."
    modal.file._values = [FakeAttachment("ibis.Altis.pbo", b"pbo")]
    uploaded = acting(organise, guild, 1)
    await modal.on_submit(uploaded)
    assert said(uploaded).startswith("Mission set: ibis.Altis.pbo")
    modal.file._values, modal.on_server._value = [], " big.Altis.pbo "
    picked = acting(organise, guild, 1)
    await modal.on_submit(picked)
    assert said(picked).startswith("Mission set: big.Altis.pbo")
    assert fake.operations[op["id"]]["mission"]["name"] == "big.Altis.pbo"


async def test_the_preset_and_mission_buttons_follow_the_supervisors_actions(organise, guild, fake):
    op = fake.operation("Ibis", author_person_id=1)
    out = fake.out(fake.operations[op["id"]], fake.persons["1"])
    out["my"]["actions"] = [a for a in out["my"]["actions"] if a not in ("preset", "mission")]  # (frozen)
    assert {"preset", "mission"}.isdisjoint(ui.panel_sections(out))
    text, _view = ui.panel(out, "en-US")
    assert "/operation image" in text


async def test_operations_lists_the_coming_ones_with_the_members_place(organise, guild, fake, clock):
    op = fake.operation("Ibis", status="published", opord=OPORD)
    fake.set_orbat(op["id"], ORBAT)
    fake.sign(op["id"], 1, slot_id=fake.slots(fake.operations[op["id"]])[0]["id"])
    fake.operation("Far", status="published", opord=OPORD, assembly=clock.now + timedelta(days=60))
    await run(organise)
    cog = OperationCog(organise)
    asked = acting(organise, guild, 1)
    await invoke(cog.operations, cog, asked)
    listing = asked.replies[-1].embeds[0].description
    assert "**Ibis**" in listing and "__your slot: Lead__" in listing and "Far" not in listing
