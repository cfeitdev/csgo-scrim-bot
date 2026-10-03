"""Entry point for the CS 5v5 scrim bot.

    python bot.py
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands

from scrimbot.cogs import EXTENSIONS
from scrimbot.config import Config
from scrimbot.core import ScrimCore

log = logging.getLogger("scrimbot")


class ScrimBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        # Members are needed to resolve role membership (/liststaffids) and to
        # disconnect people from voice after a game. Enable "Server Members
        # Intent" in the developer portal.
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.scrim_config = config
        self.core = ScrimCore(config, self)
        self.tree.on_error = self.on_app_command_error

    def _fingerprint(self, guild) -> str:
        """A stable hash of every command, so an unchanged tree is not resynced.

        Discord rate limits command writes per guild, and syncing on every boot
        burns that budget for nothing. The sync blocks setup_hook, so once the
        budget is gone the bot stops responding entirely.
        """
        parts = []
        for command in sorted(
            self.tree.walk_commands(guild=guild), key=lambda c: c.qualified_name
        ):
            bits = [command.qualified_name, getattr(command, "description", "") or ""]
            perms = getattr(command, "default_permissions", None)
            bits.append(str(perms.value) if perms else "")
            for param in getattr(command, "parameters", ()) or ():
                bits.append(f"{param.name}:{param.type}:{int(param.required)}")
            parts.append("/".join(bits))
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    @property
    def _sync_marker(self) -> Path:
        store = Path(self.scrim_config.store_path or "store.json")
        return store.with_name(".command_sync")

    def _should_sync(self, fingerprint: str) -> bool:
        mode = os.environ.get("SYNC_COMMANDS", "auto").strip().lower()
        if mode in ("never", "0", "off", "false"):
            log.info("Command sync disabled by SYNC_COMMANDS=%s", mode)
            return False
        if mode in ("always", "1", "on", "true", "force"):
            return True
        try:
            return self._sync_marker.read_text(encoding="utf-8").strip() != fingerprint
        except OSError:
            return True

    def _remember_sync(self, fingerprint: str) -> None:
        try:
            self._sync_marker.write_text(fingerprint, encoding="utf-8")
        except OSError:
            log.warning("Could not record the command fingerprint; will resync next boot")

    async def setup_hook(self) -> None:
        await self.core.setup()
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            log.info("Loaded %s", extension)

        guild = (
            discord.Object(id=self.scrim_config.guild_id)
            if self.scrim_config.guild_id
            else None
        )
        if guild is not None:
            self.tree.copy_global_to(guild=guild)

        fingerprint = self._fingerprint(guild)
        if not self._should_sync(fingerprint):
            log.info("Commands unchanged since the last sync -- skipping")
            return

        try:
            synced = await self.tree.sync(guild=guild)
        except discord.HTTPException as exc:
            # Never let a throttled sync stop the bot from coming up: the
            # previously registered commands still work.
            log.error("Command sync failed (%s) -- starting with the existing set", exc)
            return

        self._remember_sync(fingerprint)
        if guild is not None:
            log.info("Synced %d commands to guild %s", len(synced), self.scrim_config.guild_id)
        else:
            log.info("Synced %d global commands (may take up to an hour)", len(synced))

    async def close(self) -> None:
        await self.core.close()
        await super().close()

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        """Log the failure, then tell the user something went wrong."""
        log.exception("Slash command raised", exc_info=error)
        await self.core.audit.log_command(interaction, interaction.command, error)
        message = "Something went wrong running that command."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="/host")
        )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    config = Config.from_env()
    bot = ScrimBot(config)
    async with bot:
        await bot.start(config.token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
