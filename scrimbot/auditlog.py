"""Mirrors everything people do with the bot into a log channel.

Off until an administrator points ``/setcommandlogchannel`` at a channel.
Covers slash commands that succeeded, slash commands that blew up, and the
button clicks that change a game's outcome.
"""

from __future__ import annotations

import logging
import re

import discord

log = logging.getLogger(__name__)

SETTING = "command_log_channel"

# Discord application command option types.
SUB_COMMAND = 1
SUB_COMMAND_GROUP = 2
USER_OPTION = 6
CHANNEL_OPTION = 7
ROLE_OPTION = 8
MENTIONABLE_OPTION = 9

MENTION_FORMAT = {
    USER_OPTION: "<@{}>",
    CHANNEL_OPTION: "<#{}>",
    ROLE_OPTION: "<@&{}>",
    MENTIONABLE_OPTION: "<@{}>",
}

# Anything that looks like it is setting a secret gets its value masked, so the
# log channel never becomes the place your rcon password leaks.
_SECRET = re.compile(r"(\S*(?:password|token|secret|apikey|api_key)\S*)(\s+)(\S+)", re.I)
_SECRET_OPTION = ("password", "token", "secret", "key")


def redact(value: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***", value)


def flatten_options(options, path=None, out=None):
    """Turn Discord's nested option payload into (subcommand_path, [(name, value)])."""
    path = path or []
    out = [] if out is None else out
    for option in options or []:
        kind = option.get("type")
        if kind in (SUB_COMMAND, SUB_COMMAND_GROUP):
            path.append(option.get("name", "?"))
            flatten_options(option.get("options"), path, out)
            continue
        name = option.get("name", "?")
        value = option.get("value")
        template = MENTION_FORMAT.get(kind)
        if template and value is not None:
            rendered = template.format(value)
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
            if any(word in name.lower() for word in _SECRET_OPTION):
                rendered = "***"
            else:
                rendered = redact(rendered)
        out.append((name, rendered))
    return path, out


class AuditLogger:
    def __init__(self, bot, store) -> None:
        self.bot = bot
        self.store = store
        self._warned: set[int] = set()

    # ---------------------------------------------------------------- target

    def channel(self, guild: discord.Guild | None):
        """The configured log channel, or None. Never crosses guilds."""
        if guild is None:
            return None
        channel_id = self.store.setting(guild.id, SETTING)
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "send"):
            return None
        me = guild.me
        if me is not None:
            perms = channel.permissions_for(me)
            if not (perms.send_messages and perms.embed_links):
                if channel_id not in self._warned:
                    self._warned.add(channel_id)
                    log.warning(
                        "Cannot write to the command log channel %s in guild %s "
                        "(needs Send Messages + Embed Links).",
                        channel_id,
                        guild.id,
                    )
                return None
        return channel

    def forget_warning(self, channel_id: int) -> None:
        self._warned.discard(channel_id)

    async def _send(self, guild, embed) -> None:
        channel = self.channel(guild)
        if channel is None:
            return
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.debug("Could not write to the command log channel", exc_info=True)

    # -------------------------------------------------------------- commands

    async def log_command(
        self,
        interaction: discord.Interaction,
        command,
        error: BaseException | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return

        name = getattr(command, "qualified_name", None) or "unknown"
        payload = interaction.data or {}
        path, options = flatten_options(payload.get("options"))
        full = " ".join([name.split()[0], *path]) if path else name

        if error is None:
            title = f"/{full}"
            colour = discord.Color.greyple()
        else:
            title = f"/{full} — failed"
            colour = discord.Color.red()

        embed = discord.Embed(
            title=title,
            description=f"{interaction.user.mention} in <#{interaction.channel_id}>",
            color=colour,
            timestamp=discord.utils.utcnow(),
        )
        if options:
            lines = "\n".join(f"{key}: {value}" for key, value in options)
            embed.add_field(name="Arguments", value=lines[:1000], inline=False)
        if error is not None:
            detail = f"{type(error).__name__}: {error}"
            embed.add_field(name="Error", value=f"```\n{detail[:900]}\n```", inline=False)
        embed.set_footer(text=f"{interaction.user} • {interaction.user.id}")
        await self._send(guild, embed)

    # --------------------------------------------------------------- actions

    async def log_action(
        self,
        guild: discord.Guild | None,
        user: discord.abc.User,
        action: str,
        detail: str | None = None,
        *,
        colour: discord.Color | None = None,
    ) -> None:
        """A button click or other non-command action worth recording."""
        if guild is None:
            return
        embed = discord.Embed(
            title=action,
            description=f"{user.mention}" + (f" — {detail}" if detail else ""),
            color=colour or discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"{user} • {user.id}")
        await self._send(guild, embed)
