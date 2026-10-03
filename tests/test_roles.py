"""Team / Registered role assignment. Skipped without discord.py."""

from __future__ import annotations

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import discord

    from scrimbot.roles import RoleManager
    from scrimbot.store import Store

    HAS_DISCORD = True
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    HAS_DISCORD = False

from scrimbot.match import Match, Phase, Player  # noqa: E402

GUILD = 700
TEAM1_ROLE, TEAM2_ROLE, REG_ROLE = 10, 20, 30
BOT_TOP = 99


class FakeRole:
    def __init__(self, role_id: int, position: int = 5, managed: bool = False) -> None:
        self.id = role_id
        self.name = f"role-{role_id}"
        self.position = position
        self.managed = managed
        self.members: list = []

    def is_default(self) -> bool:
        return self.id == GUILD

    def __ge__(self, other):
        return self.position >= other.position

    def __lt__(self, other):
        return self.position < other.position

    def __eq__(self, other):
        return isinstance(other, FakeRole) and other.id == self.id

    def __hash__(self):
        return hash(self.id)


class FakeMember:
    def __init__(self, user_id: int, roles=(), admin=True, manage_roles=True) -> None:
        self.id = user_id
        self.roles = list(roles)
        self.guild_permissions = discord.Permissions(manage_roles=manage_roles)
        self.top_role = FakeRole(BOT_TOP, position=100)
        self.added: list[int] = []
        self.removed: list[int] = []

    async def add_roles(self, role, reason=None):
        self.roles.append(role)
        self.added.append(role.id)
        role.members.append(self)

    async def remove_roles(self, role, reason=None):
        self.roles = [r for r in self.roles if r != role]
        self.removed.append(role.id)
        role.members = [m for m in role.members if m.id != self.id]


class FakeGuild:
    def __init__(self, roles, members, manage_roles=True) -> None:
        self.id = GUILD
        self._roles = {r.id: r for r in roles}
        self._members = {m.id: m for m in members}
        self.me = FakeMember(1, manage_roles=manage_roles)

    def get_role(self, role_id):
        return self._roles.get(role_id)

    def get_member(self, user_id):
        return self._members.get(user_id)


