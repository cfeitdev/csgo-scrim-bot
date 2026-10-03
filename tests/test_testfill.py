"""/testfill: fake players fill the lobby and play the draft themselves."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import discord

    from scrimbot.config import Config
    from scrimbot.core import ScrimCore
    from scrimbot.session import ScrimSession

    HAS_DISCORD = True
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    HAS_DISCORD = False

from scrimbot.match import TEST_ID_BASE, Match, Phase, Player  # noqa: E402

HUMAN = 4242


class FakeMessage:
    jump_url = "https://discord.com/channels/1/2/3"

    async def edit(self, **kwargs):
        return None


class FakeChannel:
    id = 55

    def __init__(self) -> None:
        self.guild = None
        self.lines: list[str] = []

    async def send(self, content=None, **kwargs):
        if content:
            self.lines.append(content)
        return FakeMessage()


class FakeUser:
    def __init__(self, user_id=HUMAN, name="human") -> None:
        self.id = user_id
        self.display_name = name


class BotPlayerTests(unittest.TestCase):
    def test_bots_render_without_a_broken_mention(self):
        bot = Player(TEST_ID_BASE + 1, "TestBot01", 1000, is_bot=True)
        human = Player(HUMAN, "human", 1000)
        self.assertEqual(bot.mention, "`TestBot01`")
        self.assertEqual(human.mention, f"<@{HUMAN}>")

    def test_has_bots_flag(self):
        match = Match(host_id=1)
        match.add_player(Player(HUMAN, "human", 1000))
        self.assertFalse(match.has_bots)
        match.add_player(Player(TEST_ID_BASE + 1, "TestBot01", 1000, is_bot=True))
        self.assertTrue(match.has_bots)

    def test_test_ids_cannot_collide_with_snowflakes(self):
        # Discord snowflakes are 17-19 digits; ours are four.
        self.assertLess(TEST_ID_BASE + 100, 2**22)


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class SoloFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        config = Config(
            token="x", guild_id=None, rcon_host="h", rcon_port=27015, rcon_password="p",
            rcon_enabled=False, connect_address="", steam_api_key="", lobby_size=10,
            pick_timeout=None, store_path=os.path.join(self._dir.name, "s.json"),
            default_elo=1000, default_mode="5v5",
            test_bot_delay=0.001,
        )
        self.core = ScrimCore(config)
        await self.core.setup()
        self.channel = FakeChannel()
        self.session = ScrimSession(self.core, self.channel, Match(host_id=HUMAN, size=10))
        self.session.message = FakeMessage()

    async def asyncTearDown(self):
        await self.core.close()
        self._dir.cleanup()

    async def settle(self):
        """Let the autoplay task run to completion."""
        for _ in range(400):
            task = self.session._autoplay_task
            if task is None or task.done():
                break
            await asyncio.sleep(0.005)

    async def test_fill_completes_the_lobby(self):
        match = self.session.match
        match.add_player(self.session.make_player(FakeUser()))
        added = await self.session.add_test_players(9)
        self.assertEqual(added, 9)
        self.assertEqual(len(match.players), 10)
        self.assertTrue(match.has_bots)

    async def test_fill_never_exceeds_the_lobby_size(self):
        added = await self.session.add_test_players(99)
        self.assertEqual(added, 10)
        self.assertEqual(len(self.session.match.players), 10)

    async def test_bot_names_are_unique(self):
        await self.session.add_test_players(10)
        ids = [p.id for p in self.session.match.players]
        names = [p.name for p in self.session.match.players]
        self.assertEqual(len(set(ids)), 10)
        self.assertEqual(len(set(names)), 10)

    async def test_an_all_bot_lobby_plays_itself_to_live(self):
        await self.session.add_test_players(10)
        await self.settle()
        match = self.session.match
        self.assertEqual(match.phase, Phase.LIVE)
        self.assertEqual(len(match.teams[0]), 5)
        self.assertEqual(len(match.teams[1]), 5)
        self.assertIsNotNone(match.selected_map)

    async def test_human_captain_keeps_control_of_their_picks(self):
        match = self.session.match
        match.add_player(self.session.make_player(FakeUser()))
        await self.session.add_test_players(9)
        # Installed in place: forcing a captain no longer re-rolls the draft.
        match.set_forced_captain(HUMAN, 0)
        self.session.schedule_autoplay()
        await self.settle()

        # The bot captain picks; the human's turns stall waiting for input.
        self.assertEqual(match.phase, Phase.DRAFT)
        self.assertEqual(match.current_captain.id, HUMAN)
        self.assertFalse(match.current_captain.is_bot)

    async def test_get5_is_skipped_for_test_games(self):
        # A core with get5 actually switched on, so the skip is a real decision
        # rather than get5 being off anyway.
        config = Config(
            token="x", guild_id=None, rcon_host="h", rcon_port=27015, rcon_password="p",
            rcon_enabled=True, connect_address="", steam_api_key="", lobby_size=10,
            pick_timeout=None, store_path=os.path.join(self._dir.name, "g.json"),
            default_elo=1000, default_mode="5v5", test_bot_delay=0.001,
            get5_base_url="http://bot:8080",
        )
        core = ScrimCore(config)
        await core.setup()
        self.assertTrue(core.get5.enabled)

        session = ScrimSession(core, FakeChannel(), Match(host_id=HUMAN, size=10))
        session.message = FakeMessage()
        await session.add_test_players(10)
        for _ in range(400):
            task = session._autoplay_task
            if task is None or task.done():
                break
            await asyncio.sleep(0.005)

        await session.setup_get5()
        self.assertIsNotNone(session.get5_status)
        self.assertIn("Test game", session.get5_status)
        self.assertIn("skipping get5", session.get5_status)
        await core.close()

    async def test_a_test_game_never_moves_any_rating(self):
        await self.session.add_test_players(10)
        await self.settle()
        await self.session.finish(0)
        match = self.session.match
        self.assertEqual(match.phase, Phase.COMPLETE)
        self.assertEqual(self.session.changes, {})
        for player in match.players:
            self.assertEqual(self.core.store.elo(0, player.id, "5v5"), 1000)

    async def test_bots_never_reach_the_leaderboard(self):
        await self.session.add_test_players(10)
        await self.settle()
        await self.session.finish(0)
        self.assertEqual(self.core.store.leaderboard(0, "5v5"), [])

    async def test_a_human_playing_against_bots_keeps_their_rating(self):
        match = self.session.match
        match.add_player(self.session.make_player(FakeUser()))
        # Fill to one short, pin both captains to bots, then complete the lobby.
        # Otherwise the human may be drawn as captain and the draft correctly
        # stalls waiting for a pick nobody is going to make.
        await self.session.add_test_players(8)
        bots = [p for p in match.players if p.is_bot]
        match.set_forced_captain(bots[0].id, 0)
        match.set_forced_captain(bots[1].id, 1)
        await self.session.add_test_players(1)
        await self.settle()

        self.assertEqual(match.phase, Phase.LIVE)
        await self.session.finish(match.team_of(HUMAN))
        self.assertEqual(self.core.store.elo(0, HUMAN, "5v5"), 1000)
        self.assertEqual(self.core.store.mode_stats(0, HUMAN, "5v5")["wins"], 0)


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class ViewStabilityTests(unittest.IsolatedAsyncioTestCase):
    """Components must keep their custom_ids for the whole phase.

    Rebuilding the view on every refresh randomises them, which silently kills
    every button on any copy of the message a client has not re-rendered --
    the clicks reach an unknown custom_id and are never acknowledged.
    """

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        config = Config(
            token="x", guild_id=None, rcon_host="h", rcon_port=27015, rcon_password="p",
            rcon_enabled=False, connect_address="1.2.3.4:27015", steam_api_key="",
            lobby_size=10, pick_timeout=None,
            store_path=os.path.join(self._dir.name, "s.json"),
            default_elo=1000, default_mode="5v5", test_bot_delay=0.001,
        )
        self.core = ScrimCore(config)
        await self.core.setup()
        self.session = ScrimSession(self.core, FakeChannel(), Match(host_id=HUMAN, size=10))
        self.session.message = FakeMessage()

    async def asyncTearDown(self):
        await self.core.close()
        self._dir.cleanup()

    def ids(self):
        return [c.custom_id for c in self.session.view.children]

    async def test_lobby_ids_survive_every_join(self):
        await self.session.start()
        before = self.ids()
        for i in range(1, 10):
            self.session.match.add_player(Player(i, f"p{i}", 1000))
            await self.session.refresh()
            self.assertEqual(self.ids(), before)

    async def test_live_ids_survive_repeated_status_refreshes(self):
        # This is the Connect info failure: the roster watcher refreshes the
        # message every poll while ten people are clicking the button.
        await self.session.add_test_players(10)
        for _ in range(400):
            task = self.session._autoplay_task
            if task is None or task.done():
                break
            await asyncio.sleep(0.005)
        self.assertEqual(self.session.match.phase, Phase.LIVE)

        await self.session.refresh()
        before = self.ids()
        for n in range(20):
            self.session.get5_status = f"waiting for {n}/10 players"
            await self.session.refresh()
            self.assertEqual(self.ids(), before)
        self.assertTrue(
            any(getattr(c, "label", "") == "Connect info" for c in self.session.view.children)
        )

    async def test_draft_ids_survive_every_pick(self):
        for i in range(1, 11):
            self.session.match.add_player(Player(i, f"p{i}", 1000))
        self.session.match.start_teams()
        await self.session.start()
        before = self.ids()
        counts = []
        while self.session.match.phase is Phase.DRAFT:
            counts.append(len(self.session.view.children[0].options))
            match = self.session.match
            match.pick(match.current_captain.id, match.available_players[0].id)
            if match.phase is not Phase.DRAFT:
                break
            await self.session.refresh()
            self.assertEqual(self.ids(), before)
        # options are re-synced in place as players get taken
        # the last option never appears -- one player left is auto-assigned
        self.assertEqual(counts, [8, 7, 6, 5, 4, 3, 2])

    async def test_a_phase_change_does_build_a_new_view(self):
        await self.session.start()
        lobby = self.ids()
        for i in range(1, 11):
            self.session.match.add_player(Player(i, f"p{i}", 1000))
        self.session.match.start_teams()
        await self.session.refresh()
        self.assertNotEqual(self.ids(), lobby)


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class VoiceMoveTimingTests(unittest.IsolatedAsyncioTestCase):
    """The team voice move happens after the veto, not when teams lock."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        config = Config(
            token="x", guild_id=None, rcon_host="h", rcon_port=27015, rcon_password="p",
            rcon_enabled=False, connect_address="", steam_api_key="", lobby_size=10,
            pick_timeout=None, store_path=os.path.join(self._dir.name, "s.json"),
            default_elo=1000, default_mode="5v5", test_bot_delay=0.001,
        )
        self.core = ScrimCore(config)
        await self.core.setup()
        self.session = ScrimSession(self.core, FakeChannel(), Match(host_id=HUMAN, size=10))
        self.session.message = FakeMessage()
        self.calls = []

        async def record(guild, match):
            self.calls.append(match.phase)
            return (0, 0)

        self.core.voice.move_teams = record
        await self.core.store.set_setting(0, "auto_voice", True)

    async def asyncTearDown(self):
        await self.core.close()
        self._dir.cleanup()

    async def test_not_moved_while_the_veto_is_running(self):
        for i in range(1, 11):
            self.session.match.add_player(Player(i, f"p{i}", 1000))
        self.session.match.start_teams()
        while self.session.match.phase is Phase.DRAFT:
            match = self.session.match
            match.pick(match.current_captain.id, match.available_players[0].id)
        # Teams are locked and the veto has begun.
        self.assertEqual(self.session.match.phase, Phase.MAP_BAN)
        await self.session._announce_veto()
        self.assertEqual(self.calls, [], "must not move mid-veto")

    async def test_moved_once_the_map_is_settled(self):
        for i in range(1, 11):
            self.session.match.add_player(Player(i, f"p{i}", 1000))
        self.session.match.start_teams()
        while self.session.match.phase is Phase.DRAFT:
            match = self.session.match
            match.pick(match.current_captain.id, match.available_players[0].id)
        while self.session.match.phase is Phase.MAP_BAN:
            match = self.session.match
            match.ban(match.current_banner.id, match.remaining_maps[0].name)

        await self.session.go_live()
        self.assertEqual(self.calls, [Phase.LIVE])

    async def test_the_toggle_is_respected(self):
        await self.core.store.set_setting(0, "auto_voice", False)
        for i in range(1, 11):
            self.session.match.add_player(Player(i, f"p{i}", 1000))
        self.session.match.start_teams()
        while self.session.match.phase is not Phase.LIVE:
            match = self.session.match
            if match.phase is Phase.DRAFT:
                match.pick(match.current_captain.id, match.available_players[0].id)
            else:
                match.ban(match.current_banner.id, match.remaining_maps[0].name)
        await self.session.go_live()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
