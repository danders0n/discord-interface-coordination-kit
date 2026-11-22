# Plan: updating the bot from Discord, and the two apps watching each other

Status: built, steps 1-7 (the boot step and the restart, `tests/test_boot.py`; the updater and `/update`,
`tests/test_update.py`; the heartbeat's state and health, `tests/test_supervisor_client.py`, against the supervisor's
part, built on its `feature/discord-roles` branch; the supervisor's health, `tests/test_health.py`; queued actions,
`tests/test_queue.py`; README; end to end, see *As built*). Not yet run under NSSM, nor with a real Discord login
or a real supervisor update: that is the release's test instance (docs/PLAN-front-end.md, phase 6). Written 2026-11-22 from a
short design interview with the owner. The supervisor's part is its `docs/PLAN-bot-health.md`; the two are built and
deployed separately (*Order of release*). See *As built* at the end for where the code departs from this plan.

## Goal

Updating the bot today means a session on the host: `update.ps1` (check `.env`, back up, `git pull`, `pip install`,
`nssm restart`). The supervisor can already update itself from its console with an automatic rollback
(its `docs/PLAN-app-update.md`). The bot should do the same, started **from Discord**.

And the two apps should know when the other one is **updating** rather than down:

- While the supervisor updates, members keep pressing buttons. Today they get *The app can't be reached right now*.
  Instead the bot **queues** what they asked for, and once the supervisor is back it **sends it in their name and tells
  them the answer**.
- While the bot updates, the supervisor alerts staff that the bot went silent (`integration_silent`). Instead the bot
  says beforehand that it is updating, and the supervisor holds the alert until the expected time has passed.

**Done when:**

- `/update check` lists the incoming commits, whether the dependencies or the migrations change, and which locally
  changed files would be overwritten;
- `/update now` waits for the bot's running work, backs up the database, restarts, and the bot comes back on the new
  commit; the staff log says *Bot updated from abc1234 to def5678* with the subjects;
- an update whose `requirements.txt` changed installs the new pins before the new code starts;
- an update to a commit that cannot start (a syntax error, an exception before `on_ready`, a hang) comes back on the
  old commit within a few minutes, and the staff log says *Bot update rolled back* and why;
- `/update restart` only restarts; `/update back` returns to the commit before the last update (with the database
  backup when the migrations differ);
- while the bot updates, the supervisor raises no `integration_silent` alert unless the bot overstays;
- while the supervisor updates, a member's sign-up, slot, offer, rating or organiser edit is answered *Queued: the app
  is updating* and is carried out when the supervisor is back, with the result in place of that reply (or by DM);
- staff see in the staff log when the supervisor goes into an update, comes back, or does not come back.

## Decisions

| Topic | Decision |
|---|---|
| Who starts the bot's update | **Discord only:** `/update` for *bot admin*. The supervisor's console does not drive the bot. |
| The supervisor's update | **Stays in its console.** The bot only hears about it and queues meanwhile. |
| How it updates | **As the supervisor does:** `git` on the install folder, one branch, a stdlib-only boot step before any app import, a marker file, an exit with a restart code, NSSM starting it again, **automatic rollback** after 2 failed starts or 3 minutes without becoming ready. |
| Member actions while the supervisor updates | **Queued, the result told afterwards.** Writes only; reads (opening a picker, a panel, a report) are answered *The app is updating, back in about N min*. |
| How each learns of the other's update | **Both announce and both watch.** The bot sends a heartbeat with `state: updating` before it exits. The supervisor sends a `system` live event before it exits. Each watches the other: the supervisor through the heartbeat's silence, the bot through the live stream, `GET /api/version` and failed calls. |

### Assumed while planning (not explicitly confirmed)

Change these here if they turn out wrong.

- **Branch:** `BOT_UPDATE__BRANCH`, default `feature/front-end` until the merge, then `master`. An update always goes to
  the fetched tip.
