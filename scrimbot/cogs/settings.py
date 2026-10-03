"""Per-guild configuration toggles, the map pool, and command logging."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..maps import MAP_POOL, MAPS_BY_NAME, resolve_pool
from ..match import TEAM_EMOJI, TEAM_NAMES
from ..rcon import RconError
from ..regions import DEFAULT_REGION, REGION_CHOICES, region_name
from ..store import PICK_MODES
from .base import ScrimCogBase

log = logging.getLogger(__name__)

ON_OFF = [
    app_commands.Choice(name="on", value=1),
    app_commands.Choice(name="off", value=0),
]


class SettingsCog(ScrimCogBase):
    """Everything that changes how this server runs its games."""

    maps_group = app_commands.Group(name="maps", description="Manually edit the map pool")

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(bot)
        # Channels we have already complained about missing Manage Messages in,
        # so a misconfigured host channel does not spam the log on every message.
        self._purge_warned: set[int] = set()

    # --------------------------------------------------------------- toggles

    async def _toggle(
        self,
        interaction: discord.Interaction,
        key: str,
        description: str,
        choice: app_commands.Choice[int] | None,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        value = None if choice is None else bool(choice.value)
        new = await self.store.toggle_setting(interaction.guild_id or 0, key, value)
        state = "**enabled**" if new else "**disabled**"
        await self.reply(interaction, f"{description} is now {state}.", ephemeral=False)

    @app_commands.command(
        name="accountlockgames",
        description="Require users to bind a Steam account before they can play.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(state=ON_OFF)
    async def accountlockgames(
        self, interaction: discord.Interaction, state: app_commands.Choice[int] | None = None
    ) -> None:
        await self._toggle(
            interaction, "account_lock", "Requiring a linked Steam account to play", state
        )

    @app_commands.command(
        name="togglevetoing", description="Toggle map vetoing (off = the bot rolls a map)."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(state=ON_OFF)
    async def togglevetoing(
        self, interaction: discord.Interaction, state: app_commands.Choice[int] | None = None
    ) -> None:
        await self._toggle(interaction, "vetoing", "Map vetoing", state)

    @app_commands.command(
        name="randomcaptains", description="Pick captains at random instead of by highest Elo."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(state=ON_OFF)
    async def randomcaptains(
        self, interaction: discord.Interaction, state: app_commands.Choice[int] | None = None
    ) -> None:
        await self._toggle(interaction, "random_captains", "Random captain selection", state)

    @app_commands.command(
        name="randompassword", description="Randomise the server password each game."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(state=ON_OFF)
    async def randompassword(
        self, interaction: discord.Interaction, state: app_commands.Choice[int] | None = None
    ) -> None:
        await self._toggle(interaction, "random_password", "Randomised server passwords", state)

    @app_commands.command(
        name="servercleanup", description="Kick players from the game server after a game."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(state=ON_OFF)
    async def servercleanup(
        self, interaction: discord.Interaction, state: app_commands.Choice[int] | None = None
    ) -> None:
        await self._toggle(interaction, "server_cleanup", "Post-game server cleanup", state)

    @app_commands.command(
        name="autovoice",
        description="Move players into their team voice channel when teams lock in.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(state=ON_OFF)
    async def autovoice(
        self, interaction: discord.Interaction, state: app_commands.Choice[int] | None = None
    ) -> None:
        await self._toggle(
            interaction, "auto_voice", "Auto-moving players into team voice", state
        )
        if not self.core.voice.spec_channel_ids(interaction.guild):
            await interaction.followup.send(
                "⚠️ No team voice channels are set yet — `/setteamvoice` first, or "
                "nothing will move.",
                ephemeral=True,
            )

    @app_commands.command(
        name="voicecleanup", description="Disconnect players from voice after a game."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(state=ON_OFF)
    async def voicecleanup(
        self, interaction: discord.Interaction, state: app_commands.Choice[int] | None = None
    ) -> None:
        await self._toggle(interaction, "voice_cleanup", "Post-game voice cleanup", state)

    @app_commands.command(
        name="pickmode", description="Change the way players are picked for a lobby."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(mode="How teams get formed once the lobby fills")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="captains -- snake draft (1, then 2 at a time)", value="captains"),
            app_commands.Choice(name="random -- shuffle players into teams", value="random"),
            app_commands.Choice(name="balanced -- split by Elo", value="balanced"),
        ]
    )
    async def pickmode(
        self, interaction: discord.Interaction, mode: app_commands.Choice[str] | None = None
    ) -> None:
        if not await self.require_admin(interaction):
            return
        guild_id = interaction.guild_id or 0
        if mode is None:
            current = self.store.setting(guild_id, "pick_mode")
            await self.reply(
                interaction,
                f"Pick mode is `{current}`. Options: " + ", ".join(f"`{m}`" for m in PICK_MODES),
            )
            return
        await self.store.set_setting(guild_id, "pick_mode", mode.value)
        await self.reply(interaction, f"Pick mode set to `{mode.value}`.", ephemeral=False)

    # ---------------------------------------------------------------- wiring

    @app_commands.command(
        name="setnotifyrole", description="Set the role mentioned when games are hosted."
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(role="Role to ping (leave blank to clear)")
    async def setnotifyrole(
        self, interaction: discord.Interaction, role: discord.Role | None = None
    ) -> None:
        if not await self.require_staff(interaction):
            return
        await self.store.set_setting(
            interaction.guild_id or 0, "notify_role", role.id if role else None
        )
        target = role.mention if role else "nothing"
        await self.reply(
            interaction,
            f"Game notifications will ping {target}.",
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="sethosterrole", description="Set the role allowed to host and manage lobbies."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="Role that may use /host, /sub, /forcejoin, /forcedrop, /redraft")
    async def sethosterrole(
        self, interaction: discord.Interaction, role: discord.Role | None = None
    ) -> None:
        if not await self.require_admin(interaction):
            return
        await self.store.set_setting(
            interaction.guild_id or 0, "hoster_role", role.id if role else None
        )
        if role is None:
            await self.reply(
                interaction,
                "Hoster role cleared -- only administrators can host games now.",
                ephemeral=False,
            )
            return
        await self.reply(
            interaction,
            f"**{role.name}** can now use `/host`, `/sub`, `/forcejoin`, `/forcedrop` "
            "and `/redraft`.",
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="setcommandlogchannel", description="Log every command the bot runs to a channel."
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="Channel to log into (leave blank to turn logging off)")
    async def setcommandlogchannel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        if not await self.require_staff(interaction):
            return
        await self.store.set_setting(
            interaction.guild_id or 0, "command_log_channel", channel.id if channel else None
        )
        if channel is None:
            await self.reply(interaction, "Command logging turned off.", ephemeral=False)
            return

        self.core.audit.forget_warning(channel.id)
        note = ""
        me = interaction.guild.me if interaction.guild else None
        if me is not None:
            perms = channel.permissions_for(me)
            if not (perms.send_messages and perms.embed_links):
                note = (
                    "\n⚠️ The bot needs **Send Messages** and **Embed Links** there or "
                    "nothing will be written."
                )
        await self.reply(
            interaction,
            f"Every command, and every failure, will be logged to {channel.mention}.{note}",
            ephemeral=False,
        )

    @app_commands.command(
        name="setteamroles", description="Roles handed out to each team while a game runs."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        team1="Role for Team A", team2="Role for Team B", clear="Unset both roles instead"
    )
    async def setteamroles(
        self,
        interaction: discord.Interaction,
        team1: discord.Role | None = None,
        team2: discord.Role | None = None,
        clear: bool = False,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        guild_id = interaction.guild_id or 0

        if clear:
            await self.store.set_setting(guild_id, "team1_role", None)
            await self.store.set_setting(guild_id, "team2_role", None)
            await self.reply(interaction, "Team roles cleared.", ephemeral=False)
            return
        if team1 is None and team2 is None:
            current = [
                f"{TEAM_EMOJI[i]} {TEAM_NAMES[i]}: "
                + (f"<@&{rid}>" if (rid := self.store.setting(guild_id, key)) else "`not set`")
                for i, key in enumerate(("team1_role", "team2_role"))
            ]
            await self.reply(
                interaction,
                "\n".join(current),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if team1 is not None and team1 == team2:
            await self.deny(interaction, "The two teams need different roles.")
            return

        notes = []
        for role, key, team in ((team1, "team1_role", 0), (team2, "team2_role", 1)):
            if role is None:
                continue
            await self.store.set_setting(guild_id, key, role.id)
            self.core.roles.forget_warning(guild_id, role.id)
            notes.append(f"{TEAM_EMOJI[team]} {TEAM_NAMES[team]} -> **{role.name}**")
            if interaction.guild and not self.core.roles.manageable(interaction.guild, role):
                notes.append(
                    f"⚠️ The bot cannot assign **{role.name}** -- it needs Manage Roles and "
                    "its own top role has to sit above it."
                )
        await self.reply(
            interaction,
            "\n".join(notes),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="setteamvoice",
        description="Voice channels that /spec moves spectators into.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        team1="Voice channel for Team A",
        team2="Voice channel for Team B",
        clear="Unset both instead",
    )
    async def setteamvoice(
        self,
        interaction: discord.Interaction,
        team1: discord.VoiceChannel | None = None,
        team2: discord.VoiceChannel | None = None,
        clear: bool = False,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        guild_id = interaction.guild_id or 0

        if clear:
            await self.store.set_setting(guild_id, "team1_voice", None)
            await self.store.set_setting(guild_id, "team2_voice", None)
            await self.reply(interaction, "Team voice channels cleared.", ephemeral=False)
            return
        if team1 is None and team2 is None:
            lines = []
            for index, key in enumerate(("team1_voice", "team2_voice")):
                channel_id = self.store.setting(guild_id, key)
                where = f"<#{channel_id}>" if channel_id else "`not set`"
                lines.append(f"{TEAM_EMOJI[index]} {TEAM_NAMES[index]}: {where}")
            await self.reply(interaction, "\n".join(lines))
            return
        if team1 is not None and team1 == team2:
            await self.deny(interaction, "The two teams need different channels.")
            return

        notes = []
        me = interaction.guild.me if interaction.guild else None
        for channel, key, index in (
            (team1, "team1_voice", 0),
            (team2, "team2_voice", 1),
        ):
            if channel is None:
                continue
            await self.store.set_setting(guild_id, key, channel.id)
            notes.append(f"{TEAM_EMOJI[index]} {TEAM_NAMES[index]} -> {channel.mention}")
            if me is not None and not channel.permissions_for(me).connect:
                notes.append(f"⚠️ The bot cannot see {channel.mention}.")

        if me is not None:
            perms = me.guild_permissions
            lacking = [
                name
                for name, held in (
                    ("Move Members", perms.move_members),
                    ("Mute Members", perms.mute_members),
                )
                if not held
            ]
            if lacking:
                notes.append(
                    "⚠️ `/spec` also needs the **"
                    + "** and **".join(lacking)
                    + "** permission."
                )
        await self.reply(
            interaction,
            "\n".join(notes),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="setregisteredrole",
        description="Role mirroring who has a Steam account bound.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="Role to hand out on /register (leave blank to clear)")
    async def setregisteredrole(
        self, interaction: discord.Interaction, role: discord.Role | None = None
    ) -> None:
        if not await self.require_admin(interaction):
            return
        guild_id = interaction.guild_id or 0
        await self.store.set_setting(
            guild_id, "registered_role", role.id if role else None
        )
        if role is None:
            await self.reply(interaction, "Registered role cleared.", ephemeral=False)
            return

        self.core.roles.forget_warning(guild_id, role.id)
        if interaction.guild and not self.core.roles.manageable(interaction.guild, role):
            await self.deny(
                interaction,
                f"Saved, but the bot cannot assign **{role.name}** -- it needs Manage Roles "
                "and its own top role has to sit above it.",
            )
            return

        await interaction.response.defer(ephemeral=False)
        granted, revoked = await self.core.roles.sync_registered(interaction.guild)
        await self.reply(
            interaction,
            f"**{role.name}** now tracks `/register`. Synced existing members: "
            f"**{granted}** given, **{revoked}** removed.",
            ephemeral=False,
        )

    @app_commands.command(
        name="sethostchannel",
        description="Designate a bot-only channel: everyone else's messages get deleted.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="Channel to keep clear (leave blank to turn this off)")
    async def sethostchannel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        if not await self.require_admin(interaction):
            return
        await self.store.set_setting(
            interaction.guild_id or 0, "host_channel", channel.id if channel else None
        )
        if channel is None:
            await self.reply(
                interaction,
                "Host channel cleared -- the bot will stop deleting messages.",
                ephemeral=False,
            )
            return

        self._purge_warned.discard(channel.id)
        note = ""
        me = interaction.guild.me if interaction.guild else None
        if me is not None and not channel.permissions_for(me).manage_messages:
            note = (
                "\n⚠️ The bot cannot **Manage Messages** in that channel, so it will not be "
                "able to delete anything until you grant it."
            )
        await self.reply(
            interaction,
            f"{channel.mention} is now the host channel -- every message from anyone other "
            f"than the bot will be deleted immediately.{note}",
            ephemeral=False,
        )

    @app_commands.command(
        name="setresultschannel",
        description="Post finished games to their own channel instead of the scrim message.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="Where to archive results (leave blank to turn this off)")
    async def setresultschannel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        if not await self.require_admin(interaction):
            return
        await self.store.set_setting(
            interaction.guild_id or 0, "results_channel", channel.id if channel else None
        )
        if channel is None:
            await self.reply(
                interaction,
                "Results channel cleared -- finished games stay on the scrim message.",
                ephemeral=False,
            )
            return

        note = ""
        me = interaction.guild.me if interaction.guild else None
        if me is not None:
            perms = channel.permissions_for(me)
            if not (perms.send_messages and perms.embed_links):
                note = (
                    "\n⚠️ The bot needs **Send Messages** and **Embed Links** there or the "
                    "result will fall back to the scrim message."
                )
        await self.reply(
            interaction,
            f"Finished games will be posted to {channel.mention} with the full scoreboard. "
            f"The scrim message keeps the winner and Elo and links across.{note}",
            ephemeral=False,
        )

    @app_commands.command(name="channels", description="List the channels in this server.")
    @app_commands.default_permissions(administrator=True)
    async def channels(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        guild = interaction.guild
        if guild is None:
            await self.deny(interaction, "This command only works inside a server.")
            return

        sections: dict[str, list[str]] = {"Text": [], "Voice": [], "Category": [], "Other": []}
        for channel in guild.channels:
            if isinstance(channel, discord.TextChannel):
                key = "Text"
            elif isinstance(channel, discord.VoiceChannel):
                key = "Voice"
            elif isinstance(channel, discord.CategoryChannel):
                key = "Category"
            else:
                key = "Other"
            sections[key].append(f"`{channel.id}` {channel.name}")

        embed = discord.Embed(
            title=f"Channels in {guild.name}", color=discord.Color.blurple()
        )
        for name, lines in sections.items():
            if not lines:
                continue
            body = "\n".join(lines)
            if len(body) > 1000:
                body = body[:990] + f"\n... +{len(lines)} total"
            embed.add_field(name=f"{name} ({len(lines)})", value=body, inline=False)
        await self.reply(interaction, embed=embed)

    # ------------------------------------------------------------------ maps

    def _pool(self, guild_id: int):
        return resolve_pool(
            self.store.setting(guild_id, "map_pool"),
            self.store.setting(guild_id, "workshop_ids"),
        )

    @maps_group.command(name="list", description="Show the current map pool.")
    async def maps_list(self, interaction: discord.Interaction) -> None:
        pool = self._pool(interaction.guild_id or 0)
        embed = discord.Embed(
            title="Map pool",
            description="\n".join(f"`{m.name}` -- {m.display}" for m in pool),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(pool)} maps -- veto bans down to one.")
        await self.reply(interaction, embed=embed)

    @maps_group.command(name="add", description="Add a map to the pool.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        name="Map name as the server knows it, e.g. de_nuke",
        workshop_id="Workshop id, if the map is not shipped with the server",
    )
    async def maps_add(
        self, interaction: discord.Interaction, name: str, workshop_id: str | None = None
    ) -> None:
        if not await self.require_staff(interaction):
            return
        guild_id = interaction.guild_id or 0
        pool = [m.name for m in self._pool(guild_id)]
        name = name.strip().lower()
        if name in pool:
            await self.deny(interaction, f"`{name}` is already in the pool.")
            return
        pool.append(name)
        await self.store.set_setting(guild_id, "map_pool", pool)
        if workshop_id:
            workshop = dict(self.store.setting(guild_id, "workshop_ids") or {})
            workshop[name] = workshop_id
            await self.store.set_setting(guild_id, "workshop_ids", workshop)
        await self.reply(interaction, f"Added `{name}` -- pool is now {len(pool)} maps.", ephemeral=False)

    @maps_group.command(name="remove", description="Remove a map from the pool.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(name="Map to remove")
    async def maps_remove(self, interaction: discord.Interaction, name: str) -> None:
        if not await self.require_staff(interaction):
            return
        guild_id = interaction.guild_id or 0
        pool = [m.name for m in self._pool(guild_id)]
        name = name.strip().lower()
        if name not in pool:
            await self.deny(interaction, f"`{name}` is not in the pool.")
            return
        if len(pool) <= 2:
            await self.deny(interaction, "The pool needs at least two maps to run a veto.")
            return
        pool.remove(name)
        await self.store.set_setting(guild_id, "map_pool", pool)
        await self.reply(
            interaction, f"Removed `{name}` -- pool is now {len(pool)} maps.", ephemeral=False
        )

    @maps_group.command(name="reset", description="Restore the built-in map pool.")
    @app_commands.default_permissions(manage_guild=True)
    async def maps_reset(self, interaction: discord.Interaction) -> None:
        if not await self.require_staff(interaction):
            return
        await self.store.set_setting(interaction.guild_id or 0, "map_pool", None)
        await self.reply(
            interaction, f"Map pool reset to the built-in {len(MAP_POOL)} maps.", ephemeral=False
        )

    @maps_remove.autocomplete("name")
    async def _map_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        pool = self._pool(interaction.guild_id or 0)
        current = (current or "").lower()
        return [
            app_commands.Choice(name=m.name, value=m.name)
            for m in pool
            if current in m.name.lower()
        ][:25]

    @maps_add.autocomplete("name")
    async def _known_map_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").lower()
        return [
            app_commands.Choice(name=name, value=name)
            for name in MAPS_BY_NAME
            if current in name.lower()
        ][:25]

    # ------------------------------------------------------------------ rcon

    @app_commands.command(name="rcon", description="Run a console command on the game server.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        command="Console command to execute",
        server="Which game server (default: EU)",
    )
    @app_commands.choices(server=REGION_CHOICES)
    async def rcon(
        self,
        interaction: discord.Interaction,
        command: str,
        server: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        key = server.value if server else DEFAULT_REGION
        if key not in self.core.servers:
            await self.deny(interaction, f"The {region_name(key)} server is not configured.")
            return
        await interaction.response.defer(ephemeral=True)
        try:
            output = await self.core.server_for(key).server.execute(command)
        except RconError as exc:
            await self.deny(interaction, str(exc))
            return
        body = output or "(no output)"
        if len(body) > 1900:
            body = body[:1900] + "\n... (truncated)"
        await self.reply(interaction, f"```\n{body}\n```")

    # -------------------------------------------------------------- overview

    @app_commands.command(name="settings", description="Show this server's scrim settings.")
    @app_commands.default_permissions(administrator=True)
    async def settings(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        guild_id = interaction.guild_id or 0
        values = self.store.settings(guild_id)
        pool = self._pool(guild_id)

        def show(key: str) -> str:
            value = values.get(key)
            if isinstance(value, bool):
                return "on" if value else "off"
            if value is None:
                return "not set"
            return str(value)

        embed = discord.Embed(title="Scrim settings", color=discord.Color.blurple())
        embed.add_field(
            name="Games",
            value=(
                f"Pick mode: `{show('pick_mode')}`\n"
                f"Random captains: `{show('random_captains')}`\n"
                f"Map vetoing: `{show('vetoing')}`\n"
                f"Map pool: `{len(pool)} maps`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Gatekeeping",
            value=(
                f"Steam account required: "
                f"{steam_required_label(bool(values.get('account_lock')), self.core.get5.enabled)}\n"
                f"Steam API key: `{'set' if self.steam.enabled else 'missing'}`"
            ),
            inline=False,
        )
        notify = values.get("notify_role")
        log_channel = values.get("command_log_channel")
        host_channel = values.get("host_channel")
        results_channel = values.get("results_channel")
        team_roles = [values.get("team1_role"), values.get("team2_role")]
        registered_role = values.get("registered_role")
        staff = values.get("staff_role")
        hoster = values.get("hoster_role")
        embed.add_field(
            name="Server & wiring",
            value=(
                "".join(
                    f"{target.name}: RCON `{target.config.rcon_target}`"
                    + (f" via SSH `{target.link.host}`" if target.link else "")
                    + f", connect `{target.connect_target}`\n"
                    for target in self.core.servers.values()
                )
                + f"RCON: `{'enabled' if self.config.rcon_enabled else 'disabled'}`\n"
                f"Random password: `{show('random_password')}`\n"
                f"Server cleanup: `{show('server_cleanup')}`\n"
                f"Voice cleanup: `{show('voice_cleanup')}`\n"
                f"Notify role: {f'<@&{notify}>' if notify else '`not set`'}\n"
                f"Hoster role: {f'<@&{hoster}>' if hoster else '`admins only`'}\n"
                f"Staff role: {f'<@&{staff}>' if staff else '`not set`'}\n"
                f"Command log: {f'<#{log_channel}>' if log_channel else '`off`'}\n"
                f"Host channel: {f'<#{host_channel}> (bot only)' if host_channel else '`off`'}\n"
                f"Results channel: {f'<#{results_channel}>' if results_channel else '`scrim message`'}\n"
                f"Team roles: {' vs '.join(f'<@&{r}>' if r else '`not set`' for r in team_roles)}\n"
                f"Registered role: {f'<@&{registered_role}>' if registered_role else '`not set`'}"
            ),
            inline=False,
        )
        await self.reply(
            interaction, embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    # ------------------------------------------------------------- listeners

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Keep the designated host channel bot-only.

        Off unless an administrator points ``/sethostchannel`` at a channel.
        Only the bot's own messages survive -- other bots and webhooks included.
        """
        if message.guild is None or self.bot.user is None:
            return
        if message.author.id == self.bot.user.id:
            return
        channel_id = self.store.setting(message.guild.id, "host_channel")
        if not channel_id or message.channel.id != channel_id:
            return

        try:
            await message.delete()
        except discord.NotFound:
            pass  # someone else already removed it
        except discord.Forbidden:
            if channel_id not in self._purge_warned:
                self._purge_warned.add(channel_id)
                log.warning(
                    "Missing Manage Messages in host channel %s (guild %s) -- "
                    "cannot keep it clear.",
                    channel_id,
                    message.guild.id,
                )
        except discord.HTTPException:
            log.debug("Failed to delete a message in the host channel", exc_info=True)

    @commands.Cog.listener()
    async def on_app_command_completion(
        self, interaction: discord.Interaction, command: app_commands.Command
    ) -> None:
        await self.core.audit.log_command(interaction, command)


def steam_required_label(account_lock: bool, get5_on: bool) -> str:
    """What /settings shows: the rule actually enforced, not just the toggle.

    get5 needs every player's Steam ID for its team lock, so while it is on
    registration is required whatever /accountlockgames says.
    """
    if get5_on:
        return "`on` (get5 requires it)"
    return "`on`" if account_lock else "`off`"


async def setup(bot: commands.Bot) -> None:  # pragma: no cover - discord entrypoint
    await bot.add_cog(SettingsCog(bot))
