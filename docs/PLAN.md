# Plan: community event bot

Status: phases 1 (skeleton), 2 (supervisor: reservations, `/server`), 3 (ORBAT and sign-up logic) and 4a (the
event service) built on 2026-10-31; 4b-4d and the later phases are planned. Written 2026-10-14 from design interviews with the owner, and revised the same
day after a review of the plan's gaps. Revised on 2026-10-31: the supervisor facts were checked again, and the
plan now serves one group with four event kinds (Zeus among them) and approval by the moderators.

## Goal

The Discord server belongs to **one Arma 3 group** (e.g. IBC). Other groups have their own Discord and
infrastructure; they appear in the bot only as names (see *Event kinds*). Members find upcoming missions in
Discord, take a slot in the ORBAT, and get pinged before the briefing. Organisers prepare an event in a draft and
publish it, or, without the right to publish, send it to the moderators for approval. For events on our own
server the bot reserves the host on **arma-server-supervisor**, and afterwards reports who actually played, using
the supervisor's player statistics. It also keeps the group's squad.xml in step with Discord roles.

**Done when:** an IBC mission maker presses *Create event* in the header channel and fills in the wizard: kind
*Internal*, type Coop, climate Cold War, 18.10, assembly ("zbiórka") 19:00, start 19:30, duration 3 h. Then:

- a hidden draft channel `18-10-ibc` appears in the events category. The supervisor has a reservation for the
  default event server from 19:00 − lead time to 19:30 + 3 h + buffer;
- in the **organiser panel** they enter the briefing, paste the ORBAT, set the author, upload the launcher preset
  with `/event preset`, set sign-up to open on 15.10 at 18:00, and press *Publish*;
- the channel becomes visible and read-only, with the info, briefing, ORBAT and preset messages and a discussion
  thread. An announcement mentioning @Cold War links to it. On 15.10 at 18:00 sign-up opens, with a ping;
- members take slots through *Sign up* or `/slot`, and the ones who can't make it press *Maybe* / *Can't come*;
- slot holders are pinged in the channel at the configured offsets before the assembly;
- the mission ends `finished` at 22:10. After the settle delay with no new mission, the bot ends the reservation
  and posts the attendance report (slot holders, plus walk-ins) in the channel. Some time later the channel
  moves to *Archiwum Q4 2026*, read-only.

**And for the other kinds** (added 2026-10-31; each is an end-to-end test on the Discord fakes and the fake
supervisor):

- **A member's event:** a *Klanowicz* creates an internal event. No reservation exists yet. With the briefing and
  ORBAT in place they press *Send for approval*: the reservation is created, the moderation thread gets the
  request, a moderator presses *Approve*, and the event is published at once. Had the moderator pressed *Reject*
  with a reason, the event would be a draft again, without a reservation, and the creator pinged.
- **A rejected change:** the creator moves that approved event by a day. Nothing moves yet: the moderators get a
  request with the old and new times, they reject it, and the event and its reservation stay where they were;
  the creator is pinged in the thread with the reason.
- **Zeus:** a *Klanowicz* creates a Zeus event with a limit of 20 and only a briefing, and publishes it without
  approval; the info embed says the server isn't reserved yet. They press *Request the server*, a moderator
  approves, and the reservation is made. Twenty members press *Coming*, and the button turns off; one presses
  *Leave*, and it turns on again.
  The mission never ends `finished`; at the reservation's end, plus the settle delay, the report lists those
  *Coming* as attended or no-show.
- **Guest:** a mission maker creates a guest event with host group *ABC* (added on the spot); the channel is
  `dd-mm-abc`. No reservation is made. They fill in *How to join* and our part of the ORBAT, and publish. At start
  + duration + buffer the report lists every slot holder as *to confirm*; the organiser sets them with *Edit
  attendance*, and attendees are asked to rate the cooperation with ABC.
- **Hosted:** a mission maker creates a hosted event inviting *ABC* and *XYZ*, with only our part of the ORBAT.
  The reservation is made as for an internal event; after the mission the report lists our slot holders, our
  members without a slot by name, and the rest as *guests: N players*.

## Decisions

### Event kinds

Decided 2026-10-31. Every event is one of four kinds, picked first in the wizard:

| | **Internal** | **Guest** | **Hosted** | **Zeus** |
|---|---|---|---|---|
| Who organises | We do | Another group | We do, and invite other groups | We do |
| Whose server | Ours | The host group's | Ours | Ours |
| Other groups | None | The **host group** | The **invited groups** | None |
| Sign-up | The whole ORBAT | ORBAT: only the slots the host gave us | ORBAT: only our part; the invited groups fill their parts themselves, outside the bot | **No ORBAT**: just *Coming* / *Not coming* (see *Zeus sign-up*) |
| Approval of a member's event | Yes | Members can't create one: mission maker role and above only | Yes | **Only the reservation**: the event is published at once, its host reservation waits for a moderator |
| Reservation and event profile | Yes | No (no supervisor features at all) | Yes | Yes |
| *How to join* text | No | Yes: server address, password, the host's TeamSpeak / Discord, notes | No | No |
| Attendance | From the supervisor stats | **By hand**: every slot holder starts as *to confirm* | From the supervisor stats; players we don't know are counted as guests | From the supervisor stats, read when the **reservation ends** (a Zeus mission rarely ends `finished`) |

Other groups are rows in the database (acronym, full name), not config. The wizard and the panel suggest the known
ones, and a new one can be added on the spot (acronym and name). *Manage any event* roles can correct a group's
name or acronym; a group is never deleted, because events refer to it.

### Event channel

