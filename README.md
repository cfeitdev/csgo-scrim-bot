# CS 5v5 Scrim Bot

A Discord bot that runs CS:GO / CS2 pickup games end to end: **queue → teams → map veto → map
change over RCON → result and Elo.**

```
/host
   │
   ├─ Join / Leave buttons  ──► 10 players, each shown with their Elo
   │
   ├─ TEAMS   pick mode decides: captains (draft) · random · balanced
   │          snake draft: A · B B · A A · B B · A
   │
   ├─ VETO    captains alternate bans (B bans first) down to one map
   │
   ├─ LIVE    changelevel, then get5 loads the config and locks the teams
   │          players press "Connect info" for the address + password
   │          all 10 connected ──► get5_forceready, match starts itself
   │
   └─ RESULT  get5 final score ──► Elo + W/L applied automatically
              (or /game end by hand if the score cannot be read)
```

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your token + rcon details
python bot.py
```

### Inviting the bot

Scopes: `bot` + `applications.commands`. Permissions integer **289467392**:

| Permission | Needed for | Optional? |
| --- | --- | --- |
| View Channels | Seeing the channel a lobby is hosted in | required |
| Send Messages | The scrim message and its phase announcements | required |
| Embed Links | Every lobby, draft, veto and result embed | required |
| Attach Files | `/listids` and `/liststaffids` when the dump is too long to inline | required |
| Manage Roles | The Team A / Team B / Registered roles | only with those roles set |
| Manage Messages | Keeping the `/sethostchannel` channel bot-only | only with a host channel |
| Move Members | `/spec`, and `/voicecleanup` disconnecting players after a game | only with those |
| Mute Members | `/spec` server-muting a spectator | only with `/spec` |

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&permissions=289467392&scope=bot%20applications.commands
```

Drop the optional four and it is **16829440**. **Manage Roles is not enough on its own** — the
bot's own highest role has to sit *above* every role it hands out, or Discord refuses. The role
setup commands check this and tell you when it is wrong.

Enable the **Server Members Intent** in the developer portal — it is needed for `/liststaffids`
role membership and for voice cleanup. Message Content is *not* required.

If your notify role is not marked "Allow anyone to @mention this role", either make it
mentionable (preferred) or add **Mention @everyone, @here and All Roles** — permissions integer
**289598464** with it.

Set `DISCORD_GUILD_ID` in `.env` while developing so slash commands sync instantly to your test
server; leave it blank for a global (up to ~1h propagation) sync. Set `RCON_ENABLED=0` to
exercise the whole flow without a game server — the bot reports the `changelevel` it *would*
have sent instead of connecting.

## Permissions

Three tiers:

| Tier | Who | How it is enforced |
| --- | --- | --- |
| **Administrator** | Discord's Administrator permission | `default_member_permissions`, so the commands are hidden from everyone else, plus a runtime check |
| **Hoster** | A role you nominate with `/sethosterrole` (admins always qualify) | Runtime check -- Discord cannot gate a slash command on a custom role, so these stay visible and refuse politely |
| **Everyone** | Anyone in the server | -- |

Until an admin runs `/sethosterrole`, the hoster commands are administrator-only. Manage Server
is *not* enough to host.

## Commands

**Playing**

