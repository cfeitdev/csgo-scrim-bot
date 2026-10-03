"""Spectators: move someone into a team voice channel and server-mute them.

The mute is lifted automatically when they leave that channel. Who is muted is
persisted, so a bot restart mid-scrim cannot strand someone muted with no way
to undo it from Discord.
"""

from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)

TEAM_VOICE_KEYS = ("team1_voice", "team2_voice")
SPEC_KEY = "spec_muted"


class VoiceError(Exception):
    """Something the caller needs to fix before a spectator can be moved."""


class VoiceManager:
    def __init__(self, store) -> None:
        self.store = store

    # ---------------------------------------------------------------- lookup

    def team_channel(self, guild: discord.Guild | None, team: int):
        if guild is None:
            return None
        channel_id = self.store.setting(guild.id, TEAM_VOICE_KEYS[team])
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        # /setteamvoice only accepts voice channels; this guards a stale id
        # whose channel was since deleted and replaced by another type.
        if getattr(channel, "type", None) is not discord.ChannelType.voice:
            return None
        return channel

    def spec_channel_ids(self, guild: discord.Guild | None) -> set[int]:
        return {
            channel.id
            for channel in (self.team_channel(guild, 0), self.team_channel(guild, 1))
            if channel is not None
        }

    # -------------------------------------------------------------- registry

    def muted_ids(self, guild_id: int) -> list[int]:
        return [int(uid) for uid in (self.store.setting(guild_id, SPEC_KEY) or [])]

    def is_muted(self, guild_id: int, user_id: int) -> bool:
        return user_id in self.muted_ids(guild_id)

    async def _track(self, guild_id: int, user_id: int) -> None:
        ids = self.muted_ids(guild_id)
        if user_id not in ids:
            ids.append(user_id)
            await self.store.set_setting(guild_id, SPEC_KEY, ids)

    async def _untrack(self, guild_id: int, user_id: int) -> None:
        ids = self.muted_ids(guild_id)
        if user_id in ids:
            ids.remove(user_id)
            await self.store.set_setting(guild_id, SPEC_KEY, ids)

    # --------------------------------------------------------------- actions

    async def spectate(self, guild: discord.Guild, member: discord.Member, team: int):
        """Move a member into a team voice channel and server-mute them."""
        channel = self.team_channel(guild, team)
        if channel is None:
            raise VoiceError(
                "No voice channel is set for that team. An admin can set them "
                "with `/setteamvoice`."
            )

        me = guild.me
        if me is not None:
            perms = me.guild_permissions
            missing = [
                name
                for name, held in (
                    ("Move Members", perms.move_members),
                    ("Mute Members", perms.mute_members),
                )
                if not held
            ]
            if missing:
                raise VoiceError(
                    "The bot is missing the " + " and ".join(missing) + " permission."
                )
            if not channel.permissions_for(me).connect:
                raise VoiceError(f"The bot cannot see or join {channel.mention}.")

        if member.voice is None or member.voice.channel is None:
            raise VoiceError(
                f"{member.mention} has to be connected to a voice channel first -- "
                "Discord cannot move someone who is not in a call."
            )

        try:
            await member.move_to(channel, reason="Spectating the scrim")
            await member.edit(mute=True, reason="Spectator: muted while in the team call")
        except discord.Forbidden as exc:
            raise VoiceError(
                "Discord refused the move or mute. Check the bot's role is above "
                f"{member.mention}'s and that it can act in {channel.mention}."
            ) from exc
        except discord.HTTPException as exc:
            raise VoiceError(f"Discord rejected that: {exc}") from exc

        await self._track(guild.id, member.id)
        return channel

    def can_move(self, guild: discord.Guild) -> bool:
        me = guild.me
        return me is None or me.guild_permissions.move_members

    async def move_to_team(
        self, guild: discord.Guild, member: discord.Member, team: int
    ) -> bool:
        """Move one player into their team's call. False if it did not happen."""
        channel = self.team_channel(guild, team)
        if channel is None or member is None or not self.can_move(guild):
            return False
        # Discord cannot move somebody who is not already in a call.
        if member.voice is None or member.voice.channel is None:
            return False
        if member.voice.channel.id == channel.id:
            return False  # already there
        try:
            await member.move_to(channel, reason="Scrim: moved to team voice")
        except discord.HTTPException:
            log.warning("Could not move %s to team voice", member.id, exc_info=True)
            return False
        return True

    async def move_teams(self, guild: discord.Guild | None, match) -> tuple[int, int]:
        """Move every drafted player into their team's call.

        Returns ``(moved, waiting)`` where waiting counts players who are not
        in any voice channel -- Discord cannot pull those in.
        """
        if guild is None:
            return 0, 0
        moved = waiting = 0
        for team in (0, 1):
            if self.team_channel(guild, team) is None:
                continue
            for player in match.teams[team]:
                if getattr(player, "is_bot", False):
                    continue
                member = guild.get_member(player.id)
                if member is None:
                    continue
                if member.voice is None or member.voice.channel is None:
                    waiting += 1
                    continue
                if await self.move_to_team(guild, member, team):
                    moved += 1
        return moved, waiting

    async def release(self, guild: discord.Guild, member: discord.Member) -> bool:
        """Lift the spectator mute. False if it has to wait for a reconnect."""
        if member.voice is None or member.voice.channel is None:
            # Discord refuses mute edits on a member who is not connected, so
            # stay tracked and clear it when they next join any channel.
            log.debug("Deferring spectator unmute for %s until they reconnect", member.id)
            return False
        try:
            await member.edit(mute=False, reason="Spectator left the team call")
        except discord.Forbidden:
            log.warning("Not allowed to unmute %s", member.id)
            return False
        except discord.HTTPException:
            log.warning("Failed to unmute %s", member.id, exc_info=True)
            return False
        await self._untrack(guild.id, member.id)
        return True

    async def disconnect(self, guild: discord.Guild, member: discord.Member) -> bool:
        """Drop a member out of voice. False if they were not in a call or it failed."""
        if member.voice is None or member.voice.channel is None:
            return False
        if not self.can_move(guild):
            log.warning("Missing Move Members, cannot disconnect %s", member.id)
            return False
        try:
            await member.move_to(None, reason="Stopped spectating")
        except discord.HTTPException:
            log.warning("Could not disconnect %s from voice", member.id, exc_info=True)
            return False
        return True

    async def handle_voice_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Unmute a tracked spectator once they are out of the team call."""
        guild = member.guild
        if guild is None or not self.is_muted(guild.id, member.id):
            return
        channel = after.channel
        if channel is not None and channel.id in self.spec_channel_ids(guild):
            return  # still spectating
        await self.release(guild, member)
