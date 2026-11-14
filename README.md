# Operations bot

A Discord bot for one Arma 3 group, the Discord front end of [arma-server-supervisor](../arma-server-supervisior).
The supervisor owns the operations (the group's missions), sign-ups, approval, attendance, ratings, Steam links,
squad.xml and who may do what. The bot draws all of it on Discord and passes members' actions to the supervisor in
their name. The design is in [docs/PLAN-front-end.md](docs/PLAN-front-end.md). How things look on Discord is in
[docs/PLAN.md](docs/PLAN.md), whose rules about data and decisions the supervisor now applies.

**Status:** built on `feature/front-end` (*As built* in the front-end plan) and tested against a fake supervisor. It
has not yet run against the real one; that is the release step (*Next* in the plan).

## What it does

- **Roster:** pushes the server's members and roles to the supervisor (at start, daily, and on every join, leave,
  rename or role change). The supervisor makes accounts from them by its Discord roles table.
- **Operation channels:** every operation, whether made on Discord or in the app, gets a channel in the operations
  category. It holds the info, the OPORD, the preset, the ORBAT with its sign-up buttons (or *Coming* / *Not coming*
  for Zeus and training), and later the attendance report. It is marked DRAFT or WAITING FOR APPROVAL until
  published; then it gets a discussion thread and an announcement. When the operation is cancelled or over, it moves
  to a quarter's archive category, and old quarters move into the database.
- **Members:** sign up with the buttons or `/slot`, take freed slots offered by DM, rate missions, link Steam with
  `/link`, see `/operations`, `/attendance`, `/profile`, `/mission-maker` and `/group`.
- **Organisers:** *Create operation* on the header opens the wizard; *Organiser panel* under each operation opens the
  panel (details, OPORD, ORBAT and templates, sign-up, co-organisers, publish or send for approval, copy, cancel,
  end). `/operation preset | image | mission` uploads files.
- **Moderators:** approval requests in the staff channel's moderation thread, with *Approve* / *Reject*.
- **Pings:** sign-up opening, reminders before the assembly, cancellations and lost slots, all in the operation's
  thread.