| Command | Who | What |
| --- | --- | --- |
| `/host [gamemode] [size]` | **hoster** | Create a new game lobby |
| `/redraft` | **hoster** | Tear the teams down and draft again |
| `/sub <out> <into>` | **hoster** | Substitute a user into a game |
| `/forcejoin <user>` | **hoster** | Add someone to the queue |
| `/forcedrop <user>` | **hoster** | Remove someone from the lobby |
| `/game end [winner]` | **admin** | End the lobby, apply Elo and W/L (blank = draw) |
| `/cleargames` | **admin** | Force-remove every game in the server |
| `/testfill [captain] [count]` | **admin** | Fill the lobby with fake players for a dry run |
| `/testget5 [map]` | **admin** | Load a one-player get5 config to prove the wiring works |
| `/testget5 [map] [server]` | **admin** | Same, on the EU or NA server |
| `/region <server>` | **admin** | Override the EU/NA server vote -- only while it is open |
| `/cancel` | **hoster** | Cancel the game in this channel |
| `/forcecaptain <user> <team>` | **hoster** | Name a captain |
| `/moveteams` | **hoster** | Pull everyone into their team voice channel |
| `/spec <team>` | anyone | Spectate: join a team's call muted so you can listen in |
| `/unspec` | anyone | Stop spectating: lift your own mute and disconnect from voice |
| `/notify [message]` | anyone | Ping the notify role about the lobby in progress |
| `/status` | anyone | Ephemeral snapshot of the current game |

**Players and stats**

| Command | Who | What |
| --- | --- | --- |
| `/register <steam> [user]` | anyone | Bind a Steam account to a Discord ID |
| `/unregister [user]` | anyone | Unbind a Steam account |
| `/stats [user] [gamemode]` | anyone | Elo, rank, W/L and Steam link |
| `/leaderboard [gamemode] [page]` | anyone | Ranked by Elo |
| `/elo show\|give\|take\|set` | **admin** | Manually adjust Elo |
| `/games add\|remove\|set` | **admin** | Manually adjust wins/losses |
| `/listids [format]` | **admin** | Every bound Steam ID, for a server whitelist |
| `/liststaffids [role] [format]` | **admin** | Staff Steam IDs, for an admin whitelist |
| `/resetstats [gamemode]` | **admin** | Wipe Elo and W/L (keeps registrations) |
| `/blacklist [user] [reason] [duration]` | **hoster** | Block a user from queueing (blank user = list) |
| `/whitelist <user>` | **hoster** | Let a blacklisted user queue again |
| `/blackcapt [user]` | **hoster** | Toggle whether a user can be picked captain (blank user = list). Only overridden if too few other players are left to captain |

**Server configuration** — per-guild

| Command | Who | What |
| --- | --- | --- |
| `/pickmode <mode>` | **admin** | `captains` (draft), `random`, or `balanced` |
| `/randomcaptains [on\|off]` | **admin** | Random captains, or the two highest-Elo players |
| `/togglevetoing [on\|off]` | **admin** | Off = the bot rolls a map from the pool |
| `/accountlockgames [on\|off]` | **admin** | Require `/register` before a user can queue |
| `/randompassword [on\|off]` | **admin** | Randomise `sv_password` each game |
| `/servercleanup [on\|off]` | **admin** | Kick players off the game server after a game |
| `/autovoice [on\|off]` | **admin** | Move players into their team call once the map is set |
| `/voicecleanup [on\|off]` | **admin** | Disconnect players from voice after a game |
| `/channels` | **admin** | List this server's channels and their ids |
| `/sethosterrole [role]` | **admin** | Role allowed to host and manage lobbies |
| `/setteamroles [team1] [team2]` | **admin** | Roles worn while a game runs (`clear:true` to unset) |
| `/setteamvoice [team1] [team2]` | **admin** | Voice channels `/spec` moves people into |
| `/setregisteredrole [role]` | **admin** | Role mirroring who has `/register`ed |
| `/maps list\|add\|remove\|reset` | staff to edit | Edit the map pool (add takes an optional workshop id) |
| `/setnotifyrole [role]` | staff | Role pinged when games are hosted |
| `/setcommandlogchannel [channel]` | staff | Audit log channel (see below) |
| `/sethostchannel [channel]` | **admin** | Designate a bot-only channel (see below) |
| `/setresultschannel [channel]` | **admin** | Archive finished games to their own channel (see below) |
| `/settings` | **admin** | Show everything above at a glance |
| `/rcon <command> [server]` | **admin** | Raw console passthrough (EU by default, or NA) |

Toggles flip when called with no argument, or take an explicit `on`/`off`.