- **Access:** the bot's checkout uses `origin` as configured. With an HTTPS remote, a read-only GitHub token in `.env`
  (`BOT_UPDATE__GITHUB_TOKEN`) goes to git through `GIT_CONFIG_*`, never on the command line, as in the supervisor.
  With an SSH remote (the checkout today: `git@github.com:...`) git uses the service account's key, and the token is
  not used. A failed fetch says which of the two to fix.
- **Restarter:** NSSM (`BOT_RESTARTER=nssm`, set by `nssm set EventBot AppEnvironmentExtra`, because the bot has no
  psutil to look at its parent process), or the console loop when `python -m app` runs in a terminal. With neither,
  `/update now` and `/update restart` are refused with the reason, rather than exiting and staying down.
- **Restart code 75**, as in the supervisor (NSSM restarts on any exit by default).
- **Queue limits:** a queued action expires after **60 minutes** (`BOT_QUEUE__MAX_MINUTES`); after that the member is
  told it was not sent. At most **20** per member.
- **The bot's update does not wait for the supervisor** and does not care if it is updating too: the queue is in the
  database and survives the bot's restart.
- **The first version with this feature is installed by hand** (`update.ps1`), like the supervisor's.

## What the code says (facts the steps rely on)

- **Start:** `python -m app` → `app/__main__.py` imports `app.settings`, `app.db` and the rest at the top, then
  `asyncio.run(run(...))`, which migrates (backing up first when a migration is due) and runs `EventBot`. Nothing can
  end the process with a code today; `KeyboardInterrupt` is swallowed.
- **Ready:** `EventBot.on_ready` resolves the ids, makes the staff threads, posts *Bot started*, starts the scheduler
  and the supervisor gate. It does not wait for the supervisor (the gate holds that work back until it passes).
- **Running work:** the scheduler runs due actions one at a time (`run_due` → `_run_one`); the daily
  `archive_export` can run for minutes. `Redrawer` batches channel redraws; `StaffLog` queues notes and sends them
  joined. `close()` cancels every task.
- **The supervisor down:** `SupervisorUnreachable` is raised for both a refused connection and a timeout
  (`supervisor_client/__init__.py`), and `discord_util.supervisor_words` answers either one *can't be reached*. A
  timed-out write may have been applied, so only a refused connection may be queued.
- **Act-as:** every member action goes through `signup.ui.act()` → `Roster.as_member(discord_id, call)`. The callers
  pass lambdas, so nothing records which call was meant. About 45 call sites; the writes worth queuing are listed in
  *Queued actions*.
- **Heartbeat:** `app/heartbeat.py` posts `name` and `interval_s` every 60 s when the supervisor has the `heartbeat`
  capability. The supervisor alerts after 5 silent intervals (`app/integrations.py` there).
- **Live events:** `app/live.py` reads `/api/events/stream` (`notice`, `operation`). The `hello` event carries the
  supervisor's `boot` id, which changes with every start. The supervisor's update states (`app_update`) go only to
  its WebSocket hub today, not to this stream.
- **`GET /api/version`** is public and returns the build and `boot`. It is not in the bot's contract yet.
- **Migrations:** `app/db/migrations/versions/`. An older commit started on a database migrated past its head fails in
  `prepare_database` (Alembic does not know the revision).

## Design

### Files

- **`<install>/.app-update/`** (new, gitignored): `pending.json` (the marker) and `boot.log` (git and pip output,
  attempts, rollbacks, trimmed to ~1 MB).
- **`<data_dir>/updates/`**: `<stamp>-<from7>.db` (backup), `<stamp>-<from7>.diff` (local changes discarded),
  `history.jsonl` (one line per update / go back / restart / rollback, `reported: false` until the staff log had it).

### The boot step (`app/boot.py`)

The supervisor's design, written again for the bot (the bot never imports the supervisor's code). Standard library
only, imported first by `app/__main__.py` (which moves its other imports into the functions). It must stay importable
by every version it may roll back to and from.

