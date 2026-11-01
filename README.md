# Event bot

A Discord bot for one Arma 3 group: events with ORBAT sign-ups, host reservations and event profiles on
[arma-server-supervisor](../arma-server-supervisior), attendance from the supervisor's player stats, and the group's
squad.xml. The design is in [docs/PLAN.md](docs/PLAN.md).

**Status: all phases of the plan (1–8).** The bot starts, checks its configuration on the server, keeps its database, backups,
logs and staff threads, reports to the supervisor, and answers `/reload`, `/privacy` and `/server start | stop |
restart | say`. Host reservations, the ORBAT format, templates, the sign-up rules and the event rules
(drafts, approval, times, cancelling, timed actions) work, and an event gets its channel: hidden while a
draft, then published with an announcement, its messages kept in order and redrawn as sign-ups change, archived
by quarter. *Create event* in the header channel starts a wizard; *Organiser panel* under an event's info
edits it, publishes or sends it for approval; moderators decide in the moderation thread. `/event preset` and
`/event image` attach the launcher preset and the briefing image. Members sign up with the buttons under the
ORBAT or `/slot take | leave`, freed slots are offered to the reserve list, and `/events` lists what is coming.
Members link their Steam account with `/link` (or *Link now* in a reminder), `/unlink` removes it, staff use
`/links set | remove`, and the bot keeps squad.xml in step with the rank and remark roles. After a mission the
bot ends the event (a finished mission, the reservation's end, or *End event*), reads who played from the
supervisor and posts the attendance report in the event channel, where the organisers correct it with *Edit
attendance*. Attendees rate the mission under the report; `/attendance`, `/profile`, `/mission-maker` and
`/group` show the totals, and bot admins get `/attendance-report` with a CSV and `/privacy-erase`. Mission
makers set the mission with `/event mission` and build the event's server profile in the panel (*Server profile*):
the bot clones `BOT_SUPERVISOR__BASE_PROFILE`, imports the preset, sets the mission, puts the profile on the event
server for the reservation and deletes it some days later. A year on, the oldest quarter's channels move into
the database (transcripts and attachments) and are deleted; `/archive event` gives an event back as an HTML file.

## Install

Python 3.11 or newer, on the same machine as the supervisor.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # then fill it in, see below
.venv\Scripts\python.exe -m app check-config
.venv\Scripts\python.exe -m app
```

### The Discord application

1. In the [developer portal](https://discord.com/developers/applications), create an application with a bot. Under
   *Bot*, switch on the privileged **Server Members** and **Message Content** intents. Copy the token into
   `BOT_DISCORD_TOKEN`.
2. Invite it with the `bot` and `applications.commands` scopes. It needs View Channels, Send Messages, Send Messages
   in Threads, Create Public Threads, Manage Threads, Embed Links, Attach Files, Read Message History, Manage
   Channels and Manage Roles, plus *Mention @everyone, @here and All Roles* unless the announcement and climate roles
   are mentionable. At start-up the bot checks this and posts what is missing to the staff log.
3. Create two categories (drafts: hidden from members; events: members may view and read the history, may not
   send messages, and may *send messages in threads*, which is where they talk), the header and
   announcements channels, and one staff-only channel. In the staff channel the bot makes two threads itself,
   *Staff log* and *Moderacja* (names in `.env`). Put each channel and role into `.env` by **name or id**: a name is
   quicker, an id survives renames (turn on Developer Mode in *Settings → Advanced*, then right-click → *Copy ID*).
   A name that matches nothing, or more than one channel or role, is reported in the staff log.
4. Staff commands (`/reload`, `/server`, `/links`, `/attendance-report`, `/privacy-erase`) are hidden from
   everyone but admins at first. In *Server Settings → Integrations → the bot*, allow them for the roles you gave
   those permissions in `.env`. The bot still checks the roles on every use.

### The supervisor account

In the supervisor's folder, create the bot's user with the `integration` role and print its token once:

```powershell
.venv\Scripts\python.exe -m app create-user discord-bot --role integration --token discord-bot
```

Put the token into `BOT_SUPERVISOR__TOKEN`. With a supervisor that offers the `heartbeat` capability, the bot reports
in every minute, and the supervisor alerts through its own Discord webhook when the bot falls silent.

## Configuration

Everything is in `.env`; [.env.example](.env.example) lists each setting with its default. Channels and roles take
a name or an id; lists separate them by `;` or use JSON. `python -m app check-config` validates a file without
starting anything (names are only matched against the server when the bot connects) and lists unknown `BOT_`
variables, such as typos or settings renamed in a newer version. `/reload` (bot admins)
applies a changed file; the token, the guild, the data folder and the supervisor URL and token need a restart.

The `/privacy` text ships in `app/privacy/`; to change it, put `privacy.pl.md` / `privacy.en.md` into the data
folder.

### squad.xml and the old bot's links

The bot writes only `squad.xml`, into `BOT_SQUAD_XML__OUTPUT_DIR`; IIS serves that folder, with `squad.dtd`,
`squad.xsl` and the logo kept there by hand. Members are those with a `BOT_ROLES__RANK` role and a Steam link;
`BOT_ROLES__REMARK` roles make the remark. On the switch-over day, stop the old bot's squad.xml export, then import
its links before the first start:

```powershell
.venv\Scripts\python.exe -m app import-old-links D:\Workspace\python\IBC_BOT\data\steam_ids.json
```

It prints what it skipped (bad ids, duplicates). On its first start the bot posts one *Link now* announcement in
`BOT_CHANNELS__LINK_REMINDER`; members with a rank and still no link get a DM `BOT_LINKS__REMINDER_DAYS` later
(at most `BOT_LINKS__DMS_PER_HOUR` an hour), and again every that many days.

## Run as a service (NSSM)

As for the supervisor ([its DEPLOYMENT.md](../arma-server-supervisior/docs/DEPLOYMENT.md)):

```powershell
$dir = "D:\Workspace\python\deployment-interface-coordination-Kit"
nssm install EventBot "$dir\.venv\Scripts\python.exe" "-m app"
nssm set EventBot AppDirectory $dir
nssm set EventBot DisplayName "Event bot"
nssm set EventBot Start SERVICE_AUTO_START
nssm set EventBot DependOnService ArmaSupervisor
nssm set EventBot AppStdout "$dir\data\service.log"
nssm set EventBot AppStderr "$dir\data\service.log"
nssm start EventBot
```

## Data, logs and backups

Everything lives in `BOT_DATA_DIR` (`data` by default):

| Path | What |
|---|---|
| `bot.db` | The database (SQLite, WAL). Migrated at every start; backed up first when a migration is due. |
| `backups/bot-*.db` | A copy every night at 04:00 local time, the newest `BOT_BACKUP__KEEP` kept. `python -m app backup` makes one now. |
| `logs/bot.log` | The application log, rotated daily, `BOT_LOGS__KEEP_DAYS` kept. Each error has an id, also posted to the staff log. |
| `service.log` | What NSSM caught: failures before logging starts. |

The nightly backup covers the database only. **The host's own backups must include the data folder** (presets,
briefing images, and the archived events' attachments in `archive/` unless `BOT_ARCHIVE__ATTACHMENTS_DIR` puts them
elsewhere) **and `.env`.**

## Update

```powershell
.\update.ps1     # checks .env, backs up, git pull, pip install, restarts the service
```

## Development

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest tests/test_scheduler.py::test_a_repeating_action_moves_on -q
```

The tests need no network and no Discord: services are tested directly, the Discord layer through the fakes in
`tests/fakes.py`, the supervisor through the aiohttp app in `tests/fake_supervisor.py`.

- **Texts:** every text a member sees goes through `t("English text", locale, ...)` and needs a Polish entry in
  `app/i18n/pl.json` with the same `{fields}`; `tests/test_i18n.py` fails otherwise.
- **Database migrations:** after changing `app/db/models.py`, run
  `python -m app new-migration "what changed"`, read the file it writes in `app/db/migrations/versions/`, and commit
  it with the change. `tests/test_db.py` fails while the models and the migrations differ.
- **Test instance:** a separate Discord server, a second bot token, and its own `.env` and data folder. Point
  `BOT_SUPERVISOR__URL` at the fake supervisor (`python -m tests.fake_supervisor 8099`, token `test-token`), never at
  the real one: a reservation takes the whole host.
