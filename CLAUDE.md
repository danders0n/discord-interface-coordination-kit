# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Discord bot for one Arma 3 group's Discord server: the **Discord front end of arma-server-supervisor**
([docs/PLAN-front-end.md](docs/PLAN-front-end.md)). The supervisor owns the operations (the group's missions, `OP-n`;
kinds internal, guest, hosted, Zeus, training), sign-ups, offers, approval, attendance, ratings, persons, Steam links,
squad.xml and who may do what. The bot keeps no operation data of its own:
- it pushes the guild's members and roles to the supervisor (the roster);
- it draws the operation channels, the header calendar and the announcements from the supervisor's change feed;
- it delivers the supervisor's notices and the pings it schedules itself;
- it passes every member's press, modal and command on in their name (`X-Act-As`);
- it keeps the Discord plumbing and the archive of members' messages.

The supervisor is a separate project at `D:\Workspace\python\arma-server-supervisior`; read its `CLAUDE.md`, `README.md`
and `docs/PLAN-operations.md` (*As built*) before touching the integration.

Status: the old stand-alone bot of [docs/PLAN.md](docs/PLAN.md) is on `master`. On `feature/front-end` the front end
is built: steps 1–7, the plan's phases 1–5, tested against the fake supervisor. Next is phase 6, the release: a test
instance against a scratch supervisor, then the merge (*As built*, *Next* in the front-end plan). `docs/PLAN.md` stays
the reference for how things look and behave on Discord; its rules about data and decisions are the supervisor's. Read
the relevant section before implementing a feature, and keep the plans and this file up to date as the code lands.
[README.md](README.md) covers installing and running it.

## Stack

| Topic | Decision |
|---|---|
| Discord | discord.py: `app_commands` slash commands, buttons / selects / modals, persistent views and `DynamicItem` |
| Storage | SQLite through SQLAlchemy 2.x async + aiosqlite (never block the event loop); Alembic migrations applied at startup; a daily online backup |
| Supervisor client | HTTP over `aiohttp` (already a discord.py dependency), `app/supervisor_client/`. Never import the supervisor's code or open its database. The routes are its `docs/api/openapi-bot.json` (the contract) |
| Config | Only the bot's own settings in `.env` (secrets, channels and roles by id or name, the *bot admin* roles, a few times, the archive), read with pydantic-settings like the supervisor: one prefix, `__` for nesting, lists as JSON or `;`-separated. `/reload` re-reads it |
| Install | `pip` + pinned `requirements.txt` (the supervisor's convention), `tzdata` included; Python 3.11+ |
| Layout | `app/` by domain (operations, channel, signup, approval, attendance, links, roster, notices, archive, privacy), logic without discord.py where there is logic of its own, plus `ui.py` / `cog.py`; run with `python -m app` |
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

These rules span many features; read them before writing any handler.

- **The supervisor decides.** Every member action is an act-as call (`act_as=<discord id>`, through
  `Roster.as_member`, which pushes an unknown member and repeats once). The bot never checks who may do what, except
  for its own commands (*bot admin*). A `SupervisorError` reaching a command, button or modal error handler is
  answered in words (`discord_util.supervisor_words`, codes in `app/operations/words.py`), so handlers rarely catch
  one. Buttons are shown for what `my.actions` allows, and pressing one reads the operation again.
- **Organiser writes send `If-Match`** with the revision read (`app/operations/ui.write`); a 409 `stale` answers with
  *Reopen*. Nothing is merged. Sign-up presses send none.
- **Drawing is idempotent and converges** on the operation as the feed last gave it (`operation_channel.operation`):
  a redraw after a crash, a restart or the bot's own echo does no harm. Never skip the echo.
- **Nothing timed lives in memory.** Sign-up opening and closing, reminders, the rating window's close, retries and
  archiving are rows in a `scheduled_action` table, run by one loop. After downtime, actions that change state run
  late, and pings run only while still useful. The loop starts in `on_ready`, once the guild and the ids are there; a
  handler that can't do its work (no guild) raises, so it is retried, never returns as if done. Anything that changes
  with the clock but not in the feed (sign-up state, the rating window) is computed from the times and the clock, and
  a scheduled redraw follows it.
- **Notices are delivered at most once** (`notice_delivery` written before acting); a handler that can't start raises
  `Later`.
- **Nothing typed goes into a supervisor URL unchecked.** Autocomplete only suggests, and the client quotes every
  outside path segment (`_segment`).
- **Every button survives a restart:** persistent `DynamicItem`s with ids in `custom_id` (`operation:create`,
  `operation:panel:<op>`, `operation:act:<op>:<section>`, `signup:`, `squad:`, `offer:<op>:<offer>:`, `approval:`,
  `attendance:`, `link:now`). Private pickers and the wizard live in memory for 15 minutes.
- **The operation channel is a set of bot-owned messages** (info, the OPORD paragraphs, preset, ORBAT, report, the
  thread link last), kept in order and edited in place by stored message ids; redraws are batched. Members talk in the
  thread, and **every ping goes to the thread** too (a draft has none: DMs); the channel is read-only for members.
- **Times** are typed in the config time zone, sent to the supervisor zone-aware (it reads naive times as its own
  local time), and shown as Discord timestamps.
- **Naming:** *operation* in code, commands and texts (Polish *operacja*, feminine), as in the supervisor. The
  supervisor uses "event" for log and notifier events.

## Integrating with arma-server-supervisor

- **The contract:** the routes, bodies and which allow act-as (`x-act-as`) are the supervisor's
  `docs/api/openapi-bot.json`, copied to `tests/contract/` by `python scripts/sync_contract.py` whenever it changes.
  The fake supervisor serves exactly the contract's routes and holds every request to it. Response shapes aren't in
  the contract: take them from the supervisor's code (`app/operations/*.py`, `app/api/operations.py`) and its *As
  built* notes, and mirror them in the fake.
- **Auth:** `Authorization: Bearer <personal API token>` for the bot's account, which needs only the `integration`
  role (`integration.act_as`). Acting for a member adds `X-Act-As: discord:<id>`; the supervisor cuts the member's
  permissions down to the act-as routes.
- **Errors:** JSON `{detail, code, ...}` with HTTP status codes (409 for conflicts, `stale` with the operation,
  `bad_orbat` with `problems`, `overlap` with `other`). Word known codes; show the supervisor's message otherwise.
- **The feed** (`GET /api/operations/changes?after=`) gives whole operations oldest first, tombstones, and `reset`;
  the cursor moves only after a change is drawn or queued. The live stream (`/api/events/stream`) only wakes workers.
- **The supervisor check** (`app/supervisor_check.py`) gates all supervisor work on `api_level` and the capabilities;
  raise `API_LEVEL` and the list when the bot starts to need newer routes, because the two projects are deployed
  separately.
- The supervisor posts server status and alerts to Discord through its own **webhook**. Don't duplicate them.
- `master` still has the stand-alone bot's direct integration (reservations, event profiles, stats); none of it is on
  this branch.