## Rules implemented

**Draft.** Two players captain and are seeded onto their own team, leaving 8 picks in the order
`A, B, B, A, A, B, B, A` — a snake: the first captain gets a single opening pick, then every
block after it is a pair. That single is what compensates B for A picking first, and it keeps the
sides level the whole way down at any lobby size. Both teams end with five. Only the captain on the clock can use the
dropdown. Captains are random by default, or the two highest-Elo players with
`/randomcaptains off`, or whoever staff names with `/forcecaptain`.

The **last player is assigned automatically** — with one left there is nothing to choose, so a
5v5 draft is seven picks rather than eight and nobody sits on the clock in front of a
single-option dropdown.

**Veto.** The whole pool goes in. The *second* captain bans first, as compensation for the first
draft pick, alternating until one map remains — with the default 14 maps that is 7 bans for B
and 6 for A.

**Forcing a captain.** `/forcecaptain` installs them into the draft as it stands — it does not
re-roll. Whoever they displace takes their place: their old team slot if they were already
drafted, otherwise back into the pool. Team sizes and every pick made so far are preserved, so
it is safe to use mid-draft.

**Re-drafting.** `/redraft` rerolls the captains rather than reshuffling around the same two —
the previous pair is pushed to the back of the pool, so a reroll produces new captains wherever
there are enough players. It also **clears any forced captains**, so a re-draft is a genuinely
fresh roll; name them again afterwards if you want them. `/randomcaptains off` still picks the
two highest rated.

**Pick clock.** `PICK_TIMEOUT` (default 120s) auto-picks/auto-bans at random when a captain
stalls, and says so in the channel. Set it to `0` to let picks hang indefinitely.

**Elo.** Ratings belong to **Discord accounts**, keyed `guild → user id → gamemode`. A Steam ID
is just another field on that record, so changing Steam account keeps someone's rating, W/L and
rank. Only real accounts can hold one: `/testfill` bots are excluded from every rating write, and
the leaderboard ignores any id too small to be a Discord snowflake.

Team-average Elo, so beating a stronger team pays more than beating a weaker one, and a draw
pulls both teams toward each other. Ratings are per gamemode, so `/host 2v2` keeps its own
ladder, leaderboard and W/L.

**Steam.** `/register` accepts **one** format: `STEAM_1:0:12345678`. Players read it straight
out of the game console (`~` then `status`), and a single accepted shape means one clear error
message instead of five. steamID64, `[U:1:...]` and profile URLs are refused with instructions.
Internally the id is stored as a steamID64, so `/listids` can still emit any of the three formats
for a server whitelist. `STEAM_API_KEY` is optional and only adds a persona-name lookup. VAC bans
are not checked — every account may register and play.

## Server regions (EU / NA)

With an NA server configured (`NA_SSH_HOST` and friends in `.env`), every full
lobby votes on where to play. Without one, nothing below happens and the bot
behaves exactly as before.

- **When.** The vote is posted as its own message the moment the queue fills,
  with **EU** and **NA** buttons, and runs alongside the draft and the veto. It
  is a separate message on purpose: a click on the draft/veto message resets
  the captain's pick clock.
- **Who.** Only the players in that game. Each can change their vote until it
  closes; the message shows the running count, not who voted what.
- **Closing.** Once every slot has voted, or when the veto (or, with vetoing
  off, the draft) finishes -- whichever comes first. Majority wins. A tie,
  including nobody voting, is a coin flip, and the channel is told which.
- **No draft and no veto** (`random`/`balanced` with vetoing off): the game
  goes live at once, so there is no vote and it is played on EU.
- **Override.** `/region` (administrators only) settles it while the vote is
  open. Once it is locked it cannot be changed.
- **Roster changes.** `/forcedrop` removes that player's vote, `/sub` drops the
  outgoing vote and lets the sub cast their own, and `/redraft` keeps the votes
  -- or the locked region.
- **Test games.** `/testfill` bots vote at random.

