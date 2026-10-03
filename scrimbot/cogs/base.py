from __future__ import annotations

import discord
from discord.ext import commands

from ..core import ScrimCore
from ..session import ScrimSession


class ScrimCogBase(commands.Cog):
    """Shared plumbing: access to the core services and a few helpers."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.core: ScrimCore = bot.core

    @property
    def config(self):
        return self.core.config

    @property
    def store(self):
        return self.core.store

    @property
    def server(self):
        return self.core.server

    @property
    def steam(self):
        return self.core.steam

    # ---------------------------------------------------------------- helpers

    def session(self, interaction: discord.Interaction) -> ScrimSession | None:
        return self.core.session_for(interaction.channel_id)

    def mode(self, interaction: discord.Interaction, override: str | None = None) -> str:
        if override:
            return override
        session = self.session(interaction)
        if session is not None:
            return session.match.mode
        return self.config.default_mode

    # ------------------------------------------------------------ permissions

    @staticmethod
    def is_admin(interaction: discord.Interaction) -> bool:
        """Discord's Administrator permission -- the top tier."""
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and perms.administrator)

    @staticmethod
    def is_staff(interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and (perms.manage_guild or perms.administrator))

    def hoster_role_id(self, interaction: discord.Interaction) -> int | None:
        return self.store.setting(interaction.guild_id or 0, "hoster_role")

    def is_hoster(self, interaction: discord.Interaction) -> bool:
        """Holds the configured hoster role. Administrators always qualify."""
        if self.is_admin(interaction):
            return True
        role_id = self.hoster_role_id(interaction)
        if not role_id:
            return False
        return any(role.id == role_id for role in getattr(interaction.user, "roles", []))

    @staticmethod
    async def reply(
        interaction: discord.Interaction, content=None, *, ephemeral: bool = True, **kwargs
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content=content, ephemeral=ephemeral, **kwargs)
        else:
            await interaction.response.send_message(
                content=content, ephemeral=ephemeral, **kwargs
            )

    async def deny(self, interaction: discord.Interaction, message: str) -> None:
        await self.reply(interaction, f"❌ {message}")

    async def require_session(self, interaction: discord.Interaction) -> ScrimSession | None:
        session = self.session(interaction)
        if session is None:
            await self.deny(
                interaction, "There is no active game in this channel. Start one with `/host`."
            )
            return None
        return session

    async def require_staff(self, interaction: discord.Interaction) -> bool:
        if self.is_staff(interaction):
            return True
        await self.deny(interaction, "You need the **Manage Server** permission to do that.")
        return False

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if self.is_admin(interaction):
            return True
        await self.deny(interaction, "You need the **Administrator** permission to do that.")
        return False

    async def require_hoster(self, interaction: discord.Interaction) -> bool:
        if self.is_hoster(interaction):
            return True
        role_id = self.hoster_role_id(interaction)
        if role_id:
            await self.deny(interaction, f"You need the <@&{role_id}> role to do that.")
        else:
            await self.deny(
                interaction,
                "No hoster role is set up yet, so only administrators can do this. "
                "An admin can set one with `/sethosterrole`.",
            )
        return False