def drafted_match(size: int = 10) -> Match:
    match = Match(host_id=1, size=size)
    for i in range(1, size + 1):
        match.add_player(Player(id=i, name=f"p{i}", elo=1000))
    match.start_teams(random.Random(4))
    while match.phase is Phase.DRAFT:
        match.pick(match.current_captain.id, match.available_players[0].id)
    return match


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class RoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self._dir.name, "s.json"), 1000)
        await self.store.load()
        self.manager = RoleManager(self.store)
        self.match = drafted_match()

        self.team1 = FakeRole(TEAM1_ROLE)
        self.team2 = FakeRole(TEAM2_ROLE)
        self.registered = FakeRole(REG_ROLE)
        self.members = [FakeMember(p.id) for p in self.match.players]
        self.guild = FakeGuild([self.team1, self.team2, self.registered], self.members)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def enable_teams(self):
        await self.store.set_setting(GUILD, "team1_role", TEAM1_ROLE)
        await self.store.set_setting(GUILD, "team2_role", TEAM2_ROLE)

    def member(self, user_id):
        return self.guild.get_member(user_id)

    # ------------------------------------------------------------ team roles

    async def test_nothing_happens_when_unset(self):
        applied = await self.manager.apply_teams(self.guild, self.match)
        self.assertEqual(applied, 0)
        self.assertEqual(self.members[0].added, [])

    async def test_each_player_gets_their_own_team_role(self):
        await self.enable_teams()
        applied = await self.manager.apply_teams(self.guild, self.match)
        self.assertEqual(applied, 10)
        for player in self.match.teams[0]:
            self.assertIn(TEAM1_ROLE, self.member(player.id).added)
        for player in self.match.teams[1]:
            self.assertIn(TEAM2_ROLE, self.member(player.id).added)

    async def test_applying_twice_changes_nothing(self):
        await self.enable_teams()
        await self.manager.apply_teams(self.guild, self.match)
        again = await self.manager.apply_teams(self.guild, self.match)
        self.assertEqual(again, 0)

    async def test_clear_strips_both_roles(self):
        await self.enable_teams()
        await self.manager.apply_teams(self.guild, self.match)
        removed = await self.manager.clear_teams(self.guild, self.match)
        self.assertEqual(removed, 10)
        for member in self.members:
            self.assertEqual(member.roles, [])

    async def test_wrong_team_role_is_swapped(self):
        await self.enable_teams()
        stray = self.member(self.match.teams[0][0].id)
        await stray.add_roles(self.team2)
        await self.manager.apply_teams(self.guild, self.match)
        self.assertNotIn(self.team2, stray.roles)
        self.assertIn(self.team1, stray.roles)

    async def test_sub_moves_the_role(self):
        await self.enable_teams()
        await self.manager.apply_teams(self.guild, self.match)
        out = self.match.teams[1][2]
        await self.manager.clear_member(self.guild, out.id)
        self.assertEqual(self.member(out.id).roles, [])

        newcomer = FakeMember(999)
        self.guild._members[999] = newcomer
        await self.manager.assign_member(self.guild, 999, 1)
        self.assertIn(self.team2, newcomer.roles)

    async def test_members_who_left_are_skipped(self):
        await self.enable_teams()
        del self.guild._members[self.match.teams[0][0].id]
        applied = await self.manager.apply_teams(self.guild, self.match)
        self.assertEqual(applied, 9)

    # ------------------------------------------------------------ guardrails

    async def test_role_above_the_bot_is_refused_and_warned_once(self):
        await self.enable_teams()
        self.team1.position = 500  # above the bot's top role
        await self.manager.apply_teams(self.guild, self.match)
        await self.manager.apply_teams(self.guild, self.match)
        self.assertEqual(self.manager._warned, {(GUILD, TEAM1_ROLE)})
        for player in self.match.teams[0]:
            self.assertEqual(self.member(player.id).added, [])
        # the other team is unaffected
        self.assertIn(TEAM2_ROLE, self.member(self.match.teams[1][0].id).added)

    async def test_missing_manage_roles_blocks_everything(self):
        await self.enable_teams()
        self.guild.me = FakeMember(1, manage_roles=False)
        applied = await self.manager.apply_teams(self.guild, self.match)
        self.assertEqual(applied, 0)

    async def test_managed_role_is_refused(self):
        await self.enable_teams()
        self.team1.managed = True
        await self.manager.apply_teams(self.guild, self.match)
        self.assertEqual(self.member(self.match.teams[0][0].id).added, [])

    async def test_deleted_role_is_ignored(self):
        await self.store.set_setting(GUILD, "team1_role", 4242)  # no such role
        await self.store.set_setting(GUILD, "team2_role", TEAM2_ROLE)
        applied = await self.manager.apply_teams(self.guild, self.match)
        self.assertEqual(applied, 5)

    async def test_no_guild_is_a_no_op(self):
        self.assertEqual(await self.manager.apply_teams(None, self.match), 0)
        self.assertEqual(await self.manager.clear_teams(None, self.match), 0)
        self.assertFalse(await self.manager.grant_registered(None, 1))

    # ------------------------------------------------------ registered role

    async def test_grant_and_revoke(self):
        await self.store.set_setting(GUILD, "registered_role", REG_ROLE)
        self.assertTrue(await self.manager.grant_registered(self.guild, 1))
        self.assertIn(self.registered, self.member(1).roles)
        self.assertFalse(await self.manager.grant_registered(self.guild, 1))
        self.assertTrue(await self.manager.revoke_registered(self.guild, 1))
        self.assertNotIn(self.registered, self.member(1).roles)

    async def test_sync_grants_the_registered_and_strips_the_rest(self):
        await self.store.set_setting(GUILD, "registered_role", REG_ROLE)
        await self.store.register(GUILD, 1, "76561197960265729", "one")
        await self.store.register(GUILD, 2, "76561197960265730", "two")
        # someone holding the role without a registration
        await self.member(5).add_roles(self.registered)

        granted, revoked = await self.manager.sync_registered(self.guild)
        self.assertEqual(granted, 2)
        self.assertEqual(revoked, 1)
        self.assertIn(self.registered, self.member(1).roles)
        self.assertNotIn(self.registered, self.member(5).roles)

    async def test_sync_without_a_role_set_is_a_no_op(self):
        self.assertEqual(await self.manager.sync_registered(self.guild), (0, 0))


if __name__ == "__main__":
    unittest.main()