The locked region decides the server for **everything** in that match: the
map change, the get5 config, the roster watch and `get5_forceready`, the live
score and the stats file, `/sub`, `/game end`, `/cancel`, `/redraft`, post-game
kicks, and the Connect info button. `/rcon` and `/testget5` take a `server`
option (EU by default).

**How the bot reaches NA.** The bot runs next to the EU (home) server. NA is reached over
one SSH connection: get5's match config is uploaded and its stats file read
back over **SFTP**, so NA runs get5 in file mode exactly like EU, and **RCON
is tunnelled through the same connection**, so NA's RCON port never faces the
internet. The host key is pinned (`NA_SSH_KNOWN_HOSTS`), the link reconnects
by itself after a drop, and an NA outage shows up as the usual "could not
change the map" message with the address scrubbed.

### Setting up the NA server

The NA server should be a copy of the EU one: the same game build, plugins and
configs. On the NA machine (Linux, as root):

1. **Install the game.** Copy your EU install across (rsync works), or install
   it fresh with SteamCMD, along with the 32-bit libraries your EU server
   needed. Give it its own GSLT -- one token cannot run two servers at once --
   and its own `rcon_password`.
2. **Run it as its own user.** Create a `csgo` user to own the install, and
   start it with [server-configs/remote/csgo.service](server-configs/remote/csgo.service)
   using your EU launch line. RCON stays off the internet: srcds binds it to
   the address the machine's hostname resolves to (often `127.0.1.1`; check
   with `ss -tlnp` once it is running). That address is `NA_RCON_HOST`.
3. **Create a locked-down user for the bot.** Add a `scrimbot` user in the
   `csgo` group, let that group write get5's config folder, and authorise the
   bot's key so it can do nothing except SFTP and reach RCON:
   ```bash
   useradd -m -s /bin/bash -G csgo scrimbot
   chmod 2775 /home/csgo/server/csgo/addons/sourcemod/configs/get5
   # in /home/scrimbot/.ssh/authorized_keys, on one line:
   from="BOT_IP",restrict,port-forwarding,permitopen="127.0.1.1:27015",command="/usr/lib/openssh/sftp-server" ssh-ed25519 AAAA... scrimbot
   ```
   The bot's key has no passphrase because the bot runs unattended; the
   `from=`, `permitopen=` and `command=` options are what keep that safe.
4. **Pin NA's host key on the bot's machine.** Put NA's
   `/etc/ssh/ssh_host_ed25519_key.pub` in the file `NA_SSH_KNOWN_HOSTS` points
   at, as `NA_HOST ssh-ed25519 AAAA...`. Copy it from the server itself rather
   than trusting a first connection.
5. **Open the game port** for UDP, including in any firewall your hosting
   provider runs in front of the machine.
6. **Fill in `NA_*` in the bot's `.env`** (see `.env.example`), run
   `pip install -r requirements.txt`, and restart the bot. Check it with
   `/rcon status server:NA`, then `/testget5 server:NA`, then join -- get5
   should put you on Team A.

## get5

Targets **get5 0.6.0**. Off until you set `GET5_BASE_URL` or `GET5_CONFIG_DIR`; with either set
the bot takes over the whole match setup after the veto:

1. `changelevel` to the vetoed map, then wait `GET5_LOAD_DELAY` seconds for it to load.
2. Build a match config from the draft — `skip_veto: true`, the single vetoed map, and both
   rosters keyed by steamID64 — and load it with `get5_loadmatch_url` or `get5_loadmatch`.
3. The config sets `get5_check_auths 1`, which is what **pins each player to the team they were
   drafted onto**. Anyone not in a roster gets moved out.
