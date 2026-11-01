# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Discord bot for one Arma 3 group's Discord server. Other groups have their own infrastructure and appear in the
bot only by name. Events are *internal* (ours, on our server), *guest* (another group hosts; we get some of its
slots), *hosted* (ours, on our server, with invited groups) or *Zeus* (ours, on our server, no ORBAT, just
*Coming* / *Not coming*). Members' events, except Zeus events, need a moderator's approval before they are
published. It runs events (missions) with ORBAT slot sign-ups, reserves the game host
for them on **arma-server-supervisor** and builds each event's server profile there (mods from the preset, the
mission), reports attendance from the supervisor's player stats, and generates the
group's squad.xml. The supervisor is a separate project at `D:\Workspace\python\arma-server-supervisior`;
read its `CLAUDE.md` and `README.md` before touching the integration.

Status: **phases 1–4 are built**: settings, database and migrations, backups, i18n, permissions, the
scheduled-action loop, logging and the staff threads, start-up checks, the heartbeat, `/reload`, `/privacy`, the
supervisor client, host reservations as a service (`app/reservations/`), `/server`, and the ORBAT format,
templates and sign-up logic as services (`app/orbat/`, `app/signup/`), and the event service
(`app/events/`, phase 4a), the event channel (`app/eventchannel/`, phase 4b), and the header, wizard and
organiser panel (4c-1), `/event image` and `/event preset` (4c-2), and the sign-up buttons, offers, approval
messages, `/slot` and `/events` (4d), the Steam link and squad.xml (phase 5), and the attendance trigger,
report and corrections (6a); `/attendance`, `/profile` and the ratings (6b) and `/privacy erase` (6c) come next. The
full design is in [docs/PLAN.md](docs/PLAN.md), with every decision, the assumptions made while planning, a draft
data model, and the build phases. Read the relevant section before implementing a feature, and keep the plan and
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
- `app/services.py`: what cogs share. `app/discord_util.py`: `require(Perm)`, `reply()`, the command error handler.
- `app/settings.py`: `.env` model and `SettingsStore` (`/reload`; restart-only values keep their old value).
  Channels and roles are `Ref`s: an id or a name.
- `app/resolve.py`: turns the `Ref`s into `Ids` on the server (start-up, `/reload`); code reads `services.ids`,
  never the raw settings, for channels and roles.
- `app/permissions.py`: `Access` from role ids against `Ids` (no discord.py); per-event rights come later.
- `app/staff_threads.py`: the staff log and moderation threads in the staff channel (made, remembered, reopened).
- `app/scheduler.py`: `scheduled_action` rows; handlers return the next due time or None.
- `app/db/`: engine (WAL pragmas), models (`UtcDateTime`: aware UTC only), migrations, backup.
- `app/logs.py`, `app/staff_log.py`: rotated file log, ERROR ids, the staff log queue.
- `app/supervisor_client/`, `app/heartbeat.py`: HTTP client (servers, reservations, `ReservationRefused` for a
  409 with `reservation`), capabilities, heartbeat loop.
- `app/reservations/service.py`: an event's host reservation: `book` (create or move; `Frozen` from the quiet
  period), `retry`, `end`, `cancel` (ends one under way first); unreachable → scheduled retries, refused →
  `failed` + staff note. State in `host_reservation`. Its `listener` (set by `EventService` to the notifier's
  `reservation_changed`) hears changes made in the background, so the info embed and approval requests redraw.
- Domains so far: `app/admin/` (`/reload`), `app/privacy/` (`/privacy` and its texts), `app/servers/` (`/server`
  with confirmation and override buttons).
- `app/orbat/parser.py`: the ORBAT text format (`parse` names every problem's line; `render` writes it back with
  `xN`; `!` only for slots locked closed, never for assignments). `app/orbat/templates.py`: shared templates.
- `app/signup/service.py`: `SignupService`, the sign-up rules (see its docstring): atomic claims, the partial
  unique index, offers with expiry actions, Zeus's limit in one statement, `set_orbat` matching after sign-ups,
  role changes. It talks to Discord only through the `Notifier` protocol and `roles_of`; refusals are `Refusal`
  codes for the Discord layer to word. Only one instance per scheduler (it registers the offer-expiry handler).
- `app/events/`: `times.py` (what organisers type: `dd.mm` = next such date, a start before the assembly = next
  day, aware UTC out), `groups.py` (other groups), `service.py` (`EventService`: create, update with proposals
  for members' new times, publish or send for approval, approve / reject / withdraw, cancel, end, and the timed
  actions; refusals are `Problem` codes; the Discord side listens through `EventNotifier`).
- `app/eventchannel/`: `render.py` (pure: event + board -> `Message`s keyed `info`, `briefing`, `preset`,
  `orbat:N`), `sync.py` (keeps them in order: edit in place, append / delete continuations at the end, re-post
  after a late preset or a message deleted by hand; edits always pass `view`, as leaving it out keeps old buttons),
  `service.py` (`EventChannels`: the draft's hidden channel and thread, overwrites that follow the co-organisers,
  batched redraws via `Redrawer`, background renames, publish + announcement, a redraw at the start (sign-up
  closes) and the archive, both moved with the event's times, cancel + thread ping, archive by quarter with
  overflow categories, the 45 / 50 channel room of the categories; it is the notifier of `EventService` and
  `SignupService`). `EventBot.
  build_events` wires the three together; `refresh_all` redraws after a restart.
