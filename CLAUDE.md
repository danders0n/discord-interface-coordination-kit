# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Discord bot for one Arma 3 group's Discord server. Other groups have their own infrastructure and appear in the
bot only by name. Events are *internal* (ours, on our server), *guest* (another group hosts; we get some of its
slots), *hosted* (ours, on our server, with invited groups), *Zeus* (ours, on our server, no ORBAT, just
*Coming* / *Not coming*) or *training* (Zeus's rules, another name). Members' events, except Zeus and training events, need a moderator's approval before they are
published. It runs events (missions) with ORBAT slot sign-ups, reserves the game host
for them on **arma-server-supervisor** and builds each event's server profile there (mods from the preset, the
mission), reports attendance from the supervisor's player stats, and generates the
group's squad.xml. The supervisor is a separate project at `D:\Workspace\python\arma-server-supervisior`;
read its `CLAUDE.md` and `README.md` before touching the integration.

Status: the bot of [docs/PLAN.md](docs/PLAN.md) (phases 1–8) was built and is on `master`. On `feature/front-end`
it is being rebuilt as **the supervisor's Discord front end** ([docs/PLAN-front-end.md](docs/PLAN-front-end.md)):
the supervisor owns operations (the bot's events), sign-ups, approval, attendance, Steam links and who may do what;
the bot draws, delivers and acts for members (`X-Act-As`). Steps 1–5 are done: the roster, notices, live events,
Steam links, the supervisor check, the operation channels drawn from the change feed (the old event stack is
deleted), the ORBAT with its sign-up buttons, `/slot`, offers, reminders and the moderation thread, and the wizard,
the organiser panel, `/operation preset | image | mission` and `/operations`, and attendance, ratings, the report
commands and erasure (steps 1–7, the plan's phases 1–5). Next: phase 6, the release against the real supervisor
(*As built*, *Next* in the front-end plan). `docs/PLAN.md` stays the reference for how things look on Discord; its
data rules moved to the supervisor. Read the relevant section before implementing a feature, and keep the plans and
this file up to date as the code lands. [README.md](README.md) covers installing and running it.

## Stack

| Topic | Decision |
|---|---|
| Discord | discord.py: `app_commands` slash commands, buttons / selects / modals, persistent views and `DynamicItem` |
| Storage | SQLite through SQLAlchemy 2.x async + aiosqlite (never block the event loop); Alembic migrations applied at startup; a daily online backup |
| Supervisor client | HTTP over `aiohttp` (already a discord.py dependency). Never import the supervisor's code or open its database; code worth reusing, such as the preset parser in `app/steam/presets.py`, is ported |
| Config | Everything in `.env` (channels and roles by id, the role → permission mapping, times, secrets), read with pydantic-settings like the supervisor: one prefix, `__` for nesting, lists as JSON or `;`-separated. `/reload` re-reads it |
| Install | `pip` + pinned `requirements.txt` (the supervisor's convention), `tzdata` included; Python 3.11+ |
| Layout | `app/` by domain (events, orbat, signup, approval, attendance, …), each a `service.py` without discord.py plus a thin `cog.py` / `views.py`; run with `python -m app`. Details in the plan, *Code and tooling* |
| Logs and service | Rotated `data/logs/bot.log` (ERRORs also to the staff log); runs under NSSM like the supervisor |
| Language | Polish and English. Private replies follow the member's Discord locale; public messages use the config language. Every string goes through `t()` with the English text as the key and `app/i18n/pl.json` (a test enforces coverage); slash commands use `app_commands.Translator` |
| Deployment | One guild (commands synced to it only), on the same host as the supervisor |

## Commands

Windows paths; there is no linter (as in the supervisor).

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # includes requirements.txt
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m pytest tests/test_scheduler.py::test_a_repeating_action_moves_on -q
.venv/Scripts/python.exe -m app                     # run the bot (validates .env, migrates, connects)
.venv/Scripts/python.exe -m app check-config        # validate .env only
.venv/Scripts/python.exe -m app new-migration "what changed"   # after editing app/db/models.py
.venv/Scripts/python.exe -m tests.fake_supervisor 8099         # fake supervisor for a test instance
```

Tests never need the network or a live Discord: services are tested directly, the Discord layer through the
fakes in `tests/fakes.py` (`invoke()` runs a command's checks, callback and error handler like the tree), the
supervisor through `tests/fake_supervisor.py`. Two tests guard the conventions: `tests/test_i18n.py` (every
`t()` literal and command text has a Polish entry with the same fields; `t()` takes literals only) and
`tests/test_db.py` (the models match the migrations). `tests/test_settings.py` checks that `.env.example` lists
exactly the settings.

## Code map

- `app/__main__.py`: the CLI; `run` wires everything (settings store, logging, database, scheduler, services, bot).
- `app/bot.py`: the Discord client, guild-only commands, background tasks, the start-up check and staff note.
- `app/services.py`: what cogs share. `app/discord_util.py`: `require(Perm)`, `reply()` (plain text
  replies disappear after `times.reply_seconds`), `finish()` (a last word replacing a pressed message), the
  command error handler.
- `app/settings.py`: `.env` model and `SettingsStore` (`/reload`; restart-only values keep their old value).
  Channels and roles are `Ref`s: an id or a name. Only the bot's own settings are left (front-end plan,
  *Configuration*).
- `app/resolve.py`: turns the `Ref`s into `Ids` on the server (start-up, `/reload`); code reads `services.ids`,
  never the raw settings, for channels and roles.
- `app/permissions.py`: `Access` from role ids against `Ids` (no discord.py); only *bot admin* is left, every other
  right is the supervisor's (act-as).
- `app/staff_threads.py`: the staff log and moderation threads in the staff channel (made, remembered, reopened).
- `app/scheduler.py`: `scheduled_action` rows; handlers return the next due time or None.
- `app/db/`: engine (WAL pragmas), models (`UtcDateTime`: aware UTC only), migrations, backup.
- `app/logs.py`, `app/staff_log.py`: rotated file log, ERROR ids, the staff log queue (notes waiting together go out joined).
- `app/supervisor_client/`, `app/heartbeat.py`: HTTP client (act-as, the roster, notices, the stream, the
  operations' feed, `meta`, files and plumbing), capabilities, heartbeat loop.
- Domains: `app/admin/` (`/reload`), `app/privacy/` (`/privacy` and its texts; `/privacy-erase` with a
  confirmation, erasing in the supervisor in the staff member's name; `erase.py` emptying what the bot holds when
  the `erased` notice comes: link reminders, archived messages, as the supervisor's `former:<n>` stored as `-n`).
- `app/operations/`: `feed.py` (`Feed`: `GET /api/operations/changes` oldest first into the channels, the cursor in
  `bot_state`, `reset`, `meta`), `header.py` (the header message and its calendar, from `operation_channel`),
  `times.py` (what organisers type: `dd.mm` = next such date, a start before the assembly = next day, aware UTC
  out; for the wizard), `words.py` (the supervisor's operation refusal codes in words, used by
  `discord_util.supervisor_words`; typed-time problems), `ui.py` (the wizard: `CreateOperation`, `Choices`,
  `DetailsModal`, `expired()`; the panel: `PanelButton`, `PanelAction` per section from `my.actions`, `write()` with
  `If-Match` and *Reopen* on `stale`), `orbat_ui.py` (`OrbatTools`, `TemplateTools`, `SlotTools`), `cog.py`
  (`/operation preset | image | mission`, `/operations`).
- `app/channel/`: `render.py` (pure: the operation JSON -> `Message`s keyed `info` (embed), the OPORD paragraphs
  `situation` ... `command` (each `[:N]`, split by `texts()` at 2000 characters), `preset`, `orbat:0` ... (the ORBAT,
  or Zeus / training's Coming list; `phase(op, now)` is the sign-up state from the times), `discussion` (the thread
  link, last); `organisers(op)` gives the Discord ids), `sync.py` (keeps them in order: edit in place, append /
  delete continuations, re-post what follows a message that must go before it, or one deleted by hand; edits always
  pass `view`, as leaving it out keeps old buttons), `order.py` (a category's channels: `Ids.top` first, then the
  rest, then the operations by start; one bulk position request, only when the order differs), `service.py`
  (`OperationChannels`: `apply(op)` / `gone(id)` from the feed; a redraw converges on the operation: the channel, the
  overwrites (organisers and `meta`'s staff roles), the messages, the thread at publishing (Discord's card last,
  stored as `card`), the plumbing back, the announcement, the rename, the sign-up ping, the reminders, the archive by
  quarter; batched via `Redrawer`, `sorter` for the order; `refresh_all` after a restart; the sign-up buttons on the
  last ORBAT message; `ping()` in the thread (a draft: DMs); `by_channel()`; `watchers` hear `changed(op)` /
  `gone(id)`). `EventBot.build_operations` wires channels, header and feed.
- `app/signup/`: `ui.py` (persistent `SquadList` / `SignupAction` / `OfferAction`, the private `SlotPicker`, `act()`:
  an act-as call through `Roster.as_member`), `cog.py` (`/slot take | leave`), `offers.py` (`Offers`: the `offer`
  notice by DM or thread, `delivered`, the message closed when the offer ends; table `offer_message`).
- `app/approval/`: `ui.py` (the request embed, `ApprovalAction`, `RejectModal`), `moderation.py` (`Moderation`: the
  moderation thread, the request / decision / stale-draft notices; table `moderation_message`).
- `app/attendance/`: `ui.py` (the report's `ReportAction` *Rate* / *Edit attendance*, `RateModal`, `Editor`), `cog.py`
  (`/attendance`, `/profile`, `/mission-maker`, `/group`, `/attendance-report`, from the supervisor's reports). The
  report message itself is `render.report`.
- `app/links/` (the links and squad.xml are the supervisor's): `service.py` (`LinkService`: the member's own link
  through act-as, `LinkError` for what the member can fix, `person()` / `unlinked()` read from `GET /api/members`,
  and the reminder bookkeeping in `link_reminder` with the rollout flag), `ui.py` (*Link now* `link:now`, the
  modal, `save()` shared by `/link` and the modal), `cog.py` (`/link`, `/unlink`, *bot admin*'s `/links remind |
  reminders`), `watch.py` (`LinkWatch`: a member who gains a role and is then unlinked is asked once at once, the
  hourly `link_reminders` action with DMs or a reminder-channel ping, skipping `muted` members, the first-run
  announcement, retried hourly while the supervisor is away).
- `app/archive/`: `service.py` (`ArchiveService`: quarters as numbers, which archived operations leave
  (`keep_quarters`), `save_transcript`, who may read: organisers and *bot admin*), `export.py` (`Exporter`: the
  daily `archive_export` action copies members' messages and downloads attachments, checks the copy, deletes the
  channel, then empty archive categories), `html.py` (the file from the operation as last drawn: `inline`, `zip` or
  `listed` attachments), `cog.py` (`/archive event`, picking the form that fits the upload limit).
- The rest of the front end: `app/roster/service.py` (`Roster`:
  the guild's members and roles pushed to the supervisor, `roster_push` daily, `member_push:<id>` / `roles_push`
  retries, `as_member()` pushing an unknown member and repeating an act-as call once), `app/notices/` (`service.py`:
  `Notices`, the supervisor's notices delivered at most once through `notice_delivery`; `deliver.py`: the Discord
  handlers per kind), `app/live.py` (`Live`: the supervisor's live events wake the workers),
  `app/supervisor_check.py` (`api_level`, `integration.act_as`, the zone; `gate()` holds that work back until it
  passes). Act-as calls pass `act_as=<discord id>` to the client; a `SupervisorError` reaching the command, button
  or modal error handler is answered in words (`discord_util.supervisor_words`).
- `tests/contract/openapi-bot.json` is the supervisor's contract (`python scripts/sync_contract.py`);
  `tests/contract_check.py` holds the fake's requests to it.
- `tests/fake_supervisor.py` mimics the real one's contract routes (act-as, the roster guard, notices, operations
  with both counters and `If-Match`, the feed with tombstones and `reset`); tests change operations as the app would
  (`State.operation` / `change` / `set_file` / `delete_operation`). The `fake` fixture runs it and ties its clock to
  the tests'; `tests/test_channel.py`'s `stack` adds the channels and the feed.

## Cross-cutting rules

These rules span many features; read them before writing any handler. On `feature/front-end` the rules about data
and decisions (atomic slot claims, frozen reservations, guest events, permissions, attendance) are the supervisor's:
the bot passes the member's action on and words the refusal. The rules about Discord (nothing timed in memory,
buttons that survive a restart, the channel as bot-owned messages, pings in the thread, times) stay the bot's.

- **Nothing timed lives in memory.** Sign-up opening, reminders, reserve offers, stale drafts, reservation
  retries, attendance and archiving are rows in a `scheduled_action` table, run by one loop. After downtime,
  actions that change state run late, and pings run only while still useful (*Scheduled actions and downtime* in
  the plan). The loop starts in `on_ready`, once the guild and the ids are there; a handler that can't do its
  work (no guild) raises, so it is retried, never returns as if done.
- **Nothing typed goes into a supervisor URL unchecked.** Autocomplete only suggests: a slug is checked against
  the supervisor's list, and the client quotes every outside path segment (`_segment`).
- **Every button survives a restart:** the header's *Create event*, the event messages' buttons, offers and
  reminders use persistent views / `DynamicItem` with ids in `custom_id`.
- **The event channel is a set of bot-owned messages** (info embed, briefing and ORBAT as plain text with
  continuation messages, preset, report, the thread link last), kept in order and edited in place by stored message ids. Edits are batched to stay within rate
  limits. Members talk in the thread, and **every ping goes to the thread** too, so nothing lands between the
  bot's messages; the channel is read-only for members.
- **Reservations under way are frozen:** from the quiet period on, the supervisor ignores new times and refuses
  `DELETE` (409). Lock the times in the panel then, and end by id before deleting.
- **Slot claims are atomic** ("take it if still free" in one update), because sign-up opening causes a rush.
- **Events store config items by value** (the climate), so `/reload` can remove an item without
  breaking existing events. Other groups are database rows that are renamed, never deleted.
- **Guest events never touch the supervisor:** no reservation, no profile, attendance by hand.
- **Permissions:** the config's role mapping, plus each event's creator and co-organisers. Every Discord user acts
  on the supervisor through the bot's one supervisor account, so the bot's checks are the only per-member limit.
- **Times** are typed in the config time zone, stored as UTC, shown as Discord timestamps, and sent to the
  supervisor zone-aware (it reads naive times as its own local time).
- **Naming:** in this bot an *event* is a community mission. The supervisor calls its host bookings
  *reservations* and uses "event" for log and notifier events. Keep the words apart.

## Integrating with arma-server-supervisor

On `feature/front-end` the routes are the supervisor's `docs/api/openapi-bot.json` (copied to `tests/contract/`
by `python scripts/sync_contract.py`) and the rules are in `docs/PLAN-front-end.md`: the bot's account needs only
the `integration` role (`integration.act_as`); reservations, event profiles and server control are gone from the
bot. What follows describes `master`.

The routes, payloads and permissions the bot relies on are listed in the plan, under *What the supervisor offers*
(checked 2026-10-14 and again 2026-10-31, after the reservation work was committed). In short:

- **Auth:** `Authorization: Bearer <personal API token>` for the bot's own supervisor user. That user needs
  `server.control`, `server.protected`, `server.message`, `players.view`, `reservations.edit`,
  `reservations.override`, and for event profiles also `profiles.edit`, `mods.manage`, `steam.manage`,
  `missions.manage` and `jobs.view`.
- **Event profiles:** the bot clones a base profile, imports the launcher preset (`download=missing`, a
  supervisor job it follows), sets the mission cycle, then assigns the profile to the event server just before
  the reservation and restores the previous one afterwards. Only change a server's assignment back if it still
  holds the event profile.
- **Errors:** services return JSON errors with HTTP status codes (409 for conflicts, including a refused start
  during a reservation). Show the supervisor's message to the Discord user rather than a generic failure.
- **Reservations:** `/api/reservations`, one per event, from assembly − lead time to start + duration + buffer.
  `PUT` replaces the whole reservation, so send every field. End it early with `POST /api/reservations/{id}/end`; only without that capability, `POST /current/end`
  after checking that `GET /current` is this event's `reservation_id`.
- **Attendance:** read from mission plays (`GET /api/stats/missions?server=<numeric id>&all=true`, then
  `GET /api/stats/missions/{id}` for players with seconds and Steam IDs). The stats routes don't take a slug,
  and the plays list has no time filter, so the bot resolves the id via `GET /api/servers` and filters the
  window itself. Only a `finished` play, with no new play
  during the settle delay, ends an event; crashes and restarts don't.
- The supervisor already posts server status and alerts to Discord through a **webhook**. Don't duplicate them.
- **Supervisor changes planned for the bot** are in the supervisor's `docs/PLAN-integration.md`: reservation
  `profile_id`, `managed_by` / `external_ref` / `link`, end by id, `/api/events/stream`,
  `/api/stats/attendance`, `?uid=` lookup, the `integration` role, and `GET /api/system/info`. As of
  2026-11-01 its phases 1–7 are committed (attendance window, `?uid=`, notifier, heartbeat, docs); its *As
  built* notes give the exact payloads. Build against the new routes, with the fallbacks behind the capability checks. Gate every use on
  the capabilities from `/api/system/info`, and keep the fallbacks listed in the plan, because the two projects
  are deployed separately.
