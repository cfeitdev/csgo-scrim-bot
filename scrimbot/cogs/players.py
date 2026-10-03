"""Player-facing data: Steam registration, stats, Elo and the blacklist."""

from __future__ import annotations

import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..steam import SteamError, to_steam2, to_steam3
from ..util import parse_duration, timestamp, utcnow
from ..leaderboardview import LeaderboardView
from .base import ScrimCogBase

log = logging.getLogger(__name__)

ID_FORMATS = [
    app_commands.Choice(name="steamID64 (7656119...)", value="steam64"),
    app_commands.Choice(name="steamID (STEAM_1:0:...)", value="steam2"),
    app_commands.Choice(name="steamID3 ([U:1:...])", value="steam3"),
]


def format_id(steam_id64: str, style: str) -> str:
    if style == "steam2":
        return to_steam2(steam_id64)
    if style == "steam3":
        return to_steam3(steam_id64)
    return steam_id64


class ConfirmView(discord.ui.View):
    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=30)
        self.author_id = author_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This confirmation is not yours.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = False
        await interaction.response.defer()
        self.stop()


class PlayerCog(ScrimCogBase):
    """/register, /stats, /leaderboard, /elo, /games, /blacklist and friends."""

    elo_group = app_commands.Group(
        name="elo",
        description="Manually give, set, and take Elo from players",
        default_permissions=discord.Permissions(administrator=True),
    )
    games_group = app_commands.Group(
        name="games",
        description="Manually add, remove, or set a player's wins and losses",
        default_permissions=discord.Permissions(administrator=True),
    )

    # -------------------------------------------------------------- register

    @app_commands.command(name="register", description="Register a Steam account to a user.")
    @app_commands.describe(
        steam="Your Steam ID in STEAM_1:0:12345678 form (type `status` in the game console)",
        user="Register on behalf of someone else (moderators only)",
    )
    async def register(
        self,
        interaction: discord.Interaction,
        steam: str,
        user: discord.Member | None = None,
    ) -> None:
        guild_id = interaction.guild_id or 0
        target = user or interaction.user
        if user is not None and not self.is_staff(interaction):
            await self.deny(interaction, "Only moderators can register an account for someone else.")
            return

        await interaction.response.defer(ephemeral=True)
        try:
            account = await self.steam.lookup(steam)
        except SteamError as exc:
            await self.deny(interaction, str(exc))
            return

        owner = self.store.owner_of_steam_id(guild_id, account.steam_id64)
        if owner is not None and owner != target.id:
            await self.deny(
                interaction, f"That Steam account is already registered to <@{owner}>."
            )
            return

        await self.store.register(guild_id, target.id, account.steam_id64, account.name)
        granted = await self.core.roles.grant_registered(interaction.guild, target.id)

        embed = discord.Embed(title="Steam account registered", color=discord.Color.green())
        if granted:
            role = self.core.roles.registered_role(interaction.guild)
            if role is not None:
                embed.add_field(name="Role", value=f"Given **{role.name}**", inline=False)
        embed.add_field(name="Discord", value=target.mention, inline=False)
        embed.add_field(name="steamID64", value=f"`{account.steam_id64}`", inline=True)
        embed.add_field(name="steamID", value=f"`{account.steam_id2}`", inline=True)
        if account.name:
            embed.add_field(name="Profile", value=account.name, inline=False)
        if account.profile_url:
            embed.url = account.profile_url
        if not self.steam.enabled:
            embed.set_footer(text="No Steam API key set -- ID stored without a name lookup.")
        await self.reply(interaction, embed=embed)

    @app_commands.command(name="unregister", description="Unbind a Steam account from a user.")
    @app_commands.describe(user="Whose account to unbind (moderators only)")
    async def unregister(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        target = user or interaction.user
        if user is not None and not self.is_staff(interaction):
            await self.deny(interaction, "Only moderators can unregister someone else.")
            return
        removed = await self.store.unregister(interaction.guild_id or 0, target.id)
        if not removed:
            await self.deny(interaction, f"{target.mention} has no Steam account registered.")
            return
        await self.core.roles.revoke_registered(interaction.guild, target.id)
        await self.reply(interaction, f"Unbound the Steam account from {target.mention}.")

    # ------------------------------------------------------------- id dumps

    async def _send_ids(
        self,
        interaction: discord.Interaction,
        rows: list[tuple[int, str]],
        style: str,
        title: str,
        filename: str,
    ) -> None:
        if not rows:
            await self.deny(interaction, "Nobody matches that yet.")
            return
        lines = [f"{format_id(steam_id, style)}" for _uid, steam_id in rows]
        blob = "\n".join(lines)
        if len(blob) > 1800:
            file = discord.File(io.BytesIO(blob.encode("utf-8")), filename=filename)
            await self.reply(interaction, f"{title} ({len(rows)})", file=file)
            return
        await self.reply(interaction, f"**{title}** ({len(rows)})\n```\n{blob}\n```")

    @app_commands.command(
        name="listids", description="List every bound Steam ID for a server whitelist."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(format="ID format to output")
    @app_commands.choices(format=ID_FORMATS)
    async def listids(
        self,
        interaction: discord.Interaction,
        format: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        style = format.value if format else "steam64"
        rows = [
            (uid, record["steam_id"])
            for uid, record in self.store.registered(interaction.guild_id or 0)
        ]
        await self._send_ids(interaction, rows, style, "Registered Steam IDs", "whitelist.txt")

    @app_commands.command(
        name="liststaffids", description="List bound Steam IDs for staff (admin whitelist)."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        role="Staff role to read (saved as the server's staff role)",
        format="ID format to output",
    )
    @app_commands.choices(format=ID_FORMATS)
    async def liststaffids(
        self,
        interaction: discord.Interaction,
        role: discord.Role | None = None,
        format: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        guild_id = interaction.guild_id or 0
        style = format.value if format else "steam64"

        if role is not None:
            await self.store.set_setting(guild_id, "staff_role", role.id)
        else:
            role_id = self.store.setting(guild_id, "staff_role")
            role = interaction.guild.get_role(role_id) if role_id else None

        registered = dict(self.store.registered(guild_id))
        if role is not None:
            members = [m.id for m in role.members]
        else:
            members = [
                uid
                for uid in registered
                if (member := interaction.guild.get_member(uid))
                and member.guild_permissions.manage_guild
            ]
        rows = [
            (uid, registered[uid]["steam_id"]) for uid in members if uid in registered
        ]
        label = f"Staff Steam IDs ({role.name})" if role else "Staff Steam IDs"
        await self._send_ids(interaction, rows, style, label, "staff_whitelist.txt")

    # ----------------------------------------------------------------- stats

    @app_commands.command(name="stats", description="Display a user's game stats.")
    @app_commands.describe(user="Whose stats to show", gamemode="Which stats bucket")
    async def stats(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        gamemode: str | None = None,
    ) -> None:
        guild_id = interaction.guild_id or 0
        target = user or interaction.user
        record = self.store.player(guild_id, target.id)
        modes = record.get("modes") or {}
        if gamemode:
            modes = {gamemode: self.store.mode_stats(guild_id, target.id, gamemode)}
        if not modes:
            modes = {self.config.default_mode: self.store.mode_stats(
                guild_id, target.id, self.config.default_mode
            )}

        embed = discord.Embed(
            title=f"Stats -- {target.display_name}", color=discord.Color.blurple()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        for mode, stats in sorted(modes.items()):
            wins, losses = int(stats.get("wins", 0)), int(stats.get("losses", 0))
            ties = int(stats.get("ties", 0))
            played = wins + losses + ties
            winrate = f"{wins / played * 100:.0f}%" if played else "--"
            leaderboard = self.store.leaderboard(guild_id, mode)
            rank = next(
                (i for i, (uid, _s) in enumerate(leaderboard, start=1) if uid == target.id), None
            )
            embed.add_field(
                name=mode,
                value=(
                    f"**Elo** `{stats.get('elo', self.store.default_elo)}`"
                    + (f" (#{rank})" if rank else "")
                    + f"\n**W/L** `{wins}-{losses}`"
                    + (f"-{ties}" if ties else "")
                    + f"\n**Winrate** {winrate} over {played} game(s)"
                ),
                inline=True,
            )

        steam_id = record.get("steam_id")
        if steam_id:
            embed.add_field(
                name="Steam",
                value=f"[{record.get('steam_name') or steam_id}]"
                f"(https://steamcommunity.com/profiles/{steam_id})\n`{to_steam2(steam_id)}`",
                inline=False,
            )
        active, reason, expiry = self.store.blacklist_state(guild_id, target.id)
        if active:
            when = timestamp(expiry) if expiry else "permanent"
            embed.add_field(
                name="⛔ Blacklisted", value=f"{reason or 'no reason given'} ({when})", inline=False
            )
        await self.reply(interaction, embed=embed)

    @app_commands.command(name="leaderboard", description="Show the leaderboard for a gamemode.")
    @app_commands.describe(gamemode="Which stats bucket", page="Page number")
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        gamemode: str | None = None,
        page: app_commands.Range[int, 1, 100] = 1,
    ) -> None:
        guild_id = interaction.guild_id or 0
        mode = gamemode or self.config.default_mode
        rows = self.store.leaderboard(guild_id, mode)
        if not rows:
            await self.deny(interaction, f"Nobody has played any **{mode}** games yet.")
            return

        view = LeaderboardView(mode, rows, page)
        # One page needs no controls.
        if view.pages == 1:
            await self.reply(interaction, embed=view.embed, ephemeral=False)
            return
        await self.reply(interaction, embed=view.embed, view=view, ephemeral=False)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            # Paging still works; the buttons just stay live until they expire.
            log.debug("Could not resolve the leaderboard message for timeout cleanup")

    # ------------------------------------------------------------------- elo

    async def _elo_change(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: int,
        gamemode: str | None,
        verb: str,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        guild_id = interaction.guild_id or 0
        mode = gamemode or self.config.default_mode
        if verb == "set":
            before = self.store.elo(guild_id, user.id, mode)
            after = await self.store.set_elo(guild_id, user.id, mode, amount)
        else:
            delta = amount if verb == "give" else -amount
            before, after = await self.store.add_elo(guild_id, user.id, mode, delta)
        await self.reply(
            interaction,
            f"{user.mention} **{mode}** Elo: `{before}` -> `{after}` ({after - before:+d}).",
            ephemeral=False,
        )

    @elo_group.command(name="give", description="Give Elo to a player.")
    async def elo_give(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 100000],
        gamemode: str | None = None,
    ) -> None:
        await self._elo_change(interaction, user, amount, gamemode, "give")

    @elo_group.command(name="take", description="Take Elo away from a player.")
    async def elo_take(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 100000],
        gamemode: str | None = None,
    ) -> None:
        await self._elo_change(interaction, user, amount, gamemode, "take")

    @elo_group.command(name="set", description="Set a player's Elo outright.")
    async def elo_set(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 0, 100000],
        gamemode: str | None = None,
    ) -> None:
        await self._elo_change(interaction, user, amount, gamemode, "set")

    @elo_group.command(name="show", description="Show a player's Elo.")
    async def elo_show(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        gamemode: str | None = None,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        target = user or interaction.user
        mode = gamemode or self.config.default_mode
        rating = self.store.elo(interaction.guild_id or 0, target.id, mode)
        await self.reply(interaction, f"{target.mention} is rated **{rating}** in `{mode}`.")

    # ----------------------------------------------------------------- games

    async def _games_change(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        wins: int | None,
        losses: int | None,
        gamemode: str | None,
        mode_of_change: str,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        guild_id = interaction.guild_id or 0
        mode = gamemode or self.config.default_mode
        kwargs = {}
        if mode_of_change == "set":
            kwargs = {"wins": wins, "losses": losses}
        else:
            sign = 1 if mode_of_change == "add" else -1
            kwargs = {"add_wins": sign * (wins or 0), "add_losses": sign * (losses or 0)}
        stats = await self.store.set_record(guild_id, user.id, mode, **kwargs)
        await self.reply(
            interaction,
            f"{user.mention} **{mode}** record is now `{stats['wins']}W / {stats['losses']}L`.",
            ephemeral=False,
        )

    @games_group.command(name="add", description="Add wins/losses to a player.")
    async def games_add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        wins: int = 0,
        losses: int = 0,
        gamemode: str | None = None,
    ) -> None:
        await self._games_change(interaction, user, wins, losses, gamemode, "add")

    @games_group.command(name="remove", description="Remove wins/losses from a player.")
    async def games_remove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        wins: int = 0,
        losses: int = 0,
        gamemode: str | None = None,
    ) -> None:
        await self._games_change(interaction, user, wins, losses, gamemode, "remove")

    @games_group.command(name="set", description="Set a player's win/loss record.")
    async def games_set(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        wins: app_commands.Range[int, 0, 100000],
        losses: app_commands.Range[int, 0, 100000],
        gamemode: str | None = None,
    ) -> None:
        await self._games_change(interaction, user, wins, losses, gamemode, "set")

    # ------------------------------------------------------------- blacklist

    @app_commands.command(name="blacklist", description="Add a user to the blacklist.")
    @app_commands.describe(
        user="Player to blacklist (omit to list everyone currently blacklisted)",
        reason="Why they are being blacklisted",
        duration="How long, e.g. 7d, 12h, 30m. Blank = permanent",
    )
    async def blacklist(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        reason: str | None = None,
        duration: str | None = None,
    ) -> None:
        if not await self.require_hoster(interaction):
            return
        guild_id = interaction.guild_id or 0

        if user is None:
            rows = self.store.blacklisted(guild_id)
            if not rows:
                await self.reply(interaction, "Nobody is blacklisted.")
                return
            lines = []
            for uid, _record in rows:
                _active, why, expiry = self.store.blacklist_state(guild_id, uid)
                when = timestamp(expiry) if expiry else "permanent"
                lines.append(f"<@{uid}> -- {why or 'no reason'} ({when})")
            embed = discord.Embed(
                title="Blacklist", description="\n".join(lines), color=discord.Color.red()
            )
            await self.reply(interaction, embed=embed)
            return

        try:
            delta = parse_duration(duration)
        except ValueError as exc:
            await self.deny(interaction, str(exc))
            return
        until = utcnow() + delta if delta else None
        await self.store.blacklist(guild_id, user.id, reason, until)

        session = self.session(interaction)
        if session is not None and session.match.has_player(user.id):
            try:
                await session.force_drop(user.id)
            except Exception:  # noqa: BLE001 - dropping is a courtesy
                log.exception("Could not drop blacklisted player from the lobby")

        when = f"until {timestamp(until)}" if until else "permanently"
        await self.reply(
            interaction,
            f"⛔ {user.mention} is blacklisted {when}." + (f" Reason: {reason}" if reason else ""),
            ephemeral=False,
        )

    @app_commands.command(name="whitelist", description="Allow a blacklisted user to queue again.")
    @app_commands.describe(user="Player to un-blacklist")
    async def whitelist(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not await self.require_hoster(interaction):
            return
        was = await self.store.unblacklist(interaction.guild_id or 0, user.id)
        if not was:
            await self.reply(interaction, f"{user.mention} was not blacklisted.")
            return
        await self.reply(interaction, f"✅ {user.mention} can queue again.", ephemeral=False)

    @app_commands.command(
        name="blackcapt",
        description="Toggle whether a user can be picked captain (omit user to list).",
    )
    @app_commands.describe(user="Player to bar from (or re-allow as) captain")
    async def blackcapt(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        if not await self.require_hoster(interaction):
            return
        guild_id = interaction.guild_id or 0

        if user is None:
            ids = self.store.captain_blacklisted(guild_id)
            if not ids:
                await self.reply(interaction, "Nobody is captain-blacklisted.")
                return
            embed = discord.Embed(
                title="Captain blacklist",
                description="\n".join(f"<@{uid}>" for uid in ids),
                color=discord.Color.red(),
            )
            await self.reply(interaction, embed=embed)
            return

        was = self.store.is_captain_blacklisted(guild_id, user.id)
        await self.store.set_captain_blacklist(guild_id, user.id, not was)
        if was:
            await self.reply(
                interaction, f"✅ {user.mention} can be picked captain again.", ephemeral=False
            )
            return

        note = ""
        session = self.session(interaction)
        if session is not None and any(c.id == user.id for c in session.match.captains):
            note = (
                " They are already captaining this game -- it applies from the next "
                "draft (`/redraft` to apply it now)."
            )
        await self.reply(
            interaction,
            f"🚫 {user.mention} can no longer be picked captain.{note}",
            ephemeral=False,
        )

    # ------------------------------------------------------------ danger zone

    @app_commands.command(name="resetstats", description="Reset all players' stats.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(gamemode="Only reset one bucket (blank = every gamemode)")
    async def resetstats(
        self, interaction: discord.Interaction, gamemode: str | None = None
    ) -> None:
        if not await self.require_admin(interaction):
            return
        scope = f"**{gamemode}**" if gamemode else "**every gamemode**"
        view = ConfirmView(interaction.user.id)
        await self.reply(
            interaction,
            f"⚠️ This wipes Elo and win/loss records for {scope} in this server. "
            "Steam registrations and blacklists are kept. Continue?",
            view=view,
        )
        await view.wait()
        if not view.value:
            await interaction.followup.send("Reset cancelled.", ephemeral=True)
            return
        touched = await self.store.reset_stats(interaction.guild_id or 0, gamemode)
        await interaction.followup.send(f"Reset {touched} stat record(s).", ephemeral=True)


async def setup(bot: commands.Bot) -> None:  # pragma: no cover - discord entrypoint
    await bot.add_cog(PlayerCog(bot))