- `app/events/ui.py`: the wizard (`Choices` + `DetailsModal`, in memory; `expired()` answers its buttons after a
  restart), the persistent `CreateEvent`, `PanelButton` and `PanelAction` (`event:act:<id>:<action>`, one
  `_<action>` method each), and the panel's modals and tools (templates renamed / deleted by their author, other groups corrected by
  *manage any event*, which redraws their events through `EventChannels.group_changed`). `guarded()` turns service refusals into words
  (`app/events/words.py`). `app/events/header.py`: the header message and its calendar. `app/events/cog.py`:
  `/event preset | image` (in the event's channel or thread, found by `EventService.by_channel`), `/slot take |
  leave`, `/events`.
- `app/presets.py`: the Arma 3 Launcher preset parser (ported from the supervisor) with the Creator DLC table;
  presets and images are files under `data/presets/` and `data/images/`, sent with the event's messages.
- `app/signup/ui.py`: the sign-up buttons under `orbat:0` (`signup:<id>:<action>`, a squad-then-slot picker),
  the offer buttons (`offer:<id>:take|decline`, by DM or in the thread), and `answer()` (refusal or done, plus
  the DLC warning); the buttons are off before sign-up opens, after the start, and (*Coming*) at a Zeus limit.
  `app/approval/ui.py`: the request message in the moderation thread with *Approve* / *Reject*, and *Retry
  reservation* while Approve is off (`approval:<id>:<action>`).
- `app/links/`: `service.py` (`LinkService`: Steam ID parsing, one Steam ID per member, `rank_lost_at` and the
  deletion after N days, reminder bookkeeping, the rollout flag; `import_old` for `python -m app import-old-links`),
  `ui.py` (*Link now* `link:now`, the modal, `save()` shared by `/link`, the modal and `/links set`), `cog.py`
  (`/link`, `/unlink`, staff `/links set | remove`), `watch.py` (`LinkWatch`: role changes, departures, renames,
  the hourly `link_reminders` action with DMs or a reminder-channel ping, the first-run announcement).
  `app/squadxml/service.py`: `render` (the old bot's layout) and `SquadXml` (debounced atomic writes, only when
  changed, never while `roster()` is None). Both get the server's members through `EventBot.roster`.
- `app/attendance/`: `stats.py` (`StatsReader`: plays and mission time from `/api/stats/attendance`, or the plays
  fallback), `service.py` (`AttendanceService`: the `attendance_watch` action every minute from the assembly,
  `finished_at` and `compute` as pure rules, `attendance_read` with retries until the deadline, guest events'
  *to confirm* with the 24 h ping and the N-day auto-confirm, `correct` and `confirm_probable`; `EventService.
  attendance` calls it on publish, move, cancel, *End event* and a guest event's end), `ui.py` (*Edit attendance*
  `attendance:<id>:edit` and its editor). The report is the `report` message, last in the channel.
- `tests/fake_supervisor.py` mimics the real one's reservation rules (frozen under way, 409 on delete, managed
  creates return the existing one); the `fake` fixture runs it and ties its clock to the tests'.

## Cross-cutting rules

These rules span many features; read them before writing any handler.

- **Nothing timed lives in memory.** Sign-up opening, reminders, reserve offers, stale drafts, reservation
  retries, attendance and archiving are rows in a `scheduled_action` table, run by one loop. After downtime,
  actions that change state run late, and pings run only while still useful (*Scheduled actions and downtime* in
  the plan).
- **Every button survives a restart:** the header's *Create event*, the event messages' buttons, offers and
  reminders use persistent views / `DynamicItem` with ids in `custom_id`.
- **The event channel is a set of bot-owned messages** (info embed, briefing, ORBAT plus continuation messages,
  preset), kept in order and edited in place by stored message ids. Edits are batched to stay within rate
  limits. Members talk in the thread, and **every ping goes to the thread** too, so nothing lands between the
  bot's messages; the channel is read-only for members.
- **Reservations under way are frozen:** from the quiet period on, the supervisor ignores new times and refuses
  `DELETE` (409). Lock the times in the panel then, and end by id before deleting.
- **Slot claims are atomic** ("take it if still free" in one update), because sign-up opening causes a rush.
- **Events store config items by value** (mission type, climate), so `/reload` can remove an item without
  breaking existing events. Other groups are database rows that are renamed, never deleted.
- **Guest events never touch the supervisor:** no reservation, no profile, attendance by hand.
- **Permissions:** the config's role mapping, plus each event's creator and co-organisers. Every Discord user acts
  on the supervisor through the bot's one supervisor account, so the bot's checks are the only per-member limit.
- **Times** are typed in the config time zone, stored as UTC, shown as Discord timestamps, and sent to the
  supervisor zone-aware (it reads naive times as its own local time).
- **Naming:** in this bot an *event* is a community mission. The supervisor calls its host bookings
  *reservations* and uses "event" for log and notifier events. Keep the words apart.

## Integrating with arma-server-supervisor

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