| Topic | Decision |
|---|---|
| Where | A new text channel per event. Drafts and pending events live in a hidden **drafts category**; publishing moves the channel to the **events category** (both in config). Each category holds at most 50 channels: at 45 in either the staff log is warned, and at 50 the wizard refuses with an explanation (decided 2026-10-31). |
| Name | `dd-mm-xxx`: the day and month of the event date, then the acronym of the group whose server it is: ours for internal, hosted and Zeus events (`18-10-ibc`), the host group's for a guest event (`18-10-abc`). Same-day events with the same host get the same name; the channel topic tells them apart (title, kind, times). |
| Members' rights | **Read-only** for members: only the bot and the organisers post there, so the event messages stay together at the top. |
| Messages, in order | 1. **Info embed**: title, kind, mission type, climate, the host group (a guest event) or the invited groups (a hosted event), author, date, assembly time, start time, duration, the modset summary, sign-up state, and the *Organiser panel* button. 2. **Briefing** (text and an optional image). 3. **Preset**: the launcher `.html` posted by the bot. 4. **ORBAT**: one embed per squad with the holders, then the Maybe / Can't come / reserve lists. It carries the sign-up buttons (*Sign up*, *Reserve*, *Maybe*, *Can't come*, *Leave*). If it no longer fits in one message (10 embeds, 6000 characters), it continues in **"ORBAT (cont.)" messages** after it. A Zeus event has the **sign-up list** here instead (see *Zeus sign-up*). |
| Order kept | The bot stores every message id and edits the messages in place. The ORBAT is **last** (decided 2026-10-31), so a new continuation message is simply posted at the end and nothing is deleted; a continuation no longer needed is deleted from the end. A preset added later than the ORBAT is the one exception: the preset message is posted then, and the ORBAT messages are re-posted after it. |
| Discussion | A **public thread** in the channel ("Dyskusja"), created **with the channel** (a draft's thread is as hidden as its channel). **Every ping goes to the thread** (decided 2026-10-31): sign-up opening, reminders, offers when DMs are closed, approval results, stale-draft warnings, cancelling. The main channel holds only the bot's own messages, so their order never breaks; the attendance report is the one message added there later, after the ORBAT has stopped changing. |
| Native Discord Scheduled Event | **None.** |
| Archive | A configured delay after the attendance report (see *Supervisor: attendance*), the channel moves to the archive category of the event date's **quarter**, e.g. `Archiwum Q1 2026` (the name pattern is in config). The bot creates the category when missing. The channel and its thread become read-only and locked. If a quarter reaches Discord's 50-channel limit, an overflow category `Archiwum Q1 2026 (2)` is created (assumed). |
| Retention | Discord allows 500 channels per server, categories included, so only the **current quarter and the 3 before it** stay as channels (the number is in config). When a new quarter begins, the oldest kept quarter is moved into the database (see *Long-term archive*), its channels are deleted, and so are its now empty categories. |

### Long-term archive

Decided 2026-10-31.

| Topic | Decision |
|---|---|
| When | A scheduled action on the first day of each quarter, and again daily until every channel of the quarter that is leaving has been moved. |
| What is kept | The event's own data is already in the database (event, ORBAT, sign-ups, attendance). The bot adds the **transcript** of every message not posted by the bot, in the channel and in its thread: author id and name at the time, time, edit time, content, reply-to, and attachments. **Attachments are downloaded** into a folder from config (`<event id>/<message id>-<file name>`), because Discord's CDN links expire. |
| Order | Copy first, check it (the message count matches, and every attachment is on disk), then delete the channel. A failure leaves the channel in place, is retried the next day, and goes to the staff log. |
| Reading it | `/archive event` with autocomplete (date and title) replies privately with an **HTML file** made from the database: the info, briefing, final ORBAT or sign-up list, the attendance report, and the transcript. Images are inlined when the file fits the server's upload limit; otherwise the reply is a ZIP with the HTML and the attachments; if even that is too big, the HTML lists the attachments by name and says they are on the host (assumed). It is open to the event's **participants** (anyone who held a slot, was *Coming*, *Maybe*, *Can't come* or on the reserve list), its organisers, and *manage any event* and *bot admin* roles (decided 2026-10-31); autocomplete offers only those events. There is no way to bring the channel back. |
| Backups | The daily backup covers the database only. The host's own backups must include the attachments folder, the briefing images folder and `.env` (deployment note in the README). |

Reading members' messages for the transcript needs the privileged **Message Content** intent, as well as Server
Members (both are switched on in the Discord developer portal).

### Creating an event

| Topic | Decision |
|---|---|
| Entry point | A **header channel** in the events category, with instructions and a persistent *Create event* button (it keeps working across bot restarts). |
| Calendar | Decided 2026-10-31. The header message also lists the **published events of the next N days** (config; assumed 14): date as a Discord timestamp, kind, title, taken / total slots (in a Zeus event, *Coming* and the limit), and a link to the channel. It is edited whenever one of them changes, batched like the other edits. `/events` shows the same list privately, plus the member's own sign-ups. |
| Flow | A short step-by-step **ephemeral wizard** (selects, buttons, modals). It creates a **draft**; everything else is filled in the **organiser panel**. The wizard's answers live in memory until its last step creates the draft: after a restart its buttons say the session expired and to start again (decided 2026-10-31). The panel's buttons carry the event id in `custom_id`, so they keep working. |
| Wizard fields | Kind, then the host group (guest) or the invited groups (hosted, optional at this point), title, mission type, climate, date, assembly time, start time, duration. |
| Who | Any member with one of the group's **member roles** (config, a list, e.g. *Klanowicz*); guest events only the mission maker role and above. Whether the event then needs approval depends on the creator (see *Approval*). |
| Kind | *Internal*, *Guest*, *Hosted* or *Zeus* (see *Event kinds*). |
| Other groups | Picked from the known groups, or added on the spot (see *Event kinds*). |
| Mission type | Picked from a **fixed list in config**. For display only; it changes no rules. |
| Climate | Picked from the **climate roles** in config: Discord interest roles such as @WW2, @Cold War and @Modern, which members assign themselves. Shown in the info embed, and mentioned in the announcement and the sign-up-open ping. |
| Author | The mission maker. It defaults to the creator and can be changed in the panel to a member with the **mission maker role** (config). |
| Date | `dd.mm` means the **next** such date: this year if it hasn't passed, otherwise next year. `dd.mm.yyyy` is accepted too. A date more than **N days** ahead (config; e.g. 120) is refused as a likely typo. |
| Times | Assembly ("zbiórka", the briefing) and start, typed as `HH:MM` and read in the **config time zone** (e.g. `Europe/Warsaw`). The assembly is on the event date; a start earlier in the day than the assembly is on the **next** day (assembly 23:30, start 00:15; decided 2026-10-31). They are echoed back as Discord timestamps, which make the day plain. |
| Duration | Pre-filled with the config default (e.g. 3 h), and editable. It drives the reservation and the attendance fallback. |
| On finish | Create the channel, hidden: visible to the creator, co-organisers, *manage any event* roles and *approve events* roles. Post the messages, marked **DRAFT**. For an internal, hosted or Zeus event created by someone who may publish without approval, create the reservation now; otherwise it is created when the event (or, for a member's Zeus event, its reservation) is sent for approval (see *Approval*). A guest event never has one. |

### Draft and publish

| Topic | Decision |
|---|---|
| Draft | The channel is hidden from members while the organisers prepare it. When the reservation already exists (see *On finish*), the time slot is held and a clash shows up early. |
| Publish | *Publish* in the panel, for creators who may publish (see *Approval*); the others get *Send for approval* in its place. Both are refused until the **briefing** and the **ORBAT** exist (a Zeus event has no ORBAT, so only the briefing; a guest event also needs the *How to join* text); the modset preset is optional (some missions are vanilla). The panel shows a checklist of what is missing. Publishing makes the channel visible (read-only), creates the discussion thread, and posts the announcement. |
| Stale draft | Still a draft, or still waiting for approval, **N hours** before the assembly (config; e.g. 48 h): the creator is pinged in the draft's thread, and the staff log is told; a waiting event is also re-posted to the moderation thread. Still not published at **M hours** (config; e.g. 24 h): the event is cancelled automatically, any reservation is deleted, and the **channel is deleted** (a draft has nothing worth archiving). |
| Sign-up start | Optional, set in the panel as a date and time (config time zone). Without one, sign-up opens at publish (with no ping of its own: the announcement is the ping); a time before the publish moment also means "at publish". Until it opens, the info embed shows *Sign-up opens \<t:…:R\>* and *Sign up* / *Reserve* / `/slot take` (in a Zeus event, *Coming*) are disabled, while *Maybe* and *Can't come* (*Not coming*) work. When it opens, the announcement role and the climate role are pinged in the event channel. There is no priority window. |

### Approval

Decided 2026-10-31.

| Topic | Decision |
|---|---|
| Who needs it | Internal and hosted events whose *Publish* is pressed by someone with only the **member role**. The roles of **the person pressing** count, not the creator's: a mission maker, *manage any event* or *approve events* role pressing *Publish* (e.g. as a co-organiser) publishes at once, and the staff log notes who did (decided 2026-10-31). A moderator never approves their own event, they just publish it. **Guest events** can only be created by the mission maker role and above, so they never need it. |
| Zeus | A member's **Zeus event** is published at once, but its **host reservation** needs approval (decided 2026-10-31): *Request the server* makes the reservation (so a clash shows in the request, as for the other kinds) and sends the same kind of request; a rejection deletes it, and until it is approved the info embed says the server isn't confirmed. Mission makers and above reserve at once. |
| Sending | *Send for approval* in the panel, once the publish checklist is complete. The event becomes **pending**. For an internal or hosted event the reservation is created now, so a clash shows up in the request. While pending, the details, briefing and ORBAT are **locked**; *Withdraw request* turns it back into a draft (and deletes the reservation) so it can be edited (decided 2026-10-31). |
| Kind | The kind can be changed **only in a draft** (decided 2026-10-31), before publishing or sending for approval, when there are no sign-ups yet; the reservation and profile are created or deleted to fit the new kind. After that, a wrong kind means cancelling and creating a new event. A member can't pick *Guest*. |
| Where | The **moderation thread** in the staff channel (see *Notifications*) gets an embed: title, kind, other groups, times, the creator, the reservation state, and a link to the hidden channel, which *approve events* roles can see. It has persistent *Approve* and *Reject* buttons; *Reject* asks for a reason in a modal. **Approve is disabled while the reservation failed** (shown with the reason); the moderator rejects, or resolves the clash and presses *Retry reservation*, which turns Approve on (decided 2026-10-31). |
| Approve | **Approving publishes** the event at once (one step for the moderator). The creator is pinged in the event's thread. |
| Reject | The event goes back to draft with the reason shown in the panel, the creator is pinged, and the reservation is deleted. |
| Changes after approval | Changing the **date, times or duration** of an approved member's event is a **proposal**: the event and its reservation stay on the old times until a moderator approves it (decided 2026-10-31). The request shows old and new times; approving moves the event and the reservation (a clash shows as 409 in the request, and Approve stays off); rejecting changes nothing, and the creator is pinged with the reason. Briefing, ORBAT, preset and the rest change freely, as *Edits after publish*. |
| Log | Requests, approvals and rejections go to the staff log, with who decided. |

### Organiser panel

| Topic | Decision |
|---|---|
| Where | The *Organiser panel* button on the info embed. Only the creator, co-organisers and *manage any event* roles can open it; it opens as an **ephemeral** message with a button per section. |
| Details | Title, kind (draft only), host group or invited groups, mission type, climate, author, date, assembly, start, duration (the same validation as the wizard). The date and times are **locked from the start of the reservation's quiet period** (reservation start − 30 min), because the supervisor no longer moves an occurrence under way (decided 2026-10-31). A new date or host group **renames the channel**, and the reservation, reminders and sign-up start move with it. Discord allows only 2 channel renames per 10 minutes, so a rename may be queued while the messages update at once (assumed). |
| Briefing | A modal with **Situation**, **Mission** and **Notes** fields (Discord markdown, up to 4000 characters in total), shown in the briefing message. An optional banner or map image is uploaded with `/event image file:<png/jpg>`. The bot **stores the file on its disk** (a folder from config) and attaches it to the briefing message itself (`attachment://` in the embed), re-attaching it on every edit, so it never depends on the organiser's message or an expiring CDN link (decided 2026-10-31). |
| ORBAT | Pick a **template**, or **paste text** (see *ORBAT format*); editing pre-fills the modal with the current ORBAT. Pasted text can be saved as a new template. Also here: lock, assign and remove slots, and see slots flagged ⚠ (holder lost the required role). |
| Modset | The panel explains the upload: `/event preset file:<.html>` in the event channel, with an Arma 3 Launcher preset; running it again replaces the preset. The bot parses it, reporting mod names, Workshop ids and Creator DLC; the parser is a port of the supervisor's `app/steam/presets.py`, including its Creator DLC table. A file without mods or DLC is refused. The info embed shows a summary (the preset name, N mods, the DLC required), and the preset message carries the `.html`. The server's mods stay managed in the supervisor. |
| Sign-up | The sign-up start (see *Draft and publish*). |
| How to join | Guest events only: one text field (server address, password, the host's TeamSpeak / Discord, notes), shown in the info embed. |
| Sign-up limit | Zeus events only: an optional limit on *Coming* (see *Zeus sign-up*). |
| Reservation | Internal, hosted and Zeus events. Its state and any failure reason, *Retry reservation*, and *End reservation* (for missions that never "finish", which is every Zeus event). |
| Server profile | Internal, hosted and Zeus events, and only for organisers with the **mission maker role** and *manage any event* roles. Covers the mission (upload or pick), the hostname, max players and signatures, the supervisor's mission report and warnings, *Build profile*, and the build job's progress (see *Supervisor: event profile*). |
| Other | Co-organisers (add, remove), *Publish* or *Send for approval* (or *Withdraw request*), *Request the server* (a member's Zeus event), *Cancel event* (before the start; after it, *End event*). |
| Edits after publish | **Silent**: the messages are updated and nobody is pinged. The exception is ORBAT changes, which keep their rule for lost slots. |

### ORBAT and slots

| Topic | Decision |
|---|---|
| Who may sign up | Members with one of the **sign-up roles** (`.env`, e.g. *Klanowicz*, *Kadet*; decided 2026-10-31). Anyone else sees the event, and the buttons answer privately that sign-up is for members. An organiser can still assign a slot to anyone on the server (e.g. a guest Zeus). |
| Lost the sign-up role | Treated like leaving the server: on events that haven't started, the member's slot, *Coming* or reserve place is freed (and offered on), while slots an organiser assigned (`locked`) stay. The staff log gets the list (decided 2026-10-31). |
| Our part only | In guest and hosted events the ORBAT holds **only our slots**. Other groups' slots aren't in the bot, and their members never sign up through it. |
| Templates | **Shared**: every event creator sees and uses all of them. The member who saved one, and *manage any event* roles, can rename, overwrite or delete it. An event **copies** the template, so later template changes don't touch existing events. |
| Restrictions | A slot may require a **Discord role** (`[Role]` in the format). The name is matched when the ORBAT is parsed, and the slot stores the role's **id**, so renaming the role later changes nothing; editing the ORBAT shows the current name. Slots without one are open to every member who may sign up. |
| Manual lock | The organiser can lock a slot **closed** (nobody may take it, e.g. Zeus; `!` in the format) or **assign** it to a named member. Members can't take or leave a locked slot themselves. |
| RSVP | **Taking a slot means attending.** One slot per member per event; taking another one moves them. *Maybe* and *Can't come* put the member on those lists and free their slot. |
| Creator DLC | Decided 2026-10-31. Members' DLC are **Discord roles**; `.env` maps each Creator DLC of the preset parser's table to its role. When the event's preset needs a DLC a member's roles don't show, taking a slot (or *Coming*) still works, but the reply warns them privately, and the panel lists the holders without it. |
| Overlapping events | Taking a slot is **refused** if the member holds a slot in another event whose window (assembly → start + duration) overlaps. The reply links the other event. |
| Full | When every slot is taken, *Reserve* puts the member on the reserve list. |
| Picking | (a) The *Sign up* button opens an ephemeral two-step select: squad, then free slot. This works around Discord's limit of 25 options per select. (b) `/slot take` with autocomplete. |
| Leaving | Allowed until the **start time**; sign-up also closes then. A freed slot is **offered** to the first member on the reserve list who meets its role rule: they get a **DM** with a *Take it* button, or, with DMs closed, a ping in the event's thread (decided 2026-10-31). If they don't answer within the offer timeout (config; assumed 30 min), the offer goes to the next one. Near the start (decided 2026-10-31): an offer lasts at most until the start; in the last hour before the assembly a freed slot is offered to the **whole** reserve list at once (one ping in the thread), and the first to confirm takes it; after the start there are no offers. |
| Holder left the server | The slot is freed and offered to the reserve list. |
| Holder lost the required role | The slot is **kept** but flagged ⚠ in the panel; the organisers decide. |
| ORBAT edited after sign-ups | Slots are matched by squad name, role name and position among same-named slots (*Rifleman x4*), and holders of a matching slot keep it. A member whose slot disappeared goes to the **top of the reserve list** and is pinged. |
| Concurrency | Claiming a slot is one atomic database update ("take it if still free"), so two members pressing at the same moment can't both get it. Message edits are **batched** (assumed: at most one edit per message every 2 s) to stay within Discord's rate limits during the rush when sign-up opens. |

### ORBAT format

A line starting with `#` is a squad; every other non-blank line is a slot. Markers on a slot line:
`[Role]` at the end requires that Discord role (matched by name), `xN` repeats the slot N times, and a leading `!`
locks it closed.

```
# Command
Platoon Leader [Officer]
!Zeus

# Alpha
Squad Leader [NCO]
Medic [Medic]
Autorifleman
Rifleman x4

# Eagle 1-1
Pilot [Pilot]
Co-pilot [Pilot]
```

The modal takes at most 4000 characters. A role name that doesn't exist on the server, an empty squad, or a slot
before the first squad is a validation error that names the line.

### Zeus sign-up

Decided 2026-10-31. A Zeus event has no ORBAT, only a list of who is coming.

| Topic | Decision |
|---|---|
| Buttons | *Coming*, *Not coming* and *Leave* on the sign-up message, which lists both groups of members. Pressing the other button moves the member; pressing the same one again only answers privately that they are already on that list; *Leave* takes them off both (decided 2026-10-31). No *Maybe*, no reserve list. |
| Limit | Optional, set in the panel; none by default. When *Coming* reaches it, *Coming* is disabled until someone leaves. No offers, no reserve. Lowering it below the number already coming keeps everyone. |
| Rules kept | Sign-up start, closing at the start time, and the overlap check work as for slots: *Coming* counts as holding a slot. |
| Pings | Reminders mention everyone *Coming*, as they do slot holders. |
| Max players | The event profile's default is the mission's playable slots, as for any event; without a report, the limit, else the base profile's value (assumed). |
| Attendance | Those *Coming* are treated as slot holders in the report; others who played are *played without a slot*. |

### Who may do what

| Who | What |
|---|---|
| Creator | Edit and cancel the event, lock and assign slots, remove members from slots, add **co-organisers**, correct attendance. |
| Co-organisers | The same rights as the creator, on that one event. **Anyone on the Discord server** can be one (e.g. a guest Zeus from another group); on a draft they get a permission overwrite for the hidden channel. |
| Roles with *manage any event* (config) | The same, on every event; publish without approval; edit and delete any ORBAT template; correct other groups' names. |
| The *member role* (config, e.g. *Klanowicz*) | Use the wizard for internal, hosted and Zeus events. Their internal and hosted events need approval before they are published. |
| The *mission maker* role (config) | Use the wizard and publish without approval. As organisers, build the event's supervisor profile and upload missions. |
| Roles with *approve events* (config) | See pending events, approve or reject them in the moderation thread; publish their own events without approval. |
| Roles with *server control* / *server message* (config) | The `/server` commands. |
| Roles with *server override* (config) | *Start anyway?* during a reservation. |
| Roles with *manage links* (config) | Set, change or remove anyone's Steam link and nick. |
| Roles with *bot admin* (config) | `/reload`, `/attendance report`, `/privacy erase`. |
| Everyone with a sign-up role | Sign up; `/events`, `/link`, `/privacy`, `/archive event` (their own events); `/attendance`, `/profile`, `/mission-maker` and `/group` with the limits in *Who sees what*. |

The role → permission mapping lives in `.env`, as lists of role ids or names. Every Discord user acts on the supervisor through the bot's
one supervisor account, so these checks are the only thing limiting what each member can do there.

### Notifications

| What | When |
|---|---|
| Announcement | On **publish**: a short notice in the **announcements channel** (config), mentioning the announcement role (config) and the event's **climate role**, and linking to the event channel. It is edited when the event changes or is cancelled. |
| Sign-up open ping | When sign-up opens: the announcement role and the climate role, in the event's thread. |
| Reminder ping | In the event's thread, mentioning every slot holder (in a Zeus event, everyone *Coming*), at the **offsets before the assembly** set in config (e.g. 24 h, 1 h). |
| Approval request | In the **moderation thread**, see *Approval*. |
| Staff threads | Decided 2026-10-31. The staff log and the moderation requests are **threads** in one staff-only channel (config, `BOT_CHANNELS__STAFF`; thread names in config). The bot makes them, remembers their ids, unarchives one Discord archived after a quiet week before writing, follows a renamed thread name, and makes a new one when the old is deleted or the staff channel changes. |
| Staff log | The **staff log thread** gets stale drafts, approval decisions, reservation failures and auto-ends, `/server` use and overrides, Steam link changes and duplicates, and attendance reports. |

A ping that mentions members (reminders, cancelling) is **split** when it would pass Discord's 2000 characters:
the first message has the text and the first mentions, the next ones only mentions (decided 2026-10-31).
A member with both mentioned roles gets one notification, because Discord notifies once per message. There are
no DM **event** reminders and no unfilled-slot warnings; the only DMs are Steam link reminders.

### Cancelling

The info embed shows **CANCELLED** with the reason, slot holders are pinged in the thread, the announcement is
edited, the supervisor reservation (if any) is deleted, then the event profile, in that order (the supervisor
protects a profile a reservation still uses), and the channel is archived straight away. Cancelling a **draft**
deletes its channel instead. A reservation already in its quiet period can't be deleted (409 "under way"), so
the bot first ends it by id, then deletes it. **After the start** there is no *Cancel*: the panel offers *End
event*, which ends the reservation and runs the attendance as usual (assumed).

### Scheduled actions and downtime

Every timed action is a row in the database, not an in-memory timer, so a restart loses nothing. The timed
actions are: the sign-up opening, the reminder pings, reserve-offer expiry, stale-draft reminder and cancel,
reservation retry, the attendance check, the archive move, moving the oldest quarter into the database, and deleting links of members long without a
rank. A loop runs whatever is due. Persistent views /
`DynamicItem` (ids in `custom_id`) keep every button working after a restart.

| After downtime | Rule |
|---|---|
| Actions that change state (sign-up opening, archive, stale-draft cancel, attendance, reservation retry, offer expiry, profile swap and cleanup, the quarter move, link deletion, the end of the rating window) | **Always run late.** |
| Reminder ping | Skipped once a later reminder or the assembly has passed. |
| Sign-up-open ping | Skipped if more than 1 h late (the opening itself still happens). |

### Supervisor: reservation

| Topic | Decision |
|---|---|
| What | One one-off reservation per **internal, hosted or Zeus** event (`repeat="once"`); guest events have none. It is created when the wizard finishes if the creator may publish, so a draft holds its slot, and otherwise when the event is sent for approval; a rejection deletes it (see *Approval*). Its `name` is the channel name and title, cut to 80 characters (e.g. `18-10-ibc Operation X`). `server_id` is the default event server's slug (the API accepts an id or a slug). |
| Window | From **assembly − lead time** (config; e.g. 45 min) to **start + duration + buffer** (config; e.g. 30 min). The supervisor (re)starts the event server at the reservation start, kicking anyone on it, so the lead time makes the server come up fresh before members join. The supervisor's own quiet period (30 min) and countdown (15 min) on the other servers come before that. |
| Edit or cancel | Changed times move the reservation (`PUT`, which replaces the whole reservation, so the bot always sends every field). From the start of the quiet period the supervisor keeps an occurrence's times ("the change applies from the next one", and a one-off reservation has none), so the bot locks the times then (see *Organiser panel*). Cancelling, including a stale draft's auto-cancel, deletes it; under way, it is ended by id first, as `DELETE` answers 409 then. |
| Ending early | When the event's attendance trigger fires (see *Supervisor: attendance*, *Trigger*), the bot calls `POST /api/reservations/{id}/end`; without `reservation_end_by_id`, `POST /current/end` after checking that `GET /current` returns this event's `reservation_id`. *End reservation* in the panel does the same by hand. Both are logged to staff. |
| Failure | The event is created **anyway**. The staff log and the organiser panel say "no reservation: *reason*". If the supervisor was unreachable, the bot retries automatically. On 409 (overlap), staff resolve the clash and press *Retry reservation*. |

### Supervisor: event profile

From what the mission maker provides, the bot builds a **supervisor profile** for the event and puts it on the
event server for the reservation. It is optional: an event without one runs on the server's normal profile.

| Topic | Decision |
|---|---|
| Who | Organisers with the **mission maker role**, and *manage any event* roles. Mission uploads need the same. |
| Built from | The bot **clones a base profile** named in config (e.g. `IBC base`, which holds difficulty, BattlEye, admins, headless clients, CBA settings) with `POST /api/profiles/{id}/clone`. It then imports the event's **launcher preset** as the mod list (`POST /api/profiles/{id}/mods/import-preset`, mode `replace`, which keeps the base's server-side mods) and sets the event's **mission** as the only entry in the mission cycle (`PATCH /api/profiles/{id}/config/server` with `missions`, `hostname`, `max_players` and `verify_signatures`; the route deep-merges dictionaries and replaces lists, so the cycle becomes exactly this one mission). The base must not be protected; cloning a protected profile yields a protected clone, which would need `profiles.protected`. Without a preset the profile keeps the base's mods (a vanilla mission). A mission is required. |
| Name | `EVT <channel name> <title>`, cut to 100 characters (e.g. `EVT 18-10-ibc Operation X`). |
| Settings the mission maker may change | **Hostname**, defaulting to our group's acronym and the title (e.g. `IBC \| Operation X`); **max players**, defaulting to the mission's playable slots from its report (headless clients take a slot when the mission has HC modules, so these are counted there), or, without a report, the number of ORBAT slots plus the base profile's headless clients; **signatures** on or off (`verify_signatures` 2 / 0). Everything else comes from the base. |
| Mission | Either uploaded with `/event mission file:<.pbo>` (to `POST /api/missions`), or picked from the missions already on the server (`GET /api/missions`, autocomplete). Discord limits uploads to 10 MB (more on boosted servers), so bigger missions go onto the server another way and are then picked. |
| Same file name | Overwriting (`overwrite=true`) is allowed only when **no other profile** uses that mission: `GET /api/missions` lists the profiles using each mission. Otherwise the upload is refused and the organiser renames the file (e.g. `_v2`). The supervisor itself refuses replacing a mission a running server has open, and it keeps backups of edited missions. |
| Validation | The supervisor's mission report (`GET /api/missions/{name}/report`, also returned by an upload) is shown in the panel: title, author, terrain, playable slots, status, and the mods / DLC its addons need. Warnings appear for needed mods or DLC missing from the preset, fewer playable slots than ORBAT slots, and an error status. Warnings **don't block** publishing; they also go to the staff log. While the supervisor's addon index is being built (the report's `index.job_id`), the panel says the mod warnings are incomplete; the bot follows that job and reads the report again when it ends. |
| When built | *Build profile* in the panel, enabled once a mission is set. Publishing builds it too if it hasn't been built yet. Downloads therefore start days ahead. Changing the preset, mission or settings later **rebuilds** it: the same profile is updated, and the preset is imported again. |
| Downloads | `download=missing`: only mods not yet on the server are downloaded. They go into new folders, so no running server is stopped. Updates of installed mods are left to the supervisor's own update tasks; the panel notes outdated mods. The import is a supervisor **job**: the bot follows it (`GET /api/jobs/{id}`), shows its progress in the panel, and flags a failure to the organisers and the staff log. A job submitted during another reservation is held by the supervisor until that one ends. |
| On the server | **With the supervisor's `reservation_profile` capability:** the bot sets the reservation's `profile_id` to the event profile, and the supervisor starts the event server with it at the reservation start; the server's assigned profile is never touched. **Fallback without it:** at **reservation start − N min** (config; e.g. 5), the bot sets the event server's assigned profile to the event profile (`PATCH /api/servers/{slug}` `profile_id`) and remembers the previous one. The reservation then starts the server with it. After the reservation ends, the previous profile is assigned again, but only if the server still has the event profile (assumed: a manual change in between is left alone). The supervisor leaves the event server running at the end, so the event profile keeps running until the next restart. If no profile was built, or the build failed, nothing is swapped and staff are warned. |
| Cleanup | The profile is deleted **N days** after the event (config; e.g. 14) unless staff mark it *keep* in the panel. Cancelling an event deletes it at once. The supervisor refuses to delete a profile a server is running, so a refused delete is retried **daily** (a scheduled action); after 7 refusals in a row the staff log is told, and the retries go on. The bot never restarts a server to free the profile. Uploaded missions and downloaded mods stay on the server. |

### Supervisor: server control

| Topic | Decision |
|---|---|
| Commands | `/server start`, `stop`, `restart`, `say`, with the server picked by autocomplete from `GET /api/servers`. |
| Safety | Stop and restart ask for confirmation (an ephemeral button) that shows how many players are online. |
| During a reservation | A refused start or restart (409 with `reservation`) is shown with the reservation's name and end time. Members with the **server override** role get *Start anyway?*, which resends with `override=true`. |
| Log | Every use goes to the staff log, and overrides are marked. |

### Supervisor: attendance

| Topic | Decision |
|---|---|
| Which events | Internal, hosted and Zeus events. A **guest** event is on another group's server, so nothing is read: at start + duration + buffer the event becomes `ended` and the bot posts the report with every slot holder as **to confirm**; the organisers set attended, no-show or excused with *Edit attendance*. After 24 h without it the organisers are pinged in the thread; after N days (config; e.g. 3) everyone still *to confirm* becomes **attended** (a sign-up means attending), noted in the staff log (decided 2026-10-31). Those events count in `/attendance` like the others. |
| Identity | The member's linked **Steam64 ID** (see *Steam link and squad.xml*). The bot asks for it on a member's first sign-up if it is missing, but signing up does not require it. |
| Window | From the reservation's start (without a reservation: assembly − lead time) to the trigger. |
| Plays | With `attendance_window`: one `GET /api/stats/attendance?server=<slug>&from=&to=` gives the plays and every player's `mission_s` in the window. Fallback: the **mission plays** overlapping the window from `GET /api/stats/missions?server=<id>&all=true` (numeric id only, looked up in `GET /api/servers`; no time filter, newest first, 100 per page, so the bot pages until it is past the window and filters the plays itself). |
| Trigger | **Whichever comes first:** (a) a play in the window ends `finished`, and **no new play starts** on the event server within the settle delay; (b) the reservation ends, at its end time, through *End now*, or through *End reservation*. A play that ends `crashed`, `server_stopped`, `lost` or `next_mission` does **not** trigger (a), so a crash with a restart or a lobby mission doesn't end the event. With neither (no reservation, and the mission never started), the **fallback** is start + duration + buffer. A **Zeus** event ignores (a): only the reservation's end (or the fallback) triggers it. For an evening with several missions (decided 2026-10-31): (a) counts only after start + a share of the duration (config; e.g. 60 %), and the panel has *Don't end automatically*, which turns (a) off for that event. A `finished` play **before** that point is remembered: when the point passes with no new play since, and the settle delay is over, the event ends then; a new play in between forgets it (decided 2026-10-31). |
| Stats ready | There is **one settle delay** (config; assumed 10 min), used once (decided 2026-10-31). Arma on Windows writes its console log late, so the supervisor can record the last sessions after the end. For trigger (a) the wait for a new play is that delay, so the bot ends the reservation and reads the stats straight after it. For trigger (b) and the fallback, the bot waits the settle delay after the end, then reads the stats. If the supervisor is unreachable, it retries until a **deadline** (config; assumed 24 h after the trigger), then reports "stats unavailable". |
| Counted | A player's **mission time** in the window: `mission_s` from the attendance window, or, in the fallback, the sum of `seconds` from `GET /api/stats/missions/{id}` over the plays (both need `players.view` for Steam IDs). At least **N minutes** (config) counts as attended. |
| Report | For slot holders: **attended**, **no-show**, **not linked**. Then **played without a slot**: linked members by Discord name, and unknown Steam IDs by in-game name, except in a **hosted** event, where they are the invited groups' players and the report only says *guests: N players*. Before that, an unknown Steam ID whose in-game name matches the nick or Discord name of an **unlinked** slot holder is listed as *probably \<member\>*, with a *Link* button for the organisers that creates the Steam link and counts the time (decided 2026-10-31). Posted in the event channel and the staff log. |
| Corrections | An *Edit attendance* button on the report (organisers only) opens a select per status, so members can be moved between attended, no-show and **excused**. Each correction is stored with who made it, and the report message is updated. |
| Use | `/attendance @member`: their recent events, attendance rate and no-shows. `/attendance report <period>` (bot admin): totals per member for a period. |
| Who sees what | Decided 2026-10-31. About **themselves** every member sees everything. About **others**, a member with the member role sees only the basics: the nick, roles, the number of events attended and the attendance rate. The details (the per-event history, no-shows and excused, the Steam ID, ratings with comments) are for the administration: *manage any event*, *approve events* and *bot admin* roles (assumed). This applies to `/attendance` and `/profile` alike, and to `/mission-maker` (members see the averages and counts, the author and the administration also the comments) and `/group` (members see the number of events with that group, the administration also the cooperation ratings). |
| Member profile | Decided 2026-10-31. `/profile @member` gathers in one private reply: the squad.xml nick and Steam link, their roles as the bot reads them (rank, specializations, climates, DLC), attendance (rate, recent events, no-shows), and the average rating of missions they authored. |
| Mission feedback | Decided 2026-10-31. The report carries *Rate the mission*: members marked **attended** give 1–5 stars and an optional comment (a modal), for N days (config; assumed 3), and can change their rating until then. The report shows only the average and the number of ratings; the ratings with their authors and comments are seen by the mission's author and the organisers (in the panel). `/mission-maker @member` shows the averages of their missions. Corrections that remove *attended* also remove that member's rating. It applies to internal, hosted and Zeus events. A **guest** event asks instead *Rate the cooperation with \<group\>* (1–5 and a comment), seen only by staff, open at once to everyone signed up (attendance there is confirmed by hand later); `/group <acronym>` shows each other group's events with us and its average (decided 2026-10-31). |
| Archive | The channel is archived the configured delay **after the report**, so members see it in place. If the report can't be made by the normal archive time (start + duration + archive delay), the channel is archived anyway, and the report is posted into the archived channel when it comes. |

### Steam link and squad.xml

The bot keeps the group's Arma **squad.xml** in step with Discord roles. The same Steam link also serves
attendance.

| Topic | Decision |
|---|---|
| Linking | `/link <steam64 or profile URL> <nick>`, or the *Link now* button in a reminder, which opens a modal. Accepted forms: a 17-digit Steam64 ID and `steamcommunity.com/profiles/<id>`. A custom URL (`steamcommunity.com/id/<name>`) is refused with a short how-to for finding the Steam64 ID; the bot doesn't use the Steam Web API, so it needs no key (decided 2026-10-31). An ID the supervisor hasn't seen is **still linked**; when the supervisor has seen it, the reply shows the last in-game name. The reply also gives the squad.xml URL (config) to put in the Arma profile. |
| Privacy notice | The `/link` modal and reply say plainly what is **published** in squad.xml (Steam ID, nick, Discord username, remarks) and at which URL. A member who doesn't want that doesn't link, and `/link remove` deletes the link. |
| Nick | Typed at `/link`; it is the `nick` in squad.xml. Staff can change it. |
| Changes | Members can relink themselves, and each change is posted to the staff log. Roles with **manage links** can set, change or remove anyone's link and nick with `/link set @member` / `/link remove @member`. |
| Uniqueness | One Steam ID belongs to one member. A duplicate is refused, and staff are told in the staff log. |
| Lost rank or left | The member drops out of squad.xml at once. After **N days** without a rank role or off the server (config; e.g. 90), the link itself is deleted and the staff log is told; the attendance history keeps the Steam ID, so it stays readable (decided 2026-10-31). Regaining a rank within N days changes nothing. |
| Reminder | Applies to a member with a membership rank role and no link. They get a **DM** with a *Link now* button when they gain the rank, then again every **N days** (config) until they link. If their DMs are closed, they are pinged in a **reminder channel** (config) instead. |
| First rollout | On the first run the bot posts **one announcement** in the reminder channel with a *Link now* button. Individual DMs start N days later for members still missing a link, **throttled** (assumed: at most 20 per hour). |

squad.xml:

| Topic | Decision |
|---|---|
| Scope | **One file for the group.** |
| Members | Members with any of the configured **membership rank roles** and a linked Steam ID. A member without a link is left out. |
| `<squad nick>`, `<name>`, `<email>`, `<web>`, `<picture>`, `<title>` | From config. `picture` is the file name of the logo `.paa` that IIS serves. |
| `<member id>` | Steam64 ID. |
| `<member nick>` | The nick typed at `/link`. |
| `<remark>` | The configured **remark roles** the member has (e.g. Pilot, Medic), all of them, joined with `, ` in config order. Empty if none. The rank itself is not in the remark. |
| `<icq>` | The member's Discord **username**. |
| `<member><name>`, `<email>` | `N/A` (assumed). |
| Order | Members sorted by nick (assumed). |
| Output | The bot writes **only `squad.xml`** to a directory from config. IIS hosts that directory with `squad.dtd`, `squad.xsl` and the logo, which are maintained by hand. The write is atomic (temp file, then replace), so IIS never serves a half-written file. Text is XML-escaped and UTF-8. |
| When | On every relevant change: a link or nick changes, a member gains or loses a rank or remark role, or a member leaves. Changes are **debounced** (assumed 30 s), so a bulk role edit writes once, and the file is written only if its content changed. A full rebuild runs at startup and on `/reload`. |

Role changes and departures arrive as member events, so the bot needs the privileged **Server Members** intent
(enabled in the Discord developer portal).

### Language

Polish and English. Private replies (the wizard, the panel, the picker, errors) follow each member's **Discord
locale**. Public messages (the event messages, announcements, pings, reports) use the **one language set in
config**. Slash command names and descriptions are localized through `app_commands.Translator`.

### Configuration and deployment

| Topic | Decision |
|---|---|
| Config | **Everything in `.env`** (decided 2026-10-31), read with pydantic-settings and validated at startup, following the supervisor's convention: one prefix (assumed `BOT_`), `__` for nested fields, lists as JSON or separated by `;`, real environment variables override the file. Channels and roles are given by Discord **id or name** (decided 2026-10-31): names are matched on the server, ignoring case, at start-up and on `/reload`; a name that matches nothing or several is reported, and an id never breaks on a rename (Developer Mode → *Copy ID*). `check-config` also lists unknown `BOT_` variables (typos, renamed settings). A `.env.example` lists every setting with its default and a comment. Its contents are listed under *Config contents*. |
| `/reload` | Only the **bot admin** role. The bot reads `.env` again and **validates it first**: on error nothing changes, and the errors are shown. An item removed from the config that existing events still use (a mission type, climate role, or role) stays readable on those events, because events store it **by value**, but it can't be picked for new ones. The reply lists the affected events. The token and the supervisor URL need a restart. |
| Secrets | The Discord bot token and the supervisor API token, in the same `.env`. |
| Check at startup | The bot checks that every configured channel and role exists, and that it has the Discord permissions it needs (View Channels, Send Messages, Send Messages in Threads, Create Public Threads, Manage Threads, Embed Links, Attach Files, Read Message History, Manage Channels, Manage Roles for channel overwrites, and *Mention @everyone, @here and All Roles* unless the announcement and climate roles are mentionable). What is missing is logged and posted to the staff log (assumed). |
| Command visibility | Decided 2026-10-31. The staff commands (`/reload`, `/server`, `/link set` / `remove`, `/attendance report`, `/privacy erase`) carry `default_permissions` (Manage Server), so only admins see them at first; the server owner shows them to the roles from `.env` in *Server Settings → Integrations* (a setup step in the README). Members' commands are visible to all. The bot checks its own permissions on every use either way. |
| What the bot sets up itself | The event channels with their permission overwrites, the archive categories, and the *Create event* message in the header channel (posted at startup when missing, then edited in place; its id is stored). The events category's own permissions (members read, don't write) are set by hand once (assumed). |
| Host | The same machine as the supervisor (`http://localhost:8000`). |
| Guild | **One** Discord server. Slash commands are synced to that guild only. |
| Database | SQLite through SQLAlchemy 2.0 async + aiosqlite. Schema changes are **Alembic** migrations, applied at startup. A **daily online backup** (the sqlite3 backup API) goes into a folder from config, keeping the last N. |

### Privacy

Decided 2026-10-31.

| Topic | Decision |
|---|---|
| Retention | Nothing is deleted on a timer: events, sign-ups, attendance, ratings and archived transcripts are kept **without limit**. The exceptions already decided stay: application logs (14 days) and a Steam link N days after its member lost the rank. |
| Erasure | On request, a *bot admin* runs `/privacy erase @member` (or with a user id, for someone who has left). The Steam link is deleted and the member drops out of squad.xml. Their Discord id **and Steam ID** are replaced by one fixed pseudonym `former:<n>` in **every** table (slots, sign-ups, attendance, ratings, whose comments are emptied, organisers, creator and author fields, approval decisions, archived messages), so past events still add up but nothing points back to the person; in archived transcripts their messages keep their place with the author "deleted" and empty content, and their attachments are deleted from disk. Database backups are not rewritten; they age out with the backup rotation, and `/privacy` says so (decided 2026-10-31). The staff log records that an erasure ran, without the person's data. |
| Information | `/privacy` explains privately what the bot stores (Steam ID, sign-ups, attendance, ratings, messages in event channels once archived), what it publishes (squad.xml), that it keeps it without limit, and how to ask for erasure. The text is a file staff can edit (Polish and English). `/link` and the header message point to it. |

### Moving over from the old bot

Decided 2026-10-31. The group runs an earlier bot, `D:\Workspace\python\IBC_BOT` (cogs, `data.json` with 550
past events, `data/steam_ids.json`, `data/clan_members.json`, and its own squad.xml export).

| Topic | Decision |
|---|---|
| What is imported | Only the **Steam links**: `data/steam_ids.json` (Discord id → `steam_uid`, `game_nick`) becomes `steam_link` rows, with `game_nick` as the squad.xml nick. A one-off command, `python -m app import-old-links <path>`, refuses duplicates and invalid ids and prints what it skipped. Reminders then start only for members still without a link. |
| What is not | The old events (they have sign-ups but no attendance), its missions, and `clan_members.json`. Specializations, climate preferences and owned DLC are **Discord roles** already, so the bot reads them from the members' roles; other games are out of scope. |
| Switch-over | A set **switch-over day**. Events the old bot announced before it stay with the old bot until they end; from that day new events are made only in the new bot. squad.xml moves to the new bot at once, right after the import, and the old bot's export is turned off then, so only one program writes the file. The old bot is shut down after its last event. |

### Code and tooling

Decided 2026-10-31, following the supervisor's conventions where it has one.

| Topic | Decision |
|---|---|
| Layout | One package `app/`, run with `python -m app`, split **by domain**: `events`, `orbat`, `signup`, `approval`, `calendar`, `attendance`, `feedback`, `links`, `squadxml`, `archive`, `servers`. Each has a `service.py` (the logic and database work, no discord.py import) and a thin `cog.py` / `views.py`. Shared: `app/settings.py`, `app/db/` (engine, models, Alembic), `app/i18n/`, `app/scheduler.py` (the scheduled-action loop), `app/supervisor_client/`, and a batched message editor. |
| SQLite | Every connection sets `PRAGMA journal_mode=WAL` and `busy_timeout=5000` (as the supervisor's `app/db.py`). One slot per member per event is a **partial unique index** on `slot(event_id, holder_id) WHERE holder_id IS NOT NULL`; moving to another slot is one transaction. |
| Time zones | `tzdata` is a pinned requirement: on Windows `zoneinfo` has no system database without it. |
| Translations | As the supervisor: the English text is the key. Code calls `t("Sign-up opens {when}", locale, when=…)`; `app/i18n/pl.json` maps the English text to Polish with the same `{fields}`. A Polish value may be `{"one", "few", "many"}`, picked by a `count` field. A test collects every `t()` call from the code (AST) and fails on a missing translation or mismatched fields. The same catalog feeds `app_commands.Translator`. |
| Logging | `logging` to `data/logs/bot.log`, rotated daily, kept 14 days (`.env`). Each ERROR also goes to the staff log in short, with an id to find it in the file. |
| Service | NSSM, as the supervisor (`docs/DEPLOYMENT.md` there): automatic start, stdout / stderr to `data/service.log` for start-up failures, and a dependency on the supervisor's service (assumed). |
| Updates | Decided 2026-10-31. `update.ps1`: back the database up, `git pull`, `pip install -r requirements.txt`, restart the NSSM service; the migrations run at start-up. |
| Test instance | A separate test Discord server with a second bot token, its own `.env`, database and data folder. It points at the **fake supervisor** from the tests, run on its own, never at the real one, because a reservation takes the whole host. Bigger changes go through it before `update.ps1` runs on the real bot. |
| Tests | pytest. Services without Discord; the supervisor client against a fake supervisor (an aiohttp app); the Discord layer through light fakes of `Interaction`, `Member` and `TextChannel` that record sent and edited messages, modals and permission overwrites, with button and command callbacks called directly. No network, no live Discord. |

Config contents (all in `.env`):
- the Discord bot token, the supervisor API token;
- guild id; categories and channels (drafts, events, header, announcements, staff, reminder), the names of the
  staff log and moderation threads,
  and the archive category name pattern;
- the sign-up roles;
- our group (acronym, full name), mission types, climate roles (other groups live in the database);
- the role → permission mapping: approve events, manage any event, server control, server message, server
  override, manage links, bot admin; plus the member role, the mission maker role and the announcement role;
- times: reminder offsets, stale-draft hours (N, M), default duration, lead time, buffer, archive delay, offer
  timeout, settle delay, the share of the duration before `finished` ends an event, attendance deadline,
  attendance minimum, max days ahead, calendar days, rating days, days before a guest event's unconfirmed
  attendance counts as attended;
- public language and time zone;
- supervisor URL and default event server; the base profile, the profile swap lead (minutes before the
  reservation start), and how many days event profiles are kept;
- squad.xml: output directory, public URL, header fields, membership rank roles, remark roles, link reminder
  interval and channel, days without a rank before a link is deleted;
- the folder for briefing images;
- Creator DLC → role (for the DLC warning);
- backup folder and how many backups to keep;
- archive: how many quarters stay as channels (4), and the folder for the attachments of archived events.

## Data model (draft)

| Table | Holds |
|---|---|
| `event` | Phase 3 built its core (kind, title, status, assembly / start / duration, sign-up start and limit, the preset's Creator DLC, creator); phase 4 adds the rest. Kind (`internal` / `guest` / `hosted` / `zeus`), host group (guest), *How to join* text, sign-up limit (Zeus), title, type, climate (by value), author, creator, date and times (UTC), duration, status (`draft` / `pending` / `published` / `ended` / `cancelled`; `ended` is set by the attendance trigger: sign-up closed, the report still correctable), and apart from it `archived_at` (the channel moved to an archive category) and `exported_at` (moved into the database), the approval (what is asked: the event, a Zeus reservation, or a change of times with the proposed values; moderation message id, decided by, reason), *Don't end automatically*, a remembered early `finished`, sign-up start, briefing fields and image, preset (name, mods, DLC, file), channel / thread / message ids, reservation id and state, the time of the attendance trigger; the supervisor profile (id, build state, job id, mission template, hostname, max players, signatures, the previous profile of the event server, *keep*) |
| `host_reservation` | Built in phase 2. One per event (keyed by the event's id): the supervisor reservation's id, state (`booked` / `retrying` / `failed` / `ended` / `cancelling` / `cancelled`), the supervisor's error, and what was asked (name, times, link, profile). It holds the event's "reservation id and state" |
| `event_organiser` | Event, member (co-organisers) |
| `approval_request` | Built in phase 4a. Event, kind (`publish` / `server` / `times`), state (`pending` / `approved` / `rejected` / `withdrawn`), the proposed times, requested and decided by, reason, the moderation message. It holds the event's "approval" |
| `other_group` | Acronym, full name, added by |
| `event_guest_group` | Event, group (the invited groups of a hosted event) |
| `squad`, `slot` | Event, order, name; slot: required role (id), holder, `locked`, ⚠ flag. An organiser's assignment is a holder plus `locked`; a slot locked closed (`!`) is `locked` without a holder |
| `signup` | Event, member, status (`maybe` / `cant` / `reserve`, with the reserve position; `coming` in a Zeus event) |
| `offer` | Built in phase 3. A freed slot offered to one member, or (no member) to the whole reserve list; state (`open` / `taken` / `declined` / `expired` / `withdrawn`) and expiry. A member gets one offer per freed slot |
| `orbat_template` | Name, owner, text |
| `steam_link` | Member, Steam64 ID (unique), nick, linked by, times; reminder state; since when the member has had no rank (for the deletion after N days) |
| `attendance` | Event, member or Steam ID, seconds, status (`to_confirm` / `attended` / `no_show` / `excused` / `not_linked` / `walk_in`), corrected by; the guest count of a hosted event |
| `scheduled_action` | Kind, event, due time, done time, attempts, last error |
| `mission_rating` | Event, member, stars (1–5), comment, times (in a guest event, the rating of the cooperation with the host group) |
| `archived_message` | Event, channel or thread, Discord message id, author id and name, times (created, edited), content, reply-to, attachments (file name, path on disk, size); the event gets the time its channel was moved |

## Phases

Each phase leaves the bot usable, with tests green. The tests run against a **fake supervisor** (an aiohttp app
speaking the routes this plan uses), with no live Discord.

1. **Skeleton:** config and `/reload`, database and Alembic, backups, i18n catalogs and the translator,
   permissions, the scheduled-action loop, logging and the staff log, the startup checks, the Discord test
   fakes, the NSSM setup and `update.ps1`, the heartbeat (when the supervisor offers it), and `/privacy` with
   its text file.
2. **Supervisor:** the client (capabilities, errors), reservations as a service (create, move, retry, end), and
   `/server` with override.
3. **ORBAT and sign-up logic:** the format parser, templates, slots, the reserve list and offers, overlap
   checks, the DLC warning, locks and assignment, edits after sign-ups, the concurrency rules, and the Zeus sign-up with its
   limit, as services tested without Discord.
4. **Events:** the header channel and wizard, the four kinds and the other groups, drafts, the organiser panel
   (details, briefing, image, preset, ORBAT, *How to join*), approval, publish, the channel messages and
   thread, the sign-up buttons and `/slot`, the calendar and `/events`, the announcement, cancel, stale drafts, reminders, and archiving per
   quarter. It uses phases 2 and 3 as they are, so nothing is stubbed.
5. **Steam link and squad.xml:** `/link`, the import of the old bot's links, reminders and rollout, and
   squad.xml generation. The switch-over day can follow this phase.
6. **Attendance:** the trigger, reading the plays, the report, corrections, `/attendance`, `/profile`, the
   mission feedback with `/mission-maker` and `/group`, and `/privacy erase`.
7. **Event profile:** mission upload and pick, the mission report and warnings, cloning the base, the preset
   import job, the swap onto the event server and back, and cleanup.
8. **Long-term archive:** moving the oldest quarter into the database (transcripts, attachments), deleting its
   channels, and `/archive event`. It is first needed a year after launch, when a fifth quarter begins.

The supervisor's integration phases 1–4 already exist (see *Supervisor changes this plan uses*), so phase 2 uses
system info, managed objects, ending by id and the events stream, and phase 7 puts the event profile onto the
reservation; the polling and the profile swap are built as fallbacks only, behind the capability checks. Phase 6
needs the supervisor's phase 5 (attendance window), or uses its fallback until it lands.

## Supervisor changes this plan uses

Planned in the supervisor repo: `docs/PLAN-integration.md` (2026-10-16, revised 2026-10-31). **Being built:**
as of 2026-10-31 its phases 1–4 are committed (`83253d6` system info, integration role and `create-user` /
`create-token`; `c32d0a1` managed objects and ending by id; `166a7ed` the reservation's own profile; `4f3874e`
the live-events stream), phase 5 (`/api/stats/attendance`, `?uid=`) is in progress, and phases 6 (notifier) and 7
(docs) are still to come. Its *As built* notes give the exact payloads (e.g. the stream's `<boot>:<seq>` ids and
`hello` message, per-play seconds and `counted` in the attendance window) and win over the summary below. So the
bot is written against the new routes from the start; the fallbacks stay for a supervisor without them. The bot reads
`GET /api/system/info` at startup and after reconnecting; a 404 means no capability is there. For each capability
it uses the new route when it is listed, and the fallback when it isn't. Features with no fallback are turned
off, and staff are told.

| Capability | The bot uses it for | Fallback |
|---|---|---|
| `integration_role` | The bot's supervisor user is created with `create-user … --role integration --token …` | An admin builds the role and scoped token by hand |
| `managed_refs` | Reservations and profiles are created with `managed_by="discord-bot"` and `external_ref="event:<id>"`. Creates are idempotent, and the bot finds its objects again after losing state. The reservation's `link` is the event channel URL | Ids stored in the bot's database; a retried create after a timeout may duplicate (the bot checks by name first) |
| `reservation_profile` | The event profile is set as the reservation's `profile_id` | Swapping the server's assigned profile before and after (*Supervisor: event profile*, *On the server*) |
| `reservation_end_by_id` | `POST /api/reservations/{id}/end` | `GET /current`, check `reservation_id`, then `POST /current/end` |
| `events_stream` | `GET /api/events/stream`: `mission_ended` with `end` and `mission_session_id`, reservation state, job progress; resume with `Last-Event-ID` | Polling `stats/missions`, `reservations/current` and `jobs/{id}` every minute |
| `attendance_window` | One `GET /api/stats/attendance` call per event. The minimum counts **mission time** (`mission_s`) in both modes, so a player's result doesn't depend on the supervisor's version (decided 2026-10-31); `connected_s` is shown only as a hint | One `GET /api/stats/missions/{id}` per play; mission time |
| `player_uid_lookup` | `/link` uses `GET /api/stats/players?uid=` | `q=` with an exact `uid` match on the client |
| `heartbeat` (proposed 2026-10-31, not built) | Every minute `POST /api/integrations/heartbeat` (`discord-bot`, 60 s); the supervisor alerts on its Discord webhook when the bot falls silent (decided 2026-10-31) | None: without it, only start-up messages on the staff log show restarts |

The supervisor also stops posting its own upcoming / started / ended notices for managed reservations (unless
`notify.managed_reservations` is on), and links its status message to the event channel, so the bot and the
supervisor don't announce the same thing twice.

## What the supervisor offers (facts the plan relies on)

Checked in `D:\Workspace\python\arma-server-supervisior` on 2026-10-14, and again on 2026-10-31, when the
reservation work had been committed (`d2f201b`). These are the routes that existed before the integration work;
the new ones (system info, managed objects, ending by id, the reservation's profile, the events stream, the
attendance window) are described in the supervisor's `docs/PLAN-integration.md` and its *As built* notes. Also
checked 2026-10-31 in `app/supervisor/reservations.py`: `update` keeps an occurrence's times once it is under way
(quiet period included), and `delete` answers 409 then.

- **Auth:** a bearer token (personal API token from `POST /api/auth/tokens`) or `X-API-Key`. The bot uses its own
  supervisor user. That user needs:
  - `server.control`, plus `server.protected` for protected servers (`server-1` is created protected);
  - `server.message`;
  - `players.view`, so the stats routes include Steam IDs;
  - `reservations.edit` and `reservations.override`, which by default only `admin` holds;
  - for event profiles: `profiles.edit`, `mods.manage`, `steam.manage` (the preset import needs all three),
    `missions.manage`, and `jobs.view`.
- **Profiles** (`app/api/profiles.py`):
  - `POST /api/profiles/{id}/clone` (`name`); a clone of a protected profile is protected.
  - `PATCH /api/profiles/{id}` (name, description, protected only); `DELETE /api/profiles/{id}`, refused while
    a server runs the profile (servers it is only assigned to are left without one).
  - `PATCH /api/profiles/{id}/config/{kind}` deep-merges partial changes into one config kind (lists are
    replaced); `PUT` replaces it.
  - `POST /api/profiles` takes partial `configs` per kind (server, basic, difficulty, launch).
  - `POST /api/profiles/{id}/mods/import-preset` (form: `file`, `mode`, `download`, `include_cdlc`,
    `restart_server`, `include_dependencies`) submits a `preset_import` job and returns it. It also reports
    Creator DLC that isn't installed. `/mods/preset-preview` shows what an import would do.
  - In the server config, `missions` is the mission cycle (`template` = file name without `.pbo`, `difficulty`,
    `params`), next to `hostname`, `max_players` and `verify_signatures` (0 / 2).
- **Missions** (`app/api/missions.py`):
  - `GET /api/missions` lists `.pbo`s with `template`, `title`, `author`, `players`, `status`, `needs` (mods /
    DLC for missing addons) and `profiles` (which profiles use it). The inspected fields are `null` for a mission
    nobody has inspected yet, so for a picked mission the bot reads `GET /api/missions/{name}/report`. The first
    report ever also queues the addon index build (a job, held during a reservation like any other).
  - `POST /api/missions` uploads them (form field `files`; `overwrite`; 2 GB limit; refused while a running
    server has the file open), each with its inspection `report`.
- **Jobs:** `GET /api/jobs/{id}` (status, progress); one job runs at a time, and jobs are held during a
  reservation's quiet and active periods.
- **Servers:**
  - `GET /api/servers` lists their statuses; each has `server` with id, slug and the assigned profile.
  - `PATCH /api/servers/{slug}` with only `profile_id` needs `server.control` (other fields need
    `server.protected`).
  - `/api/servers/{slug}/status|start|stop|restart|say`.
  - Start and restart take `override`. Without it, a start that a reservation refuses answers 409 with a
    `reservation` body; with it but without `reservations.override`, the answer is 403.
- **Reservations** (`app/api/reservations.py`):
  - `POST /api/reservations` takes `name` (≤ 80), `server_id` (id **or slug**), `repeat`, `starts_at`,
    `ends_at`, `quiet_minutes` (30), `warn_minutes` (15), `public`, `note`, `enabled`.
  - `PUT /api/reservations/{id}` (the full body, as for `POST`) and `DELETE /api/reservations/{id}`.
  - `GET /api/reservations/current` returns the occurrence in its quiet period or under way, with
    `reservation_id`, `state` and times.
  - `POST /api/reservations/current/end`; `POST /api/reservations/current/extend` (`minutes`).
  - Overlaps are refused with 409, and times without a zone are read as the supervisor's local time, so the bot
    always sends zone-aware UTC.
  - At the start the supervisor (re)starts the event server even with players on it: from `warn_minutes` before
    the start, a running event server gets a countdown restart at the start time, and a stopped one is started
    then. Only one found running without that countdown (adopted after an API restart) is left as it is. The
    restart reads the server's assigned profile when it runs, not when it is scheduled, so a profile assigned
    a few minutes before the start is the one that comes up.
- **Player stats:**
  - `GET /api/stats/missions?server=<id>&all=true` lists mission plays (`id`, `started_at`, `ended_at`, `end`,
    `running`). `all=true` includes plays below the stats thresholds. A play ends with `finished`,
    `next_mission`, `server_stopped`, `crashed` or `lost`. `server` is the numeric id (no slug, unlike
    reservations). There is no time filter, only `days`; the plays come newest first, `limit` 100 by default,
    with `offset`.
  - `GET /api/stats/missions/{id}` → `players`: everyone whose session overlapped the play, with `seconds`,
    `sessions`, `name`, and `uid` (Steam64, with `players.view`).
  - `GET /api/stats/players?q=<steam64>` finds a player, for the last in-game name at `/link`. The search also
    matches names, so the bot must pick the exact `uid`. `Player.uid` can be `name:<name>` until the log reports
    the ID.
- **Discord webhook:** the supervisor already keeps a server-status message and posts crash and job alerts. The bot
  doesn't duplicate them.

## Not in this plan

- Native Discord Scheduled Events, DM event reminders, unfilled-slot warnings.
- Other groups' parts of the ORBAT, and their members signing up through the bot.
- Recurring events.
- Choosing the event server per event (the default from config applies), or the base profile per event.
- Updating already-installed mods for an event, or stopping servers for downloads.
- Mission parameters, difficulty and join passwords set per event.
- Priority sign-up windows for some roles.
- Building the ORBAT from mission files.
- Several Discord servers.
