from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING

import discord

from . import embeds, views
from .elo import team_deltas
from .get5 import Get5Error
from .match import TEAM_EMOJI, TEST_ID_BASE, TEAM_NAMES, Match, Phase, Player, ScrimError
from .rcon import RconAuthError, RconError
from .regions import DEFAULT_REGION, region_name
from .util import random_password, timestamp, utcnow

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .core import ScrimCore

log = logging.getLogger(__name__)


class ScrimSession:
    """Owns one scrim message and drives it through every phase.

    All state transitions funnel through here so there is exactly one place
    that touches the match, the Discord message and the game server.
    """

    def __init__(self, core: "ScrimCore", channel: discord.abc.Messageable, match: Match) -> None:
        self.core = core
        self.channel = channel
        self.match = match
        self.message: discord.Message | None = None
        self.view: discord.ui.View | None = None
        self.rcon_status: str | None = None
        self.get5_status: str | None = None
        self.changes: dict[int, tuple[int, int]] = {}
        self._get5 = None
        self._get5_task: asyncio.Task | None = None
        self._get5_result_task: asyncio.Task | None = None
        self._autoplay_task: asyncio.Task | None = None
        self._view_phase: Phase | None = None
        self.final_score: tuple[int, int] | None = None
        self.player_stats: list[list[dict]] | None = None
        self.result_url: str | None = None
        self.vote_message: discord.Message | None = None
        self.vote_view: discord.ui.View | None = None
        self._region_announced = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- properties

    @property
    def guild(self) -> discord.Guild | None:
        return getattr(self.channel, "guild", None)

    @property
    def guild_id(self) -> int:
        guild = self.guild
        return guild.id if guild else 0

    @property
    def store(self):
        return self.core.store

    @property
    def config(self):
        return self.core.config

    @property
    def game_server(self):
        """The server this match plays on -- the voted region, else the default.

        The region is locked before the map is ever sent, and never changes
        afterwards, so every RCON, get5, status, result and cleanup call for
        this match lands on the same machine.
        """
        return self.core.server_for(self.match.region)

    @property
    def server(self):
        return self.game_server.server

    @property
    def connect_target(self) -> str:
        return self.game_server.connect_target

    def rcon_hint(self, command: str) -> str:
        """The /rcon line for this match's server, for messages to staff."""
        target = self.game_server
        suffix = "" if target is self.core.servers.get(DEFAULT_REGION) else f" server:{target.label}"
        return f"/rcon {command}{suffix}"

    @property
    def region_label(self) -> str | None:
        """Shown on the live message only when there was a choice to make."""
        if not self.match.has_region_vote:
            return None
        return region_name(self.match.region)

    @property
    def pick_timeout(self) -> float | None:
        return self.config.pick_timeout

    @property
    def finished(self) -> bool:
        return self.match.finished

    def setting(self, key: str):
        return self.store.setting(self.guild_id, key)

    # ----------------------------------------------------------------- render

    def _build(self) -> tuple[discord.Embed, discord.ui.View | None]:
        embed = embeds.render(
            self.match,
            self.rcon_status,
            connect=self.connect_target,
            changes=self.changes,
            get5=self.get5_status,
            region=self.region_label,
            players=self.player_stats,
            score=self.final_score,
            result_url=self.result_url,
        )
        # Keep the same view object for the whole phase. Components get random
        # custom_ids at construction, so building a fresh view on every refresh
        # silently breaks every button on any copy of the message a client has
        # not re-rendered yet -- those clicks reach a custom_id we no longer
        # know and go unanswered until Discord times them out.
        if (
            self.view is not None
            and self._view_phase == self.match.phase
            and not self.view.is_finished()
        ):
            self.view.refresh_items()
            return embed, self.view
        return embed, views.view_for(self)

    async def start(self) -> discord.Message:
        embed, view = self._build()
        self.view = view
        self._view_phase = self.match.phase
        self.message = await self.channel.send(embed=embed, view=view)
        return self.message

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        """Re-render the scrim message for the current phase."""
        embed, view = self._build()
        if self.view is not None and self.view is not view:
            self.view.stop()
        self.view = view
        self._view_phase = self.match.phase

        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=view)
            return
        if self.message is not None:
            try:
                await self.message.edit(embed=embed, view=view)
            except discord.HTTPException:
                log.exception("Failed to edit scrim message")

    async def _private(self, interaction: discord.Interaction, message: str) -> None:
        """Reply so only the person who clicked can see it."""
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def _deny(self, interaction: discord.Interaction, message: str) -> None:
        await self._private(interaction, message)

    # ------------------------------------------------------------ gatekeeping

    def join_blocker(self, user_id: int) -> str | None:
        """Why this user may not queue, or None if they are clear."""
        active, reason, expiry = self.store.blacklist_state(self.guild_id, user_id)
        if active:
            when = f" until {timestamp(expiry)}" if expiry else " permanently"
            because = f" ({reason})" if reason else ""
            return f"You are blacklisted{when}{because}."
        reason = self.steam_requirement(user_id)
        if reason:
            return (
                f"You need to link a Steam account first -- {reason}. "
                "Run `/register` with your Steam profile URL."
            )
        return None

    def steam_requirement(self, user_id: int) -> str | None:
        """Why this user needs a linked Steam account to play, if they lack one.

        With get5 on this is not optional: a player without a Steam ID cannot
        be written into the match config, so the whole team lock would fail.
        """
        needs_steam = self.setting("account_lock") or self.core.get5.enabled
        if not needs_steam or self.store.steam_id(self.guild_id, user_id):
            return None
        if self.core.get5.enabled:
            return "get5 locks players to their drafted team by Steam ID"
        return "this server requires a linked Steam account"

    def make_player(self, user: discord.abc.User) -> Player:
        rating = self.store.elo(self.guild_id, user.id, self.match.mode)
        return Player(id=user.id, name=user.display_name, elo=rating)

    # --------------------------------------------------------------- handlers

    async def handle_join(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            blocker = self.join_blocker(interaction.user.id)
            if blocker:
                await self._deny(interaction, blocker)
                return
            try:
                self.match.add_player(self.make_player(interaction.user))
            except ScrimError as exc:
                await self._deny(interaction, str(exc))
                return
            formed = await self._maybe_form_teams()
            await self.refresh(interaction)
        await self._post_form(formed)

    async def handle_leave(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            try:
                self.match.remove_player(interaction.user.id)
            except ScrimError as exc:
                await self._deny(interaction, str(exc))
                return
            await self.refresh(interaction)

    async def handle_pick(self, interaction: discord.Interaction, player_id: int) -> None:
        async with self._lock:
            try:
                team, player = self.match.pick(interaction.user.id, player_id)
            except ScrimError as exc:
                await self._deny(interaction, str(exc))
                return
            phase = self.match.phase
            await self.refresh(interaction)

        if phase is Phase.MAP_BAN:
            await self._announce_veto()
        elif phase is Phase.LIVE:
            await self.go_live()
        else:
            self.schedule_autoplay()

    async def handle_ban(self, interaction: discord.Interaction, map_name: str) -> None:
        async with self._lock:
            try:
                team, game_map = self.match.ban(interaction.user.id, map_name)
            except ScrimError as exc:
                await self._deny(interaction, str(exc))
                return
            live = self.match.phase is Phase.LIVE
            await self.refresh(interaction)

        if live:
            await self.go_live()
        else:
            self.schedule_autoplay()

    async def handle_cancel(self, interaction: discord.Interaction) -> None:
        # Matches the gate on /cancel -- otherwise the button routes around it.
        if not self.is_hoster(interaction.user):
            await self._deny(
                interaction, "Only a hoster or an administrator can cancel this scrim."
            )
            return
        async with self._lock:
            self.match.cancel()
            await self.refresh(interaction)
        self.core.forget(self)
        await self._settle_vote_message()
        await self.core.audit.log_action(
            self.guild,
            interaction.user,
            "Cancelled the game",
            f"{self.match.mode} lobby",
            colour=discord.Color.red(),
        )
        await self._teardown()

    async def handle_finish(
        self, interaction: discord.Interaction, winner: int | None, *, tie: bool = False
    ) -> None:
        # Matches the gate on /game end -- otherwise the button routes around it.
        if not self.is_admin(interaction.user):
            await self._deny(
                interaction, "Only an administrator can report the result of this game."
            )
            return
        await interaction.response.defer()
        outcome = "a draw" if winner is None else TEAM_NAMES[winner]
        try:
            await self.finish(winner, tie=tie)
        except ScrimError as exc:
            await self._deny(interaction, str(exc))
            return
        await self.core.audit.log_action(
            self.guild,
            interaction.user,
            "Reported the result",
            f"winner: {outcome}",
            colour=discord.Color.green(),
        )

    async def handle_connect_info(self, interaction: discord.Interaction) -> None:
        """Server address and password, visible only to players in this match."""
        team = self.match.team_of(interaction.user.id)
        if team is None and not self.match.has_player(interaction.user.id):
            await self._deny(
                interaction, "Only players in this match can see the connect details."
            )
            return

        line = f"connect {self.connect_target}"
        if self.match.password:
            line += f"; password {self.match.password}"
        body = [f"{TEAM_EMOJI[team]} You are on **{TEAM_NAMES[team]}**." if team is not None else ""]
        if self.match.selected_map:
            body.append(f"Map: **{self.match.selected_map.display}**")
        body.append(f"```\n{line}\n```")
        body.append("Paste that into your console (`~`).")
        await self._private(interaction, "\n".join(part for part in body if part))
        # The password itself never goes in the log -- only who asked for it.
        await self.core.audit.log_action(
            self.guild, interaction.user, "Requested connect info"
        )

    async def handle_rcon_retry(self, interaction: discord.Interaction) -> None:
        if not self.can_manage(interaction.user):
            await self._deny(interaction, "Only the scrim host or a moderator can do that.")
            return
        if self.match.selected_map is None:
            await self._deny(interaction, "No map has been selected yet.")
            return
        await interaction.response.defer()
        await self.core.audit.log_action(
            self.guild,
            interaction.user,
            "Re-sent the map change",
            f"`{self.match.selected_map.name}`",
        )
        await self.apply_map()

    async def handle_timeout(self, view: discord.ui.View) -> None:
        """A captain ran out the clock -- make the pick for them."""
        if view is not self.view or self.finished:
            return
        async with self._lock:
            before = self.match.phase
            if self.match.phase is Phase.DRAFT:
                result = self.match.auto_pick()
                if result is None:
                    return
                team, player = result
                note = (
                    f"⏱️ {TEAM_EMOJI[team]} **{TEAM_NAMES[team]}** ran out of time -- "
                    f"auto-picked {player.mention}."
                )
            elif self.match.phase is Phase.MAP_BAN:
                result = self.match.auto_ban()
                if result is None:
                    return
                team, game_map = result
                note = (
                    f"⏱️ {TEAM_EMOJI[team]} **{TEAM_NAMES[team]}** ran out of time -- "
                    f"auto-banned `{game_map.name}`."
                )
            else:
                return
            phase = self.match.phase
            changed = phase is not before
            await self.refresh()

        await self._log_line(note)
        if changed and phase is Phase.MAP_BAN:
            await self._announce_veto()
        elif changed and phase is Phase.LIVE:
            await self.go_live()

    async def handle_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Unhandled error in scrim view", exc_info=error)
        await self._deny(interaction, "Something went wrong handling that interaction.")

    # ------------------------------------------------------- command-driven

    async def force_join(self, user: discord.abc.User) -> Player:
        # Staff may override the blacklist, but not the Steam requirement:
        # one unregistered player stops get5 locking the teams for everyone.
        reason = self.steam_requirement(user.id)
        if reason:
            raise ScrimError(
                f"{user.display_name} has no Steam account linked -- {reason}. "
                "They need to `/register` first."
            )
        async with self._lock:
            player = self.make_player(user)
            self.match.add_player(player)
            formed = await self._maybe_form_teams()
            await self.refresh()
        await self._post_form(formed)
        return player

    async def force_drop(self, user_id: int) -> Player:
        async with self._lock:
            player = self.match.remove_player(user_id, force=True)
            await self.refresh()
        await self._refresh_vote_message()
        await self.core.roles.clear_member(self.guild, user_id)
        return player

    async def substitute(self, out_id: int, user: discord.abc.User) -> tuple[Player, int | None]:
        async with self._lock:
            outgoing, team = self.match.substitute(out_id, self.make_player(user))
            await self.refresh()
        await self._refresh_vote_message()
        await self.core.roles.clear_member(self.guild, outgoing.id)
        if team is not None:
            await self.core.roles.assign_member(self.guild, user.id, team)
        if team is not None and self.setting("auto_voice"):
            try:
                await self.core.voice.move_to_team(self.guild, user, team)
            except Exception:  # noqa: BLE001
                log.exception("Failed to move a substitute into team voice")
        await self.sync_get5_roster(outgoing, user, team)
        where = f" on {TEAM_NAMES[team]}" if team is not None else ""
        await self._log_line(f"🔄 {user.mention} subbed in for {outgoing.mention}{where}.")
        return outgoing, team

    async def sync_get5_roster(
        self, outgoing: Player, incoming: discord.abc.User, team: int | None
    ) -> None:
        """Tell get5 about a sub once the match config is already loaded.

        Before the config is built there is nothing to patch -- it will be
        generated from the current roster. Afterwards the server is still
        enforcing the old list, so the replacement gets moved out by
        get5_check_auths and the player they replaced stays whitelisted.
        """
        entry = self._get5
        if entry is None or team is None or not self.config.rcon_enabled:
            return

        in_steam = self.store.steam_id(self.guild_id, incoming.id)
        if not in_steam:
            self.get5_status = (
                f"⚠️ {incoming.display_name} has no Steam account registered, so "
                "get5 was not updated -- they will be moved to spectator."
            )
            await self.refresh()
            return

        out_steam = self.store.steam_id(self.guild_id, outgoing.id)
        try:
            await self.get5.swap_player(
                entry,
                out_steam_id=out_steam,
                in_steam_id=in_steam,
                team=team,
                name=incoming.display_name,
            )
        except RconError as exc:
            self.get5_status = self._safe(f"⚠️ Could not update the get5 roster: {exc}")
            log.warning("get5 roster swap failed: %s", exc)
        else:
            log.info(
                "get5 roster: %s -> %s on team%d", out_steam, in_steam, team + 1
            )
            self.get5_status = (
                f"🔄 get5 roster updated: {incoming.display_name} replaces "
                f"{outgoing.name} on {TEAM_NAMES[team]}."
            )
        await self.refresh()

    def _sync_captain_blacklist(self) -> None:
        """Pick up /blackcapt changes made since the lobby opened."""
        self.match.captain_blacklist = set(self.store.captain_blacklisted(self.guild_id))

    async def force_captain(self, user: discord.abc.User, team: int) -> Player:
        async with self._lock:
            self._sync_captain_blacklist()
            player = self.match.set_forced_captain(user.id, team)
            await self.refresh()
        await self._log_line(
            f"{TEAM_EMOJI[team]} {player.mention} is now captain of **{TEAM_NAMES[team]}**."
        )
        return player

    async def redraft(self) -> None:
        await self._teardown()
        async with self._lock:
            self._sync_captain_blacklist()
            self.match.redraft()
            self.rcon_status = None
            self.get5_status = None
            self.final_score = None
            await self.refresh()
        await self._log_line("♻️ Teams have been re-drafted.")
        if self.match.phase is Phase.MAP_BAN:
            await self._announce_veto()
        elif self.match.phase is Phase.LIVE:
            await self.go_live()

    async def cancel(self) -> None:
        async with self._lock:
            self.match.cancel()
            await self.refresh()
        self.core.forget(self)
        await self._settle_vote_message()
        await self._teardown()

    # ------------------------------------------------------------ transitions

    # ------------------------------------------------------------- test bots

    async def add_test_players(self, count: int) -> int:
        """Pad the lobby with fake players for a dry run."""
        added = 0
        async with self._lock:
            for _ in range(count):
                if self.match.is_full:
                    break
                index = 1
                while self.match.has_player(TEST_ID_BASE + index):
                    index += 1
                self.match.add_player(
                    Player(
                        id=TEST_ID_BASE + index,
                        name=f"TestBot{index:02d}",
                        elo=self.store.default_elo,
                        is_bot=True,
                    )
                )
                added += 1
            formed = await self._maybe_form_teams()
            await self.refresh()
        await self._post_form(formed)
        return added

    def schedule_autoplay(self) -> None:
        """If a /testfill bot is on the clock, have it act shortly."""
        actor = self.match.current_actor
        if actor is None or not actor.is_bot:
            return
        if self._autoplay_task is not None and not self._autoplay_task.done():
            return
        self._autoplay_task = asyncio.create_task(self._autoplay())

    async def _autoplay(self) -> None:
        delay = self.config.test_bot_delay
        while True:
            actor = self.match.current_actor
            if actor is None or not actor.is_bot or self.finished:
                return
            await asyncio.sleep(delay)

            async with self._lock:
                before = self.match.phase
                if self.match.phase is Phase.DRAFT:
                    if self.match.auto_pick() is None:
                        return
                elif self.match.phase is Phase.MAP_BAN:
                    if self.match.auto_ban() is None:
                        return
                else:
                    return
                after = self.match.phase
                await self.refresh()

            if after is not before and after is Phase.MAP_BAN:
                await self._announce_veto()
            elif after is not before and after is Phase.LIVE:
                await self.go_live()
                return

    async def _maybe_form_teams(self) -> bool:
        if not self.match.is_full or self.match.phase is not Phase.LOBBY:
            return False
        self._sync_captain_blacklist()
        self.match.start_teams()
        return True

    async def _post_form(self, formed: bool) -> None:
        if not formed:
            return
        if self.match.phase is Phase.DRAFT:
            await self._announce_draft()
            await self._open_vote_message()
            self.schedule_autoplay()
        elif self.match.phase is Phase.MAP_BAN:
            await self._announce_teams()
            await self._open_vote_message()
            await self._announce_veto()
        elif self.match.phase is Phase.LIVE:
            await self._announce_teams()
            await self.go_live()

    async def go_live(self) -> None:
        """Teams and map are settled -- configure the server and open it up."""
        # The match model locks the region the moment the map is decided.
        await self._settle_vote_message()
        await self._apply_team_roles()
        await self._move_to_team_voice()
        if self.setting("random_password"):
            self.match.password = random_password()
        await self.refresh()
        await self.apply_map()

    async def finish(self, winner: int | None, *, tie: bool = False) -> None:
        async with self._lock:
            self.match.finish(winner, tie=tie)
            self.changes = await self._apply_ratings(winner, tie)
            # Read the scoreboard before teardown drops the match entry.
            await self._load_player_stats()
            await self._post_result()
            await self.refresh()
        self.core.forget(self)
        await self._settle_vote_message()
        await self._teardown(winner)
        await self._cleanup()

    # ----------------------------------------------------------- region vote

    def vote_embed(self) -> discord.Embed:
        return embeds.region_vote_embed(self.match)

    async def _open_vote_message(self) -> None:
        """Post the server vote as its own message.

        Kept off the draft/veto view on purpose: any click on a view resets
        its timeout, so votes there would keep resetting the pick clock.
        """
        if not self.match.vote_open or self.vote_message is not None:
            return
        self.vote_view = views.RegionVoteView(self)
        try:
            self.vote_message = await self.channel.send(
                embed=self.vote_embed(), view=self.vote_view
            )
        except (discord.HTTPException, AttributeError, TypeError):
            log.exception("Could not post the server vote")
            self.vote_view.stop()
            self.vote_view = None
            return
        await self._bot_votes()

    async def _bot_votes(self) -> None:
        """/testfill bots vote at random so the vote can be tested solo."""
        bots = [
            p for p in self.match.players
            if p.is_bot and p.id not in self.match.region_votes
        ]
        if not bots or not self.match.vote_open:
            return
        async with self._lock:
            for bot in bots:
                if not self.match.vote_open:
                    break
                self.match.vote_region(bot.id, random.choice(self.match.regions))
        if self.match.vote_open:
            await self._refresh_vote_message()
        else:
            await self._settle_vote_message()

    async def _refresh_vote_message(self) -> None:
        if self.vote_message is None:
            return
        view = self.vote_view if self.match.vote_open else None
        try:
            await self.vote_message.edit(embed=self.vote_embed(), view=view)
        except discord.HTTPException:
            log.debug("Could not update the server vote message", exc_info=True)

    async def _settle_vote_message(self) -> None:
        """Once the region is locked, or the game is over: freeze the vote
        message on the outcome and say where the game is being played."""
        if self.match.vote_open and not self.match.finished:
            return
        if self.vote_view is not None:
            self.vote_view.stop()
            self.vote_view = None
        await self._refresh_vote_message()
        await self._announce_region()

    async def _announce_region(self) -> None:
        match = self.match
        if self._region_announced or match.region is None or match.finished:
            return
        self._region_announced = True
        name = region_name(match.region)
        tally = match.region_tally()
        score = " : ".join(str(tally[r]) for r in match.regions)
        if match.region_decided_by == "vote":
            note = f"🌍 Server vote closed ({score}) -- playing on **{name}**."
        elif match.region_decided_by == "coin":
            what = (
                "Nobody voted" if not match.region_votes
                else f"The server vote tied {score}"
            )
            note = f"🌍 {what} -- a coin flip picked **{name}**."
        elif match.region_decided_by == "admin":
            note = f"🌍 An administrator set the server to **{name}**."
        else:
            return  # defaulted with no vote: the live message names the server
        await self._log_line(note)

    async def handle_region_vote(self, interaction: discord.Interaction, region: str) -> None:
        async with self._lock:
            try:
                closed = self.match.vote_region(interaction.user.id, region)
            except ScrimError as exc:
                await self._deny(interaction, str(exc))
                return
        view = None if closed else self.vote_view
        try:
            await interaction.response.edit_message(embed=self.vote_embed(), view=view)
        except discord.HTTPException:
            log.debug("Could not update the vote message", exc_info=True)
        await self._private(
            interaction,
            f"You voted **{region_name(region)}**."
            + ("" if closed else " You can change it until the vote closes."),
        )
        if closed:
            await self._settle_vote_message()

    async def set_region(self, region: str) -> None:
        """Administrator override -- only while the vote is open."""
        async with self._lock:
            self.match.override_region(region)
        await self._settle_vote_message()

    # ------------------------------------------------------------------ get5

    @property
    def get5(self):
        return self.game_server.get5

    def roster_steam_ids(self) -> dict[int, str | None]:
        return {
            player.id: self.store.steam_id(self.guild_id, player.id)
            for player in self.match.players
        }

    async def setup_get5(self) -> None:
        """Load the match config once the map has changed, then wait for players."""
        if not self.get5.enabled or self.match.selected_map is None:
            return
        if self.match.has_bots:
            self.get5_status = (
                "🧪 Test game -- skipping get5. The map change above was real; "
                "run a game with real, registered players to exercise get5."
            )
            await self.refresh()
            return
        if not self.config.rcon_enabled:
            self.get5_status = "⚠️ get5 is configured but RCON is disabled."
            await self.refresh()
            return

        steam_ids = self.roster_steam_ids()
        missing = [
            f"<@{uid}>" for uid, steam_id in steam_ids.items() if not steam_id
        ]
        if missing:
            self.get5_status = (
                "❌ get5 needs every player registered. Missing: " + ", ".join(missing)
            )
            await self.refresh()
            return

        matchid = f"{self.guild_id}-{getattr(self.channel, 'id', 0)}-{int(utcnow().timestamp())}"
        cvars = {}
        if self.match.password:
            cvars["sv_password"] = self.match.password
        try:
            entry = self.get5.prepare(
                self.match, {k: v for k, v in steam_ids.items() if v},
                matchid=matchid, cvars=cvars,
            )
        except Get5Error as exc:
            self.get5_status = f"❌ {exc}"
            await self.refresh()
            return

        self._get5 = entry
        self.get5_status = "⏳ Waiting for the map to finish loading..."
        await self.refresh()

        # The server is unreachable while it swaps maps. The delay is a floor,
        # not a bet: how long a map takes to load varies, so wait for RCON to
        # actually answer before assuming the server is back.
        await asyncio.sleep(self.config.get5_load_delay)

        async def still_loading(attempt: int) -> None:
            if attempt == 1 or attempt % 5 == 0:
                self.get5_status = (
                    "⏳ Waiting for the server to finish loading the map..."
                )
                await self.refresh()

        try:
            ready = await self.server.wait_until_ready(
                timeout=self.config.get5_load_timeout, on_wait=still_loading
            )
        except RconError as exc:
            ready = False
            log.error("RCON never recovered after the map change: %s", exc)
        if not ready:
            self.get5_status = (
                "❌ The server never came back on RCON after the map change, so "
                "the match config was not loaded. Retry with **Change map again**."
            )
            log.error("get5 load skipped for %s: server not answering RCON", matchid)
            await self.refresh()
            return

        try:
            # A previous scrim left loaded would make get5 refuse this one.
            await self.get5.ensure_idle()
            sent = await self.get5.load(entry)
        except (RconError, Get5Error) as exc:
            self.get5_status = f"❌ Could not load the match config: {self._safe(str(exc))}"
            log.error("get5 load failed for %s: %s", matchid, exc)
            await self.refresh()
            return

        # get5 reports most failures to its own error log rather than over
        # RCON, so confirm against get5_status instead of trusting silence.
        try:
            verified = await self.get5.verify_loaded(entry)
        except Get5Error as exc:
            verified = False
            log.error("get5 verification failed for %s: %s", matchid, exc)

        if verified is False:
            self.get5_status = (
                "❌ The server did not accept the match config. Check "
                "`addons/sourcemod/logs/errors_*.log` on the game server for the "
                "reason, then retry with **Change map again**."
            )
            log.error(
                "get5 config %s was sent (%s) but get5_status still reports no match",
                matchid,
                sent,
            )
            await self.refresh()
            return

        log.info("get5 match %s loaded via %s (verified=%s)", matchid, sent, verified)
        note = ""
        if verified is None:
            # get5_status needs smjansson, which the match config itself does
            # not. Everything still works except reading the final score back.
            note = (
                " (unverified: `get5_status` needs the smjansson extension, so "
                "the result cannot be read automatically -- use `/game end`)"
            )
        self.get5_status = (
            f"✅ Match config loaded{note}. Teams are locked -- "
            f"waiting for **0/{len(entry.steam_ids)}** players to connect."
        )
        await self.refresh()

        # Watch for the result from the moment the config is loaded. This must
        # not hang off the auto-start path: a match readied up by hand never
        # goes through it, and the result would then never be read at all.
        self._start_result_watch(entry)

        if self.config.get5_autostart:
            self._get5_task = asyncio.create_task(self._await_roster(entry))

    def _start_result_watch(self, entry) -> None:
        if self._get5_result_task is not None and not self._get5_result_task.done():
            return
        self._get5_result_task = asyncio.create_task(self._await_result(entry))

    async def _await_roster(self, entry) -> None:
        """Poll the server and force the match live once everyone is on."""

        async def on_progress(present, missing, connected, empty_polls) -> None:
            total = len(entry.steam_ids)
            text = (
                f"✅ Match config loaded. Teams are locked -- "
                f"waiting for **{len(present)}/{total}** players to connect."
            )
            if not connected and empty_polls >= 3:
                # Either nobody is on, or `status` is not parsing. Say so
                # rather than sitting on 0/10 with no explanation.
                text += (
                    "\n⚠️ No players are being read from the server's `status`. "
                    f"If people *are* connected, run `{self.rcon_hint('status')}` and send me "
                    "the output -- the parser is not matching your layout."
                )
            elif missing:
                names = sorted(entry.name_for(sid) for sid in missing)
                text += "\nStill missing: " + ", ".join(f"`{n}`" for n in names[:10])
                if connected - entry.steam_ids:
                    text += (
                        f"\n({len(connected - entry.steam_ids)} connected player(s) "
                        "are not on the roster -- wrong Steam account?)"
                    )
            self.get5_status = text
            await self.refresh()

        try:
            complete = await self.get5.wait_for_roster(entry, on_progress=on_progress)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the scrim survives a failed watcher
            log.exception("get5 roster watcher failed")
            self.get5_status = "⚠️ Stopped watching for players (see the bot log)."
            await self.refresh()
            return

        if not complete:
            self.get5_status = (
                "⚠️ Not everyone connected in time -- start the match with "
                f"`!ready` in game, or `{self.rcon_hint('get5_forceready')}`."
            )
            await self.refresh()
            return

        try:
            await self.get5.force_start()
        except RconError as exc:
            self.get5_status = f"⚠️ Everyone is connected but forceready failed: {exc}"
            await self.refresh()
            return

        self.get5_status = "🔴 All players connected -- match started."
        await self._log_line("🔴 Everyone is on the server. **Match is live.**")
        await self.refresh()
        self._start_result_watch(entry)  # already running unless it was cancelled

    async def _await_result(self, entry) -> None:
        """Watch get5 for the final score, then settle Elo without anyone asking."""

        async def on_update(scores: tuple[int, int]) -> None:
            self.get5_status = (
                f"🔴 Live -- **{TEAM_NAMES[0]} {scores[0]} : {scores[1]} {TEAM_NAMES[1]}**"
            )
            await self.refresh()

        try:
            outcome = await self.get5.wait_for_result(entry, on_update=on_update)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let the watcher break the scrim
            log.exception("get5 result watcher failed")
            return

        if outcome is None:
            self.get5_status = (
                "⚠️ The match ended but no score could be read. Close it out with "
                "`/game end`. (get5 writes its stats to "
                f"`{self.get5.stats_path(entry.matchid)}` -- check the bot can read it.)"
            )
            await self.refresh()
            return
        if self.match.finished:
            return

        winner, scores = outcome
        self.final_score = scores
        label = (
            f"{TEAM_NAMES[winner]} win" if winner is not None else "a draw"
        )
        await self._log_line(
            f"🏁 Match over -- **{TEAM_NAMES[0]} {scores[0]} : {scores[1]} "
            f"{TEAM_NAMES[1]}** ({label}). Elo updated."
        )
        try:
            await self.finish(winner, tie=winner is None)
        except ScrimError:
            log.info("Match was already closed out before the watcher finished")

    async def _load_player_stats(self, attempts: int = 3, delay: float = 2.0) -> None:
        """Pull the per-player scoreboard out of get5's stats file.

        The result can land from get5_status a moment before get5 has flushed
        the stats file, so give the file a couple of chances to appear.
        """
        entry = self._get5
        if entry is None or not self.get5.enabled:
            return
        for attempt in range(attempts):
            try:
                stats = await self.get5.fetch_player_stats(entry)
            except Exception:  # noqa: BLE001 - a scoreboard is never worth failing over
                log.exception("Could not read the get5 player stats")
                return
            if stats and any(stats):
                self.player_stats = stats
                return
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
        log.info("No get5 player stats at %s", self.get5.stats_path(entry.matchid))

    async def _post_result(self) -> None:
        """Archive the finished game in the results channel, if there is one.

        On any failure ``result_url`` stays None and the full result -- the
        scoreboard included -- falls back to the scrim message as before.
        """
        channel = self.results_channel
        if channel is None:
            return
        embed = embeds.result_embed(
            self.match, self.changes, self.player_stats, self.final_score
        )
        try:
            message = await channel.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
        except (discord.HTTPException, AttributeError):
            log.exception("Could not post the result to the results channel")
            return
        self.result_url = message.jump_url

    @property
    def results_channel(self):
        """The archive channel, or None when it is unset, missing or the
        channel the game is already being played out in."""
        channel_id = self.setting("results_channel")
        if not channel_id or self.guild is None:
            return None
        channel = self.guild.get_channel(channel_id)
        if channel is None or channel.id == self.channel.id:
            return None
        return channel

    async def _teardown(self, winner: int | None = None) -> None:
        """Undo everything the bot handed out for this game."""
        await self._teardown_get5(winner)
        removed = await self.core.roles.clear_teams(self.guild, self.match)
        if removed:
            log.info("Removed %d team role(s) after the game", removed)

    async def _apply_team_roles(self) -> None:
        """Hand out team roles and, if enabled, pull players into team voice."""
        if not self.match.teams_locked and self.match.phase is not Phase.MAP_BAN:
            return
        try:
            await self.core.roles.apply_teams(self.guild, self.match)
        except Exception:  # noqa: BLE001 - roles must never break a scrim
            log.exception("Failed to apply team roles")

    async def _move_to_team_voice(self) -> None:
        """Pull players into their team call once the map is settled.

        Deliberately after the veto rather than at team lock: captains need to
        stay where they are to run the veto, and moving mid-veto would scatter
        everyone while the dropdown is still in use.
        """
        if not self.setting("auto_voice"):
            return
        try:
            moved, waiting = await self.core.voice.move_teams(self.guild, self.match)
        except Exception:  # noqa: BLE001 - voice must never break a scrim
            log.exception("Failed to move players into team voice")
            return
        if moved or waiting:
            note = f"🔊 Moved {moved} player(s) into their team call."
            if waiting:
                note += (
                    f" {waiting} were not in any voice channel, so Discord could "
                    "not pull them in -- they can join one and run `/moveteams`."
                )
            await self._log_line(note)

    @staticmethod
    def _stop(task: asyncio.Task | None) -> None:
        """Cancel a watcher -- but never the task we are currently running in."""
        if task is None or task.done():
            return
        try:
            if task is asyncio.current_task():
                return
        except RuntimeError:
            pass
        task.cancel()

    async def _teardown_get5(self, winner: int | None = None) -> None:
        task, self._get5_task = self._get5_task, None
        self._stop(task)
        result_task, self._get5_result_task = self._get5_result_task, None
        self._stop(result_task)
        autoplay, self._autoplay_task = self._autoplay_task, None
        self._stop(autoplay)
        entry, self._get5 = self._get5, None
        if entry is None:
            return
        if self.config.rcon_enabled:
            try:
                await self.get5.end_match(winner)
            except RconError as exc:
                log.warning("get5_endmatch failed: %s", exc)
        self.get5.forget(entry.matchid)

    async def _apply_ratings(self, winner: int | None, tie: bool) -> dict[int, tuple[int, int]]:
        match = self.match
        win_team = 0 if winner is None else winner
        lose_team = 1 - win_team
        # Ratings belong to Discord accounts, so /testfill bots are skipped --
        # and with a side missing, nobody's rating moves. A test game therefore
        # never touches the real ladder.
        winners = [p.id for p in match.teams[win_team] if not p.is_bot]
        losers = [p.id for p in match.teams[lose_team] if not p.is_bot]
        if not winners or not losers:
            return {}

        mode = match.mode
        win_ratings = [self.store.elo(self.guild_id, uid, mode) for uid in winners]
        lose_ratings = [self.store.elo(self.guild_id, uid, mode) for uid in losers]
        win_delta, lose_delta = team_deltas(win_ratings, lose_ratings, tie=tie)
        deltas = {uid: win_delta for uid in winners}
        deltas.update({uid: lose_delta for uid in losers})
        return await self.store.record_result(
            self.guild_id, mode, deltas, winners, losers, tie=tie
        )

    async def _cleanup(self) -> None:
        if self.setting("server_cleanup") and self.config.rcon_enabled:
            try:
                kicked = await self.server.kick_all("Scrim finished")
                await self._log_line(f"🧹 Kicked {kicked} player(s) from the server.")
            except RconError as exc:
                log.warning("Server cleanup failed: %s", exc)
        if self.setting("voice_cleanup"):
            await self._voice_cleanup()

    async def _voice_cleanup(self) -> None:
        guild = self.guild
        if guild is None:
            return
        moved = 0
        for player in self.match.players:
            member = guild.get_member(player.id)
            if member is None or member.voice is None:
                continue
            try:
                await member.move_to(None, reason="Scrim finished")
                moved += 1
            except discord.HTTPException:
                log.debug("Could not disconnect %s from voice", player.id)
        if moved:
            await self._log_line(f"🔇 Disconnected {moved} player(s) from voice.")

    # ------------------------------------------------------------ permissions

    @staticmethod
    def is_admin(user: discord.abc.User) -> bool:
        perms = getattr(user, "guild_permissions", None)
        return bool(perms and perms.administrator)

    def is_hoster(self, user: discord.abc.User) -> bool:
        if self.is_admin(user):
            return True
        role_id = self.setting("hoster_role")
        if not role_id:
            return False
        return any(role.id == role_id for role in getattr(user, "roles", []))

    def can_manage(self, user: discord.abc.User) -> bool:
        """Host, hoster role, or a moderator -- used by /cancel and the map retry."""
        if user.id == self.match.host_id or self.is_hoster(user):
            return True
        perms = getattr(user, "guild_permissions", None)
        return bool(perms and (perms.manage_guild or perms.manage_channels))

    # ------------------------------------------------------------ server side

    def server_commands(self) -> list[tuple[str, bool]]:
        """(command, expect_response) pairs fired when the map is locked in."""
        game_map = self.match.selected_map
        assert game_map is not None
        commands: list[tuple[str, bool]] = [
            (f'mp_teamname_1 "{TEAM_NAMES[0]}"', True),
            (f'mp_teamname_2 "{TEAM_NAMES[1]}"', True),
        ]
        if self.setting("random_password"):
            commands.append((f'sv_password "{self.match.password or ""}"', True))
        commands.append((game_map.changelevel_command(), False))  # drops the connection
        return commands

    def _safe(self, message: str) -> str:
        """Strip every server's RCON/SSH address out of anything shown in the channel."""
        for secret in self.core.secrets:
            message = message.replace(secret, "the game server")
        return message

    async def apply_map(self) -> str:
        """Push the vetoed map to the game server over RCON."""
        game_map = self.match.selected_map
        if game_map is None:
            return "No map selected."

        if not self.config.rcon_enabled:
            self.rcon_status = (
                f"⚠️ RCON is disabled -- run `{game_map.changelevel_command()}` manually."
            )
            await self.refresh()
            return self.rcon_status

        try:
            for command, expect_response in self.server_commands():
                await self.server.execute(command, expect_response=expect_response)
        except RconAuthError as exc:
            self.rcon_status = self._safe(f"❌ RCON auth failed: {exc}")
        except RconError as exc:
            self.rcon_status = self._safe(f"❌ Could not change the map: {exc}")
        except Exception as exc:  # noqa: BLE001 - never let rcon kill the scrim
            log.exception("Unexpected RCON failure")
            self.rcon_status = f"❌ Unexpected RCON failure: {exc}"
        else:
            # Nothing to report on success: the map is already in the embed
            # title, and the RCON address must never be shown to players.
            self.rcon_status = None
            await self.refresh()
            await self.setup_get5()
            return self.rcon_status

        await self.refresh()
        return self.rcon_status

    # ---------------------------------------------------------- announcements

    async def _log_line(self, content: str) -> None:
        try:
            await self.channel.send(content, delete_after=120, silent=True)
        except (discord.HTTPException, TypeError, AttributeError):
            pass

    async def _announce_draft(self) -> None:
        captains = self.match.captains
        await self._log_line(
            f"Lobby is full. Captains: {TEAM_EMOJI[0]} {captains[0].mention} and "
            f"{TEAM_EMOJI[1]} {captains[1].mention}. {captains[0].mention} picks first."
        )

    async def _announce_teams(self) -> None:
        lines = [
            f"{TEAM_EMOJI[t]} **{TEAM_NAMES[t]}**: "
            + ", ".join(p.mention for p in self.match.teams[t])
            for t in (0, 1)
        ]
        await self._log_line(
            f"Teams set with pick mode `{self.match.pick_mode}`.\n" + "\n".join(lines)
        )

    async def _announce_veto(self) -> None:
        await self._apply_team_roles()
        banner = self.match.current_banner
        if banner is not None:
            await self._log_line(f"Map veto starts with {banner.mention}.")
        self.schedule_autoplay()