| marker state | the boot step |
|---|---|
| none | go on |
| `pending` | `git reset --hard <to>`, pip when `requirements`, the database restore when `restore_db` → `applied`, exit 75 |
| `applied` | `attempts += 1`; over 2 → `rolling_back`; otherwise start with the watchdog |
| `rolling_back` | `reset --hard <from>`, pip, put the replaced database back when the update restored one → `rolled_back_applied`, exit 75 |
| `rolled_back_applied` | like `applied`, two failed starts → `gave_up` |
| `gave_up` | start normally, try nothing more (`boot.log` says why) |

- **Ready** (`boot.ready()`) is called at the end of the first `on_ready`, after the staff threads are made: Discord
  connected, the guild found, the database migrated. It does not wait for the supervisor. It stops the watchdog,
  writes the history line (`ok`, or `rolled_back` with the last error) and deletes the marker.
- **Watchdog:** a daemon thread; 3 minutes without `ready()` → log, `os._exit(1)`. pip time is not counted.
- **The last error** is kept from the root logger (and discord.py's), reduced to its last line, for the history.
- **The console loop** (no `BOT_RESTARTER`, stdin a terminal): runs `python -m app` as a child with
  `BOT_RESTARTER=console`, starts it again on 75, or on any non-zero exit while a marker exists.
- **Exiting:** `run()` gets a `request_exit(code)`. It closes the bot (`EventBot.close()`: tasks cancelled, the
  staff log flushed first, the client and database closed), and `main()` then calls `sys.exit(code)`.

### The update service (`app/update/service.py`, `Updater`)

No discord.py. Built in `run()` and kept on `services.updater`.

- `status()`: build (commit, date; from `.git`), branch, checkout and `git` available, restarter (or why not), a
  pending marker, what the bot is busy with, the history (newest first) with `can_go_back`.
- `check()` (in a thread): `git fetch origin <branch>` (token through `GIT_CONFIG_*`, `GIT_TERMINAL_PROMPT=0`), then
  on `HEAD..origin/<branch>`: commits (sha, subject, author, date), `requirements` changed, **`migrations` changed**
  (`git diff --name-only` under `app/db/migrations/versions/`), files `reset --hard` would overwrite (`git diff
  --name-only HEAD`), local commits that would be lost. Errors are an `UpdateError` with git's message, token removed.
- `start(kind, actor, now=False)` for `update` / `back` / `restart`:
  1. refused when: no restarter, not a checkout (except `restart`), one already under way (a `_starting` flag set
     before the first `await`, as the supervisor's review fix), `.env` invalid for the current code, for `update` the
     tip equal to `HEAD`, for `back` nothing to go back to;
  2. **hold:** the scheduler starts no new action (`Scheduler.hold()`; `run_due` returns at once while held), the
     feed and the notices worker stop taking new work after the current item;
  3. **wait** for the running scheduled action and the redraw batch to finish (polled every 2 s, at most
     `BOT_UPDATE__WAIT_MINUTES`, default 10; then it goes anyway). With `now` it does not wait: the running action
     is cancelled and, being a row that was never marked done, runs again after the restart;
  4. **backup** (`db/backup.py`, into `data/updates/`), the diff when the tree is dirty, older ones beyond
     `BOT_UPDATE__KEEP_BACKUPS` (3) trimmed, never the one about to be restored;
  5. **announce:** a heartbeat with `state: updating`, `eta_s` (60, or 600 when `requirements` changed) and the target
     commit (*Heartbeat* below); the presence *Updating…*; a staff-log line *Bot updating to def5678 (by X)*;
  6. the marker (`pending`; a restart writes only its history line), then `request_exit(75)` once the staff log is
     flushed.
- **Go back** goes to the last `ok` entry's `from`. When `migrations` differ between the two, it **must** restore that
  update's backup (the older code cannot open the newer database) and says what is lost: everything since the update.
  Otherwise the restore is offered, off by default. The restore is the boot step's (temp copy, `-wal` / `-shm`
  removed, `os.replace`), and a rollback of a restored go back puts the replaced database back, as in the supervisor.
  The bot's database holds no access rights, so the supervisor's access carry-over has no counterpart here.
- **At start-up**, after the staff log runs: history lines with `reported: false` become staff-log lines (*Bot
  updated from abc1234 to def5678 (by X)* with up to 10 subjects; *Bot update to def5678 rolled back: <error>*; *Bot
  restarted (by X)*), then they are marked reported.

### Commands (`app/update/cog.py`, *bot admin* only)

- `/update status`: build, branch, restarter, what is under way, the last five history lines.
- `/update check`: the check above, as an embed: commits, *Dependencies change (the restart takes longer)*,
  *Migrations change (going back later needs the database backup)*, *N local changes will be discarded (kept in
  data/updates)*, local commits that would be lost. An *Update now* button (`update:go:<tip>`, persistent, refused when
  the tip moved since: *check again*).
- `/update now`: check, then start; the reply says what it waits for. While waiting, *Restart now* and *Cancel*
  buttons (`update:now`, `update:cancel`).
- `/update restart`, `/update back [restore_db]`: confirm buttons first, then start.
- Every start, cancel and outcome goes to the staff log, with who did it.

### Heartbeat (`app/heartbeat.py`)

With the supervisor's `heartbeat_state` capability the body grows: `state` (`ok` / `updating` / `stopping`),
`version` (commit, short), `eta_s` (for `updating`), and `health` (`discord`: connected, `guild`: found, `scheduler`:
running / held / late by N s, `queue`: queued member actions, `supervisor`: as the bot sees it). Without the
capability the old body goes, as today.

- `updating` goes once, just before the exit; `stopping` goes from `close()` on a plain service stop, so the
  supervisor can tell a stop from a crash (it still alerts, the text says *stopped*).
- The interval stays 60 s.

### The supervisor's health as the bot sees it (`app/health.py`, `SupervisorHealth`)

One object, read by the act-as path and the queue; no discord.py.

| state | meaning | from |
|---|---|---|
| `up` | answers | a successful call, a `hello` |
| `updating` | announced its restart, not back yet | a `system` event `restarting` (or `waiting` with `now`), then failed calls |
| `down` | not answering, nothing announced (or an update overstayed) | a refused connection, a broken stream; `updating` past its `eta_s` + 10 min |
| `blocked` | answers but the supervisor check fails (an older API after a go back) | `supervisor_check.gate` |

- **Signals:** the live stream's new `system` events (`waiting`, `restarting`, `cancelled`, `failed`, with `to`,
  `kind`, `eta_s`); the `hello` event's `boot` (a new one = it restarted); every client call (a refused connection →
  `down` unless `updating`; any answer → `up`); while not `up`, a poll of `GET /api/version` every 10 s (a new `boot`
  → back).
