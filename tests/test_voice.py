"""/spec: move a member into a team call and mute them until they leave."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import discord

    from scrimbot.store import Store
    from scrimbot.voice import VoiceError, VoiceManager

    HAS_DISCORD = True
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    HAS_DISCORD = False

GUILD = 900
TEAM1_VC, TEAM2_VC, OTHER_VC = 11, 22, 33


def drafted_match(with_bots=False):
    import random

    from scrimbot.match import Match, Phase, Player

    match = Match(host_id=1, size=10)
    for i in range(1, 11):
        match.add_player(Player(100 + i, f"p{i}", 1000, is_bot=with_bots))
    match.start_teams(random.Random(4))
    while match.phase is Phase.DRAFT:
        match.pick(match.current_captain.id, match.available_players[0].id)
    return match


class FakeChannel:
    type = None  # set below to ChannelType.voice once discord is importable

    def __init__(self, channel_id, connect=True) -> None:
        self.id = channel_id
        self.name = f"vc-{channel_id}"
        self.mention = f"<#{channel_id}>"
        self._connect = connect

    def permissions_for(self, member):
        return discord.Permissions(connect=self._connect)


class FakeVoiceState:
    def __init__(self, channel) -> None:
        self.channel = channel


class FakeMember:
    def __init__(self, user_id=7, channel=None) -> None:
        self.id = user_id
        self.display_name = "spectator"
        self.mention = f"<@{user_id}>"
        self.voice = FakeVoiceState(channel) if channel else None
        self.guild = None
        self.moved_to = []
        self.mutes = []

    async def move_to(self, channel, reason=None):
        # move_to(None) is how discord.py disconnects a member from voice.
        self.moved_to.append(channel.id if channel is not None else None)
        self.voice = FakeVoiceState(channel) if channel is not None else None

    async def edit(self, mute=None, reason=None):
        self.mutes.append(mute)


class FakeGuild:
    def __init__(self, channels, move=True, mute=True) -> None:
        self.id = GUILD
        self._channels = {c.id: c for c in channels}
        self.me = FakeMember(1)
        self.me.guild_permissions = discord.Permissions(
            move_members=move, mute_members=mute
        )

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_member(self, user_id):
        return getattr(self, "members", {}).get(user_id)


if HAS_DISCORD:
    FakeChannel.type = discord.ChannelType.voice


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class SpectatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self._dir.name, "s.json"), 1000)
        await self.store.load()
        self.voice = VoiceManager(self.store)
        self.team1 = FakeChannel(TEAM1_VC)
        self.team2 = FakeChannel(TEAM2_VC)
        self.other = FakeChannel(OTHER_VC)
        self.guild = FakeGuild([self.team1, self.team2, self.other])
        await self.store.set_setting(GUILD, "team1_voice", TEAM1_VC)
        await self.store.set_setting(GUILD, "team2_voice", TEAM2_VC)

    async def asyncTearDown(self):
        self._dir.cleanup()

    def member(self, channel=None):
        m = FakeMember(7, channel)
        m.guild = self.guild
        return m

    # ---------------------------------------------------------------- moving

    async def test_moves_and_mutes(self):
        member = self.member(self.other)
        channel = await self.voice.spectate(self.guild, member, 0)
        self.assertEqual(channel.id, TEAM1_VC)
        self.assertEqual(member.moved_to, [TEAM1_VC])
        self.assertEqual(member.mutes, [True])
        self.assertTrue(self.voice.is_muted(GUILD, member.id))

    async def test_team_two_uses_the_other_channel(self):
        member = self.member(self.other)
        channel = await self.voice.spectate(self.guild, member, 1)
        self.assertEqual(channel.id, TEAM2_VC)

    async def test_refuses_when_no_channel_is_set(self):
        await self.store.set_setting(GUILD, "team1_voice", None)
        with self.assertRaises(VoiceError) as ctx:
            await self.voice.spectate(self.guild, self.member(self.other), 0)
        self.assertIn("/setteamvoice", str(ctx.exception))

    async def test_refuses_when_the_member_is_not_in_voice(self):
        with self.assertRaises(VoiceError) as ctx:
            await self.voice.spectate(self.guild, self.member(None), 0)
        self.assertIn("connected to a voice channel", str(ctx.exception))

    async def test_refuses_without_mute_members(self):
        guild = FakeGuild([self.team1, self.team2, self.other], mute=False)
        member = self.member(self.other)
        member.guild = guild
        with self.assertRaises(VoiceError) as ctx:
            await self.voice.spectate(guild, member, 0)
        self.assertIn("Mute Members", str(ctx.exception))

    async def test_refuses_without_move_members(self):
        guild = FakeGuild([self.team1, self.team2, self.other], move=False)
        with self.assertRaises(VoiceError) as ctx:
            await self.voice.spectate(guild, self.member(self.other), 0)
        self.assertIn("Move Members", str(ctx.exception))

    # ------------------------------------------------------------ releasing

    async def test_leaving_for_another_channel_unmutes(self):
        member = self.member(self.other)
        await self.voice.spectate(self.guild, member, 0)
        member.mutes.clear()

        member.voice = FakeVoiceState(self.other)
        await self.voice.handle_voice_update(
            member, FakeVoiceState(self.team1), FakeVoiceState(self.other)
        )
        self.assertEqual(member.mutes, [False])
        self.assertFalse(self.voice.is_muted(GUILD, member.id))

    async def test_staying_in_the_team_call_keeps_the_mute(self):
        member = self.member(self.other)
        await self.voice.spectate(self.guild, member, 0)
        member.mutes.clear()

        await self.voice.handle_voice_update(
            member, FakeVoiceState(self.team1), FakeVoiceState(self.team1)
        )
        self.assertEqual(member.mutes, [])
        self.assertTrue(self.voice.is_muted(GUILD, member.id))

    async def test_disconnect_defers_the_unmute_until_they_return(self):
        # Discord refuses mute edits on a member who is not connected, so the
        # mute has to be cleared on their next join instead of being lost.
        member = self.member(self.other)
        await self.voice.spectate(self.guild, member, 0)
        member.mutes.clear()

        member.voice = None
        await self.voice.handle_voice_update(
            member, FakeVoiceState(self.team1), FakeVoiceState(None)
        )
        self.assertEqual(member.mutes, [])
        self.assertTrue(self.voice.is_muted(GUILD, member.id), "must stay tracked")

        member.voice = FakeVoiceState(self.other)
        await self.voice.handle_voice_update(
            member, FakeVoiceState(None), FakeVoiceState(self.other)
        )
        self.assertEqual(member.mutes, [False])
        self.assertFalse(self.voice.is_muted(GUILD, member.id))

    async def test_unspec_unmutes_then_disconnects(self):
        member = self.member(self.other)
        await self.voice.spectate(self.guild, member, 0)
        member.mutes.clear()
        member.moved_to.clear()

        self.assertTrue(await self.voice.release(self.guild, member))
        self.assertTrue(await self.voice.disconnect(self.guild, member))
        self.assertEqual(member.mutes, [False])
        self.assertEqual(member.moved_to, [None])
        self.assertIsNone(member.voice)
        self.assertFalse(self.voice.is_muted(GUILD, member.id))

    async def test_disconnect_needs_a_call(self):
        member = self.member()
        self.assertFalse(await self.voice.disconnect(self.guild, member))
        self.assertEqual(member.moved_to, [])

    async def test_disconnect_needs_move_members(self):
        guild = FakeGuild([self.team1, self.team2, self.other], move=False)
        member = self.member(self.other)
        self.assertFalse(await self.voice.disconnect(guild, member))
        self.assertEqual(member.moved_to, [])

    async def test_untracked_members_are_ignored(self):
        member = self.member(self.other)
        await self.voice.handle_voice_update(
            member, FakeVoiceState(self.team1), FakeVoiceState(None)
        )
        self.assertEqual(member.mutes, [])

    # ------------------------------------------------------------ registry

    async def test_registry_survives_a_reload(self):
        member = self.member(self.other)
        await self.voice.spectate(self.guild, member, 0)

        fresh = Store(self.store._path, 1000)
        await fresh.load()
        self.assertTrue(VoiceManager(fresh).is_muted(GUILD, member.id))

    async def test_registry_is_per_guild(self):
        member = self.member(self.other)
        await self.voice.spectate(self.guild, member, 0)
        self.assertFalse(self.voice.is_muted(12345, member.id))

    async def test_two_spectators_are_tracked_independently(self):
        first = self.member(self.other)
        second = FakeMember(8, self.other)
        second.guild = self.guild
        await self.voice.spectate(self.guild, first, 0)
        await self.voice.spectate(self.guild, second, 1)
        self.assertEqual(sorted(self.voice.muted_ids(GUILD)), [7, 8])

        second.voice = FakeVoiceState(self.other)
        await self.voice.handle_voice_update(
            second, FakeVoiceState(self.team2), FakeVoiceState(self.other)
        )
        self.assertEqual(self.voice.muted_ids(GUILD), [7])


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class TeamMoveTests(unittest.IsolatedAsyncioTestCase):
    """Pulling drafted players into their own team call."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self._dir.name, "s.json"), 1000)
        await self.store.load()
        self.voice = VoiceManager(self.store)
        self.team1 = FakeChannel(TEAM1_VC)
        self.team2 = FakeChannel(TEAM2_VC)
        self.other = FakeChannel(OTHER_VC)
        self.guild = FakeGuild([self.team1, self.team2, self.other])
        await self.store.set_setting(GUILD, "team1_voice", TEAM1_VC)
        await self.store.set_setting(GUILD, "team2_voice", TEAM2_VC)

        self.match = drafted_match()
        self.members = {}
        for team in (0, 1):
            for player in self.match.teams[team]:
                member = FakeMember(player.id, self.other)
                member.guild = self.guild
                self.members[player.id] = member
        self.guild.members = self.members

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_everyone_lands_in_their_own_team_channel(self):
        moved, waiting = await self.voice.move_teams(self.guild, self.match)
        self.assertEqual((moved, waiting), (10, 0))
        for team, channel_id in ((0, TEAM1_VC), (1, TEAM2_VC)):
            for player in self.match.teams[team]:
                self.assertEqual(self.members[player.id].moved_to, [channel_id])

    async def test_players_not_in_voice_are_counted_not_moved(self):
        for player in self.match.teams[0]:
            self.members[player.id].voice = None
        moved, waiting = await self.voice.move_teams(self.guild, self.match)
        self.assertEqual((moved, waiting), (5, 5))

    async def test_already_in_the_right_channel_is_not_moved_again(self):
        await self.voice.move_teams(self.guild, self.match)
        for member in self.members.values():
            member.moved_to.clear()
        moved, _waiting = await self.voice.move_teams(self.guild, self.match)
        self.assertEqual(moved, 0)

    async def test_nothing_happens_without_channels(self):
        await self.store.set_setting(GUILD, "team1_voice", None)
        await self.store.set_setting(GUILD, "team2_voice", None)
        self.assertEqual(await self.voice.move_teams(self.guild, self.match), (0, 0))

    async def test_missing_move_members_moves_nobody(self):
        guild = FakeGuild([self.team1, self.team2, self.other], move=False)
        guild.members = self.members
        moved, _waiting = await self.voice.move_teams(guild, self.match)
        self.assertEqual(moved, 0)

    async def test_test_bots_are_skipped(self):
        match = drafted_match(with_bots=True)
        self.assertEqual(await self.voice.move_teams(self.guild, match), (0, 0))

    async def test_single_player_move(self):
        player = self.match.teams[1][0]
        member = self.members[player.id]
        self.assertTrue(await self.voice.move_to_team(self.guild, member, 1))
        self.assertEqual(member.moved_to, [TEAM2_VC])

    async def test_no_guild_is_a_no_op(self):
        self.assertEqual(await self.voice.move_teams(None, self.match), (0, 0))


if __name__ == "__main__":
    unittest.main()
