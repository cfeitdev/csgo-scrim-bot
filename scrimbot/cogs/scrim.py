"""Game lifecycle: hosting, the draft, subs, and closing games out."""

from __future__ import annotations

import logging
import random

import discord
from discord import app_commands
from discord.ext import commands

from ..get5 import Get5Error
from ..maps import resolve_pool
from ..match import TEAM_EMOJI, TEAM_NAMES, Match, Phase, Player, ScrimError
from ..rcon import RconError
from ..regions import DEFAULT_REGION, REGION_CHOICES, region_name
from ..session import ScrimSession
from ..voice import VoiceError
from ..util import utcnow
from .base import ScrimCogBase

log = logging.getLogger(__name__)

# A structurally valid steamID64 that nobody owns -- team2 must exist in a get5
# config, but for a load probe it never needs to connect.
PROBE_PLACEHOLDER_ID = 1
PROBE_PLACEHOLDER_STEAM = "76561197960265730"

TEAM_CHOICES = [
    app_commands.Choice(name=TEAM_NAMES[0], value=0),
    app_commands.Choice(name=TEAM_NAMES[1], value=1),
]


class ScrimCog(ScrimCogBase):
    """/host, /cancel, /game, and the staff overrides for a running lobby."""

    game = app_commands.Group(
        name="game",
        description="Manage the game lobby",
        default_permissions=discord.Permissions(administrator=True),
    )

    # ------------------------------------------------------------------ host

    @app_commands.command(name="host", description="Create a new game lobby.")
    @app_commands.describe(
        gamemode="Stats bucket for this game (default from config)",
        size="Total players, must be even (default from config)",
    )
    async def host(
        self,
        interaction: discord.Interaction,
        gamemode: str | None = None,
        size: app_commands.Range[int, 2, 20] | None = None,
    ) -> None:
        if not await self.require_hoster(interaction):
            return
        existing = self.session(interaction)
        if existing is not None:
            await self.deny(
                interaction,
                "A game is already running in this channel. Use `/cancel` to end it first.",
            )
            return
        if size is not None and size % 2:
            await self.deny(interaction, "Lobby size has to be an even number.")
            return

        guild_id = interaction.guild_id or 0
        settings = self.store.settings(guild_id)
        match = Match(
            host_id=interaction.user.id,
            size=size or self.config.lobby_size,
            mode=gamemode or self.config.default_mode,
            map_pool=resolve_pool(settings.get("map_pool"), settings.get("workshop_ids")),
            pick_mode=settings.get("pick_mode", "captains"),
            random_captains=bool(settings.get("random_captains", True)),
            vetoing=bool(settings.get("vetoing", True)),
            regions=self.core.regions,
            default_region=DEFAULT_REGION,
        )
        session = ScrimSession(self.core, interaction.channel, match)
        self.core.register(interaction.channel_id, session)

        await self.reply(interaction, f"Lobby opened for **{match.mode}**.")
        await session.start()

        role_id = settings.get("notify_role")
        if role_id:
            await interaction.channel.send(
                f"<@&{role_id}> {interaction.user.mention} is hosting a **{match.mode}** game.",
                allowed_mentions=discord.AllowedMentions(roles=True),
            )

    @app_commands.command(name="cancel", description="Cancel the game in this channel.")
    async def cancel(self, interaction: discord.Interaction) -> None:
        if not await self.require_hoster(interaction):
            return
        session = await self.require_session(interaction)
        if session is None:
            return
        await session.cancel()
        await self.reply(interaction, "Game cancelled.", ephemeral=False)

    @app_commands.command(name="notify", description="Alert members about the lobby in progress.")
    @app_commands.describe(message="Optional note to include with the ping")
    async def notify(self, interaction: discord.Interaction, message: str | None = None) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        role_id = self.store.setting(interaction.guild_id or 0, "notify_role")
        if not role_id:
            await self.deny(
                interaction, "No notify role is configured. Set one with `/setnotifyrole`."
            )
            return

        match = session.match
        if match.phase is Phase.LOBBY:
            status = f"**{len(match.players)}/{match.size}** in queue"
        else:
            status = f"in the {match.phase.value.replace('_', ' ')} phase"
        jump = f"\n{session.message.jump_url}" if session.message else ""
        note = f"\n> {message}" if message else ""
        await interaction.channel.send(
            f"<@&{role_id}> **{match.mode}** lobby {status}.{note}{jump}",
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        await self.reply(interaction, "Ping sent.")

    # ------------------------------------------------------------------ game

    @game.command(name="end", description="End the game lobby and record the result.")
    @app_commands.describe(winner="Which team won (leave blank for a draw)")
    @app_commands.choices(winner=TEAM_CHOICES)
    async def game_end(
        self,
        interaction: discord.Interaction,
        winner: app_commands.Choice[int] | None = None,
    ) -> None:
        session = await self.require_session(interaction)
        if session is None:
            return
        if not await self.require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await session.finish(
                winner.value if winner else None, tie=winner is None
            )
        except ScrimError as exc:
            await self.deny(interaction, str(exc))
            return
        outcome = f"{TEAM_NAMES[winner.value]} win" if winner else "a draw"
        await self.reply(interaction, f"Game closed out as {outcome}.")

    @app_commands.command(
        name="region", description="Override the server vote (only while it is open)."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(server="Where this game will be played")
    @app_commands.choices(server=REGION_CHOICES)
    async def region(
        self, interaction: discord.Interaction, server: app_commands.Choice[str]
    ) -> None:
        if not await self.require_admin(interaction):
            return
        session = await self.require_session(interaction)
        if session is None:
            return
        if not session.match.has_region_vote:
            await self.deny(interaction, "Only one server is configured, so there is no vote.")
            return
        try:
            await session.set_region(server.value)
        except ScrimError as exc:
            await self.deny(interaction, str(exc))
            return
        await self.reply(interaction, f"Server set to **{region_name(server.value)}**.")

    @app_commands.command(name="redraft", description="Re-draft the game lobby.")
    async def redraft(self, interaction: discord.Interaction) -> None:
        if not await self.require_hoster(interaction):
            return
        session = await self.require_session(interaction)
        if session is None:
            return
        try:
            await session.redraft()
        except ScrimError as exc:
            await self.deny(interaction, str(exc))
            return
        await self.reply(interaction, "Teams re-drafted.")

    @app_commands.command(name="sub", description="Substitute a user into a game.")
    @app_commands.describe(out="Player leaving the game", into="Player taking their place")
    async def sub(
        self, interaction: discord.Interaction, out: discord.Member, into: discord.Member
    ) -> None:
        if not await self.require_hoster(interaction):
            return
        session = await self.require_session(interaction)
        if session is None:
            return
        blocker = session.join_blocker(into.id)
        if blocker:
            await self.deny(interaction, f"{into.mention} cannot play: {blocker}")
            return
        try:
            outgoing, team = await session.substitute(out.id, into)
        except ScrimError as exc:
            await self.deny(interaction, str(exc))
            return
        where = f" on {TEAM_NAMES[team]}" if team is not None else ""
        await self.reply(interaction, f"{into.mention} replaced {outgoing.mention}{where}.")

    # ------------------------------------------------------------- overrides

    @app_commands.command(name="forcejoin", description="Forcefully join a user into a lobby.")
    @app_commands.describe(user="Player to add to the queue")
    async def forcejoin(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not await self.require_hoster(interaction):
            return
        session = await self.require_session(interaction)
        if session is None:
            return
        try:
            await session.force_join(user)
        except ScrimError as exc:
            await self.deny(interaction, str(exc))
            return
        await self.reply(interaction, f"Added {user.mention} to the lobby.")

    @app_commands.command(name="forcedrop", description="Force a user out of a lobby.")
    @app_commands.describe(user="Player to remove")
    async def forcedrop(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not await self.require_hoster(interaction):
            return
        session = await self.require_session(interaction)
        if session is None:
            return
        try:
            await session.force_drop(user.id)
        except ScrimError as exc:
            await self.deny(interaction, str(exc))
            return
        note = ""
        if session.match.teams_locked:
            note = " Their team is now a player short -- use `/sub` to fill the slot."
        await self.reply(interaction, f"Removed {user.mention} from the lobby.{note}")

    @app_commands.command(
        name="forcecaptain", description="Force a user to be captain of a game."
    )
    @app_commands.describe(user="Player to make captain", team="Which side they captain")
    @app_commands.choices(team=TEAM_CHOICES)
    async def forcecaptain(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        team: app_commands.Choice[int],
    ) -> None:
        if not await self.require_hoster(interaction):
            return
        session = await self.require_session(interaction)
        if session is None:
            return
        try:
            await session.force_captain(user, team.value)
        except ScrimError as exc:
            await self.deny(interaction, str(exc))
            return
        await self.reply(
            interaction,
            f"{TEAM_EMOJI[team.value]} {user.mention} will captain **{team.name}**.",
        )

    @app_commands.command(
        name="spec",
        description="Spectate: join a team's voice channel muted so you can listen in.",
    )
    @app_commands.describe(team="Which team's voice channel to listen to")
    @app_commands.choices(team=TEAM_CHOICES)
    async def spec(
        self,
        interaction: discord.Interaction,
        team: app_commands.Choice[int],
    ) -> None:
        # Self-service only: you can move and mute yourself, nobody else.
        member = interaction.user
        try:
            channel = await self.core.voice.spectate(interaction.guild, member, team.value)
        except VoiceError as exc:
            await self.deny(interaction, str(exc))
            return

        await self.core.audit.log_action(
            interaction.guild, member, "Started spectating", channel.name
        )
        await self.reply(
            interaction,
            f"{TEAM_EMOJI[team.value]} Moved you into {channel.mention} and muted you. "
            "The mute lifts by itself when you leave that call.",
        )

    @app_commands.command(
        name="moveteams",
        description="Pull everyone in the current game into their team voice channel.",
    )
    async def moveteams(self, interaction: discord.Interaction) -> None:
        if not await self.require_hoster(interaction):
            return
        session = await self.require_session(interaction)
        if session is None:
            return
        if not session.match.teams_locked and session.match.phase is not Phase.MAP_BAN:
            await self.deny(interaction, "Teams have not been picked yet.")
            return

        voice = self.core.voice
        if not voice.spec_channel_ids(interaction.guild):
            await self.deny(
                interaction,
                "No team voice channels are set. An admin can set them with "
                "`/setteamvoice`.",
            )
            return

        await interaction.response.defer(ephemeral=True)
        moved, waiting = await voice.move_teams(interaction.guild, session.match)
        text = f"🔊 Moved **{moved}** player(s) into their team call."
        if waiting:
            text += (
                f"\n**{waiting}** are not in any voice channel — Discord cannot pull "
                "someone in who is not already in a call, so they need to join one "
                "first and then run this again."
            )
        await self.reply(interaction, text)

    @app_commands.command(
        name="unspec",
        description="Stop spectating: lift your spectator mute and leave voice.",
    )
    async def unspec(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        voice = self.core.voice
        if not voice.is_muted(interaction.guild_id or 0, member.id):
            await self.deny(interaction, "You are not spectator-muted.")
            return
        # Unmute first: Discord only accepts the edit while they are connected.
        if await voice.release(interaction.guild, member):
            if await voice.disconnect(interaction.guild, member):
                await self.reply(interaction, "You are unmuted and disconnected from voice.")
            else:
                await self.reply(
                    interaction,
                    "You are unmuted, but the bot could not disconnect you from voice.",
                )
        else:
            await self.deny(
                interaction,
                "You are not connected to voice, so Discord will not accept the "
                "unmute. It clears itself the moment you rejoin a channel.",
            )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        try:
            await self.core.voice.handle_voice_update(member, before, after)
        except Exception:  # noqa: BLE001 - never let this break voice handling
            log.exception("Spectator voice update failed for %s", member.id)

    @app_commands.command(
        name="testfill",
        description="Debug: fill the lobby with fake players so you can test the flow solo.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        captain="Make yourself a captain so you drive one side of the draft",
        count="How many fake players to add (default: fill the lobby)",
    )
    async def testfill(
        self,
        interaction: discord.Interaction,
        captain: bool = True,
        count: app_commands.Range[int, 1, 19] | None = None,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        session = await self.require_session(interaction)
        if session is None:
            return

        match = session.match
        if match.phase is not Phase.LOBBY:
            await self.deny(interaction, "The lobby is already past the queue stage.")
            return

        free = match.size - len(match.players)
        wanted = min(count or free, free)
        if wanted <= 0:
            await self.deny(interaction, "The lobby is already full.")
            return

        added = await session.add_test_players(wanted)
        note = ""
        if captain and match.has_player(interaction.user.id):
            try:
                match.set_forced_captain(interaction.user.id, 0)
                note = " You will captain **Team A**."
            except ScrimError:
                pass
        await self.reply(
            interaction,
            f"🧪 Added **{added}** fake player(s).{note}\n"
            "They pick and ban on their own, so you can drive the whole flow. "
            "get5 is skipped for test games; the map change still fires for real.",
        )

    @app_commands.command(
        name="testget5",
        description="Debug: load a one-player get5 match config to prove the wiring works.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        map="Map to use (default: a random one from the pool)",
        server="Which game server to test (default: EU)",
    )
    @app_commands.choices(server=REGION_CHOICES)
    async def testget5(
        self,
        interaction: discord.Interaction,
        map: str | None = None,
        server: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await self.require_admin(interaction):
            return

        key = server.value if server else DEFAULT_REGION
        if key not in self.core.servers:
            await self.deny(interaction, f"The {region_name(key)} server is not configured.")
            return
        target = self.core.server_for(key)
        server_arg = "" if key == DEFAULT_REGION else f" server:{target.label}"
        get5 = target.get5
        if not get5.enabled:
            await self.deny(
                interaction,
                "get5 is off. Set `GET5_CONFIG_DIR` (+ `GET5_CONFIG_PREFIX`) or "
                "`GET5_BASE_URL` in `.env`, then restart the bot.",
            )
            return
        if not self.config.rcon_enabled:
            await self.deny(interaction, "RCON is disabled, so nothing can be sent.")
            return

        guild_id = interaction.guild_id or 0
        steam_id = self.store.steam_id(guild_id, interaction.user.id)
        if not steam_id:
            await self.deny(
                interaction, "Register first: `/register steam:STEAM_1:0:12345678`."
            )
            return

        settings = self.store.settings(guild_id)
        pool = resolve_pool(settings.get("map_pool"), settings.get("workshop_ids"))
        if map:
            game_map = next((m for m in pool if m.name == map), None)
            if game_map is None:
                await self.deny(interaction, f"`{map}` is not in the pool.")
                return
        else:
            game_map = random.choice(pool)

        # A 1v1 shell: you on team1, a placeholder on team2 that never connects.
        # Loading is the whole test -- the match does not need to start.
        probe = Match(host_id=interaction.user.id, size=2, mode="get5-probe", map_pool=pool)
        me = Player(interaction.user.id, interaction.user.display_name, 1000)
        stand_in = Player(PROBE_PLACEHOLDER_ID, "Placeholder", 1000)
        probe.players = [me, stand_in]
        probe.teams = [[me], [stand_in]]
        probe.selected_map = game_map
        probe.phase = Phase.LIVE

        await interaction.response.defer(ephemeral=True)
        matchid = f"probe-{int(utcnow().timestamp())}"
        entry = get5.prepare(
            probe,
            {me.id: steam_id, stand_in.id: PROBE_PLACEHOLDER_STEAM},
            matchid=matchid,
        )

        lines = [
            f"**Server:** {target.name}",
            f"**Map:** `{game_map.name}`   **Match id:** `{matchid}`",
            f"**Mode:** `{get5.mode}`   **Your ID:** `{steam_id}`",
        ]
        if get5.mode == "file":
            action = "Uploading to" if get5.files.remote else "Writing to"
            lines.append(
                f"**{action}:** "
                f"`{target.config.get5_config_dir}/{entry.filename}`\n"
                f"**Server path:** `{target.config.get5_config_prefix}/{entry.filename}`"
            )

        try:
            await get5.ensure_idle()
            sent = await get5.load(entry)
        except (RconError, Get5Error) as exc:
            lines.append(f"\n❌ **Load refused:** {exc}")
            await self.reply(interaction, "\n".join(lines))
            return

        lines.append(f"\n**Sent:** `{sent}`")
        try:
            verified = await get5.verify_loaded(entry)
        except Get5Error as exc:
            lines.append(f"⚠️ {exc}")
            verified = False

        if verified is True:
            lines.append(
                "✅ **get5 confirms the match is loaded.** Join the server — you "
                f"should be forced onto Team A. Clear it with `/rcon get5_endmatch{server_arg}`."
            )
        elif verified is None:
            lines.append(
                "⚠️ Sent without error, but `get5_status` returned no JSON, so it "
                "could not be confirmed (needs the **smjansson** extension). "
                f"Check in game with `/rcon get5_status{server_arg}`."
            )
        else:
            lines.append(
                "❌ **The server did not load it.** get5 logs the real reason to "
                "`addons/sourcemod/logs/errors_*.log` on the game server — check "
                "there. Usual causes: wrong `GET5_CONFIG_PREFIX`, the bot writing "
                "somewhere the server cannot read, or smjansson missing (get5 0.6.0 "
                "cannot parse JSON configs without it)."
            )
        await self.reply(interaction, "\n".join(lines))

    @app_commands.command(
        name="cleargames", description="Debug: forcefully remove all games in this server."
    )
    @app_commands.default_permissions(administrator=True)
    async def cleargames(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        sessions = self.core.guild_sessions(interaction.guild_id or 0)
        for session in list(sessions):
            try:
                await session.cancel()
            except Exception:  # noqa: BLE001 - best effort teardown
                log.exception("Failed to cancel session during /cleargames")
                self.core.forget(session)
        await self.reply(interaction, f"Cleared **{len(sessions)}** game(s).")

    @app_commands.command(name="status", description="Show the current game state.")
    async def status(self, interaction: discord.Interaction) -> None:
        from .. import embeds

        session = await self.require_session(interaction)
        if session is None:
            return
        embed = embeds.render(
            session.match,
            session.rcon_status,
            connect=session.connect_target,
            changes=session.changes,
            region=session.region_label,
        )
        if session.message is not None:
            embed.add_field(name="Jump", value=session.message.jump_url, inline=False)
        await self.reply(interaction, embed=embed)


async def setup(bot: commands.Bot) -> None:  # pragma: no cover - discord entrypoint
    await bot.add_cog(ScrimCog(bot))