- **Back:** when `boot` changes, the bot re-runs the supervisor check (the API level may have changed with the
  update), then flushes the queue, and the workers' usual `gap` wake-up re-reads the feed and the notices.
- **Staff log:** *The supervisor is updating (to abc1234)*; *The supervisor is back (abc1234 → def5678); sending N
  queued actions*; *The supervisor has not come back for 15 min* (once). A supervisor that is merely down without an
  announcement is not news (its own webhook says so), as `supervisor_check` decided.

### Queued actions (`app/queue/`)

**What is queued.** Only writes a member makes, where a late answer still makes sense, and only when the request
cannot have reached the supervisor: the health is `updating` / `down` before sending, or the call failed with a
refused connection. A timeout is answered as today (*may or may not have gone through; check the channel*), never
queued.

| kind | from | collapses with |
|---|---|---|
| `signup` (`coming` / `maybe` / `reserve` / `leave`) | the sign-up buttons | the member's other `signup` / `slot` for the same operation: the last one wins |
| `slot` (take slot N) | `/slot take` | as above |
| `offer` (accept / decline) | the offer buttons | the same offer |
| `rating` | the rate modal | the same operation |
| `attendance` (correct) | the report's editor | none (applied in order) |
| `approval` (approve / reject) | the moderation buttons | the same request |
| `organiser` (fields, OPORD, organisers, ORBAT text, step: submit / publish / withdraw / end / cancel) | the panel's modals and buttons | none; each carries the `If-Match` revision it was typed against |