4. Poll `status` every `GET5_POLL_INTERVAL` seconds. Once all ten drafted Steam IDs are on the
   server **and in game**, fire `get5_forceready` — **the match starts on its own**. The live
   message shows the count filling up. If they never all show, it stops after
   `GET5_READY_TIMEOUT` and tells you to `!ready` manually.

   "In game" means the `status` state column reads `active`. A player who is still `connecting`
   or `spawning` is on the server but not yet on their team, and readying there starts the match
   a man down. The roster also has to read complete on `GET5_READY_POLLS` consecutive polls (2 by
   default) so a single clean read mid-reconnect cannot trigger the start. Layouts that do not
   print a state column — CS2, some mods — count everyone, as before.
5. Keep polling once it is live. The scoreline shows on the message as it moves, and when get5
   reaches its post-game state the bot reads the final score, **applies Elo and W/L on its own**,
   and posts the result. `/game end` remains as the manual fallback and calls
   `get5_endmatch team1|team2`. `/cancel` and `/redraft` also tear the match down.

Automatic results need `get5_status` to return JSON, which in 0.6.0 means the **smjansson**
extension — the same one get5 needs to read a JSON match config, so if step 2 worked this does
too. If it ever replies with plain text the bot says so once and falls back to `/game end`.

**Plugin config.** [server-configs/get5.cfg](server-configs/get5.cfg) holds every get5 0.6.0
cvar with notes, tuned for being driven by this bot. Copy it to the game server at
`csgo/cfg/sourcemod/get5.cfg` — get5 calls `AutoExecConfig(true, "get5")`, so SourceMod execs
that path on plugin load. Put your round rules (`mp_maxrounds`, overtime, start money) in
`cfg/get5/live.cfg`, which get5 ships.

**Config format.** get5 0.6.0 reads JSON *or* KeyValues, but JSON needs the **smjansson**
SourceMod extension, which a stock install does not have — you get `Cannot load a json config
without the smjansson extension loaded`. File mode therefore writes **KeyValues** (`.cfg`) by
default and needs no extension at all. `GET5_CONFIG_FORMAT=json` forces JSON if you do have
smjansson. URL mode is always JSON, since that is what `get5_loadmatch_url` fetches.

**Reading results without smjansson.** `get5_status` needs that extension too, so the bot uses
two extension-free sources instead: the `get5_game_state` cvar tells it when the match reaches
post-game, and get5's own **stats file** (`get5_matchstats_<matchid>.cfg`, written to the csgo
directory) carries the winner and the score. In file mode the bot is already on that filesystem,
so results still land automatically. `GET5_STATS_DIR` overrides the location if your
`get5_stats_path_format` is non-standard; blank derives it from `GET5_CONFIG_DIR` minus
`GET5_CONFIG_PREFIX`.

If neither source is readable the scrim message says so and names the path it looked in, and you
close the game out with `/game end`.

**Per-player stats.** The same stats file carries a block per player, keyed by steamID64 (get5
uses `AuthId_SteamID64`, the same form the bot registers), so the result embed also gets a
monospaced scoreboard per team -- kills, assists, deaths, ADR and headshot percentage, best
first. It is read once, just before teardown, and retried a couple of times in case get5 has not
flushed the file yet. No stats file means no scoreboard; the winner, score and Elo still post.

