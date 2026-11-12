# Plan: the bot as the supervisor's Discord front end

Status: building on `feature/front-end`; the additive first step is done (*As built*). Written 2026-11-12 from the supervisor's `docs/PLAN-operations.md`
(`D:\Workspace\python\arma-server-supervisior`, interviews 2026-11-11 and 2026-11-12), which is the source of
every rule about operations, people and permissions. This plan says what changes **in this repo**.

`docs/PLAN.md` stays the reference for how things **look and behave on Discord** (the channel's messages and
their order, the thread, the wizard and panel layout, pings and their late rules, the archive). Its rules about
**data and decisions** (sign-up logic, approval, attendance, Steam links, squad.xml, reservations, event
profiles, who may do what) move to the supervisor and are no longer the bot's.

## Goal

The bot keeps no event data of its own. The supervisor owns operations (the bot's *events*), sign-ups, offers,
approval, attendance, ratings, persons, Steam links and squad.xml. The bot:

- pushes the guild's members and roles to the supervisor (the **roster**);
- passes every member's button press, modal and command to the supervisor **in their name** (`X-Act-As`);
- **draws** the event channels, the header calendar and the announcements from what the supervisor returns;
- **delivers** what the supervisor asks to be said (notices) and the pings it schedules itself;
- keeps the Discord plumbing (channel, thread and message ids, categories and their order) and the **archive**
  of members' messages.

**Done when** (each an end-to-end test on the Discord fakes and the fake supervisor):

- A member gets *Klanowicz*. The bot pushes the change, and the supervisor makes their account. Staff press *Send
  login link* in the app; the bot DMs the link, or tells the app that DMs are closed.
- A mission maker creates an operation **in the app**. The bot draws the draft channel `18-12-ibc` without
  anyone touching Discord, and redraws it on every edit made in the app.
- A member presses *Sign up* on Discord while another signs up on Home. The channel shows both; a third press on
  a taken slot gets the supervisor's refusal in words.
- A member's operation is sent for approval in the app. The moderation thread gets the request; a moderator
  presses *Approve* on Discord; the channel turns public and the announcement goes out.
- A slot is freed while the bot is down. When the bot comes back, the reserve member gets the DM, and their 30
  minutes start then.
- The bot is stopped for a day. On start it reads the change feed and the notices, redraws what changed, drops
  the pings that are too late, and delivers the rest.

## Decisions

### What stays, what goes

| Area | Fate |
|---|---|
| `app/events/service.py`, `app/events/groups.py` | **Deleted.** Operations, kinds, approval, proposals, stale drafts and other groups live in the supervisor. |
| `app/events/times.py` | **Kept for input only:** it turns what organisers type (`dd.mm`, `HH:MM`, a start before the assembly = next day) into zone-aware times. The supervisor validates (max days ahead, start after assembly) and its refusal is shown. |
| `app/events/ui.py`, `header.py`, `cog.py`, `words.py` | **Kept, rewired:** the wizard, panel, header and commands call the supervisor. `words.py` shrinks to wording for refusals the supervisor returns as codes. |
| `app/eventchannel/` | **Kept.** `render.py` takes the supervisor's operation JSON instead of bot rows; `sync.py`, `order.py` and `service.py` (channels, thread, categories, redraws, renames) stay. |
| `app/signup/service.py`, `app/orbat/`, `app/approval/` (logic), `app/feedback/` | **Deleted** (ported to the supervisor with their tests). `app/signup/ui.py` and `app/approval/ui.py` stay as buttons that call the supervisor. |
| `app/attendance/service.py`, `stats.py`, `history.py` | **Deleted.** `ui.py` (report, *Edit attendance*, *Rate*) and `cog.py` (`/attendance`, `/profile`, `/mission-maker`, `/group`, `/attendance-report`) stay and read supervisor routes. |
| `app/links/service.py`, `app/squadxml/` | **Deleted.** `/link` and `/unlink` stay (act-as). `links/watch.py` becomes the roster push. Reminder bookkeeping stays (it is delivery). |
| `app/reservations/`, `app/profiles/`, `app/presets.py` | **Deleted.** The supervisor builds and holds the host itself; the bot uploads the preset and mission files and shows the supervisor's report. |
| `app/servers/` (`/server`) | **Deleted.** Servers are controlled in the app only (act-as refuses server control). |
| `app/activity.py` | **Deleted.** Activity lines come from the supervisor as `activity` notices, so changes made in the app are logged too. |
| `app/archive/`, `app/privacy/` (`/privacy` text), `scheduler.py`, `staff_threads.py`, `staff_log.py`, `logs.py`, `heartbeat.py`, `startup_checks.py`, `i18n/`, `discord_util.py`, `resolve.py`, `db/backup.py` | **Kept.** `/privacy-erase` calls the supervisor. |
| `app/permissions.py` | **Shrinks** to the bot's own commands (`/reload`, archive access for *bot admin*). Every other check is the supervisor's. |
| `app/supervisor_client/` | **Rewritten** for the operations API, `X-Act-As`, `If-Match`, the change feed and the notices. |

The bot never went live, so nothing is migrated: the old database is deleted and the Alembic history is replaced
by one new baseline migration.

### Naming

*Operation* in code, commands and texts (Polish *Operacja*), as in the supervisor; ids `OP-n`. `/event …`
becomes `/operation …` and `/events` becomes `/operations`. The supervisor's *reservations* are gone, so the old
warning about keeping "event" and "reservation" apart is dropped.

### Roster

| Topic | Decision |
|---|---|
| What | The whole guild: every member (id, username, display name, role ids), and the guild's roles (id, name, position, colour). |
| When | `PUT /api/members/roster` and `PUT /api/members/roles` at start-up (after the guild is ready) and daily (a scheduled action); `PUT /api/members/{discord_id}` on a join, leave, rename or role change; `PUT /api/members/roles` again when a role is created, renamed or deleted. |
| Guard | A full push that would disable too many accounts answers **409**: the supervisor keeps it waiting for staff. The bot writes one staff-log note and does not retry it until the next daily push. Single-member pushes always apply. |
| Supervisor down | Single-member pushes are queued in a scheduled action and retried; the daily full push repairs anything lost. |
| Intents | Server Members (already required). |

### Acting for members

| Topic | Decision |
|---|---|
| How | Every action a member starts on Discord calls the supervisor with the bot's token plus `X-Act-As: discord:<id>`. The bot never decides who may do what. |
| Refusals | The supervisor answers 403 / 409 / 422 with a message and a code; the bot shows the message privately (following the member's locale through `words.py` for known codes, the supervisor's text otherwise). A disabled account has its own wording ("your account is disabled"). An unknown person is pushed to the roster and the call repeated once; if it still fails: "you are not on the roster yet, try again in a minute". |
| Which buttons | An operation read through act-as carries `my` (the member's slot, list, offer, organiser role) and `actions` (what the supervisor would allow now). The panel and the sign-up picker show only those actions; the supervisor still checks every call. Public messages show buttons for everyone, and a press answers privately. |
| Edits | Panel writes send `If-Match: <revision>`. A 409 answers "the operation changed meanwhile" with a *Reopen* button that reloads the section; nothing is merged by the bot. Sign-up presses send no `If-Match`. |
| Scope | Act-as works only on the supervisor's allow-list (operations, sign-ups, offers, templates, other groups, attendance and reports, ratings, the member's own Steam link, erasure). Staff link management (`/links set`, `remove`, `remark`, `status`, `list`) therefore **moves to the app**, where the confirmation screen is. |

### Sync and drawing

| Topic | Decision |
|---|---|
| Wake-up | The existing `/api/events/stream` client: `operation`, `member` and `notice` events wake the worker; `hello` with `gap: true` and a reconnect do too. |
| Feed | `GET /api/operations/changes?after=<seq>` returns whole operations, oldest first. The bot stores the last `seq` in `bot_state` only after each change is drawn (or queued for a batched redraw that is itself durable). A tombstone deletes the draft channel. `reset: true` means: redraw everything listed and treat operations missing from it as gone. |
| Echo | No skipping: the bot draws its own changes again. Drawing is idempotent (`sync.py` edits a message only when its content differs), and the redraw is batched, so the cost is small and a crash between a call and its redraw loses nothing. |
| Cache | An in-memory map of the last operation read per id, filled at start-up from `GET /api/operations?upcoming=true` and kept current by the feed. It is never the truth: a lost cache is rebuilt the same way. |
| Render | `render.py` builds the same messages as today from the operation JSON: info embed, **OPORD** (five paragraphs, one message each, split at 2000 characters, the images attached), preset, ORBAT or sign-up list (from the supervisor's slots and lists), report, thread card / link last. Files (preset, images) come from `GET /api/operations/{id}/files/{name}` and are cached on disk by hash. |
| Channel lifecycle | Draft → channel in the events category marked DRAFT; pending → WAITING FOR APPROVAL; published → public, read-only, thread made, announcement posted; times or host group changed → renamed (queued, 2 per 10 min); cancelled → CANCELLED and archived at once; tombstone (a deleted draft) → channel deleted. Overwrites follow the organisers (persons → Discord ids) and the Discord roles whose app role holds `operations.manage` or `operations.approve` (from `meta`). |
| Plumbing back | After making the channel or thread, `PUT /api/operations/{id}/discord` (`channel_url`, `thread_url`). |
| Header | The calendar of the next `calendar_days` from the cache; edited when one of its operations changes. |
| Announcement | On publish, in the announcements channel, mentioning the announcement role and the climate's role (from `meta`); edited on change and cancel. |
| Order | `order.py` as today, by the operations' start. |

### Pings the bot schedules

The supervisor sends no time-based pings. From each operation's times the bot keeps `scheduled_action` rows,
recomputed on every change of that operation:

| Ping | Rule |
|---|---|
| Sign-up open | At `signup_opens_at`, the announcement and climate roles in the thread; skipped if more than 1 h late. Publishing without a later opening posts no ping (the announcement is it). |
| Reminders | At `times.reminder_minutes` before the assembly, every slot holder (or *Coming*) in the thread, split past 2000 characters; skipped once a later reminder or the assembly has passed. |

### Notices

`GET /api/notices?after=<id>` (oldest first), then `POST /api/notices/{id}/done` with `delivered` or `failed`
and a reason. A notice past its `expires_at` is marked `expired` without being delivered. The bot records each
notice it starts in `notice_delivery` before acting, so a crash never sends a DM twice.

| Notice | On Discord |
|---|---|
| `approval_requested`, `proposal_requested` | The request embed in the moderation thread with *Approve* / *Reject* (act-as); its message id is kept in `moderation_message`. |
| decision of either | The request embed edited; the creator pinged in the thread (a draft: by DM) with the reason. |
| `stale_draft` | The creator pinged (DM for a draft), a staff-log note, and a pending one re-posted to the moderation thread. |
| `cancelled` | The listed persons pinged in the thread with the reason. |
| `slot_lost` | The listed persons pinged in the thread ("your slot was removed, you are first on the reserve list"). |
| `offer` | One person: a DM with *Take it* / *Decline* (`offer:<id>:take`), or a thread ping if DMs are closed; then `POST /api/operations/{id}/offers/{offer}/delivered` with `dm` or `thread`. The whole reserve list: one thread ping. The DM is edited when the offer is taken, declined or expires (the feed says so). |
| `attendance_reported` | The report message drawn or redrawn; a staff-log copy. |
| `attendance_unconfirmed` | The organisers pinged in the thread. |
| `profile_failed`, `link_duplicate`, `link_request`, `roster_guard` | A staff-log note (a link request links to the app's confirmation screen). |
| `login_link` | `POST /api/notices/{id}/claim` for the URL, then a DM; `failed` with `dm_closed` if Discord refuses, and the app shows staff the link. The URL is never logged. |
| `erased` | Archived messages of that Discord id: author "deleted", content emptied, attachments deleted from disk; ids replaced by `former:<n>`. |
| `activity` | One staff-log line, as `app/activity.py` wrote them (who, what, old → new), mentions silenced. |

### Commands and buttons

| Command / button | Change |
|---|---|
| *Create operation* (header), the wizard | Ends with `POST /api/operations` (act-as); the supervisor refuses a guest kind without `operations.publish`. |
| Organiser panel | Each section reads the operation through act-as and writes with `If-Match`: details, OPORD (one modal with five fields, one per paragraph; Discord allows five inputs of 4000 characters), ORBAT (text, templates), co-organisers, sign-up opening and limit, *How to join*, hold the host (or *Request the host*), server and base, the mission report and warnings, *Publish* / *Send for approval* / *Withdraw request*, *Copy operation*, *Cancel* / *End operation*, *Don't end automatically*, keep the profile. The freeze from the quiet period is the supervisor's: frozen fields come back in `actions` as unavailable, with the reason. |
| `/operation preset`, `image`, `mission` | Download the attachment and upload it (act-as) to the operation's file routes; show the supervisor's report (and, for a member's operation, that nothing is downloaded until approval). Missions over Discord's limit are picked from the server's list (autocomplete from the supervisor). |
| Sign-up buttons, the squad picker, `/slot take | leave` | Act-as calls; the reply adds the supervisor's warnings (DLC). |
| `/operations` | The member's sign-ups and the calendar, from act-as reads. |
| `/link`, `/unlink`, *Link now* | Act-as on the member's own Steam link; the reply says the link waits for staff confirmation and gives the squad.xml URL. |
| Link reminders, `/links remind`, `/links reminders` | Kept in the bot: members come from `GET /api/members?unlinked=true`, the bookkeeping (`link_reminder`) stays here. |
| `/links set | remove | remark | status | list` | **Removed** (the app's Steam links and squad.xml pages). |
| *Edit attendance*, *Confirm links*, *Rate* | Act-as calls. |
| `/attendance`, `/profile`, `/mission-maker`, `/group`, `/attendance-report` | Act-as reads; the supervisor applies *who sees what*. |
| `/privacy-erase` | Act-as; the transcripts follow through the `erased` notice. |
| `/archive event` | As today; the HTML takes the operation (info, OPORD, final ORBAT, report) from the supervisor, and who may read it comes from `my` (participant or organiser) plus the *bot admin* role. |
| `/reload` | Kept (*bot admin*). |
| `/server` | **Removed.** |

### Configuration

Removed from `.env`, because the supervisor owns them: `roles.member`, `roles.mission_maker`, `roles.signup`,
`roles.rank`, `roles.remark`, all of `permissions` except `bot_admin`, `dlc_roles`, `events.climate_roles`,
`events.kind_names`, `supervisor.event_server`, `supervisor.base_profile`, `supervisor.profile_swap_minutes`,
`supervisor.profile_keep_days`, from `times` everything except `reminder_minutes`, `archive_hours`,
`calendar_days`, `reply_seconds` and `default_duration_minutes` (the wizard's pre-fill), all of `attendance`,
all of `squad_xml` except `url` (shown at `/link`; or read from `meta`), and `links.delete_after_days`.

Kept: the Discord token, guild, language, time zone (for typed input; a start-up warning if it differs from the
supervisor's), data folder, `group`, `channels`, `roles.announcement`, `permissions.bot_admin`,
`events.discussion`, the kept `times`, `supervisor.url` and `supervisor.token`, `links.reminder_days` and
`links.dms_per_hour`, `archive`, `backup`, `logs`. `.env.example` and `tests/test_settings.py` follow.

### Database

One new baseline migration. Tables:

| Table | Holds |
|---|---|
| `operation_channel` | operation id, channel id, thread id, card message id, message ids (JSON, keyed as `render.py` keys them), announcement message id, category id, the last drawn `change_seq`, `archived_at`, `exported_at` |
| `moderation_message` | approval request id, operation id, message id |
| `offer_message` | offer id, operation id, where (DM / thread), message id |
| `notice_delivery` | notice id, state, started, finished |
| `archived_message` | as today, keyed by operation id |
| `link_reminder` | as today, by Discord id |
| `scheduled_action` | as today: pings, archive moves, the quarter export, link reminders, the daily roster push, queued member pushes |
| `bot_state` | the last feed `seq`, the last notice id, header message id, staff thread ids |

### Start-up checks

Added: `GET /api/system/info` must report `api_level` ≥ the one this version needs and the operations
capabilities (else the bot refuses to start, with the reason in the log and the staff channel); the bot's account
must hold `integration.act_as`; the supervisor's time zone matches `BOT_TIMEZONE` (warning only). Removed: the
checks for configured member, rank, remark, climate and DLC roles.

### Tests and the fake supervisor

`tests/fake_supervisor.py` is rewritten for the new routes: roster with the guard, act-as (refusing an unknown
or disabled person, and routes off the allow-list), operations with both counters and `If-Match`, the change
feed with tombstones and `reset`, notices with `claim` and `done`, offers with `delivered`, files, `meta`. A test
checks it against the supervisor's `docs/api/openapi-bot.json` (paths, fields, status codes), copied into
`tests/contract/` by a script and refreshed whenever the supervisor's file changes. The deleted services take
their tests with them (they live on, ported, in the supervisor).

The test Discord instance points at a **scratch supervisor** (its own data folder, the supervisor's
`tests/fake_server.py` as the Arma executable) instead of the fake, since holding the host there harms nothing
(*assumed*).

## Phases

Built on a branch, released together with the supervisor's `feature/operations` (its plan, *Delivery*). Each
phase leaves the tests green.

1. **Clean-up and contract:** delete the services, tables and commands listed above; the new baseline
   migration; the trimmed config; the rewritten client (`X-Act-As`, `If-Match`, feed, notices, files, meta); the
   rewritten fake supervisor and its contract test; start-up checks.
2. **Roster:** member and role pushes, the daily full push, the guard's 409, queued retries.
3. **Sync and drawing:** the feed worker, `operation_channel`, rendering from operation JSON (OPORD in five
   messages), the channel lifecycle, plumbing back, header, announcement, order, the scheduled pings.
4. **Actions:** the wizard, the panel with `actions` and `If-Match`, `/operation preset | image | mission`, the
   sign-up buttons and picker, `/slot`, `/operations`, offers (DM, delivered, take), the moderation thread, the
   notices worker with `notice_delivery`, the staff log from `activity` notices.
5. **Members and history:** `/link`, `/unlink`, link reminders from the supervisor's list, login-link DMs, the
   attendance report and editor, *Confirm links*, ratings, the report commands, `/privacy-erase` and the
   `erased` notice, `/archive event` with supervisor data.
6. **Release:** the scratch-supervisor test instance, the README (setup: the bot's supervisor account with the
   `integration` role, the removed settings), `CLAUDE.md`, and `docs/PLAN.md` marked as superseded for data and
   rules (a note at its top pointing here).

## Decisions from the grilling (2026-11-12)

- **Before the branch:** the uncommitted work on master (`activity.py`, the review fixes, `PLAN.md` edits) is
  committed first, so master keeps a working bot. The front-end work then goes on its own branch.
- **Build order:** phases 1–5 are built against the rewritten fake and the contract test
  (`openapi-bot.json`); only phase 6 needs the real supervisor. The bot may run ahead of the supervisor's phases.
- **Unknown person:** on that refusal the bot pushes the member (`PUT /api/members/{id}`) and repeats the call
  once; only if it still fails does the member get "try again in a minute".
- **Old database:** deleted without export (test data only); the new baseline migration starts clean.
- **Notice delivery:** at-most-once. A notice found in `notice_delivery` as started but not finished after a
  crash is marked `failed` (`bot_crashed`), except an `offer` or `login_link`, which are time-critical or
  one-shot: those are marked `failed` so the supervisor re-offers or staff share the link in the app.
- **Late pings:** only the rules in *Pings the bot schedules*; no "we were offline" post.
- **Reset load:** the batched `Redrawer`, soonest start first; edits only when the content differs.
- **Staff links on Discord:** none. All of `/links set | remove | remark | status | list` is app only.

## Assumptions to check

- The test Discord instance can run against a scratch supervisor.
- The squad.xml URL shown at `/link` comes from the supervisor's `meta`, so `.env` drops it.

## As built

### Order (2026-11-12)

The supervisor's contract only had its phases 1–2 (members, roster, notices, the member's own Steam link, system
info, the stream) when building began; the operations, feed, `meta` and file routes come with its phases 3–4. So
the work goes **additive first**: everything that needs only the existing routes is built next to the old bot,
which keeps working on the branch. The deletions and the rewiring of phase 1 (and phases 3–4) follow once the
supervisor publishes the operations routes.

### Step 1: client, contract, roster, notices, live events (2026-11-12)

- **Client** (`app/supervisor_client/`): `request(..., act_as=<discord id>)` sends `X-Act-As: discord:<id>`;
  `SupervisorError.code` is the refusal's `code`. New calls: `me`, `push_roster`, `push_roles`, `push_member`,
  `members`, `my_member`, `link_steam`, `unlink_steam`, `notices`, `notice_done`, `notice_claim`, `stream` (SSE,
  parsed by `sse.py`; a minute of silence ends it, as the supervisor sends a keep-alive every 15 s).
- **Contract:** `tests/contract/openapi-bot.json`, copied by `python scripts/sync_contract.py [supervisor
  checkout]`. `tests/contract_check.py` checks a request (query names, JSON body) against it; the fake supervisor
  runs that check on every contract route and answers 422 `contract: ...`, so a passing test sends only what the
  real supervisor accepts. `tests/test_contract.py`: the fake serves every contract route and nothing else but the
  old routes (`LEGACY`, deleted in the clean-up); the copy equals the supervisor's file when its checkout is next to
  this one (skipped otherwise). The contract's responses are untyped, so their shapes are taken from the
  supervisor's code; status codes are not checked (FastAPI lists only 200 and 422).
- **Roster** (`app/roster/service.py`, no discord.py): actions `roster_push` (start-up = scheduled for now, then
  daily; 15 min retry when down or the member list isn't loaded), `member_push:<id>` (the latest state of one
  member while the supervisor is down; a later push that gets through drops it), `roles_push`. `as_member()` is
  the *unknown person* rule. The bot (`app/bot.py`) pushes on join, leave, nickname or role change, a username or
  global name change, and on role create / rename / delete; bots and `@everyone` are left out.
- **Notices** (`app/notices/service.py`, `deliver.py`; table `notice_delivery`, migration 0014; cursor in
  `bot_state` `notices`): as *Notices* and the grilling's *Notice delivery*, plus: a handler that can't start
  (no guild, supervisor down) raises `Later` and the notice waits, nothing said; another exception is `failed`
  with `error: ...`; a kind without a handler is `failed` / `unsupported`; the bot's clock past `expires_at` is
  `failed` / `expired`. Handlers so far: `login_link` (claim, DM; `dm_closed`, `not_in_guild`, `claim: ...`),
  `roster_guard` (staff note).
- **Live events** (`app/live.py`): there was no stream client yet (the plan said "existing"); `Live` keeps
  `GET /api/events/stream` open, reconnects with the last id (1 s doubling to 60 s), and wakes listeners; a `gap`
  hello or a reconnect after a break wakes all. It listens for `notice` only: the supervisor answers 422 to kinds
  it doesn't know, so `operation` joins with the operations API. The notices worker also polls every minute.
- **Decisions taken while building:**
  - A held roster push (409 `roster_guard`) writes no staff note of the bot's own: the supervisor's `roster_guard`
    notice is the note (with the count and the accounts), so staff read it once.
  - A full push refused for another reason (401, 403, 422) raises, so the scheduler's ERROR reaches the staff log;
    a refused single-member or roles push is only a log warning (the daily push repairs it).
  - The login-link DM is in the config language (a DM outside an interaction has no locale).

### Step 2: Steam links through the supervisor (2026-11-12)

The supervisor's phase 2 (links on persons, squad.xml) is committed, so the bot's links move now rather than in
phase 5.

- **Gone:** `app/squadxml/`, the `steam_link` table (migration 0015), `/links set | remove | status | list |
  remark`, *Write now*, `python -m app import-old-links` (the supervisor has its own), `BOT_SQUAD_XML__*` and
  `BOT_LINKS__DELETE_AFTER_DAYS`, the squad.xml start-up checks, the link expiry after N days without a rank, the
  in-game name lookup at `/link`, and the attendance editor's *Confirm links*.
- **`/link steam`** (no nick: the supervisor's nick rule) and *Link now* call `PUT /api/members/me/steam` through
  act-as; the reply says the link waits for staff and gives `squadxml_url` from the answer. `/unlink` calls
  `DELETE`. Refusals: `link_taken`, `link_invalid` (worded as a bad ID or a custom address, by what was typed).
  The staff log hears of links through the supervisor's `link_request` / `link_duplicate` notices (staff-log notes,
  `app/notices/deliver.py`).
- **Reminders** use `GET /api/members?unlinked=true` instead of the bot's rank roles. A member who gains a role is
  pushed, then asked at once if the supervisor lists them as unlinked and they were never reminded.
- **Attendance** (until it moves to the supervisor): the report matches only **confirmed** links, read from
  `GET /api/members` (the supervisor's rule). A probable match is still shown; the member links with `/link` and
  staff confirm it in the app.
- **Words for refusals:** the command, button and modal error handlers answer any `SupervisorError` in words
  (`discord_util.supervisor_words`: `unknown_person`, `account_disabled`, `act_as_route`, the supervisor down or
  5xx; the supervisor's message otherwise), logged as INFO / WARNING instead of an ERROR. So an act-as call needs
  no handling of its own.
- **Interim gap:** `/privacy-erase` no longer deletes the member's Steam link (it is in the supervisor, whose
  erasure comes with its phase 5); the confirmation says to remove it in the app.
- `BOT_ROLES__RANK` and `BOT_ROLES__REMARK` stay for `/profile` until the configuration is trimmed in the clean-up.

### Step 3: the supervisor check (2026-11-12)

`app/supervisor_check.py`: at start-up (after `on_ready`) the bot checks `api_level` ≥ `API_LEVEL` (1 for now;
raised with the operations API) and that its own account holds `integration.act_as` (`GET /api/auth/me`), and
compares the supervisor's `utc_offset` with `BOT_TIMEZONE`'s now (a warning). Decisions taken while building:

- **Held back, not refused:** a failed check doesn't stop the bot (under NSSM a refusal would be a restart loop,
  each round posting to the staff channel). The bot stays on Discord and holds back its supervisor work (the
  roster push, the notices, the live events), checking every minute; the problem goes to the staff log once (again
  only if it changes), and a note says when it passes. The old event stack is not gated: it is deleted in the
  clean-up anyway.
- A supervisor that is only unreachable holds back the same way but writes no staff note (its own webhook
  reports it). A refused token (401 / 403) is a blocking problem with a hint at `BOT_SUPERVISOR__TOKEN`.
- No capability is required yet: the operations capabilities are added to the check with the operations API.
- The time zones are compared by today's UTC offset, as `system/info` gives an abbreviation (`CET`), not an IANA
  name.

### Waiting for the supervisor

Everything left (the clean-up of phase 1, then phases 3–6) needs the supervisor's operations, change-feed, `meta`
and file routes. As of 2026-11-12 they exist only as uncommitted work in the supervisor (`app/api/operations.py`,
`app/operations/`) and are not in `openapi-bot.json`. Resume when its phase 3 is committed: run
`python scripts/sync_contract.py`, then follow the contract.

## Not in this plan

- Server control from Discord.
- Staff management of Steam links and squad.xml on Discord (the app does it).
- Any data or rule the supervisor owns, and any fallback for a supervisor without the operations API.
