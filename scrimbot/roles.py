"""Discord roles the bot hands out: Team 1, Team 2 and Registered.

Every operation is best-effort: a missing role, a member who left, or a
permission problem is logged and skipped rather than allowed to break a scrim.
"""

from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)

TEAM_ROLE_KEYS = ("team1_role", "team2_role")
REGISTERED_ROLE_KEY = "registered_role"


class RoleManager:
    def __init__(self, store) -> None:
        self.store = store
        # (guild_id, role_id) pairs we have already complained about
        self._warned: set[tuple[int, int]] = set()

    # ---------------------------------------------------------------- lookup

    def role(self, guild: discord.Guild | None, key: str) -> discord.Role | None:
        if guild is None:
            return None
        role_id = self.store.setting(guild.id, key)
        if not role_id:
            return None
        return guild.get_role(role_id)

    def team_role(self, guild: discord.Guild | None, team: int) -> discord.Role | None:
        return self.role(guild, TEAM_ROLE_KEYS[team])

    def registered_role(self, guild: discord.Guild | None) -> discord.Role | None:
        return self.role(guild, REGISTERED_ROLE_KEY)

    def manageable(self, guild: discord.Guild, role: discord.Role) -> bool:
        """Can the bot actually add/remove this role?"""
        me = guild.me
        if me is None:
            return False
        if not me.guild_permissions.manage_roles:
            self._warn(guild, role, "the bot is missing Manage Roles")
            return False
        if role >= me.top_role:
            self._warn(
                guild, role, "the role sits above the bot's own highest role"
            )
            return False
        if role.is_default() or role.managed:
            self._warn(guild, role, "that role cannot be assigned by a bot")
            return False
        return True

    def _warn(self, guild: discord.Guild, role: discord.Role, why: str) -> None:
        key = (guild.id, role.id)
        if key in self._warned:
            return
        self._warned.add(key)
        log.warning("Cannot manage role %s in guild %s: %s", role.id, guild.id, why)

    def forget_warning(self, guild_id: int, role_id: int) -> None:
        self._warned.discard((guild_id, role_id))

    # ---------------------------------------------------------------- grants

    async def _add(self, guild, member, role, reason) -> bool:
        """True only when the member's roles actually changed."""
        if role is None or member is None or not self.manageable(guild, role):
            return False
        if role in member.roles:
            return False
        try:
            await member.add_roles(role, reason=reason)
            return True
        except discord.HTTPException:
            log.warning("Failed to add role %s to %s", role.id, member.id, exc_info=True)
            return False

    async def _remove(self, guild, member, role, reason) -> bool:
        """True only when the member's roles actually changed."""
        if role is None or member is None or not self.manageable(guild, role):
            return False
        if role not in member.roles:
            return False
        try:
            await member.remove_roles(role, reason=reason)
            return True
        except discord.HTTPException:
            log.warning("Failed to remove role %s from %s", role.id, member.id, exc_info=True)
            return False

    # ------------------------------------------------------------ team roles

    async def apply_teams(self, guild: discord.Guild | None, match) -> int:
        """Give everyone the role for the team they were drafted onto."""
        if guild is None:
            return 0
        applied = 0
        for team in (0, 1):
            role = self.team_role(guild, team)
            other = self.team_role(guild, 1 - team)
            if role is None:
                continue
            for player in match.teams[team]:
                member = guild.get_member(player.id)
                if member is None:
                    continue
                if other is not None and other != role:
                    await self._remove(guild, member, other, "Scrim: wrong team")
                if await self._add(guild, member, role, "Scrim: team assigned"):
                    applied += 1
        return applied

    async def clear_teams(self, guild: discord.Guild | None, match) -> int:
        """Strip both team roles from everyone in this match."""
        if guild is None:
            return 0
        removed = 0
        roles = [self.team_role(guild, 0), self.team_role(guild, 1)]
        if not any(roles):
            return 0
        for player in match.players:
            member = guild.get_member(player.id)
            if member is None:
                continue
            for role in roles:
                if await self._remove(guild, member, role, "Scrim: game over"):
                    removed += 1
        return removed

    async def clear_member(self, guild: discord.Guild | None, user_id: int) -> None:
        """Strip team roles from one player (dropped, subbed out)."""
        if guild is None:
            return
        member = guild.get_member(user_id)
        if member is None:
            return
        for team in (0, 1):
            await self._remove(
                guild, member, self.team_role(guild, team), "Scrim: left the game"
            )

    async def assign_member(self, guild: discord.Guild | None, user_id: int, team: int) -> None:
        """Put one player on one team (subbed in)."""
        if guild is None:
            return
        member = guild.get_member(user_id)
        if member is None:
            return
        await self._remove(
            guild, member, self.team_role(guild, 1 - team), "Scrim: wrong team"
        )
        await self._add(guild, member, self.team_role(guild, team), "Scrim: subbed in")

    # ------------------------------------------------------ registered role

    async def grant_registered(self, guild: discord.Guild | None, user_id: int) -> bool:
        if guild is None:
            return False
        return await self._add(
            guild,
            guild.get_member(user_id),
            self.registered_role(guild),
            "Steam account registered",
        )

    async def revoke_registered(self, guild: discord.Guild | None, user_id: int) -> bool:
        if guild is None:
            return False
        return await self._remove(
            guild,
            guild.get_member(user_id),
            self.registered_role(guild),
            "Steam account unregistered",
        )

    async def sync_registered(self, guild: discord.Guild | None) -> tuple[int, int]:
        """Bring the Registered role in line with the store.

        Returns ``(granted, revoked)``.
        """
        if guild is None:
            return 0, 0
        role = self.registered_role(guild)
        if role is None or not self.manageable(guild, role):
            return 0, 0

        registered = {uid for uid, _record in self.store.registered(guild.id)}
        granted = revoked = 0
        for user_id in registered:
            if await self._add(guild, guild.get_member(user_id), role, "Registered sync"):
                granted += 1
        for member in list(role.members):
            if member.id not in registered:
                if await self._remove(guild, member, role, "Registered sync"):
                    revoked += 1
        return granted, revoked