**Handing get5 the config.** `get5_loadmatch_url` (set `GET5_BASE_URL`) has the bot serve the
JSON on its own HTTP listener at a URL containing a per-match random token, optionally behind a
shared-secret header — this needs the SteamWorks extension on the game server, and
`GET5_BASE_URL` must be the address the *game server* can reach the *bot* on. If the bot can
instead write somewhere the server reads, set `GET5_CONFIG_DIR` (bot-side path) plus
`GET5_CONFIG_PREFIX` (same folder relative to the server's `csgo` directory) and it uses
`get5_loadmatch`. URL mode wins if both are set.

While get5 is enabled, **`/register` is mandatory to queue** — a player with no Steam ID cannot
be written into a roster, so the Join button turns them away with that explanation.

## Roles

Three optional roles, all off until set:

| Role | Set with | Behaviour |
| --- | --- | --- |
| Team A / Team B | `/setteamroles` | Given when the teams lock in, stripped when the game ends, cancels or re-drafts. Follows `/sub`, `/forcedrop` and re-drafts. |
| Registered | `/setregisteredrole` | Given on `/register`, taken on `/unregister`. Setting it **syncs every existing member** — granting it to everyone already registered and removing it from anyone who is not — and reports the counts. |

Role work never breaks a scrim: a deleted role, a member who left, or a permissions problem is
logged once and skipped, and the game carries on.

## Testing it without ten people

`/testfill` (admin) pads the lobby with fake players so you can drive the whole flow solo:

```
/host
/testfill
```

The bots pick and ban on their own after `TEST_BOT_DELAY` seconds each (1.5s by default), so the
draft and veto run to completion in seconds. Pass `captain:true` (the default) and you keep one
side of the draft yourself, with the bot captain answering each of your picks — the fastest way
to check the dropdowns, the embeds and the pick order behave.

What a test game does and does not exercise:

- **Real:** the queue, draft order, veto, team roles, the `changelevel` over RCON, the connect
  info button, `/game end`, and the Elo/W-L write.
- **Skipped:** get5. Fake players have no Steam accounts, so a match config cannot be built for
  them — the live message says so rather than failing quietly. Exercise get5 with a real
  `/host size:2` between two registered accounts.

**Test games never affect ratings.** Bots cannot hold a rating, which leaves one side of the
result empty, so no Elo or W/L is written for anyone in the game — including any real players in
it. Nothing to clean up afterwards, and no need for a separate gamemode (though `/host
gamemode:test` still works if you want the isolation for other reasons).

## Audit logging

`/setcommandlogchannel #audit` mirrors what people do with the bot into that channel. Off until
set; clear it by running the command with no channel.

What lands there:

- **Every slash command that ran** — who, where, the full subcommand path (`/elo give`, not
  `/elo`), and the arguments rendered readably, with user/channel/role options as mentions.
- **Every command that blew up**, in red, with the exception type and message. These previously
  vanished into the process log where nobody looked.
- **The button clicks that change an outcome** — cancelling a game, reporting a result,
  re-sending a map change, and who asked for the connect details. Those bypass slash commands
  entirely, so without this they left no trace at all.

Join/leave, picks and bans are not logged. They are all visible on the scrim message itself, and
ten joins per lobby would drown everything else.

**Secrets are masked.** `/rcon sv_password hunter2` is logged as `sv_password ***`, and any
option named like a password, token, secret or key is replaced outright. The connect-info button
records *that* someone asked, never the password they were shown. Pick a private channel anyway —
the log still shows which admin ran what.

## What the bot says in the channel

The scrim message is the source of truth and updates in place, so the bot does **not** narrate
individual picks and bans. Only phase changes get a line: captains drawn, teams set, the veto
opening, the match going live, and post-game cleanup. Those self-delete after two minutes.

The **RCON address is never shown in the channel.** A successful map change adds nothing to the
embed — the map is already in the title. A failed one reports the reason with the address
scrubbed out, so a misconfigured host can't leak through an error message either. Players get the
connect address from the ephemeral button, which is the only place it appears.

## Connect details

The server address and password are **not** posted publicly. The live message carries a
**Connect info** button; only players in that match can use it, and the reply is ephemeral — it
shows their team, the map, and a `connect <address>; password <pw>` line ready to paste. Everyone
else gets told it is not theirs. With `/randompassword` on, that password is fresh per game and
also pushed into the get5 config as an `sv_password` cvar.

## Team voice

With `/autovoice on` and channels set via `/setteamvoice`, players are pulled into their own
team's call **after the map veto finishes** — not when teams lock. Captains need to stay put to
run the veto, and moving mid-veto would scatter everyone while the dropdown is still in use.

`/moveteams` does the same thing on demand, for anyone who joined voice late. A `/sub` also
follows their new team into voice.

Discord cannot pull in someone who is not already in a voice channel, so those players are
counted and named rather than silently skipped — they join a channel and someone runs
`/moveteams`.

## Spectators

`/spec Team A` moves **you** into that team's voice channel and server-mutes you, so you can
listen to comms without talking. The mute lifts **automatically** the moment you leave that
channel — no cleanup command needed, though `/unspec` is there if you want it.

It is self-service and deliberately cannot target anyone else: an open command that moved and
muted *other* people would hand every member of the server a mute button.

Set the channels first with `/setteamvoice team1:#Team-A team2:#Team-B`, and give the bot **Move
Members** and **Mute Members**. `/setteamvoice` checks both and warns if either is missing.

Two details worth knowing:

- **You must already be in a voice channel.** Discord cannot move someone who is not in a call,
  so the command refuses with that explanation rather than silently doing nothing.
- **Who is muted is persisted**, so a bot restart mid-scrim cannot strand anyone muted. If you
  disconnect entirely while muted, Discord rejects mute edits on a disconnected member, so the
  bot keeps you tracked and clears it the moment you rejoin any channel.

## Repairing results posted before the map-block fix

get5 opens the *next* map's block in its stats file the moment the server
changes level, and that stub carries every player's name and `roundsplayed "1"`
and nothing else. The bot used to read the last block rather than the last
*finished* one, so results posted with a 0:0 score and a scoreboard of zeroes
under an otherwise correct winner.

`tools/fix_past_results.py` walks back over the messages the bot already posted
and rewrites them from the stats files on disk:

```bash
python -m tools.fix_past_results
```

That reports what it would change. Add `--apply` to actually edit the messages
-- it edits in place, so they keep their ids, timestamps and reactions.

It matches a message to a stats file by **who played**. The people named in the
embed are resolved to steamID64s through the bot's own registrations, and the
file whose players are those people is the right file, wherever it happens to
sit on disk. The map name has to agree too. Timing only breaks ties: a result
posted in the host channel is an *edit* of the lobby message, so it carries a
timestamp from before the match even started, and matching on time alone finds
almost nothing. Anything it cannot match confidently is reported and left alone.

`--min-overlap` (6 by default) sets how many of a message's registered players
must appear in a stats file for it to count as the same game -- lower it if
your rosters predate a lot of `/register`s, raise it to be stricter.

The Elo lines are never touched: they came from the winner, which was always
right. Re-running is safe -- a message that already says the right thing is
skipped. `--channel <id>` scans a specific channel, `--limit` sets how far back
to read (200 by default), and `--window` how long after a match starts its
result may have been posted (6 hours by default).

## The results channel

`/setresultschannel #channel` archives finished games somewhere other than the scrim message.
The full card -- winner, score, map, Elo swing and the per-team scoreboard -- is posted there as
a new message, and the scrim message keeps the winner and the Elo swing and links across to it
with **Full scoreboard**. Mentions are suppressed on the archive post, so closing a game never
pings ten people.

Leave it unset (the default) and nothing changes: the whole result, scoreboard included, stays
on the scrim message. `/setresultschannel` with no channel returns to that.

It falls back to the old behaviour rather than losing a result. If the channel is deleted, the
bot cannot post in it, or it points at the channel the game is being played out in, the full
result goes back onto the scrim message and the failure is logged. Elo is written either way --
it is applied before the post is attempted.

## The host channel

`/sethostchannel #channel` designates a channel as bot-only: **every message from anyone other
than the bot is deleted the moment it arrives**, other bots and webhooks included. Nothing the
bot itself posts is touched, so the lobby message and the pick/ban play-by-play survive.

This is off until an administrator sets it, and `/sethostchannel` with no channel turns it back
off. The bot needs **Manage Messages** in that channel — the command warns you up front if it
does not have it, and the log records one warning per channel rather than one per message.

It only deletes messages sent while the bot is running. Anything already in the channel, or
posted while the bot is offline, stays — clear that with Discord's own bulk delete.

## Overrides

Blacklisting someone mid-lobby drops them from the queue automatically. Force-dropping after
teams are locked leaves that team a player short on purpose — fill it with `/sub`.

The buttons on the scrim message carry the same gates as the equivalent commands, so they
cannot be used to route around one: **Cancel** needs the hoster role, and **Team A won / Team B
won / Draw** need Administrator, matching `/game end`. Join and Leave stay open to everyone.

## Layout

| File | Role |
| --- | --- |
| [bot.py](bot.py) | Entry point, intents, extension loading, command tree sync |
| [scrimbot/match.py](scrimbot/match.py) | Pure state machine — lobby, draft, veto, result. No Discord imports |
| [scrimbot/session.py](scrimbot/session.py) | Binds one match to one Discord message; every transition funnels here |
| [scrimbot/core.py](scrimbot/core.py) | Shared services (store, server, steam) and the session registry |
| [scrimbot/store.py](scrimbot/store.py) | JSON persistence: settings, players, stats, blacklist |
| [scrimbot/cogs/](scrimbot/cogs) | Slash commands: `scrim.py`, `players.py`, `settings.py` |
| [scrimbot/views.py](scrimbot/views.py) | Buttons and dropdowns per phase |
| [scrimbot/embeds.py](scrimbot/embeds.py) | Rendering for each phase |
| [scrimbot/rcon.py](scrimbot/rcon.py) | Async Source RCON client (no third-party dependency) |
| [scrimbot/regions.py](scrimbot/regions.py) | One `GameServer` per region (EU, optional NA) |
| [scrimbot/remote.py](scrimbot/remote.py) | Local vs SFTP file access, and the SSH link to NA |
| [scrimbot/server.py](scrimbot/server.py) | Game-server actions: map change, password, kicks, `status` parsing |
| [scrimbot/auditlog.py](scrimbot/auditlog.py) | Command/action logging to the audit channel |
| [scrimbot/roles.py](scrimbot/roles.py) | Team and Registered role assignment |
| [scrimbot/voice.py](scrimbot/voice.py) | `/spec` moving and muting, and the auto-unmute |
| [scrimbot/get5.py](scrimbot/get5.py) | get5 match config builder, config HTTP server, auto-start watcher |
| [scrimbot/steam.py](scrimbot/steam.py) | Steam ID parsing/conversion and Web API lookups |
| [scrimbot/elo.py](scrimbot/elo.py) | Elo maths and Elo-balanced team splitting |

`match.py`, `store.py`, `elo.py` and `steam.py` have no Discord dependency, which is what makes
the flow testable:

```bash
python -m unittest discover -s tests
```

## Extending

- **Workshop / classic maps.** `de_dust2_old`, `de_aztec`, `cs_workout` and friends aren't on a
  stock CS2 server. Either put the id in [scrimbot/maps.py](scrimbot/maps.py) or use
  `/maps add de_whatever workshop_id:123456` — the bot then sends `host_workshop_map <id>`
  instead of `changelevel`.
- **More server setup.** `ScrimSession.server_commands()` returns the console commands fired
  when a map locks in — add `exec` of a match config, knife-round settings, warmup. Each pair is
  `(command, expect_response)`; use `False` for anything that stalls the server.
- **Whitelisting.** `ServerController.whitelist()` is a stub that sends `sm_whitelist_add` per
  id, because every server does this differently. Point it at your plugin and `/listids` becomes
  a one-click sync.
- **Real storage.** `Store` is a flat JSON file behind an async interface. Swap in a
  database-backed class with the same methods and nothing else changes.
- **Automatic results.** `/game end` is manual. A GOTV/log listener calling
  `ScrimSession.finish()` would close games out on its own.

## Not built yet

Persistence across restarts (an in-flight game is lost on reboot), reading scores back out of
get5 (`get5_status` or its event log) so `/game end` fills itself in, automatic score detection,
per-team voice channel creation (voice cleanup only disconnects), and match series (bo3).