Not queued, answered *The app is updating, back in about N min; try again then*: anything that must read first (the
slot picker, the panel, the wizard, reports, `/operations`, autocomplete), uploads (the file is not kept), creating or
copying an operation, deleting one, templates, `/link`, `/privacy-erase`.

**Table `queued_action`:** id, `discord_id`, `kind`, `collapse_key`, `payload` (JSON: the call's arguments), `locale`,
`created_at`, `expires_at`, `interaction` (application id and token of the reply, kept at most 15 minutes, then
cleared), `state` (`queued` / `sent` / `failed` / `expired`), `answer`.

**How a call becomes a row.** The act-as path stops taking bare lambdas for these writes: each queueable write is a
named entry in `app/queue/kinds.py` (`kind` → the client call built from the payload, the words for *done*, the
collapse key). `act()` grows a sibling, `act_or_queue(interaction, kind, payload)`, which calls through the same entry
when the supervisor is `up` and queues otherwise. That way the call that runs later is the same as the one that would
have run now.

**Replay.** When the health turns `up` (and the supervisor check passes), oldest first, one at a time, each through
`Roster.as_member` with its stored `If-Match`. The answer:
- while the reply's interaction is younger than 14 minutes: **edits that reply** (*Queued* → the outcome, worded as
  the button would have);
- otherwise a **DM** in the stored locale; DMs closed → logged and dropped (the member was told it was queued);
- a refusal is worded by `supervisor_words` (`stale` for an organiser edit says the operation changed meanwhile and
  repeats the typed text, so nothing is lost);
- past `expires_at`: *Not sent: the app was away too long. Press again.*

Rows `sent` / `failed` / `expired` are pruned after 7 days. A replay that finds the supervisor gone again stops and
waits for the next `up`.

**The member's answer when queued:** *Queued: the app is updating (back in about N min). You will get the answer
here.* For a modal, the same, with what they typed in the row.

### Settings

`BOT_UPDATE__BRANCH`, `BOT_UPDATE__GITHUB_TOKEN` (secret), `BOT_UPDATE__KEEP_BACKUPS` (3),
`BOT_UPDATE__WAIT_MINUTES` (10), `BOT_QUEUE__MAX_MINUTES` (60), `BOT_QUEUE__PER_MEMBER` (20). `BOT_RESTARTER` is read
from the environment by the boot step, not from `.env`. All in `.env.example` (`tests/test_settings.py`).

## Order of release

The bot must work with a supervisor that has none of its part: `system_events` and `heartbeat_state` are **optional**
capabilities, not added to `supervisor_check.CAPABILITIES`. Without `system_events` the bot learns of an update only
from failed calls and `boot` (so it queues as `down`, not `updating`; the words say *can't be reached* instead of
*updating*). Without `heartbeat_state` it sends the old heartbeat, and a long update may still raise
`integration_silent`. `GET /api/version` is in every supervisor that has `heartbeat`.

1. The supervisor's part (its plan) can go first or later.
2. The bot's update mechanics need the first install by hand.

## Steps

1. **Boot and restart** (`app/boot.py`, `app/restarter.py`, `app/__main__.py` with lazy imports and `request_exit`,
   `.app-update/` in `.gitignore`, `boot.ready()` in `on_ready`). Tests over a throw-away git repository with a fake
   `app` package: apply, ready, two crashes → rollback, hang → watchdog, pip failure, gave up, the console loop.
2. **Update service and commands** (`app/update/`, settings, `Scheduler.hold()`, backup and diff, the go back with the
   migrations rule, the start-up report, `/update` with its persistent buttons, i18n). Tests with a local bare repo
   as `origin` and `request_exit` replaced by a recorder.