- **Staff log:** a thread in the staff channel with what changed (the supervisor's activity lines), errors and
  configuration problems.

Every button and command is checked by the supervisor, which answers a refusal in words. The bot itself decides only
its own commands (*bot admin*).

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
   Channels and Manage Roles. It also needs *Mention @everyone, @here and All Roles* unless the announcement and
   climate roles are mentionable. At start-up the bot checks this and posts what is missing to the staff log.
3. Create these channels:
   - the operations category (members may view and read the history, may not send messages, and may *send
     messages in threads*, which is where they talk);
   - the header channel and the announcements channel;
   - one staff-only channel. The bot makes two threads in it itself, *Staff log* and *Moderacja* (names in `.env`).

   Put each channel and role into `.env` by **name or id**. A name is quicker; an id survives renames (turn on
   Developer Mode in *Settings → Advanced*, then right-click → *Copy ID*). A name that matches nothing, or more than
   one channel or role, is reported in the staff log.
4. Staff commands (`/reload`, `/links`, `/privacy-erase`, `/attendance-report`) are hidden from everyone but admins
   at first. In *Server Settings → Integrations → the bot*, allow them for the staff roles. The bot or the
   supervisor still checks every use.

### The supervisor

1. In the supervisor's folder, create the bot's account with the `integration` role and print its token once:

   ```powershell
   .venv\Scripts\python.exe -m app create-user discord-bot --role integration --token discord-bot
   ```

   Put the token into `BOT_SUPERVISOR__TOKEN`, and the supervisor's address into `BOT_SUPERVISOR__URL`.
2. In the supervisor's app, fill in the **Discord roles table** (Members). It sets which Discord role gives which
   app role (and so who may create, publish, approve and sign up), which roles put a member into squad.xml, and
   which stand for Creator DLC. Who may manage or approve operations also decides who may post in operation channels.
3. Climates (with their Discord roles), other groups, the kinds' names, the offer and rating times and the
   stale-draft timers are the supervisor's settings (*Operations*).

At start-up, and every minute until it passes, the bot checks the supervisor:
- its API level and capabilities;
- that the bot's account may act for members (`integration.act_as`);
- that its time zone matches `BOT_TIMEZONE` (only a warning when it doesn't).

Until the check passes, the bot stays on Discord but holds back everything that needs the supervisor, and the staff
log says why. With the `heartbeat` capability the bot reports in every minute, and the supervisor alerts through its
own webhook when the bot falls silent.

## Configuration

Everything is in `.env`; [.env.example](.env.example) lists each setting with its default. Only the bot's own
settings are left there: secrets, channels, the announcement role, the bot admins, a few times, the archive. Channels
and roles take a name or an id; lists separate them by `;` or use JSON.

`python -m app check-config` validates a file without starting anything. Names are only matched against the server
when the bot connects. It also lists unknown `BOT_` variables, such as typos or settings removed in this version.

`/reload` (bot admins) applies a changed file; the token, the guild, the data folder and the supervisor URL and token
need a restart.

**Removed in the front end** (now the supervisor's): the member, mission maker, sign-up, rank, remark, climate and
DLC roles, every permission except `BOT_PERMISSIONS__BOT_ADMIN`, the kinds' names, the event server and profile
settings, the reservation, offer, stale-draft and attendance times, squad.xml, and the Steam-link expiry. An old
`.env` still loads, and `check-config` names what it no longer reads.

The `/privacy` text ships in `app/privacy/`. To change it, put `privacy.pl.md` / `privacy.en.md` into the data folder.

### Steam links and squad.xml

The supervisor holds the links and writes squad.xml. On its Members → Steam links page, staff confirm links, set
anyone's link and remarks, and check the file. The bot lets members link themselves (`/link`, *Link now*) and reminds
those who haven't:
- on its first start it posts one *Link now* announcement in `BOT_CHANNELS__LINK_REMINDER`;
- members with a squad.xml rank and still no link get a DM `BOT_LINKS__REMINDER_DAYS` later (at most
  `BOT_LINKS__DMS_PER_HOUR` an hour), and again every that many days;
- bot admins send one now or turn them off for a member with `/links remind` and `/links reminders`.

## Commands

| Command | Who | What |
|---|---|---|
| `/operations` | everyone | the coming operations and your place in each |
| `/slot take`, `/slot leave` | members | in an operation's channel or thread |
| `/operation preset`, `image`, `mission` | organisers | the operation's files |
| `/link`, `/unlink` | members | your Steam link |
| `/attendance`, `/profile`, `/mission-maker` | members | records; the details about yourself and for staff |
| `/group` | members | our operations with another group |
| `/archive event` | organisers, participants, bot admins | an archived operation as an HTML file |
| `/privacy` | everyone | what is stored and how to have it erased |
| `/attendance-report` | staff | totals per member with a CSV |
| `/privacy-erase` | staff (Manage users in the app) | erase a member, in the app and in the bot |
| `/links remind`, `/links reminders` | bot admins | Steam link reminders |
| `/reload` | bot admins | apply a changed `.env` |

## Run as a service (NSSM)

As for the supervisor ([its DEPLOYMENT.md](../arma-server-supervisior/docs/DEPLOYMENT.md)):

```powershell
$dir = "D:\Workspace\python\deployment-interface-coordination-Kit"
nssm install EventBot "$dir\.venv\Scripts\python.exe" "-m app"
nssm set EventBot AppDirectory $dir
nssm set EventBot DisplayName "Operations bot"
nssm set EventBot Start SERVICE_AUTO_START
nssm set EventBot DependOnService ArmaSupervisor
nssm set EventBot AppStdout "$dir\data\service.log"
nssm set EventBot AppStderr "$dir\data\service.log"
nssm start EventBot
```

## Data, logs and backups

Everything lives in `BOT_DATA_DIR` (`data` by default). The bot keeps no operation data of its own: only the
Discord plumbing, the archive, delivery bookkeeping and the link reminders.

| Path | What |
|---|---|
| `bot.db` | The database (SQLite, WAL). Migrated at every start; backed up first when a migration is due. |
| `backups/bot-*.db` | A copy every night at 04:00 local time, the newest `BOT_BACKUP__KEEP` kept. `python -m app backup` makes one now. |
| `files/` | The operations' files (presets, images) fetched from the supervisor to post them; fetched again when missing. |
| `archive/` | The archived operations' attachments (unless `BOT_ARCHIVE__ATTACHMENTS_DIR` puts them elsewhere). |
| `logs/bot.log` | The application log, rotated daily, `BOT_LOGS__KEEP_DAYS` kept. Each error has an id, also posted to the staff log. |
| `service.log` | What NSSM caught: failures before logging starts. |

The nightly backup covers the database only. **The host's own backups must include the archive's attachments and
`.env`.**

## Update

```powershell
.\update.ps1     # checks .env, backs up, git pull, pip install, restarts the service
```

The bot and the supervisor are deployed separately. A bot that needs a newer supervisor holds its supervisor work
back and says so in the staff log until the supervisor is updated.

## Development

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest tests/test_scheduler.py::test_a_repeating_action_moves_on -q
```

The tests need no network and no Discord. Services are tested directly, the Discord layer through the fakes in
`tests/fakes.py`, and the supervisor through the aiohttp app in `tests/fake_supervisor.py`.

- **The contract:** the routes the bot uses are the supervisor's `docs/api/openapi-bot.json`, copied into
  `tests/contract/` by `python scripts/sync_contract.py`. The fake checks every request against it, and
  `tests/test_contract.py` fails when the fake and the contract differ or the copy is out of date.
- **Texts:** every text a member sees goes through `t("English text", locale, ...)` and needs a Polish entry in
  `app/i18n/pl.json` with the same `{fields}`. `tests/test_i18n.py` fails otherwise, and also when an entry is no
  longer used.
- **Database migrations:** after changing `app/db/models.py`, run `python -m app new-migration "what changed"`, read
  the file it writes in `app/db/migrations/versions/`, and commit it with the change. `tests/test_db.py` fails while
  the models and the migrations differ.
- **Test instance:** a separate Discord server, a second bot token, and its own `.env` and data folder.
  - For the UI alone, point `BOT_SUPERVISOR__URL` at the fake supervisor (`python -m tests.fake_supervisor 8099`,
    token `test-token`).
  - For the real thing, use a **scratch supervisor**: its own checkout and data folder, with its
    `tests/fake_server.py` as the Arma executable.
  - Never use the group's live supervisor: an operation that holds the host takes the whole host.