3. **Heartbeat with state and health** (body, `updating` before exit, `stopping` in `close()`), the contract synced,
   the fake supervisor accepting the new fields.
4. **Supervisor health** (`app/health.py`; `Live` hands `system` events and `hello`'s `boot` to it; the client
   reports refused connections apart from timeouts (`SupervisorUnreachable.sent`); `/api/version` in the client;
   staff-log lines). The fake grows `system` events, `boot` changes and a "down" switch.
5. **Queued actions** (`app/queue/`, the migration, `act_or_queue` and the kinds table, the call sites listed above
   moved to it, replay, words). Tests: queued while `updating`, collapsed, replayed in order on `up`, the reply edited
   or a DM, `stale` repeating the text, expiry, a timeout never queued.
6. **Docs:** `README.md` (*Update*: `/update`, the first install by hand, `BOT_RESTARTER` in the NSSM lines,
   `boot.log`, what to do on `gave_up`), `update.ps1` kept for the first install and for emergencies, `CLAUDE.md`
   (code map, the rule *queue only what never reached the supervisor*), this plan's *As built*.
7. **End to end:** a scratch clone with a local `origin`, under the console loop, against the fake supervisor: update,
   rollback (a commit that raises in `on_ready`), restart, go back over a migration; then the supervisor's update
   against a scratch supervisor (its part built): members' presses queued and answered.

## Tests

- Boot (as the supervisor's `tests/test_boot.py`): each marker state; pip only with `requirements`; restore and put
  back; two failures roll back; the watchdog; no marker, nothing happens.
- Updater: check output (commits, requirements, migrations, dirty, local commits, token not in errors); refusals; one
  start at a time; the hold and the wait; `now` cancels and the action runs again later; the go back with changed
  migrations refuses without the restore; history reported once.
- Health: each signal moves the state as in the table; `updating` overstays → `down` and one staff line; a new `boot`
  → check, then flush.
- Queue: per kind, collapse keys, expiry, replay order, `If-Match` kept, the reply edited within 14 minutes and a DM
  after, reads refused with the updating words.
- Coverage: `test_i18n.py`, `test_settings.py`, `test_db.py` (the new table), the `@answers` test for the new
  buttons, `contract_check.py` (the new heartbeat fields, `/api/version`).

## As built

Step 1 (2026-11-22):

- **`app/__main__.py` is only the boot step's door:** standard library and `app.boot`, nothing else. The commands
  moved to `app/cli.py` (`load_or_exit`, `prepare_database`, `run`, `main`). Only `run` (the default) boots;
  `migrate`, `backup`, `check-config`, `new-migration` and `--help` never enter the boot step or the console loop.
- **The database restore is a plain copy** (`integrity_check` on the temp copy, `-wal` / `-shm` removed,
  `os.replace`). It still needs the go back's own `backup` to exist, so a rollback can always put it back. The marker
  says `db_restored: true` (the supervisor's `db_restore` held counts).
- **`HISTORY_FIELDS` has `migrations`**, written by the updater (step 2), for the staff-log line and *Go back*.
- **The error keeper watches the root logger only:** discord.py's loggers propagate to it (`logs.setup` adds no
  logger of its own with `propagate` off), and there is no uvicorn.
- **`boot.ready()`** runs in a thread right after *Bot started* is posted in the first `on_ready`, i.e. only once the
  guild is found. A bot that cannot find its guild is rolled back; the older commit fails the same way and the boot
  step gives up, which then starts the bot as it is.
- **Closing for a restart:** `cli.run` installs `restarter.set_exit_handler`, which schedules `EventBot.close()` on
  the loop (thread-safe); `bot.start` returns, `asyncio.run` ends, and `cli.main` exits with 75.

Step 2 (2026-11-22):

- **The branch:** `BOT_UPDATE__BRANCH` empty (the default) follows the branch checked out, so an install on
  `feature/front-end` is never reset to `master` by surprise; a detached checkout must name one.
- **A rollback of an update with new migrations puts the database from before it back** (`app/boot.py`, the same
  step as a restored go back's): the new code may have migrated before failing, and the older one can't open that.
  The marker's `backup` is always the database from before the marker's change.
- **Only the scheduler is held** (`Scheduler.hold()` / `release()`, `running`). The feed and the notices worker are
  not: drawing converges after a restart, and a notice cut off between its `notice_delivery` row and its message is
  the at-most-once rule's known loss (a window of milliseconds). `now` / *Restart now* leaves the running action to
  the exit's task cancellation; never marked done, it runs again after the restart.
- **Refusals are codes** (`UpdateError.code`), worded by `app/update/cog.py`; git's message is `detail`, token removed.
  `bad_env`: the `.env` on disk must load as it is, or the bot would not come back.
- **No confirm buttons:** `/update restart` goes at once; `/update back` restores the database only with the
  explicit `restore_db` option, and refuses without it when the migrations differ (`needs_restore`).
- **Announcing:** `Updater.announce` (set by `EventBot`) sets the presence *Updating…* / *Restarting…*; the heartbeat
  joins it in step 3. Then `StaffLog.flush()` (which returns at once when no staff-log task runs) and the exit.
- **The start-up report** runs in `on_ready` right after `boot.ready()`, so a rolled-back or given-up update is told
  by the version that came up.

Step 3 (2026-11-22):

- **The supervisor's part came first** (its `docs/PLAN-bot-health.md`, built the same day), so the contract has the
  fields; `heartbeat_state` stays optional here (not in `supervisor_check.CAPABILITIES`).
- **`to` is a field of its own** (the supervisor's *As built*): `version` is the commit the bot runs (short, read
  once in `setup_hook` with `Updater.head()`), `to` the short commit an update goes to. A restart sends `updating`
  without `to`. `eta_s`: 60 s, or 600 s when the dependencies change.
- **`heartbeat.announce()`** sends `updating` / `stopping` within 5 seconds or gives up, so neither a restart nor a
  stop waits for a supervisor that is away. `updating` goes from `EventBot.announce_restart` (the updater's hook,
  before the presence); `stopping` from `EventBot.close()` unless a restart was asked for.
- **Health:** `discord` (ready and not closed), `guild`, `scheduler` (`idle` / `busy` / `held`),
  `scheduler_started`. The queue and the supervisor as the bot sees it join in steps 4 and 5.

Step 4 (2026-11-22):

- **The poll is `GET /api/system/info`, not `/api/version`**: it is in the contract already (no supervisor change),
  and an answer also refreshes the capabilities. The restart itself shows in the stream's `hello` (a new `boot`).
- **Every call is a signal** (`SupervisorClient.on_call`, set by the health): an answer is `up` again, no answer or a
  502 / 503 / 504 is `down` (an announced update stays `updating`). `SupervisorUnreachable.sent` is False for a refused
  connection only (`aiohttp.ClientConnectorError`): never reached the supervisor, safe to queue in step 5.
- **The check before `up`** runs when the supervisor announced a restart or came back with a new `boot`
  (`EventBot.supervisor_problems`, the start-up check's `check()` again). Problems: `blocked`, the staff log hears
  them, and it is checked every 10 s until it passes (*The supervisor is ready*). The workers started at start-up are
  not stopped while blocked; the queue (step 5) waits for `up`.
- **Overstayed:** 10 minutes past the announced time, one staff-log line and `down`; its return is told too.
- **`system` is asked for only with the `system_events` capability** (`live.OPTIONAL`): an older supervisor refuses an
  unknown kind with 422. A `gap` does not wake the `system` listener.
- **Members' words:** `supervisor_words` takes the health: while `updating`, *The app is updating and should be back
  in about N min* (or *any moment* once the time passed).
- The heartbeat's `health` gains `supervisor` (the state).

Step 5 (2026-11-22):

- **`app/queued/`, not `app/queue/`** (no name like the standard library's `queue`).
- **A row is a client method and its arguments**, not a kind with its own call: `QUEUEABLE` lists the client methods
  that may wait (`answer`, `leave`, `take_slot`, `accept_offer`, `decline_offer`, `rate`, `correct_attendance`,
  `approve_request`, `reject_request`, `update_operation`, `set_opord`, `set_organisers`, `set_orbat`,
  `operation_step`, `cancel_operation`, `end_operation`), replayed as `getattr(client, method)(member, *args)`
  through `Roster.as_member`. So the organiser panel's `write()` takes the method and its arguments instead of a
  lambda (the revision read among them, for `If-Match`), and needs no table of kinds.
- **`done` is stored worded** (the member's language at the press) rather than looked up at replay; a success says
  *Sent now that the app is back: …* with the DLC warning, a refusal *Not done now that the app is back: …*
  (`supervisor_words`), `stale` *Not saved* with the texts the organiser sent, a timeout at replay *check whether
  this went through* (not sent again).
- **Order:** a member with queued rows has every next write queued too, even when the supervisor is up, so theirs
  reach it in order. Collapse keys: `signup:<op>` (the buttons, the picker and `/slot take`), `offer:<offer>`,
  `rating:<op>`, `request:<id>`; replaced rows' replies say *Replaced by your later choice*.
- **The interactions stay in memory** (`Teller`), not in the table: their tokens are credentials and expire in 15
  minutes. After a restart of the bot, or past 14 minutes, the answer is a DM; one that can't be edited is a DM too.
- **The queue sends only when the health is `up`**, oldest first, stopping at the first refused connection; its loop
  (started with the other supervisor workers) also expires rows every minute while the supervisor is away.
- **Not queued** (answered with the updating words): reads, the wizard's create, copy, delete, uploads, templates,
  `/link`, `/privacy-erase`. The heartbeat's health has no queue count yet.

Steps 6-7 (2026-11-22):

- **README:** *Update* (the commands, `BOT_RESTARTER`, `gave_up` by hand), *While the other one updates*, and
  `data/updates/` in the data table.
- **End to end, the update** (a script, not in the suite): a scratch clone of the working tree with a local bare
  `origin`, its commits' `app/cli.py` a stand-in that calls `boot.ready()` (the real one needs Discord), the working
  tree's `Updater` writing the markers, NSSM emulated (started again after any non-zero exit, `BOT_RESTARTER=nssm`).
  Update 1 → 2, go back, update again, then a commit that raises at import: `[75, 1, 1, 75, 0]`, back on 2, the
  reason in the report; a restart. All passed after two fixes:
  - **Stale bytecode:** after a go back within the same second, Python ran the newer `.pyc` (same size, same
    whole-second mtime). The boot step now deletes `app/**/__pycache__` after every `git reset`.
  - **A crash nothing logs** (an import error) left the reason empty: the boot step's watched start now also notes
    uncaught exceptions (`sys.excepthook`), and an invalid `.env` is logged as an ERROR before exiting 2.
- **End to end, the queue:** the fake supervisor over real TCP on a fixed port, the bot's real `Live`, health and
  queue loops: a press goes straight through; the supervisor announces its restart on the stream and goes away
  (connections refused); a press is queued (*back in about 2 min*); it comes back with a new boot; the press is sent
  in the member's name, the *Queued* reply becomes the answer, staff hear both lines. It found:
  - **The old process's last second:** the supervisor exits a moment after it announces, and an answer in that moment
    (the health's own poll) counted as its return: a premature *back*, then *down* rather than *updating*. While
    updating, an answer now counts only once the supervisor has been seen gone (`SupervisorHealth.gone`) or the stream
    shows a new `boot`.

## Out of scope

Background checks and automatic installs, choosing a commit, updating the supervisor from Discord, the supervisor's
console driving the bot, queuing reads or uploads, queuing anything while the **bot** is down (Discord answers *This
interaction failed* on its own; keeping the restart short is the remedy), and a second bot process for a handover
without downtime.
